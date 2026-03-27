# Polymarket Market Making Bot — Full Session Summary & Continuation Prompt

---

## PART 1: COMPLETE SESSION SUMMARY

### Project Goal
Build a production-grade, fully automated market making bot for Polymarket from scratch in Python. The bot provides liquidity on Polymarket's CLOB (Central Limit Order Book) by placing bid and ask limit orders on binary prediction markets, capturing the spread, and earning liquidity rewards.

The reference codebase used as a starting point is `warproxxx/poly-maker` on GitHub — an open-source bot that the original author warns is outdated. The goal was to modernise it substantially.

---

### Tech Stack Decided Upon
- **Language:** Python 3.13
- **OS:** Parrot OS (Debian-based security distro)
- **Key Libraries:**
  - `py-clob-client` v0.34.6 — official Polymarket SDK
  - `websockets` — async WebSocket order book feed
  - `web3.py` v6+ — Polygon blockchain interaction for position merging
  - `httpx` — HTTP client (also used internally by py-clob-client)
  - `requests` — used in market selector for Gamma API calls
  - `python-dotenv` — environment variable management
- **Blockchain:** Polygon mainnet (Chain ID 137)
- **Collateral:** USDC.e (6 decimals)
- **Gas token:** POL (formerly MATIC)
- **Wallet:** MetaMask EOA (Externally Owned Account)

---

### Architecture Built (10 Files)

```
polymarket-mm-bot/
├── config.py              # All tunable parameters via .env
├── clob_client.py         # Wraps py-clob-client SDK with retries + dry-run
├── orderbook_ws.py        # WebSocket feed: snapshots + incremental updates
├── market_selector.py     # Scores and ranks markets to trade
├── order_manager.py       # Dynamic spread, inventory skew, order lifecycle
├── risk_manager.py        # Position tracking, daily loss, stop-loss
├── position_merger.py     # Gas-efficient position merging on Polygon
├── main.py                # Async entry point + task orchestration
├── setup_credentials.py   # One-time API key derivation helper
├── .env.example           # Environment variable template
├── requirements.txt       # Python dependencies
└── README.md              # Full documentation
```

---

### Key Features Implemented

**Market Selection (market_selector.py)**
- Fetches markets from Gamma API (`https://gamma-api.polymarket.com/markets`)
- Scores markets on: 24h volume, spread width, price extremity, competition, liquidity rewards
- Filters out: closed/resolved markets, non-orderbook markets, extreme prices (near 0 or 1), spreads too tight to be profitable
- Returns top N markets ranked by score (configurable via MAX_MARKETS)

**WebSocket Order Book Feed (orderbook_ws.py)**
- Connects to `wss://ws-subscriptions-clob.polymarket.com/ws/market`
- Handles full book snapshots and incremental price-level changes
- Auto-reconnects with exponential backoff (3s → 6s → 12s → ... → 120s max)
- OrderBook dataclass with derived properties: best_bid, best_ask, mid_price, spread_bps, depth_imbalance

**Order Management (order_manager.py)**
- Dynamic spread calculation:
  - Base: TARGET_SPREAD_BPS (default 40 bps)
  - + Volatility premium (recent std-dev × 3)
  - + Depth imbalance adjustment
  - + Competition floor (respect existing book spread)
  - Clamped between MIN_SPREAD_BPS and MAX_SPREAD_BPS
- Inventory skew: if over-long YES shares → shrink bids, grow asks
- Stale order cancellation: cancel if price moves > 0.5% or order > 10 minutes old
- Rate limiting: minimum ORDER_REFRESH_INTERVAL seconds between re-quotes per market

**Risk Management (risk_manager.py)**
- Tracks per-token net position (shares + avg cost basis)
- Tracks daily P&L with UTC midnight reset
- Hard limits: MAX_POSITION_USD per market, MAX_DAILY_LOSS_USD per day
- Stop-loss: triggers warning when unrealised loss > STOP_LOSS_PCT of cost basis
- Emergency stop: halts all new orders immediately

**Position Merger (position_merger.py)**
- Calls `NegRiskAdapter.mergePositions()` on Polygon mainnet
- Converts offsetting YES + NO shares back to USDC.e (1 YES + 1 NO = 1 USDC)
- Runs every MERGE_INTERVAL seconds (default: 1 hour)
- Gas-efficient: EIP-1559 aware pricing with GAS_PRICE_BUFFER multiplier
- Contract address: `0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296` (verify against docs)

