"""
order_manager.py – Market making order lifecycle.

Responsibilities:
  • Dynamic spread calculation (volatility + depth + competition)
  • Inventory-skewed sizing  (lean against over-exposed positions)
  • Place one bid and one ask per market
  • Cancel stale quotes when the market moves
  • Sync local state against the live order API periodically
"""

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from orderbook_ws import OrderBook

logger = logging.getLogger(__name__)


# ── Data structures ────────────────────────────────────────────────────────────

@dataclass
class TrackedOrder:
    order_id: str
    token_id: str
    side: str        # "BUY" | "SELL"
    price: float
    size: float
    placed_at: float = field(default_factory=time.time)
    label: str = ""


@dataclass
class MarketState:
    condition_id: str
    token_ids: List[str]
    label: str
    # order_id → TrackedOrder
    orders: Dict[str, TrackedOrder] = field(default_factory=dict)
    # ring-buffer of (timestamp, mid_price) for vol estimate
    price_history: deque = field(default_factory=lambda: deque(maxlen=30))
    last_refresh: float = 0.0
    quote_count: int = 0    # lifetime quotes placed
    entry_blocked_until: float = 0.0


# ── Order Manager ──────────────────────────────────────────────────────────────

class OrderManager:
    """
    Drives the per-market quoting logic.
    Called from the async main loop via handle_orderbook_update().
    """

    # Cancel an order if our target moves more than this
    _PRICE_TOLERANCE = 0.005
    # Force-cancel any order older than this (safety net)
    _MAX_ORDER_AGE_SECONDS = 600

    def __init__(self, client, risk_manager, config):
        self.client = client
        self.risk = risk_manager
        self.config = config
        self.markets: Dict[str, MarketState] = {}  # condition_id → state
        self._known_orders: Dict[str, TrackedOrder] = {}
        self._global_entry_blocked_until: float = 0.0

    # ── Market Registration ────────────────────────────────────────────────────

    def register_market(self, info: Dict):
        cid = info["condition_id"]
        if cid not in self.markets:
            self.markets[cid] = MarketState(
                condition_id=cid,
                token_ids=info["token_ids"],
                label=info["label"],
            )
            logger.info(f"Registered market: {info['label'][:55]}")

    def remove_market(self, condition_id: str):
        state = self.markets.pop(condition_id, None)
        if state:
            for oid in list(state.orders):
                self.client.cancel_order(oid)
            logger.info(f"Removed market {condition_id[:12]}…")

    # ── WebSocket callback ─────────────────────────────────────────────────────

    async def handle_orderbook_update(self, book: OrderBook):
        """
        Entry point for every WS tick.
        Finds the parent market and conditionally re-quotes.
        """
        state = self._market_for_token(book.token_id)
        if not state:
            logger.debug(f"No market registered for token {book.token_id[:16]}…")
            return

        # Track price for volatility estimate
        if book.mid_price:
            state.price_history.append((time.time(), book.mid_price))

        # Rate-limit: don't re-quote more often than ORDER_REFRESH_INTERVAL
        elapsed = time.time() - state.last_refresh
        if elapsed < self.config.ORDER_REFRESH_INTERVAL:
            logger.debug(
                f"[{state.label[:20]}] Rate-limited  "
                f"({elapsed:.0f}s / {self.config.ORDER_REFRESH_INTERVAL:.0f}s)"
            )
            return

        logger.debug(
            f"[{state.label[:20]}] Refreshing quotes  "
            f"mid={book.mid_price}  spread_bps={book.spread_bps}"
        )
        await self._refresh_quotes(state, book)

    async def refresh_market_for_token(
        self,
        token_id: str,
        book: Optional[OrderBook],
        force: bool = False,
    ):
        """
        Re-quote a market on demand using the latest known book snapshot.
        Used after fills / position syncs so inventory exits do not wait for
        another WS event.
        """
        state = self._market_for_token(token_id)
        if not state or not book:
            return

        if not force:
            elapsed = time.time() - state.last_refresh
            if elapsed < self.config.ORDER_REFRESH_INTERVAL:
                return

        if book.mid_price:
            state.price_history.append((time.time(), book.mid_price))

        await self._refresh_quotes(state, book)

    def get_tracked_order(self, order_id: str) -> Optional[TrackedOrder]:
        if order_id in self._known_orders:
            return self._known_orders[order_id]
        for state in self.markets.values():
            if order_id in state.orders:
                return state.orders[order_id]
        return None

    def market_has_inventory_or_orders(self, condition_id: str) -> bool:
        state = self.markets.get(condition_id)
        if not state:
            return False
        if state.orders:
            return True
        return any(self.risk.get_position(token_id) > 0 for token_id in state.token_ids)

    # ── Core quoting logic ─────────────────────────────────────────────────────

    async def _refresh_quotes(self, state: MarketState, book: OrderBook):
        if not book.mid_price:
            logger.debug(f"[{state.label[:30]}] No mid-price yet – skipping")
            return

        now = time.time()
        mid = book.mid_price
        spread = self._dynamic_spread(book, state)

        raw_bid = mid - spread / 2.0
        raw_ask = mid + spread / 2.0

        best_bid = book.best_bid or raw_bid
        best_ask = book.best_ask or raw_ask

        # For entry, join the best bid rather than sitting far behind it.
        bid_price = round(max(0.01, min(max(raw_bid, best_bid), 0.98)), 4)
        ask_price = round(max(0.02, min(raw_ask, 0.99)), 4)

        # Shares = desired USD notional / collateral cost per share.
        # BUY  YES @ price P  →  costs P per share
        # SELL YES @ price P  →  costs (1 - P) per share (you're buying NO)
        #   unless we already hold YES shares (then it's free collateral).
        base_bid_sh = self.config.ORDER_SIZE_USD / bid_price

        yes_token = state.token_ids[0]
        held_shares = self.risk.get_position(yes_token)
        avg_cost = self.risk.get_avg_cost(yes_token)
        holding_age = self.risk.get_holding_age_seconds(yes_token)
        if held_shares > 0:
            # We own shares — selling them costs nothing extra
            base_ask_sh = self.config.ORDER_SIZE_USD / ask_price
        else:
            # No inventory — SELL requires (1 - price) collateral per share
            sell_collateral = 1.0 - ask_price
            base_ask_sh = self.config.ORDER_SIZE_USD / sell_collateral

        # Inventory skew
        bid_sh, ask_sh = self._inventory_skew(yes_token, base_bid_sh, base_ask_sh, mid)
        min_shares = max(float(self.config.MIN_ORDER_SIZE_SHARES), 0.01)

        if held_shares > 0:
            await self._cancel_side(state, "BUY")
            bid_sh = 0.0
            ask_sh = round(max(held_shares, min_shares), 2)
            ask_price = self._inventory_exit_price(book, avg_cost, holding_age)

        if ask_price <= bid_price:
            ask_price = round(min(0.99, bid_price + 0.0001), 4)

        # Risk check
        can_buy  = self.risk.can_take_position(yes_token, "BUY",  bid_sh, bid_price)
        can_sell = self.risk.can_take_position(yes_token, "SELL", ask_sh, ask_price)

        if can_buy and (now < self._global_entry_blocked_until or now < state.entry_blocked_until):
            can_buy = False

        # Cancel anything too far from our new targets
        await self._cancel_stale(state, bid_price, ask_price)

        # Place bid (always allowed — uses regular exchange path)
        if can_buy and bid_sh >= min_shares:
            if not self._order_exists(state, "BUY", bid_price):
                resp = self.client.place_limit_order(
                    token_id=yes_token,
                    side="BUY",
                    price=bid_price,
                    size=round(bid_sh, 2),
                    market_label=state.label,
                )
                self._handle_order_rejection(resp, state, "BUY")
                self._track_order(resp, yes_token, "BUY", bid_price, round(bid_sh, 2), state)

        # Place ask — only when we hold shares to sell.
        # Naked SELLs (selling shares we don't own) fail on neg-risk markets
        # because the proxy wallet isn't set up for the neg-risk exchange path.
        # Instead we buy first, then sell inventory at the ask for spread capture.
        if can_sell and ask_sh >= min_shares and held_shares > 0:
            # In inventory mode, unwind the whole tracked position to recycle capital.
            ask_sh = round(min(ask_sh, held_shares), 2)
            if ask_sh >= min_shares and not self._order_exists(state, "SELL", ask_price):
                resp = self.client.place_limit_order(
                    token_id=yes_token,
                    side="SELL",
                    price=ask_price,
                    size=round(ask_sh, 2),
                    market_label=state.label,
                )
                self._handle_order_rejection(resp, state, "SELL")
                self._track_order(resp, yes_token, "SELL", ask_price, round(ask_sh, 2), state)
        elif can_sell and held_shares <= 0:
            logger.debug(
                f"[{state.label[:30]}] Skipping SELL – no inventory "
                f"(need shares from BUY fills first)"
            )
        elif ask_sh < min_shares and held_shares > 0:
            logger.debug(
                f"[{state.label[:30]}] Skipping SELL – size {ask_sh:.2f} below "
                f"minimum {min_shares:.2f} shares"
            )
        elif bid_sh < min_shares:
            logger.debug(
                f"[{state.label[:30]}] Skipping BUY – size {bid_sh:.2f} below "
                f"minimum {min_shares:.2f} shares"
            )

        state.last_refresh = time.time()
        state.quote_count += 1

        spread_bps = (ask_price - bid_price) / mid * 10_000
        logger.info(
            f"[{state.label[:30]:<30}]  "
            f"mid={mid:.4f}  bid={bid_price:.4f}  ask={ask_price:.4f}  "
            f"spread={spread_bps:.1f}bps  "
            f"live_orders={len(state.orders)}"
        )

    # ── Spread calculation ─────────────────────────────────────────────────────

    def _dynamic_spread(self, book: OrderBook, state: MarketState) -> float:
        """
        Build a spread from three components:
          a) base target
          b) volatility premium
          c) depth-imbalance premium

        All in decimal (not bps).
        """
        min_s  = self.config.MIN_SPREAD_BPS  / 10_000
        max_s  = self.config.MAX_SPREAD_BPS  / 10_000
        target = self.config.TARGET_SPREAD_BPS / 10_000

        spread = target

        # a) Volatility premium – widen when prices are moving fast
        vol = self._recent_volatility(state)
        spread += vol * 3.0          # empirical multiplier; tune per market

        # b) Depth imbalance – if one side is very thin, risk is higher
        imbalance = abs(book.depth_imbalance)
        spread += imbalance * 0.006  # up to ~0.6 bps per 10% imbalance

        # c) Competition: don't cross inside the existing book spread
        if book.spread:
            # Match the book's own spread scaled slightly inward so we queue
            spread = max(spread, book.spread * 0.85)

        return float(max(min_s, min(spread, max_s)))

    def _recent_volatility(self, state: MarketState) -> float:
        """Standard deviation of mid-price in the price history buffer."""
        if len(state.price_history) < 5:
            return 0.0
        prices = [p for _, p in state.price_history]
        mean = sum(prices) / len(prices)
        variance = sum((p - mean) ** 2 for p in prices) / len(prices)
        return variance ** 0.5

    # ── Inventory skew ─────────────────────────────────────────────────────────

    def _inventory_skew(
        self,
        token_id: str,
        bid_sh: float,
        ask_sh: float,
        mid: float,
    ) -> Tuple[float, float]:
        """
        If we are over-long YES → shrink bid, grow ask (encourage sells).
        If we are over-short YES → grow bid, shrink ask (encourage buys).
        """
        net_pos = self.risk.get_position(token_id)
        if net_pos == 0:
            return bid_sh, ask_sh

        max_sh = self.config.MAX_POSITION_USD / max(mid, 0.01)
        skew = net_pos / max_sh           # –1 to +1

        factor = abs(skew) * self.config.MAX_INVENTORY_SKEW
        max_sh_per_order = self.config.MAX_ORDER_SIZE_USD / max(mid, 0.01)

        if skew > 0:   # long – push asks, pull bids
            bid_sh = max(0.0, bid_sh * (1.0 - factor))
            ask_sh = min(ask_sh * (1.0 + factor * 0.5), max_sh_per_order)
        else:          # short – push bids, pull asks
            bid_sh = min(bid_sh * (1.0 + factor * 0.5), max_sh_per_order)
            ask_sh = max(0.0, ask_sh * (1.0 - factor))

        return round(bid_sh, 2), round(ask_sh, 2)

    # ── Stale-order cancellation ───────────────────────────────────────────────

    async def _cancel_stale(self, state: MarketState, new_bid: float, new_ask: float):
        stale_ids = []
        now = time.time()

        for oid, order in state.orders.items():
            too_old = (now - order.placed_at) > self._MAX_ORDER_AGE_SECONDS
            bid_moved = (
                order.side == "BUY"
                and abs(order.price - new_bid) > self._PRICE_TOLERANCE
            )
            ask_moved = (
                order.side == "SELL"
                and abs(order.price - new_ask) > self._PRICE_TOLERANCE
            )
            if too_old or bid_moved or ask_moved:
                stale_ids.append(oid)

        for oid in stale_ids:
            if self.client.cancel_order(oid):
                state.orders.pop(oid, None)

    async def _cancel_side(self, state: MarketState, side: str):
        for oid, order in list(state.orders.items()):
            if order.side != side:
                continue
            if self.client.cancel_order(oid):
                state.orders.pop(oid, None)

    def _inventory_exit_price(self, book: OrderBook, avg_cost: float, holding_age: float) -> float:
        """
        Exit inventory aggressively enough to free capital on a small account.
        The longer we hold, the more we prioritize getting flat over extracting spread.
        """
        best_bid = book.best_bid or 0.01
        best_ask = book.best_ask or min(best_bid + 0.01, 0.99)
        tick = 0.0001
        if holding_age < 60:
            target_profit = max(0.002, avg_cost * 0.010) if avg_cost > 0 else 0.002
            candidate = min(best_ask - tick, max(best_bid + tick, avg_cost + target_profit))
        elif holding_age < 180:
            target_profit = max(0.001, avg_cost * 0.005) if avg_cost > 0 else 0.001
            candidate = min(best_ask - tick, max(best_bid + tick, avg_cost + target_profit))
        elif holding_age < 420:
            target_profit = max(0.0005, avg_cost * 0.002) if avg_cost > 0 else 0.0005
            candidate = max(best_bid + tick, avg_cost + target_profit)
        else:
            # After several minutes, stop holding out for the ask and just sit
            # at the bid edge if that still preserves a non-negative exit.
            candidate = max(best_bid, avg_cost + tick)
        return round(max(0.02, min(candidate, 0.99)), 4)

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _track_order(
        self,
        resp: Optional[Dict],
        token_id: str,
        side: str,
        price: float,
        size: float,
        state: MarketState,
    ):
        if not resp:
            return
        if resp.get("status") == "error":
            return
        oid = resp.get("orderID") or resp.get("order_id")
        if not oid:
            return
        state.orders[oid] = TrackedOrder(
            order_id=oid,
            token_id=token_id,
            side=side,
            price=price,
            size=size,
            label=state.label,
        )
        self._known_orders[oid] = state.orders[oid]

    def _order_exists(self, state: MarketState, side: str, price: float) -> bool:
        """True if we already have a live order on this side near this price."""
        for order in state.orders.values():
            if (
                order.side == side
                and abs(order.price - price) < self._PRICE_TOLERANCE
            ):
                return True
        return False

    def _market_for_token(self, token_id: str) -> Optional[MarketState]:
        for state in self.markets.values():
            if token_id in state.token_ids:
                return state
        return None

    def _handle_order_rejection(self, resp: Optional[Dict], state: MarketState, side: str):
        if not resp or resp.get("status") != "error":
            return
        if resp.get("error_type") != "insufficient_balance" or side != "BUY":
            return

        cooldown = max(self.config.MARKET_REFRESH_INTERVAL, 300.0)
        blocked_until = time.time() + cooldown
        state.entry_blocked_until = blocked_until
        self._global_entry_blocked_until = blocked_until

        logger.warning(
            "[%s] BUY blocked for %.0fs after insufficient balance; cancelling open bids to free collateral",
            state.label[:30],
            cooldown,
        )

        for market_state in self.markets.values():
            for oid, order in list(market_state.orders.items()):
                if order.side != "BUY":
                    continue
                if self.client.cancel_order(oid):
                    market_state.orders.pop(oid, None)

    # ── Fill Handling ──────────────────────────────────────────────────────────

    def remove_filled_order(self, order_id: str) -> Optional[TrackedOrder]:
        """Remove a filled order from tracking and return it (or None)."""
        for state in self.markets.values():
            if order_id in state.orders:
                return state.orders.pop(order_id)
        return None

    # ── Maintenance ────────────────────────────────────────────────────────────

    async def cancel_all(self):
        logger.warning("Emergency cancel – removing all open orders")
        self.client.cancel_all_orders()
        for state in self.markets.values():
            state.orders.clear()

    async def execute_stop_loss_exits(self, token_ids: List[str]):
        """
        Best-effort emergency exits for triggered stop-loss tokens.
        Cancels local quotes for affected markets, then places protective SELLs
        for any held YES inventory.
        """
        for token_id in token_ids:
            held_shares = self.risk.get_position(token_id)
            if held_shares <= 0:
                continue

            state = self._market_for_token(token_id)
            label = state.label if state else token_id[:20]
            if state:
                for oid in list(state.orders):
                    if self.client.cancel_order(oid):
                        state.orders.pop(oid, None)

            midpoint = self.client.get_midpoint(token_id) or 0.5
            exit_price = round(max(0.01, min(midpoint - 0.05, 0.95)), 4)
            exit_size = round(max(0.01, held_shares), 2)

            logger.warning(
                f"[STOP-LOSS] placing protective SELL  {exit_size:.2f} sh @ {exit_price:.4f}  "
                f"[{label[:28]}]"
            )
            self.client.place_limit_order(
                token_id=token_id,
                side="SELL",
                price=exit_price,
                size=exit_size,
                market_label=f"STOP {label[:14]}",
            )

    def sync_open_orders(self):
        """
        Reconcile local order tracking against the API.
        Removes locally-tracked IDs that are no longer open on the exchange.
        """
        try:
            api_orders = self.client.get_open_orders()
            live_ids = {o.get("id") or o.get("orderID") for o in api_orders}
            for state in self.markets.values():
                state.orders = {
                    oid: ord_
                    for oid, ord_ in state.orders.items()
                    # Keep dry-run orders (prefixed dry_) and real live orders
                    if oid.startswith("dry_") or oid in live_ids
                }
        except Exception as exc:
            logger.warning(f"sync_open_orders failed: {exc}")
