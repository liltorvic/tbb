"""
orderbook_ws.py – Real-time order book feed via Polymarket WebSocket.

Handles:
  • Initial subscription to multiple tokens
  • Full book snapshots  ("book" event)
  • Incremental price-level changes  ("price_change" event)
  • Auto-reconnect with exponential back-off
  • Thread-safe book state accessible from the main loop
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

logger = logging.getLogger(__name__)


# ── Data Model ─────────────────────────────────────────────────────────────────

@dataclass
class OrderBook:
    """Holds the current state of one token's order book."""
    token_id: str
    bids: List[List[float]] = field(default_factory=list)   # [[price, size], …]  descending
    asks: List[List[float]] = field(default_factory=list)   # [[price, size], …]  ascending
    timestamp: float = 0.0
    last_trade_price: Optional[float] = None

    # ── Derived properties ────────────────────────────────────────────────────

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0][0] if self.asks else None

    @property
    def mid_price(self) -> Optional[float]:
        if self.best_bid and self.best_ask:
            return (self.best_bid + self.best_ask) / 2.0
        return None

    @property
    def spread(self) -> Optional[float]:
        if self.best_bid and self.best_ask:
            return self.best_ask - self.best_bid
        return None

    @property
    def spread_bps(self) -> Optional[float]:
        mid = self.mid_price
        s = self.spread
        if mid and s and mid > 0:
            return (s / mid) * 10_000
        return None

    @property
    def bid_depth_5(self) -> float:
        """Total size on the top-5 bid levels."""
        return sum(b[1] for b in self.bids[:5])

    @property
    def ask_depth_5(self) -> float:
        """Total size on the top-5 ask levels."""
        return sum(a[1] for a in self.asks[:5])

    @property
    def depth_imbalance(self) -> float:
        """
        Positive → more bid pressure (price likely rising).
        Negative → more ask pressure.
        Range: -1 to +1.
        """
        total = self.bid_depth_5 + self.ask_depth_5
        if total == 0:
            return 0.0
        return (self.bid_depth_5 - self.ask_depth_5) / total

    def is_stale(self, max_age_seconds: float = 60.0) -> bool:
        return self.timestamp > 0 and (time.time() - self.timestamp) > max_age_seconds

    def __repr__(self) -> str:
        return (
            f"OrderBook({self.token_id[:12]}… "
            f"bid={self.best_bid} ask={self.best_ask} "
            f"spread={self.spread_bps:.1f}bps)" if self.spread_bps else
            f"OrderBook({self.token_id[:12]}… empty)"
        )


# ── WebSocket Feed ─────────────────────────────────────────────────────────────