**Main Loop (main.py)**
- 5 concurrent asyncio tasks: ws_feed, market_refresh, risk_monitor, merge_loop, stats_loop
- Graceful Ctrl+C shutdown: cancels all open orders first
- DRY_RUN toggle: logs orders without submitting when true

---

### Bugs Found and Fixed During First Run

#### Bug 1: web3.py v6 middleware rename
**File:** `position_merger.py`
**Error:** `ImportError: cannot import name 'geth_poa_middleware'`
**Fix:** 
```python
# Old (broken)
from web3.middleware import geth_poa_middleware
self.w3.middleware_onion.inject(geth_poa_middleware, layer=0)

# New (correct for web3.py v6+)
from web3.middleware import ExtraDataToPOAMiddleware
self.w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
```

#### Bug 2: SDK method renamed
**File:** `clob_client.py`
**Error:** `'ClobClient' object has no attribute 'get_positions'` and later `'get_portfolio'`
**Root cause:** Neither method exists in py-clob-client v0.34.6. Confirmed by running:
```python
[m for m in dir(ClobClient) if 'pos' in m.lower() or 'port' in m.lower()]
# Returns: [] — no positions method at all
```
**Fix:** Replaced with Gamma API call using `httpx`:
```python
def get_positions(self) -> List[Dict]:
    wallet = self.config.PROXY_WALLET
    url = f"https://gamma-api.polymarket.com/positions?user={wallet}"
    resp = httpx.get(url, timeout=10)
    resp.raise_for_status()
    return resp.json()
```

#### Bug 3: get_balance_allowance wrong arguments
**File:** `clob_client.py`
**Error:** `ClobClient.get_balance_allowance() got an unexpected keyword argument 'asset_type'`
**Fix:**
```python
# Old (broken)
resp = self._clob.get_balance_allowance(asset_type=2)

# New (correct)
from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
resp = self._clob.get_balance_allowance(
    params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
)
```
Also added `AssetType` and `BalanceAllowanceParams` to the existing `clob_types` import block.

#### Bug 4: WebSocket wrong URL/hostname
**File:** `config.py` and `orderbook_ws.py`
**Error:** `[Errno -5] No address associated with hostname`
**Root cause:** Wrong subdomain — used dot-separated `ws-subscriptions.clob.polymarket.com` instead of hyphen-separated `ws-subscriptions-clob.polymarket.com`
**Confirmed via dig:** The dot-separated version has no DNS A record; the hyphen version resolves correctly.
**Fix:**
```python
# Old (broken — no DNS record)
WS_URL = "wss://ws-subscriptions.clob.polymarket.com/ws/"

# New (correct)
WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
```
Note: The path must be `/ws/market` for the market channel, not just `/ws/`.

#### Bug 5: DNS resolution on Parrot OS
**Issue:** Even with correct hostname, DNS failed on Parrot OS
**Cause:** Parrot uses systemd-resolved which intercepts DNS
**Fix:** 
```bash
echo "nameserver 8.8.8.8" | sudo tee /etc/resolv.conf
sudo chattr +i /etc/resolv.conf  # lock so OS can't overwrite
```

---

### Current Bot Status After All Fixes

The bot **successfully:**
- Connects to Polymarket CLOB client (DRY-RUN mode) ✅
- Fetches 600 markets from Gamma API ✅
- Scores and selects top 5 markets ✅
- Registers markets with order manager ✅
- Connects WebSocket feed (`WS connected. Subscribing…`) ✅
- Runs stats loop every 60 seconds ✅

The bot **still has issues:**
- `get_positions` returns empty (PROXY_WALLET needed) — partially fixed
- API credentials not yet set up (401 Unauthorized on authenticated endpoints)
- Position merging disabled (Polygon RPC connectivity issue — likely needs a better RPC endpoint)

---

### Wallet & Credentials Setup

**Wallet structure:**
- EOA (MetaMask wallet): `0x44E1D8c947162526bD5e896F4100fc82dF7e3FcF`
- Proxy wallet (Polymarket-generated Gnosis Safe): `0x10F5e8E0a480BF88C398973d207131f340F8168a`
- Private key: belongs to EOA `0x44E1...` (confirmed by Signer.address())

