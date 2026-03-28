#!/usr/bin/env python3
"""
manual_derive.py – Create or derive Polymarket CLOB API keys.

Tries POST /auth/api-key (create) first, falls back to GET /auth/derive-api-key.
This mirrors the SDK's create_or_derive_api_creds() logic.
"""

import os
import sys
import time
import requests
from dotenv import load_dotenv

load_dotenv()

# ---- 1. Import the SDK components we need ----
try:
    from py_clob_client.signer import Signer
    from py_clob_client.headers.headers import sign_clob_auth_message
    from py_clob_client.constants import POLYGON
except ImportError:
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

signer = Signer(pk, POLYGON)
eoa_address = signer.address()

print(f"Owner/EOA address (from key): {eoa_address}")
print(f"Proxy wallet address:          {proxy}")


def build_l1_headers(nonce: int = 0) -> dict:
    """Build Level 1 auth headers matching the SDK exactly."""
    timestamp = int(time.time())
    signature = sign_clob_auth_message(signer, timestamp, nonce)
    return {
        "POLY_ADDRESS": eoa_address,
        "POLY_SIGNATURE": signature,
        "POLY_TIMESTAMP": str(timestamp),
        "POLY_NONCE": str(nonce),
        "User-Agent": "py_clob_client",
        "Accept": "*/*",
        "Connection": "keep-alive",
        "Content-Type": "application/json",
        "Accept-Encoding": "gzip",
    }


def try_create():
    """POST /auth/api-key – create new API key."""
    url = f"{host}/auth/api-key"
    print(f"\n[1] Trying CREATE: POST {url}")
    headers = build_l1_headers(nonce=0)
    resp = requests.post(url, headers=headers)
    resp.raise_for_status()
    return resp.json()


def try_derive():
    """GET /auth/derive-api-key – derive existing API key."""
    url = f"{host}/auth/derive-api-key"
    print(f"\n[2] Trying DERIVE: GET {url}")
    headers = build_l1_headers(nonce=0)
    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    return resp.json()


# ---- 3. Try create first, then derive (matches SDK behavior) ----
data = None

try:
    data = try_create()
    print("    → Create succeeded!")
except Exception as e:
    print(f"    → Create failed: {e}")
    if hasattr(e, 'response') and e.response is not None:
        print(f"    → Response: {e.response.text}")

if data is None:
    try:
        data = try_derive()
        print("    → Derive succeeded!")
    except Exception as e:
        print(f"    → Derive failed: {e}")
        if hasattr(e, 'response') and e.response is not None:
            print(f"    → Response: {e.response.text}")

if data:
    print("\n✅ Success! Add these to your .env file:\n")
    print(f"POLYMARKET_API_KEY={data.get('apiKey')}")
    print(f"POLYMARKET_API_SECRET={data.get('secret')}")
    print(f"POLYMARKET_API_PASSPHRASE={data.get('passphrase')}")
    print(f"\n⚠️  Keep these secret – never commit them to GitHub.")
else:
    print("\n❌ Both create and derive failed.")
    print("\nTroubleshooting:")
    print("  1. Have you signed into polymarket.com and accepted the terms of service?")
    print("  2. Has this wallet performed at least one action (deposit, trade)?")
    print("  3. Is the PRIVATE_KEY correct and for the right account?")
    print("  4. Try running: python setup_credentials.py (uses SDK directly)")
