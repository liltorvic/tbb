# Polymarket Market Making Bot  v2.0

A production-grade, fully async market making bot for Polymarket using the
current CLOB REST + WebSocket APIs, dynamic spread logic, and on-chain
position merging via the NegRiskAdapter contract.

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│              main.py  (asyncio orchestrator)            │
├──────────────┬──────────────┬──────────────┬────────────┤
│ MarketSelector│ OrderManager │ RiskManager  │  Merger    │
│ (ranks mkts) │ (quotes)     │ (limits/stop)│  (on-chain)│
└──────┬───────┴──────┬───────┴──────────────┴────────────┘
       │              │
┌──────▼──────┐ ┌──────▼──────────────┐
│ Gamma API   │ │ WebSocket Feed      │
│ CLOB REST   │ │ (real-time books)   │
└─────────────┘ └─────────────────────┘
```

### Files

| File | Purpose |
|------|---------|
| `config.py` | All tuneable parameters (via .env) |
| `clob_client.py` | Wraps py-clob-client with retries + dry-run |
| `orderbook_ws.py` | WebSocket feed: snapshots + incremental updates |
| `market_selector.py` | Scores and ranks markets to trade |
| `order_manager.py` | Dynamic spread, inventory skew, order lifecycle |
| `risk_manager.py` | Position tracking, daily loss, stop-loss |
| `position_merger.py` | Gas-efficient position merging on Polygon |
| `main.py` | Async entry point + task orchestration |
| `setup_credentials.py` | One-time API key derivation helper |

---

## Prerequisites

- Python 3.10+
- A Polymarket account (polymarket.com)
- A funded Polygon wallet:
  - **USDC.e** – for placing orders
  - **POL** (formerly MATIC) – for gas (position merging)

---

## Setup  (step by step)

### 1. Clone and install

```bash
git clone <your-repo>
cd polymarket-mm-bot
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Create your .env file

```bash
cp .env.example .env
```

Open `.env` in a text editor and fill in:

```
PRIVATE_KEY=0xYOUR_PRIVATE_KEY_HERE
```

Leave the API key fields blank for now.

### 3. Derive your Polymarket API credentials

You only need to do this **once**:

```bash
python setup_credentials.py
```

Copy the printed `API_KEY`, `API_SECRET`, and `API_PASSPHRASE` into your `.env`.

> **Tip:** If this fails, sign in to polymarket.com with your wallet first and
> accept the terms of service. The on-chain signature only works after you've
> interacted with the platform at least once.

### 4. Run in dry-run mode (recommended first step)

Ensure `DRY_RUN=true` in your `.env`, then:

```bash
python main.py
```

You should see the bot:
- Connect to the Gamma API and select markets
- Open a WebSocket connection
- Log `[DRY-RUN] BUY / SELL` messages as it would quote

Monitor `bot.log` for the full output.

### 5. Switch to live trading

When you're satisfied with the dry-run behaviour:

```
DRY_RUN=false
```

**Start small.** Set `ORDER_SIZE_USD=5` and `MAX_POSITION_USD=50` for your
first live session.

---

## Configuration Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `DRY_RUN` | `true` | `false` enables real order submission |
| `MIN_VOLUME_24H` | `10000` | Minimum USD volume to trade a market |
| `MAX_MARKETS` | `5` | Markets to run concurrently |
| `MIN_SPREAD_TO_ENTER` | `0.015` | Skip markets with spread < 1.5% |
| `TARGET_SPREAD_BPS` | `40` | Starting spread before adjustments |
| `MIN_SPREAD_BPS` | `10` | Hard floor on quoted spread |
| `MAX_SPREAD_BPS` | `200` | Hard ceiling on quoted spread |
| `ORDER_SIZE_USD` | `10` | Dollar size per bid/ask |
| `MAX_ORDER_SIZE_USD` | `50` | Cap after inventory skew |
| `MAX_POSITION_USD` | `200` | Max dollar exposure per market |
| `MAX_DAILY_LOSS_USD` | `25` | Halt trading after this daily loss |
| `STOP_LOSS_PCT` | `0.20` | 20% loss on a position triggers stop |
| `MAX_INVENTORY_SKEW` | `0.6` | 60% max size adjustment for skew |
| `ORDER_REFRESH_INTERVAL` | `30` | Seconds between re-quotes |
| `MARKET_REFRESH_INTERVAL` | `300` | Seconds between market re-scans |
| `MERGE_INTERVAL` | `3600` | Seconds between on-chain merge cycles |