**On-chain activity confirmed:**
- Proxy wallet has been active (multiple Exec Transactions, USDC.e transfers, trades)
- NFT ERC-1155 position tokens transferred to/from Neg Risk CTF Exchange
- Wallet is clearly registered and active on Polymarket

**Credential derivation — UNRESOLVED:**
All programmatic approaches to derive API credentials via the SDK returned:
```
PolyApiException[status_code=401, error_message={'error': 'Invalid L1 Request headers'}]
```

Approaches tried and failed:
1. `signature_type=0` (EOA mode) with EOA address in headers
2. `signature_type=1` (proxy mode) with proxy address in headers
3. `signature_type=1` with proxy as funder
4. Manual signing: proxy address in both ClobAuth struct AND header
5. Manual signing: EOA in struct, proxy in header
6. Nonce=0 and nonce=1
7. Both `/auth/api-key` (POST) and `/auth/derive-api-key` (GET)

All signing mechanics confirmed working (sign_clob_auth_message produces valid signatures).

**Current recommended solution:**
Get API credentials directly from the Polymarket website UI:
- Go to polymarket.com → Settings → API Keys
- Generate key there, copy `API_KEY`, `API_SECRET`, `API_PASSPHRASE` into `.env`

---

### SDK Internals Discovered (py-clob-client v0.34.6)

**Available credential methods on ClobClient:**
`assert_builder_auth`, `assert_level_1_auth`, `assert_level_2_auth`, `can_builder_auth`, `create_api_key`, `create_or_derive_api_creds`, `create_readonly_api_key`, `delete_api_key`, `delete_readonly_api_key`, `derive_api_key`, `get_api_keys`, `get_readonly_api_keys`, `set_api_creds`, `validate_readonly_api_key`

**Available trade/balance methods:**
`create_and_post_order`, `get_balance_allowance`, `get_builder_trades`, `get_last_trade_price`, `get_last_trades_prices`, `get_market_trades_events`, `get_trades`, `post_heartbeat`, `post_order`, `post_orders`, `update_balance_allowance`

**Notable: NO `get_positions` or `get_portfolio` method exists.**

**Client modes:**
- L0: host only (read-only public endpoints)
- L1: host + key (authenticated, can create API keys)
- L2: host + key + creds (full trading access)

**Signing chain:**
`ClobAuth EIP712 struct` → `signable_bytes()` → `keccak hash` → `signer.sign()` → `POLY_SIGNATURE header`

**API Endpoints:**
- CLOB REST: `https://clob.polymarket.com`
- Gamma API: `https://gamma-api.polymarket.com`
- WebSocket: `wss://ws-subscriptions-clob.polymarket.com/ws/market` (market channel)
- WebSocket: `wss://ws-subscriptions-clob.polymarket.com/ws/user` (user channel)

---

### .env Configuration (Current State)

```
PRIVATE_KEY=0x44E1...        ← EOA private key (filled in)
PROXY_WALLET=0x10F5...       ← Polymarket proxy (filled in)
POLYMARKET_API_KEY=          ← EMPTY — needs to be filled from website
POLYMARKET_API_SECRET=       ← EMPTY
POLYMARKET_API_PASSPHRASE=   ← EMPTY
DRY_RUN=true                 ← Safe mode active
POLYGON_RPC=https://polygon-rpc.com  ← Failing to connect (RPC issue)
```

---

## PART 2: PROMPT FOR CLAUDE (HIGHER VERSION)

---

