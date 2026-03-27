#!/usr/bin/env python3
"""
manual_derive.py – Manually derive API keys using the owner key but
with the proxy wallet address in the headers.
"""

import os
import sys
import time
import requests
from dotenv import load_dotenv

load_dotenv()

# ---- 1. Import the SDK components we need ----
try:
    # Try common locations
    from py_clob_client.signer import Signer
    from py_clob_client.headers.headers import sign_clob_auth_message
    from py_clob_client.constants import POLYGON
except ImportError:
    # Fallback: maybe it's in a different module
    from py_clob_client.signing import Signer
    from py_clob_client.headers import sign_clob_auth_message

# ---- 2. Read environment ----
pk = os.getenv("PRIVATE_KEY", "").strip()
proxy = os.getenv("PROXY_WALLET", "").strip()
host = os.getenv("CLOB_HOST", "https://clob.polymarket.com")

if not pk or not proxy:
    print("ERROR: PRIVATE_KEY and PROXY_WALLET must be set in .env")
    sys.exit(1)

if not pk.startswith("0x"):
    pk = "0x" + pk
if not proxy.startswith("0x"):
    proxy = "0x" + proxy

print(f"Owner address (from key): {Signer(pk, POLYGON).address()}")
print(f"Proxy wallet address:      {proxy}")

# ---- 3. Build the L1 headers manually ----
timestamp = int(time.time())
nonce = 0

# The signature must be generated using the owner's private key,
# but the address in the header must be the PROXY wallet.
signer = Signer(pk, POLYGON)
signature = sign_clob_auth_message(signer, timestamp, nonce)

headers = {
    "POLY_ADDRESS": proxy,          # <-- critical: use proxy address
    "POLY_SIGNATURE": signature,
    "POLY_TIMESTAMP": str(timestamp),
    "POLY_NONCE": str(nonce),
    "Accept": "application/json, text/plain, */*",
}

# ---- 4. Send the request to derive API key ----
url = f"{host}/auth/derive-api-key"
print(f"\nSending request to {url}...")
try:
    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    data = resp.json()
    print("\n✅ Success! API credentials:")
    print(f"API_KEY        = {data.get('apiKey')}")
    print(f"API_SECRET     = {data.get('secret')}")
    print(f"API_PASSPHRASE = {data.get('passphrase')}")
except Exception as e:
    print(f"\n❌ Request failed: {e}")
    if hasattr(e, 'response') and e.response is not None:
        print("Response body:", e.response.text)