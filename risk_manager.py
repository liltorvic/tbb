"""
risk_manager.py – Real-time risk controls for the market making bot.

Tracks:
  • Per-token net position (shares, avg cost basis)
  • Unrealised and realised P&L
  • Daily cumulative P&L with hard stop at MAX_DAILY_LOSS_USD
  • Per-market stop-loss trigger
  • Emergency stop flag (halts all new orders)
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Position data ──────────────────────────────────────────────────────────────

@dataclass
class Position:
    token_id: str
    net_shares: float = 0.0       # + long, – short
    avg_cost: float = 0.0         # dollar cost per share
    realized_pnl: float = 0.0
    last_price: float = 0.0
    peak_price: float = 0.0       # for trailing stop support (future)
    opened_at: float = 0.0

    @property
    def unrealized_pnl(self) -> float:
        if self.last_price and self.net_shares:
            return self.net_shares * (self.last_price - self.avg_cost)
        return 0.0

    @property
    def position_value_usd(self) -> float:
        """Current mark-to-market dollar value of the position."""
        return abs(self.net_shares) * (self.last_price or self.avg_cost)

    @property
    def pnl_pct(self) -> float:
        """Unrealised P&L as a fraction of cost basis."""
        if self.avg_cost == 0 or self.net_shares == 0:
            return 0.0
        return (self.last_price - self.avg_cost) / self.avg_cost


# ── Risk Manager ───────────────────────────────────────────────────────────────

class RiskManager:
    """
    All risk checks go through can_take_position().
    Returns False the moment any limit would be breached.
    """

    def __init__(self, config, client):
        self.config = config
        self.client = client

        self.positions: Dict[str, Position] = {}
        self.daily_pnl: float = 0.0
        self._day_start_ts: float = time.time()
        self._emergency_stop: bool = False
        self._stale_warned: set = set()   # tokens warned once about missing price

    # ── Main Gate ──────────────────────────────────────────────────────────────

    def can_take_position(
        self,
        token_id: str,
        side: str,      # "BUY" | "SELL"
        size: float,
        price: float,
    ) -> bool:
        """
        Returns True only if ALL of the following hold:
          1. Emergency stop is not active
          2. Daily loss limit not breached
          3. Resulting position would not exceed MAX_POSITION_USD
        """
        if self._emergency_stop:
            logger.debug("can_take_position → False  (emergency stop active)")
            return False

        if self.daily_pnl <= -self.config.MAX_DAILY_LOSS_USD:
            logger.warning(
                f"Daily loss limit reached: ${self.daily_pnl:.2f}  "
                f"(limit ${self.config.MAX_DAILY_LOSS_USD})"
            )
            return False

        pos = self.positions.get(token_id, Position(token_id=token_id))
        delta = size if side == "BUY" else -size
        projected_net = pos.net_shares + delta
        projected_value = abs(projected_net) * price

        if projected_value > self.config.MAX_POSITION_USD:
            logger.debug(
                f"Position limit: projected ${projected_value:.2f} > "
                f"max ${self.config.MAX_POSITION_USD}"
            )
            return False

        return True

    # ── Position Tracking ──────────────────────────────────────────────────────

    def get_position(self, token_id: str) -> float:
        """Net shares held (+long / –short)."""
        return self.positions.get(token_id, Position(token_id=token_id)).net_shares

    def get_avg_cost(self, token_id: str) -> float:
        """Average entry price for the token, if known."""
        return self.positions.get(token_id, Position(token_id=token_id)).avg_cost

    def get_holding_age_seconds(self, token_id: str) -> float:
        pos = self.positions.get(token_id, Position(token_id=token_id))
        if pos.net_shares <= 0 or pos.opened_at <= 0:
            return 0.0
        return max(0.0, time.time() - pos.opened_at)

    def record_fill(
        self,
        token_id: str,
        side: str,
        size: float,
        price: float,
    ):
        """
        Record a fill for P&L tracking.

        Position state (net_shares, avg_cost) is managed by refresh_from_api()
        which pulls canonical data from the Polymarket API every 15 s.
        This method only computes realised P&L and updates daily totals
        so there is no race between the two.
        """
        side = (side or "").upper()
        if side not in {"BUY", "SELL"}:
            logger.warning(
                f"record_fill received unknown side={side!r} for {token_id[:14]} "
                f"size={size:.2f} price={price:.4f}"
            )
            return

        if token_id not in self.positions:
            self.positions[token_id] = Position(token_id=token_id)
        pos = self.positions[token_id]

        if side == "BUY":
            prior_shares = pos.net_shares
            new_shares = prior_shares + size
            if new_shares > 0:
                pos.avg_cost = (
                    ((prior_shares * pos.avg_cost) + (size * price)) / new_shares
                    if prior_shares > 0 else price
                )
            if prior_shares <= 0 and new_shares > 0:
                pos.opened_at = time.time()
            pos.net_shares = new_shares
            logger.info(
                f"Fill BUY   {size:.2f}sh @ {price:.4f}  "
                f"[{token_id[:14]}]"
            )
        elif side == "SELL":
            # Use the stored avg_cost (set by refresh_from_api) for P&L calc.
            # avg_cost reflects the entry price *before* this sell reduces shares.
            avg = pos.avg_cost if pos.avg_cost > 0 else price
            realised = size * (price - avg)
            pos.realized_pnl += realised
            self.daily_pnl += realised
            logger.info(
                f"Fill SELL  {size:.2f}sh @ {price:.4f}  "
                f"avg_cost={avg:.4f}  "
                f"realised_pnl=${realised:+.2f}  "
                f"daily_pnl=${self.daily_pnl:+.2f}"
            )
            pos.net_shares = max(0.0, pos.net_shares - size)
            if pos.net_shares == 0:
                pos.avg_cost = 0.0
                pos.opened_at = 0.0

        pos.last_price = price

    def update_mark_prices(self, prices: Dict[str, float]):
        """Push live mid-prices into all held positions for P&L marking."""
        for token_id, price in prices.items():
            if token_id in self.positions:
                self.positions[token_id].last_price = price

    # ── API Sync ───────────────────────────────────────────────────────────────

    def refresh_from_api(self):
        """
        Pull the canonical position state from Polymarket.
        Called at startup and periodically by the risk monitor loop.
        """
        try:
            raw_positions = self.client.get_positions()
        except Exception as exc:
            logger.error(f"refresh_from_api: failed to fetch positions: {exc}")
            return

        seen_token_ids = set()
        for p in raw_positions:
            token_id  = p.get("asset") or p.get("token_id") or p.get("assetId", "")
            size      = float(p.get("size") or p.get("net_size") or 0)
            avg_price = float(p.get("avgPrice") or p.get("avg_price") or 0)

            if not token_id:
                continue

            seen_token_ids.add(token_id)

            if token_id not in self.positions:
                self.positions[token_id] = Position(token_id=token_id)

            pos = self.positions[token_id]
            if pos.net_shares <= 0 and size > 0:
                pos.opened_at = time.time()
            elif size <= 0:
                pos.opened_at = 0.0

            self.positions[token_id].net_shares = size
            self.positions[token_id].avg_cost   = avg_price

        for token_id, pos in self.positions.items():
            if token_id in seen_token_ids:
                continue
            if pos.net_shares != 0 or pos.avg_cost != 0:
                pos.net_shares = 0.0
                pos.avg_cost = 0.0
                pos.last_price = 0.0
                pos.opened_at = 0.0
                self._stale_warned.discard(token_id)

        held_count = sum(1 for pos in self.positions.values() if pos.net_shares)
        logger.info(f"Positions refreshed from API  ({held_count} tokens held)")

    # ── Stop-Loss ──────────────────────────────────────────────────────────────

    def check_stop_losses(self) -> List[str]:
        """
        Returns list of token IDs where the stop-loss threshold is breached.
        The caller should close / hedge these positions.
        """
        triggered: List[str] = []
        for token_id, pos in self.positions.items():
            if pos.net_shares == 0 or pos.avg_cost == 0:
                continue
            # Skip positions with no live price — can't evaluate PnL without it
            if pos.last_price == 0:
                if token_id not in self._stale_warned:
                    logger.info(
                        f"Position {token_id[:14]} has no live price "
                        f"(not in active markets) – skipping stop-loss check"
                    )
                    self._stale_warned.add(token_id)
                continue
            if pos.net_shares > 0 and pos.pnl_pct < -self.config.STOP_LOSS_PCT:
                logger.warning(
                    f"⛔ Stop-loss on {token_id[:14]}  "
                    f"pnl={pos.pnl_pct:.2%}  "
                    f"value=${pos.position_value_usd:.2f}"
                )
                triggered.append(token_id)
        return triggered

    # ── Emergency Controls ─────────────────────────────────────────────────────

    def emergency_stop(self, reason: str = ""):
        self._emergency_stop = True
        logger.critical(f"🚨 EMERGENCY STOP ACTIVATED  reason={reason!r}")

    def resume(self):
        self._emergency_stop = False
        logger.info("Emergency stop cleared – trading resumed")

    @property
    def is_halted(self) -> bool:
        return self._emergency_stop

    # ── Daily Reset ────────────────────────────────────────────────────────────

    def maybe_reset_daily_pnl(self):
        """
        Resets daily P&L at UTC midnight.
        Call this from the risk-monitor loop every ~60 s.
        """
        now = datetime.now(timezone.utc)
        if now.hour == 0 and now.minute == 0:
            self.daily_pnl = 0.0
            self._day_start_ts = time.time()
            logger.info("Daily P&L counter reset (UTC midnight)")

    # ── Summary ────────────────────────────────────────────────────────────────

    def summary(self) -> Dict:
        total_unrealised = sum(p.unrealized_pnl for p in self.positions.values())
        total_realised   = sum(p.realized_pnl   for p in self.positions.values())
        return {
            "positions_held":       len([p for p in self.positions.values() if p.net_shares]),
            "daily_pnl_usd":        round(self.daily_pnl,       2),
            "unrealised_pnl_usd":   round(total_unrealised,     2),
            "realised_pnl_usd":     round(total_realised,       2),
            "emergency_stop":       self._emergency_stop,
            "daily_loss_remaining": round(
                self.config.MAX_DAILY_LOSS_USD + self.daily_pnl, 2
            ),
        }
