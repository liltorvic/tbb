# Polymarket Market Making Bot v2.1

An async market making bot for [Polymarket](https://polymarket.com) prediction markets on Polygon. Uses real-time WebSocket order books, dynamic spread calculation, inventory-aware order sizing, fill detection with P&L tracking, and on-chain position merging.

---

## How It Works

The bot runs a **buy-first, sell-inventory** strategy on neg-risk binary markets:

1. Places BUY orders at the bid price across selected markets
2. Detects fills via the CLOB trades API (polled every 15s)
3. Once shares are owned, places SELL orders at the ask price
4. Captures the bid-ask spread as profit
5. Tracks realized P&L, positions, and daily loss limits in real time

Naked SELLs (selling shares you don't own) are not supported — the proxy wallet's neg-risk exchange path doesn't allow it. The bot only sells inventory it holds.

### Strategy Scope (Important)

This is a **market-making bot** (sometimes called an MM bot), not a copy-trading bot:

- ✅ Quotes markets and captures spread
- ✅ Rebalances inventory and enforces risk limits
- ❌ Does **not** mirror or auto-follow whale wallets
- ❌ Does **not** include wallet copytrading logic

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│               main.py  (asyncio orchestrator)                │
│                                                              │
│  6 concurrent tasks:                                         │
│    ws_feed         – real-time order books via WebSocket      │
│    market_refresh  – re-scores markets every 5 min           │
│    risk_monitor    – stop-losses, daily limits every 15s     │
│    fill_monitor    – polls /data/trades for fills every 15s  │
│    merge_loop      – on-chain position merging every 1h      │
│    stats_loop      – logs P&L summary every 60s              │
├──────────┬───────────┬────────────┬────────────┬─────────────┤
│  Market  │  Order    │    Risk    │   Fill     │  Position   │
│ Selector │ Manager   │  Manager   │  Monitor   │   Merger    │
└──────┬───┴─────┬─────┴─────┬──────┴─────┬──────┴──────┬──────┘
       │         │           │            │             │
   Gamma API  WebSocket   CLOB REST   /data/trades   Polygon
   (markets)  (books)     (orders)    (fills)        (merge tx)
```

### Files

| File | Purpose |
|------|---------|
| `main.py` | Async entry point, task orchestration, fill monitor loop |
| `config.py` | All tuneable parameters via `.env` |
| `clob_client.py` | Wraps py-clob-client with retries, dry-run, trade history |
| `orderbook_ws.py` | WebSocket feed: snapshots + incremental book updates |
| `market_selector.py` | Scores and ranks markets by volume, spread, rewards |
| `order_manager.py` | Dynamic spread, inventory skew, collateral-aware sizing |
| `risk_manager.py` | Position tracking, realized P&L, daily loss, stop-loss |
| `position_merger.py` | On-chain YES+NO merging via NegRiskAdapter on Polygon |
| `setup_credentials.py` | One-time API key derivation helper |
| `diagnose_allowance.py` | Diagnostic: checks USDC balances and contract approvals |

---

## Wallet Architecture

The bot uses two wallets:

| Wallet | Type | Holds | Purpose |
|--------|------|-------|---------|
| **EOA** (from PRIVATE_KEY) | Regular wallet | POL (gas) | Signs transactions, controls proxy |
| **Proxy** (PROXY_WALLET) | Smart contract | USDC.e | Holds trading funds, places orders |

- **USDC goes to the proxy wallet** — this is your Polymarket trading balance
- **POL goes to the EOA wallet** — needed for gas (position merging)
- The Polymarket web UI only shows proxy wallet balances

---

## Prerequisites

- Python 3.10+
- A Polymarket account (polymarket.com)
- A funded Polygon wallet:
  - **USDC.e** in the proxy wallet — for placing orders
  - **POL** in the EOA wallet — for gas (position merging, ~0.5 POL is plenty)

---

## Setup

### 1. Clone and install

```bash
git clone https://github.com/liltorvic/tbb.git
cd tbb
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Create your .env file

```bash
cp .env.example .env
```

Fill in your `.env`:

```
PRIVATE_KEY=0xYOUR_PRIVATE_KEY_HERE
PROXY_WALLET=0xYOUR_PROXY_WALLET_ADDRESS
```

Leave the API key fields blank for now.

### 3. Derive API credentials

Run once:

```bash
python setup_credentials.py
```

Copy the printed `API_KEY`, `API_SECRET`, and `API_PASSPHRASE` into `.env`:

```
POLYMARKET_API_KEY=...
POLYMARKET_API_SECRET=...
POLYMARKET_API_PASSPHRASE=...
```

### 4. Run in dry-run mode first

With `DRY_RUN=true` (the default):

```bash
python main.py
```

You should see the bot select markets, connect to WebSocket, and log `[DRY-RUN]` orders.

### 5. Go live

```
DRY_RUN=false
```

**Start small.** See the recommended settings below for small accounts.

---

## Configuration

### Recommended Settings by Account Size

**$15-25 account:**
```
ORDER_SIZE_USD=2.0
MAX_ORDER_SIZE_USD=3.0
MAX_MARKETS=3
MAX_POSITION_USD=5.0
MAX_DAILY_LOSS_USD=5.0
```

**$50-100 account:**
```
ORDER_SIZE_USD=5.0
MAX_ORDER_SIZE_USD=10.0
MAX_MARKETS=5
MAX_POSITION_USD=20.0
MAX_DAILY_LOSS_USD=15.0
```

**$200+ account:**
```
ORDER_SIZE_USD=10.0
MAX_ORDER_SIZE_USD=25.0
MAX_MARKETS=5
MAX_POSITION_USD=50.0
MAX_DAILY_LOSS_USD=25.0
```

**Capital rule of thumb:**
```
MAX_MARKETS <= Total USDC / (ORDER_SIZE_USD * 3)
```

### Full Configuration Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `DRY_RUN` | `true` | `false` enables real order submission |
| `MIN_VOLUME_24H` | `10000` | Minimum 24h USD volume to trade a market |
| `MAX_MARKETS` | `5` | Markets to run concurrently |
| `MIN_SPREAD_TO_ENTER` | `0.015` | Skip markets with spread < 1.5% |
| `TARGET_SPREAD_BPS` | `40` | Starting spread (0.40%) before adjustments |
| `MIN_SPREAD_BPS` | `10` | Hard floor on quoted spread (0.10%) |
| `MAX_SPREAD_BPS` | `200` | Hard ceiling on quoted spread (2.00%) |
| `ORDER_SIZE_USD` | `10` | Dollar size per order side |
| `MAX_ORDER_SIZE_USD` | `50` | Cap after inventory skew adjustment |
| `USE_EDGE_MODEL` | `false` | Optional EV/Kelly buy gating based on microstructure signals |
| `MIN_EV_THRESHOLD` | `0.02` | Minimum edge per $1 risked before opening/increasing longs |
| `KELLY_FRACTION` | `0.25` | Fractional Kelly multiplier (quarter-Kelly default) |
| `MAX_KELLY_BET_FRACTION` | `0.20` | Hard cap on Kelly sizing as fraction of max position |
| `EDGE_MOMENTUM_WEIGHT` | `0.30` | Weight for short-term momentum in true-prob estimate |
| `EDGE_IMBALANCE_WEIGHT` | `0.10` | Weight for order-book imbalance in true-prob estimate |
| `MAX_POSITION_USD` | `200` | Max dollar exposure per market |
| `MAX_DAILY_LOSS_USD` | `25` | Halt trading after this daily loss |
| `STOP_LOSS_PCT` | `0.20` | 20% loss on a position triggers warning |
| `MAX_INVENTORY_SKEW` | `0.6` | Max size adjustment for inventory skew |
| `ORDER_REFRESH_INTERVAL` | `30` | Seconds between re-quotes per market |
| `MARKET_REFRESH_INTERVAL` | `300` | Seconds between market re-scans |
| `MERGE_INTERVAL` | `3600` | Seconds between on-chain merge cycles |

---

## How the Spread Works

```
spread = TARGET_SPREAD_BPS
       + volatility_premium   (recent price std-dev x 3)
       + depth_imbalance      (thin-side adjustment)
       + competition_floor    (respect existing book spread x 0.85)

clamped to [MIN_SPREAD_BPS, MAX_SPREAD_BPS]
```

Quotes are placed at:
```
bid = mid - spread/2
ask = mid + spread/2
```

### Collateral-Aware Sizing

Order sizes account for actual collateral cost on Polymarket:

- **BUY YES at price P** costs `P` per share
- **SELL YES at price P** (with inventory) costs nothing — you already own the shares

On low-probability markets (e.g. 8 cents), this distinction matters significantly. A $2 BUY order at $0.08 buys ~25 shares. Selling those 25 shares back at $0.09 captures the spread.

### Inventory Skew

When holding a net long position, bids shrink and asks grow to encourage selling and rebalance toward neutral. The reverse applies for net short.

### EV + Fractional Kelly Gate (BUY side)

For opening/increasing long inventory, the bot can now apply:

- **Expected value filter**: skip BUY quotes when estimated edge is below `MIN_EV_THRESHOLD`
- **Fractional Kelly sizing**: scale BUY notional using capped quarter-Kelly style sizing

The true-probability estimate is intentionally lightweight and derived from:

- recent mid-price momentum
- live order-book depth imbalance

Sells used to unwind inventory are still allowed even when BUY EV is below threshold.

---

## Fill Detection and P&L

The bot polls the CLOB `/data/trades` API every 15 seconds to detect fills:

- Each fill is logged: `FILL DETECTED  SELL  29.70 sh @ 0.0800  [market name]`
- Realized P&L is computed: `sell_price - avg_cost` per share
- Daily P&L is tracked and triggers halt if `MAX_DAILY_LOSS_USD` is breached
- Position state is synced from the Polymarket API every 15s (source of truth)

The stats loop shows a summary every 60 seconds:
```
[STATS]  markets=3  positions=2  daily_pnl=$+0.15  realised=$+0.22  unrealised=$+0.00  fills=5  loss_room=$4.85  halt=False
```

---

## Position Merging

When you hold both YES and NO shares in the same market:

```
1 YES + 1 NO = 1 USDC.e  (at any time before resolution)
```

The `PositionMerger` calls `NegRiskAdapter.mergePositions()` on Polygon every `MERGE_INTERVAL` (default: 1 hour). Only fires if you hold shares on both sides.

**Requires:** POL in the EOA wallet for gas (~$0.01-0.05 per merge).

---

## Risk Controls

| Control | Trigger | Action |
|---------|---------|--------|
| Daily loss limit | `daily_pnl <= -MAX_DAILY_LOSS_USD` | Cancel all orders, halt trading |
| Stop-loss | Position loss >= `STOP_LOSS_PCT` | Log warning |
| Position cap | Position value >= `MAX_POSITION_USD` | Block new orders on that market |
| Emergency stop | Manual / programmatic | Block all new orders |
| Inventory-only sells | No shares held | Skip SELL order placement |

---

## Monitoring

```bash
tail -f bot.log
```

| Log Pattern | Meaning |
|-------------|---------|
| `ORDER PLACED  BUY  24.78 sh @ 0.0807` | Live order placed |
| `FILL DETECTED  SELL  29.70 sh @ 0.0800` | Order was filled |
| `Fill SELL  29.70sh @ 0.0800  realised_pnl=$+0.06` | P&L from that fill |
| `[STATS]  daily_pnl=$+0.15  fills=5` | Periodic summary |
| `[FILLS]  2 new fill(s) processed` | Fill batch detected |
| `Skipping SELL – no inventory` | Waiting for BUY fills first |
| `Stop-loss triggered` | Position hit loss threshold |
| `EMERGENCY STOP ACTIVATED` | Trading halted |

---

## Diagnostics

If orders fail with "not enough balance / allowance":

```bash
python diagnose_allowance.py
```

This shows USDC balances and ERC20 approvals for both wallets across all Polymarket exchange contracts.

---

## Important Notes

1. **Proxy Wallet Required** — The bot uses Polymarket's proxy wallet system (`signature_type=1`). Set `PROXY_WALLET` in `.env` to your Polymarket proxy address.

2. **No Naked Sells** — Selling shares you don't own fails on neg-risk markets via proxy wallets. The bot only places SELL orders when it holds inventory from prior BUY fills.

3. **API Rate Limits** — The CLOB API rate-limits requests. The client has built-in exponential backoff and handles 429 responses automatically.

4. **Contract Addresses** — Verify `NEG_RISK_ADAPTER` and `CTF_EXCHANGE` in `position_merger.py` against the official [Polymarket docs](https://docs.polymarket.com/#contracts) before deploying.

5. **Two Wallets** — USDC goes to the proxy wallet (for trading). POL goes to the EOA wallet (for gas). Don't mix them up — sending POL to the proxy wallet will fail (it's a smart contract).
