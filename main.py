"""
main.py – Polymarket Market Making Bot v2

Entry point and async orchestrator.

Concurrent tasks:
  ws_feed         – maintains real-time order books via WebSocket
  market_refresh  – re-scores and rotates markets every N minutes
  risk_monitor    – checks stop-losses and daily limits every 15 s
  merge_loop      – periodically merges offsetting positions on-chain
  stats_loop      – logs a P&L / status summary every 60 s
"""

import asyncio
import logging
import signal
import sys
import time
from datetime import datetime, timezone

from config import Config
from clob_client import PolymarketClient
from orderbook_ws import OrderBookFeed
from market_selector import MarketSelector
from order_manager import OrderManager
from risk_manager import RiskManager
from position_merger import PositionMerger


# ── Logging setup ──────────────────────────────────────────────────────────────

def configure_logging(level: str):
    fmt = "%(asctime)s  %(levelname)-8s  %(name)-20s  %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"
    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ]
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=fmt,
        datefmt=datefmt,
        handlers=handlers,
    )
    # Reduce noise from external libraries
    for noisy in ("websockets", "web3", "urllib3", "requests"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


logger = logging.getLogger(__name__)


# ── Bot ────────────────────────────────────────────────────────────────────────

class MarketMakingBot:

    def __init__(self, config: Config):
        self.config = config

        # Instantiate all components
        self.client   = PolymarketClient(config)
        self.risk     = RiskManager(config, self.client)
        self.order_mgr = OrderManager(self.client, self.risk, config)
        self.selector = MarketSelector(self.client, config)
        self.merger   = PositionMerger(config)

        self.active_markets = []
        self._ws_feed: "OrderBookFeed | None" = None
        self._running = False
        self._stop_event = asyncio.Event()

    # ── Startup ────────────────────────────────────────────────────────────────

    async def startup(self) -> bool:
        self._banner()

        # 1. Verify connectivity and balances
        try:
            usdc = self.client.get_balance()
            pol  = self.merger.pol_balance()
            logger.info(f"Wallet balance  USDC=${usdc:.2f}  POL={pol:.4f}")

            if not self.config.DRY_RUN:
                min_usdc = self.config.ORDER_SIZE_USD * 4  # at least 4 orders worth
                if usdc < min_usdc:
                    logger.error(
                        f"Insufficient USDC for live trading "
                        f"(have ${usdc:.2f}, need ≥${min_usdc:.2f})"
                    )
                    return False
                if pol < self.config.MIN_POL_BALANCE:
                    logger.warning(
                        f"Low POL ({pol:.4f}) – position merging may fail. "
                        f"Top up your wallet with at least {self.config.MIN_POL_BALANCE} POL."
                    )
        except Exception as exc:
            logger.warning(
                f"Balance check failed ({exc}) – proceeding "
                f"(normal during first-time setup)"
            )

        # 2. Sync existing positions from the API
        self.risk.refresh_from_api()

        # 3. Select initial market set
        self.active_markets = self.selector.select_markets(force=True)
        if not self.active_markets:
            logger.error(
                "No eligible markets found.\n"
                "Check: MIN_VOLUME_24H, MIN_SPREAD_TO_ENTER, API connectivity."
            )
            return False

        for m in self.active_markets:
            self.order_mgr.register_market(m)

        logger.info(f"Startup complete – trading {len(self.active_markets)} market(s)")
        return True

    # ── Main run loop ──────────────────────────────────────────────────────────

    async def run(self):
        if not await self.startup():
            logger.error("Startup failed. Exiting.")
            return

        self._running = True

        # Collect YES-token IDs for the WebSocket subscription
        token_ids = self._yes_token_ids()

        self._ws_feed = OrderBookFeed(
            token_ids=token_ids,
            on_update=self._on_book_update,
            ws_url=self.config.WS_URL,
        )

        tasks = [
            asyncio.create_task(self._ws_feed.connect(),       name="ws_feed"),
            asyncio.create_task(self._market_refresh_loop(),   name="market_refresh"),
            asyncio.create_task(self._risk_monitor_loop(),     name="risk_monitor"),
            asyncio.create_task(self._merge_loop(),            name="merger"),
            asyncio.create_task(self._stats_loop(),            name="stats"),
            asyncio.create_task(self._stop_event.wait(),       name="stop_watcher"),
        ]

        logger.info("All tasks started – bot is running. Press Ctrl+C to stop.")

        try:
            done, pending = await asyncio.wait(
                tasks,
                return_when=asyncio.FIRST_EXCEPTION,
            )
            for task in done:
                if task.exception():
                    logger.critical(
                        f"Task {task.get_name()!r} crashed: {task.exception()}"
                    )
        except asyncio.CancelledError:
            pass
        finally:
            await self._shutdown(tasks)

    # ── WS Callback ────────────────────────────────────────────────────────────

    def _on_book_update(self, book):
        """
        Called from the WS feed on every order book event.
        Dispatches to the order manager without blocking the WS reader.
        """
        asyncio.create_task(self.order_mgr.handle_orderbook_update(book))

    # ── Background loops ───────────────────────────────────────────────────────

    async def _market_refresh_loop(self):
        """Re-select markets periodically and hot-swap subscriptions."""
        while self._running:
            await asyncio.sleep(self.config.MARKET_REFRESH_INTERVAL)
            logger.info("Market refresh triggered…")
            try:
                new_markets = self.selector.select_markets(force=True)
                await self._apply_market_diff(new_markets)
            except Exception as exc:
                logger.error(f"market_refresh_loop: {exc}")

    async def _risk_monitor_loop(self):
        """Poll risk state: stop-losses, daily limit, order sync."""
        while self._running:
            await asyncio.sleep(15)
            try:
                self.risk.maybe_reset_daily_pnl()

                # Re-sync positions from API
                self.risk.refresh_from_api()

                # Check stop-losses – log warnings (auto-close logic below if desired)
                triggered = self.risk.check_stop_losses()
                if triggered:
                    logger.warning(
                        f"Stop-loss triggered on {len(triggered)} token(s): "
                        + ", ".join(t[:12] for t in triggered)
                    )
                    # TODO: place aggressive market orders to close – out of scope for v1

                # Check daily loss limit
                if self.risk.daily_pnl <= -self.config.MAX_DAILY_LOSS_USD:
                    self.risk.emergency_stop("Daily loss limit exceeded")
                    await self.order_mgr.cancel_all()

                # Reconcile local order state against the API
                self.order_mgr.sync_open_orders()

            except Exception as exc:
                logger.error(f"risk_monitor_loop: {exc}")

    async def _merge_loop(self):
        """Run position merging once per MERGE_INTERVAL (default: 1 h)."""
        await asyncio.sleep(300)  # allow positions to build up first
        while self._running:
            try:
                logger.info("Merge cycle starting…")
                self.merger.batch_merge_all(self.client, self.active_markets)
            except Exception as exc:
                logger.error(f"merge_loop: {exc}")
            await asyncio.sleep(self.config.MERGE_INTERVAL)

    async def _stats_loop(self):
        """Print a periodic status summary."""
        while self._running:
            await asyncio.sleep(60)
            s = self.risk.summary()
            logger.info(
                f"[STATS]  markets={len(self.active_markets)}  "
                f"positions={s['positions_held']}  "
                f"daily_pnl=${s['daily_pnl_usd']:+.2f}  "
                f"unrealised=${s['unrealised_pnl_usd']:+.2f}  "
                f"loss_room=${s['daily_loss_remaining']:.2f}  "
                f"halt={s['emergency_stop']}"
            )

    # ── Market diff ────────────────────────────────────────────────────────────

    async def _apply_market_diff(self, new_markets):
        new_cids = {m["condition_id"] for m in new_markets}
        old_cids = {m["condition_id"] for m in self.active_markets}

        for cid in old_cids - new_cids:
            self.order_mgr.remove_market(cid)
            logger.info(f"Dropped market {cid[:14]}…")

        for m in new_markets:
            if m["condition_id"] not in old_cids:
                self.order_mgr.register_market(m)

        self.active_markets = new_markets

        # Update WS subscriptions
        if self._ws_feed:
            self._ws_feed.update_token_ids(self._yes_token_ids())

    # ── Shutdown ───────────────────────────────────────────────────────────────

    async def _shutdown(self, tasks):
        logger.info("Graceful shutdown initiated…")
        self._running = False

        if self._ws_feed:
            self._ws_feed.stop()

        await self.order_mgr.cancel_all()

        for t in tasks:
            if not t.done():
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass

        logger.info("Bot shut down cleanly. Goodbye.")

    def request_stop(self):
        self._stop_event.set()

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _yes_token_ids(self):
        return [
            m["token_ids"][0]
            for m in self.active_markets
            if m.get("token_ids")
        ]

    def _banner(self):
        mode = "DRY-RUN (no real orders)" if self.config.DRY_RUN else "⚠️  LIVE TRADING"
        logger.info("=" * 65)
        logger.info("  Polymarket Market Making Bot  v2.0")
        logger.info(f"  Mode    : {mode}")
        logger.info(f"  Markets : up to {self.config.MAX_MARKETS}")
        logger.info(f"  Order $ : ${self.config.ORDER_SIZE_USD}  max=${self.config.MAX_ORDER_SIZE_USD}")
        logger.info(f"  Spread  : {self.config.MIN_SPREAD_BPS}–{self.config.MAX_SPREAD_BPS} bps")
        logger.info(f"  Max pos : ${self.config.MAX_POSITION_USD}  daily stop=${self.config.MAX_DAILY_LOSS_USD}")
        logger.info("=" * 65)


# ── Entry point ────────────────────────────────────────────────────────────────

async def main():
    config = Config()
    configure_logging(config.LOG_LEVEL)

    bot = MarketMakingBot(config)
    loop = asyncio.get_running_loop()

    def _on_signal():
        logger.info("Signal received – requesting shutdown…")
        bot.request_stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except (NotImplementedError, OSError):
            # Windows: SIGTERM not fully supported via add_signal_handler
            pass

    await bot.run()


if __name__ == "__main__":
    asyncio.run(main())