class OrderBookFeed:
    """
    Subscribes to Polymarket's WebSocket for one or more token IDs and
    maintains an up-to-date OrderBook for each.

    on_update(book: OrderBook) is called on every meaningful state change.
    """

    _BASE_DELAY = 3.0
    _MAX_DELAY  = 120.0

    def __init__(
        self,
        token_ids: List[str],
        on_update: Callable[[OrderBook], None],
        ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market",
    ):
        self.ws_url = ws_url
        self.on_update = on_update
        self._running = False
        self._reconnect_delay = self._BASE_DELAY

        # Initialise books for each token
        self._token_ids: List[str] = []
        self.books: Dict[str, OrderBook] = {}
        self._set_tokens(token_ids)

    # ── Public API ─────────────────────────────────────────────────────────────

    def get_book(self, token_id: str) -> Optional[OrderBook]:
        return self.books.get(token_id)

    def update_token_ids(self, new_ids: List[str]):
        """
        Hot-swap the set of subscribed tokens.
        New tokens are added; missing ones are removed from the local dict.
        The caller is responsible for reconnecting the WebSocket if needed.
        """
        added = set(new_ids) - set(self._token_ids)
        for tid in added:
            self.books[tid] = OrderBook(token_id=tid)
        self._token_ids = list(new_ids)

    def stop(self):
        self._running = False

    async def connect(self):
        """Start the feed.  Reconnects automatically on disconnection."""
        self._running = True
        while self._running:
            try:
                await self._run_session()
                self._reconnect_delay = self._BASE_DELAY  # clean exit → reset
            except (ConnectionClosed, WebSocketException) as exc:
                logger.warning(
                    f"WS disconnected: {exc}  "
                    f"Reconnecting in {self._reconnect_delay:.0f}s…"
                )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(
                    f"WS unexpected error: {exc}  "
                    f"Reconnecting in {self._reconnect_delay:.0f}s…"
                )

            if self._running:
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, self._MAX_DELAY)

    # ── Internal ───────────────────────────────────────────────────────────────

    def _set_tokens(self, token_ids: List[str]):
        self._token_ids = list(token_ids)
        for tid in token_ids:
            if tid not in self.books:
                self.books[tid] = OrderBook(token_id=tid)

    async def _run_session(self):
        logger.info(
            f"WS connecting → {self.ws_url}  "
            f"({len(self._token_ids)} tokens)"
        )
        async with websockets.connect(
            self.ws_url,
            ping_interval=20,
            ping_timeout=30,
            close_timeout=10,
            max_size=2**23,         # 8 MB – large books can be big
        ) as ws:
            logger.info("WS connected. Subscribing…")

            # Send one subscription message per token
            for tid in self._token_ids:
                sub = {"assets_ids": [tid], "type": "market"}
                await ws.send(json.dumps(sub))
                logger.debug(f"Subscribed: {tid[:16]}…")

            # Drain messages
            async for raw in ws:
                if not self._running:
                    break
                await self._dispatch(raw)

    async def _dispatch(self, raw: str):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return

        # The feed sends either a single dict or a list of events
        events = payload if isinstance(payload, list) else [payload]

        for event in events:
            etype = event.get("event_type") or event.get("type", "")

            if etype == "book":
                self._apply_snapshot(event)
            elif etype == "price_change":
                self._apply_incremental(event)
            elif etype == "last_trade_price":
                self._apply_last_trade(event)
            # "tick_size_change" and others are silently ignored

    # ── Event Handlers ─────────────────────────────────────────────────────────

    def _apply_snapshot(self, event: Dict):
        """Full book replacement."""
        tid = event.get("asset_id") or event.get("market_id", "")
        if not tid or tid not in self.books:
            return

        book = self.books[tid]

        def parse_levels(raw_list) -> List[List[float]]:
            result = []
            for item in raw_list:
                try:
                    p = float(item["price"])
                    s = float(item["size"])
                    if s > 0:
                        result.append([p, s])
                except (KeyError, ValueError):
                    pass
            return result

        book.bids = sorted(parse_levels(event.get("bids", [])), key=lambda x: -x[0])
        book.asks = sorted(parse_levels(event.get("asks", [])), key=lambda x: x[0])
        book.timestamp = float(event.get("timestamp") or time.time())

        self.on_update(book)

    def _apply_incremental(self, event: Dict):
        """Add / remove individual price levels."""
        tid = event.get("asset_id", "")
        if not tid or tid not in self.books:
            return

        book = self.books[tid]

        for change in event.get("changes", []):
            try:
                price = float(change["price"])
                size  = float(change["size"])
                side  = change.get("side", "").upper()
            except (KeyError, ValueError):
                continue

            if side == "BUY":
                book.bids = [b for b in book.bids if b[0] != price]
                if size > 0:
                    book.bids.append([price, size])
                book.bids.sort(key=lambda x: -x[0])
            elif side == "SELL":
                book.asks = [a for a in book.asks if a[0] != price]
                if size > 0:
                    book.asks.append([price, size])
                book.asks.sort(key=lambda x: x[0])

        book.timestamp = time.time()
        self.on_update(book)

    def _apply_last_trade(self, event: Dict):
        tid = event.get("asset_id", "")
        if tid in self.books:
            raw = event.get("price")
            if raw:
                self.books[tid].last_trade_price = float(raw)