```
You are helping me finish debugging and launching a Polymarket market making bot 
built in Python. I am uploading all the current script files with this prompt. 
Here is the complete context of what has been built and what still needs fixing.

═══════════════════════════════════════════════════════════
PROJECT OVERVIEW
═══════════════════════════════════════════════════════════

A 10-file Python market making bot for Polymarket that:
- Selects markets based on volume, spread, rewards scoring
- Maintains real-time order books via WebSocket
- Places dynamic bid/ask limit orders with inventory skew
- Enforces risk controls (daily loss limit, position caps, stop-loss)
- Merges offsetting positions on Polygon to recover USDC

Environment:
- OS: Parrot OS (Debian-based)
- Python 3.13
- py-clob-client v0.34.6
- web3.py v6+
- All files are in ~/Documents/polymarket-mm-bot/

═══════════════════════════════════════════════════════════
WALLET DETAILS (for context only)
═══════════════════════════════════════════════════════════

- EOA (MetaMask): 0x44E1D8c947162526bD5e896F4100fc82dF7e3FcF
- Proxy wallet (Polymarket Gnosis Safe): 0x10F5e8E0a480BF88C398973d207131f340F8168a
- Chain: Polygon mainnet (Chain ID 137)
- The private key in .env belongs to the EOA 0x44E1

═══════════════════════════════════════════════════════════
BUGS ALREADY FIXED (do not re-introduce these)
═══════════════════════════════════════════════════════════

1. position_merger.py:
   FIXED: geth_poa_middleware → ExtraDataToPOAMiddleware (web3.py v6)

2. clob_client.py:
   FIXED: get_balance_allowance now uses:
   from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
   resp = self._clob.get_balance_allowance(
       params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
   )

3. clob_client.py:
   FIXED: get_positions() now uses Gamma API via httpx:
   url = f"https://gamma-api.polymarket.com/positions?user={wallet}"
   resp = httpx.get(url, timeout=10)
   import httpx is added at the top of clob_client.py

4. config.py + orderbook_ws.py:
   FIXED: WebSocket URL corrected to:
   wss://ws-subscriptions-clob.polymarket.com/ws/market
   (was wrong: wss://ws-subscriptions.clob.polymarket.com/ws/)

═══════════════════════════════════════════════════════════
CURRENT BOT STATUS
═══════════════════════════════════════════════════════════

WORKING:
✅ CLOB client initialises in DRY-RUN mode
✅ Gamma API fetches 600 markets successfully
✅ Market scoring selects top 5 markets correctly
✅ WebSocket connects and subscribes successfully
✅ Stats loop runs every 60 seconds

NOT WORKING:
❌ API credentials — 401 Unauthorized on all authenticated endpoints
❌ Position merger — "Cannot connect to Polygon RPC"
❌ get_positions — returns empty (needs PROXY_WALLET + valid API key)

═══════════════════════════════════════════════════════════
ISSUE 1: API CREDENTIALS (HIGHEST PRIORITY)
═══════════════════════════════════════════════════════════

The .env file is missing:
  POLYMARKET_API_KEY=
  POLYMARKET_API_SECRET=
  POLYMARKET_API_PASSPHRASE=

All programmatic SDK approaches to derive credentials have failed with:
  PolyApiException[status_code=401, error_message={'error': 'Invalid L1 Request headers'}]

Approaches that FAILED:
- signature_type=0 with EOA in headers
- signature_type=1 with proxy as funder
- Manual ClobAuth struct with proxy address + EOA signing
- Manual ClobAuth struct with EOA address + proxy in header
- Nonce 0 and nonce 1
- Both /auth/api-key (POST) and /auth/derive-api-key (GET)

The signing itself works (sign_clob_auth_message produces valid signatures).
The wallet is confirmed active on-chain with recent trades.
No geo-blocking detected.

CURRENT PLAN: Get credentials from Polymarket website UI at:
  polymarket.com → Settings → API Keys

Once obtained, they go into .env as:
  POLYMARKET_API_KEY=xxxx
  POLYMARKET_API_SECRET=xxxx
  POLYMARKET_API_PASSPHRASE=xxxx

YOUR TASK FOR ISSUE 1:
If the user has obtained credentials from the website, help them verify 
the credentials work by testing authenticated endpoints. If they still 
haven't obtained credentials, investigate whether there is a working 
programmatic approach using py-clob-client v0.34.6 that we haven't tried.

═══════════════════════════════════════════════════════════
ISSUE 2: POLYGON RPC CONNECTION
═══════════════════════════════════════════════════════════

Error: "Cannot connect to Polygon RPC – merging disabled"
Current RPC in .env: https://polygon-rpc.com

This is causing position_merger.py to disable itself on startup.
The public polygon-rpc.com endpoint is often unreliable.

YOUR TASK FOR ISSUE 2:
Replace the RPC URL with a more reliable option. Recommend one of:
- https://polygon-mainnet.g.alchemy.com/v2/YOUR_KEY (Alchemy — free tier)
- https://rpc.ankr.com/polygon (Ankr — free, no key needed)
- https://polygon.llamarpc.com (LlamaNodes — free, no key needed)

Update POLYGON_RPC in .env and verify connection works with:
  python -c "from web3 import Web3; w3 = Web3(Web3.HTTPProvider('RPC_URL')); print(w3.is_connected(), w3.eth.block_number)"

═══════════════════════════════════════════════════════════
ISSUE 3: DRY-RUN QUOTES NOT FLOWING
═══════════════════════════════════════════════════════════

Even though the WebSocket connects successfully, we never see:
  [DRY-RUN] BUY  xx.xx sh @ 0.xxxx  [market name]
  [DRY-RUN] SELL xx.xx sh @ 0.xxxx  [market name]

This means order_manager.handle_orderbook_update() is either:
a) Not receiving book updates from the WebSocket callback
b) Receiving them but mid_price is None (empty book)
c) The ORDER_REFRESH_INTERVAL throttle is blocking

YOUR TASK FOR ISSUE 3:
Add temporary DEBUG logging to orderbook_ws.py _dispatch() method 
to confirm messages are being received. Also add a log line in 
order_manager._refresh_quotes() to show when it's called and 
what mid_price it sees. This will pinpoint where quotes are being dropped.

═══════════════════════════════════════════════════════════
ISSUE 4: SHUTDOWN IS SLOW / REQUIRES MULTIPLE CTRL+C
═══════════════════════════════════════════════════════════

When pressing Ctrl+C, the bot doesn't shut down cleanly — it takes 
multiple Ctrl+C presses and sometimes core dumps. This is because 
asyncio tasks aren't being cancelled properly when the stop event fires.

YOUR TASK FOR ISSUE 4:
Review main.py _shutdown() method and ensure all tasks are properly 
cancelled with asyncio.gather(*tasks, return_exceptions=True) pattern 
instead of the current loop.

═══════════════════════════════════════════════════════════
SDK FACTS DISCOVERED (use these, don't re-investigate)
═══════════════════════════════════════════════════════════

py-clob-client v0.34.6 facts:
- NO get_positions() or get_portfolio() method exists on ClobClient
- get_balance_allowance() requires BalanceAllowanceParams object
- Client modes: L0 (no key), L1 (key only), L2 (key + creds)
- L1 mode is reached when key= is passed (signer is not None)
- Signing: ClobAuth EIP712 struct → keccak → signer.sign()
- Signing lives in: py_clob_client.signing.eip712.sign_clob_auth_message
- ClobAuth model: address, timestamp, nonce, message fields
- Available trade methods: get_trades, get_last_trade_price, post_order, etc.
- WebSocket URL: wss://ws-subscriptions-clob.polymarket.com/ws/market
- Gamma API: https://gamma-api.polymarket.com (use for positions, market metadata)
- CLOB API: https://clob.polymarket.com (use for orders, auth)

═══════════════════════════════════════════════════════════
WHAT SUCCESS LOOKS LIKE
═══════════════════════════════════════════════════════════

A fully working dry-run session should show these log lines:
  INFO  clob_client    PolymarketClient ready  mode=DRY-RUN
  INFO  __main__       Wallet balance  USDC=$XX.XX  POL=X.XXXX
  INFO  market_selector  Selected 5 market(s) to trade
  INFO  orderbook_ws   WS connected. Subscribing…
  INFO  order_manager  [Market name]  mid=0.XXXX  bid=0.XXXX  ask=0.XXXX  spread=XXbps
  INFO  clob_client    [DRY-RUN] BUY   XX.XX sh @ 0.XXXX  [market]
  INFO  clob_client    [DRY-RUN] SELL  XX.XX sh @ 0.XXXX  [market]
  INFO  __main__       [STATS]  markets=5  positions=0  daily_pnl=$+0.00

Once dry-run is confirmed working for 24 hours with clean quotes flowing,
the next step is flipping DRY_RUN=false and starting with small sizes:
  ORDER_SIZE_USD=5
  MAX_POSITION_USD=50
  MAX_DAILY_LOSS_USD=10

═══════════════════════════════════════════════════════════
PLEASE START BY
═══════════════════════════════════════════════════════════

1. Review all uploaded script files carefully
2. Ask the user if they have obtained API credentials from the website yet
3. If yes: help test them and fix the authenticated endpoint errors
4. If no: help them obtain credentials (either via website or a working SDK approach)
5. Then fix Issue 2 (Polygon RPC) so position merger initialises
6. Then debug Issue 3 (quotes not flowing) to confirm the bot is quoting
7. Then fix Issue 4 (clean shutdown)
8. Finally do a full end-to-end dry-run verification before considering live trading

Do NOT suggest going live until all four issues are resolved and 
dry-run quotes are flowing cleanly for at least a few minutes.
```