---

## How the Spread Works

```
spread = TARGET_SPREAD_BPS
       + volatility_premium   (recent std-dev × 3)
       + depth_imbalance       (thin-side adjustment)
       + competition_floor     (respect existing book spread)

clamped to [MIN_SPREAD_BPS, MAX_SPREAD_BPS]
```

Then the bot quotes:
```
bid = mid − spread/2
ask = mid + spread/2
```

Sizes are adjusted by **inventory skew**: if you hold a net long position,
your bids shrink and asks grow, pushing you back to neutral over time.

---

## Position Merging

When you hold both YES and NO shares in the same market they can be combined
back into USDC at no loss:

```
1 YES share + 1 NO share = 1 USDC.e  (at any time before resolution)
```

The `PositionMerger` calls `NegRiskAdapter.mergePositions()` on Polygon.
This runs every `MERGE_INTERVAL` (default: 1 hour) and only fires if you
have ≥ 1 share on both sides of a market.

**Gas cost:** ~$0.01–0.05 per merge at normal Polygon fees.

---

## Risk Controls

| Control | Trigger | Action |
|---------|---------|--------|
| Daily loss limit | `daily_pnl ≤ -MAX_DAILY_LOSS_USD` | Cancel all orders, halt |
| Stop-loss | Position loss ≥ `STOP_LOSS_PCT` | Log warning (manual close for v1) |
| Position cap | Position value ≥ `MAX_POSITION_USD` | Block new orders on that side |
| Emergency stop | Manual / programmatic | Block all new orders |

---

## Monitoring

```bash
tail -f bot.log
```

Key log patterns:

| Pattern | Meaning |
|---------|---------|
| `[STATS]  markets=5  positions=3  daily_pnl=+$1.23` | Hourly summary |
| `[DRY-RUN] BUY  10.00 sh @ 0.6340` | Dry-run quote |
| `ORDER PLACED  BUY   10.00 sh @ 0.6340` | Live order |
| `Stop-loss triggered` | Position hit 20% loss |
| `EMERGENCY STOP ACTIVATED` | Trading halted |

---

## Important Notes

1. **API Rate Limits** – The CLOB API rate-limits requests. The client has
   built-in exponential back-off and will wait on 429 responses.

2. **IP Restrictions** – Polymarket may block datacenter IPs. If running on
   a VPS, consider a residential proxy or run from home / cloud with a good
   IP reputation.

3. **Contract Addresses** – Always verify `NEG_RISK_ADAPTER` and
   `CTF_EXCHANGE` in `position_merger.py` against the official
   [Polymarket docs](https://docs.polymarket.com/#contracts) before deploying.
   These addresses are correct as of mid-2025 but may change.

4. **USDC Approval** – Before your first live order, the CLOB SDK will prompt
   you to approve USDC spending. This is handled automatically by `py-clob-client`.

5. **Start Small** – Use $5–10 order sizes and a $50 position cap for your
   first live session. Observe for several hours before scaling up.

---

## Roadmap (v3 ideas)

- [ ] Automated stop-loss position closing (market order on breach)
- [ ] Liquidity reward contract integration for reward-adjusted spreads
- [ ] WebSocket subscription hot-swap without reconnect
- [ ] Backtesting harness against historical order book snapshots
- [ ] Prometheus metrics endpoint + Grafana dashboard
- [ ] AI-powered directional bias overlay (LLM sentiment on market question)
