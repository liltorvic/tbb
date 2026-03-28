"""
clob_client.py – Thin wrapper around the official py-clob-client SDK.

Adds:
  • Exponential-backoff retries on transient errors
  • Rate-limit handling (HTTP 429)
  • Dry-run mode (logs instead of submitting)
  • Structured logging on every order action
"""

import logging
import time
from functools import wraps
from typing import Dict, List, Optional

import httpx

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    ApiCreds,
    AssetType,
    BalanceAllowanceParams,
    OrderArgs,
    OrderType,
)
from py_clob_client.constants import POLYGON
from py_clob_client.exceptions import PolyApiException

logger = logging.getLogger(__name__)


# ── Retry decorator ────────────────────────────────────────────────────────────

def retry_on_error(max_retries: int = 3, base_delay: float = 1.0, backoff: float = 2.0):
    """Retry with exponential back-off.  Skips retry on auth / bad-request errors."""
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            delay = base_delay
            for attempt in range(1, max_retries + 1):
                try:
                    return fn(*args, **kwargs)
                except PolyApiException as exc:
                    if exc.status_code == 429:                      # rate-limited
                        wait = delay * 3
                        logger.warning(f"Rate limited – sleeping {wait:.1f}s")
                        time.sleep(wait)
                    elif exc.status_code in (400, 401, 403):        # non-retryable
                        logger.error(f"Non-retryable API error [{exc.status_code}]: {exc}")
                        raise
                    else:
                        logger.warning(
                            f"{fn.__name__} attempt {attempt}/{max_retries} failed: {exc}"
                        )
                        time.sleep(delay)
                except Exception as exc:
                    logger.warning(
                        f"{fn.__name__} attempt {attempt}/{max_retries} error: {exc}"
                    )
                    time.sleep(delay)
                delay *= backoff
            raise RuntimeError(f"Max retries exceeded for {fn.__name__}")
        return wrapper
    return decorator


# ── Client ─────────────────────────────────────────────────────────────────────

