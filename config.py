"""
config.py – Central configuration for the Polymarket Market Making Bot.
All values can be overridden via environment variables or the .env file.
"""

import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # ── Credentials ────────────────────────────────────────────────────────────
    # Your wallet private key (hex, with or without 0x prefix)
    PRIVATE_KEY: str = os.getenv("PRIVATE_KEY", "")
    # Polymarket API key, secret, and passphrase (from account settings → API)
    API_KEY: str = os.getenv("POLYMARKET_API_KEY", "")
    API_SECRET: str = os.getenv("POLYMARKET_API_SECRET", "")
    API_PASSPHRASE: str = os.getenv("POLYMARKET_API_PASSPHRASE", "")
    # Optional: proxy wallet address (used if you have a proxy/funder wallet)
    PROXY_WALLET: str = os.getenv("PROXY_WALLET", "")

    # ── API Endpoints ──────────────────────────────────────────────────────────
    CLOB_HOST: str = "https://clob.polymarket.com"
    WS_URL: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    GAMMA_API: str = "https://gamma-api.polymarket.com"

    # ── Chain ──────────────────────────────────────────────────────────────────
    CHAIN_ID: int = 137  # Polygon mainnet
    POLYGON_RPC: str = os.getenv("POLYGON_RPC", "https://polygon-bor-rpc.publicnode.com")

    # ── Trading Mode ───────────────────────────────────────────────────────────
    # DRY_RUN=true  → logs orders but never submits them (safe default)
    # DRY_RUN=false → live trading (real money, use with caution)
    DRY_RUN: bool = os.getenv("DRY_RUN", "true").lower() == "true"

    # ── Market Selection ───────────────────────────────────────────────────────
    # Minimum 24-hour volume (USD) to consider a market
    MIN_VOLUME_24H: float = float(os.getenv("MIN_VOLUME_24H", "10000"))
    # Maximum number of markets to trade simultaneously
    MAX_MARKETS: int = int(os.getenv("MAX_MARKETS", "5"))
    # Minimum existing spread before we enter (avoids over-competitive markets)
    MIN_SPREAD_TO_ENTER: float = float(os.getenv("MIN_SPREAD_TO_ENTER", "0.015"))
    # Exclude markets where price is too close to 0 or 1 (near-certain outcome)
    PRICE_EXTREME_THRESHOLD: float = float(os.getenv("PRICE_EXTREME_THRESHOLD", "0.04"))
    # ── Spread Parameters ──────────────────────────────────────────────────────
    # All in basis points (1 bps = 0.01%).  1 bps = 0.0001 in decimal.
    # Absolute minimum spread we will ever quote
    MIN_SPREAD_BPS: int = int(os.getenv("MIN_SPREAD_BPS", "10"))    # 0.10%
    # Maximum spread we will ever quote (protects from huge inventory risk)
    MAX_SPREAD_BPS: int = int(os.getenv("MAX_SPREAD_BPS", "200"))   # 2.00%
    # Starting/target spread before vol and depth adjustments
    TARGET_SPREAD_BPS: int = int(os.getenv("TARGET_SPREAD_BPS", "40"))  # 0.40%

    # ── Order Sizing ───────────────────────────────────────────────────────────
    # Base order size in USD per side (bid + ask = 2× this per market)
    ORDER_SIZE_USD: float = float(os.getenv("ORDER_SIZE_USD", "10.0"))
    # Hard cap per single order (inventory-skew can grow orders up to this)
    MAX_ORDER_SIZE_USD: float = float(os.getenv("MAX_ORDER_SIZE_USD", "50.0"))
    # Enable lightweight edge model (microstructure-based) for BUY gating/sizing
    USE_EDGE_MODEL: bool = os.getenv("USE_EDGE_MODEL", "false").lower() == "true"
    # Minimum expected value (edge per $1 risked) required to open/increase longs
    MIN_EV_THRESHOLD: float = float(os.getenv("MIN_EV_THRESHOLD", "0.02"))
    # Fractional Kelly multiplier (0.25 = quarter-Kelly, 0.50 = half-Kelly)
    KELLY_FRACTION: float = float(os.getenv("KELLY_FRACTION", "0.25"))
    # Absolute cap on Kelly bet fraction to limit drawdowns from model error
    MAX_KELLY_BET_FRACTION: float = float(os.getenv("MAX_KELLY_BET_FRACTION", "0.20"))
    # Feature weights for true-probability estimate:
    # momentum = short-term mid-price drift, imbalance = orderbook skew
    EDGE_MOMENTUM_WEIGHT: float = float(os.getenv("EDGE_MOMENTUM_WEIGHT", "0.30"))
    EDGE_IMBALANCE_WEIGHT: float = float(os.getenv("EDGE_IMBALANCE_WEIGHT", "0.10"))

    # ── Risk Controls ──────────────────────────────────────────────────────────
    # Max dollar exposure per market (shares × price)
    MAX_POSITION_USD: float = float(os.getenv("MAX_POSITION_USD", "200.0"))
    # Halt trading for the day once cumulative loss exceeds this
    MAX_DAILY_LOSS_USD: float = float(os.getenv("MAX_DAILY_LOSS_USD", "25.0"))
    # Close a position if unrealised loss exceeds this fraction of cost basis
    STOP_LOSS_PCT: float = float(os.getenv("STOP_LOSS_PCT", "0.20"))
    # How aggressively to skew quotes when inventory is unbalanced (0–1)
    MAX_INVENTORY_SKEW: float = float(os.getenv("MAX_INVENTORY_SKEW", "0.6"))

    # ── Operational Timing ─────────────────────────────────────────────────────
    # Minimum seconds between re-quoting the same market
    ORDER_REFRESH_INTERVAL: float = float(os.getenv("ORDER_REFRESH_INTERVAL", "30.0"))
    # How often to re-scan which markets to trade (seconds)
    MARKET_REFRESH_INTERVAL: float = float(os.getenv("MARKET_REFRESH_INTERVAL", "300.0"))
    # How often to run the position merger (seconds; default = 1 hour)
    MERGE_INTERVAL: float = float(os.getenv("MERGE_INTERVAL", "3600.0"))
    # Logging verbosity: DEBUG | INFO | WARNING | ERROR
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    # ── Gas / Blockchain ───────────────────────────────────────────────────────
    GAS_LIMIT_MERGE: int = int(os.getenv("GAS_LIMIT_MERGE", "300000"))
    # Multiplier on top of the base fee (EIP-1559 style)
    GAS_PRICE_BUFFER: float = float(os.getenv("GAS_PRICE_BUFFER", "1.3"))
    # Warn if POL balance drops below this (merging requires gas)
    MIN_POL_BALANCE: float = float(os.getenv("MIN_POL_BALANCE", "1.0"))