class PolymarketClient:
    """
    Wraps ClobClient with retries, dry-run, and convenience helpers.

    Usage:
        config = Config()
        client = PolymarketClient(config)
    """

    def __init__(self, config):
        self.config = config
        self.dry_run = config.DRY_RUN

        creds: Optional[ApiCreds] = None
        if config.API_KEY and config.API_SECRET and config.API_PASSPHRASE:
            creds = ApiCreds(
                api_key=config.API_KEY,
                api_secret=config.API_SECRET,
                api_passphrase=config.API_PASSPHRASE,
            )
        else:
            logger.warning(
                "No API credentials found in env. "
                "Order placement will be unavailable until you run setup_credentials.py."
            )

        # Use POLY_PROXY (1) if a proxy wallet is configured (email accounts),
        # otherwise default to EOA (0) for MetaMask/direct wallet accounts.
        sig_type = 1 if config.PROXY_WALLET else 0

        self._clob = ClobClient(
            host=config.CLOB_HOST,
            key=config.PRIVATE_KEY if config.PRIVATE_KEY else None,
            chain_id=POLYGON,
            creds=creds,
            signature_type=sig_type,
            funder=config.PROXY_WALLET if config.PROXY_WALLET else None,
        )

        mode = "DRY-RUN" if self.dry_run else "LIVE"
        logger.info(f"PolymarketClient ready  mode={mode}")

    # ── Credential Bootstrap ──────────────────────────────────────────────────

    def derive_api_key(self) -> Dict:
        """
        One-time call: derives or creates L2 API credentials from your private key.
        Run this once and save the output to your .env file.
        """
        logger.info("Deriving API credentials from private key…")
        result = self._clob.create_or_derive_api_creds()
        logger.info(f"Credentials derived: {result}")
        return result

    # ── Market Data ────────────────────────────────────────────────────────────

    @retry_on_error()
    def get_markets(self, next_cursor: str = "") -> Dict:
        return self._clob.get_markets(next_cursor=next_cursor)

    def get_all_active_markets(self) -> List[Dict]:
        """Fetch every active market (handles pagination automatically)."""
        markets: List[Dict] = []
        cursor = ""
        while True:
            resp = self.get_markets(next_cursor=cursor)
            batch = resp.get("data") or []
            markets.extend(batch)
            cursor = resp.get("next_cursor", "")
            if not cursor or cursor == "LTE=":   # "LTE=" is Polymarket's end-of-pages sentinel
                break
            time.sleep(0.25)                     # polite pacing
        logger.info(f"Fetched {len(markets)} markets from CLOB")
        return markets

    @retry_on_error()
    def get_market(self, condition_id: str) -> Dict:
        return self._clob.get_market(condition_id=condition_id)

    @retry_on_error()
    def get_orderbook(self, token_id: str) -> Dict:
        """Order-book snapshot for one token (YES or NO side)."""
        return self._clob.get_order_book(token_id=token_id)

    @retry_on_error()
    def get_midpoint(self, token_id: str) -> Optional[float]:
        resp = self._clob.get_midpoint(token_id=token_id)
        raw = resp.get("mid")
        return float(raw) if raw else None

    @retry_on_error()
    def get_price(self, token_id: str, side: str) -> Optional[float]:
        resp = self._clob.get_price(token_id=token_id, side=side)
        raw = resp.get("price")
        return float(raw) if raw else None

    # ── Account / Portfolio ────────────────────────────────────────────────────

    @retry_on_error()
    def get_balance(self) -> float:
        """Returns USDC.e balance in dollar terms."""
        resp = self._clob.get_balance_allowance(
            params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        raw = resp.get("balance", "0")
        return float(raw) / 1_000_000   # USDC.e has 6 decimals

    @retry_on_error()
    def get_open_orders(self) -> List[Dict]:
        return self._clob.get_orders() or []

    @retry_on_error()
    def get_positions(self) -> List[Dict]:
        """
        Fetch live positions from the Gamma API using the proxy wallet address.
        This is the canonical source of truth for open positions — more reliable
        and richer than anything available in the CLOB SDK directly.

        Returns a list of dicts with keys: token_id, size, avg_price.
        Requires PROXY_WALLET to be set in .env.
        """
        wallet = self.config.PROXY_WALLET
        if not wallet:
            logger.warning(
                "PROXY_WALLET not set in .env – cannot fetch live positions. "
                "Run setup_credentials.py and add PROXY_WALLET to your .env file."
            )
            return []

        url = f"https://data-api.polymarket.com/positions?user={wallet}"
        try:
            resp = httpx.get(url, timeout=10)
            if resp.status_code == 404:
                # 404 = wallet has no positions – this is normal, not an error
                logger.debug("No positions found for wallet (404) – normal for new/dry-run accounts")
                return []
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as exc:
            logger.error(
                f"get_positions HTTP error {exc.response.status_code}: {exc}"
            )
            raise
        except Exception as exc:
            logger.error(f"get_positions failed: {exc}")
            raise

        positions = []
        for p in data:
            token_id  = p.get("asset") or p.get("token_id") or p.get("assetId")
            size      = float(p.get("size", 0))
            avg_price = float(p.get("avgPrice") or p.get("avg_price") or 0)

            if token_id and size != 0:
                positions.append({
                    # Primary keys used by order_manager and risk_manager
                    "token_id":  token_id,
                    "size":      size,
                    "avg_price": avg_price,
                    # Legacy key aliases so risk_manager.refresh_from_api() works too
                    "asset":     token_id,
                    "avgPrice":  avg_price,
                })

        logger.debug(f"Fetched {len(positions)} position(s) from Gamma API")
        return positions

    # ── Order Actions ──────────────────────────────────────────────────────────

    def place_limit_order(
        self,
        token_id: str,
        side: str,          # "BUY" | "SELL"
        price: float,       # probability, e.g. 0.63
        size: float,        # number of shares (NOT dollar amount)
        market_label: str = "",
    ) -> Optional[Dict]:
        """
        Place a GTC limit order.
        In dry-run mode this only logs; no on-chain action is taken.
        """
        label = (market_label or token_id)[:20]

        if self.dry_run:
            logger.info(
                f"[DRY-RUN] {side:4s}  {size:7.2f} sh @ {price:.4f}  [{label}]"
            )
            return {
                "status": "dry_run",
                "orderID": f"dry_{int(time.time()*1000)}",
                "token_id": token_id,
                "side": side,
                "price": price,
                "size": size,
            }

        try:
            order_args = OrderArgs(
                price=price,
                size=size,
                side=side,
                token_id=token_id,
            )
            signed = self._clob.create_order(order_args)
            resp = self._clob.post_order(signed, OrderType.GTC)
            oid = resp.get("orderID", "?")
            logger.info(
                f"ORDER PLACED  {side:4s}  {size:7.2f} sh @ {price:.4f}  "
                f"[{label}]  id={oid}"
            )
            return resp
        except Exception as exc:
            logger.error(f"place_limit_order failed [{label}]: {exc}")
            return None

    def cancel_order(self, order_id: str) -> bool:
        if self.dry_run:
            logger.info(f"[DRY-RUN] CANCEL {order_id}")
            return True
        try:
            self._clob.cancel(order_id=order_id)
            logger.debug(f"Cancelled order {order_id}")
            return True
        except Exception as exc:
            logger.error(f"cancel_order {order_id} failed: {exc}")
            return False

    def cancel_all_orders(self) -> bool:
        if self.dry_run:
            logger.info("[DRY-RUN] CANCEL ALL orders")
            return True
        try:
            self._clob.cancel_all()
            logger.info("All open orders cancelled")
            return True
        except Exception as exc:
            logger.error(f"cancel_all_orders failed: {exc}")
            return False
