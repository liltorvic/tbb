#!/usr/bin/env python3
"""
debug_auth.py – Diagnose 401 "Invalid L1 Request headers" errors.

Tests clock skew, header combinations, and different signing approaches.
"""

import os
import sys
import time
import json
import requests
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

try:
    from py_clob_client.signer import Signer
    from py_clob_client.headers.headers import create_level_1_headers
    from py_clob_client.signing.eip712 import (
        sign_clob_auth_message,
        get_clob_auth_domain,
        MSG_TO_SIGN,
    )
    from py_clob_client.signing.model import ClobAuth
    from py_clob_client.constants import POLYGON
    from eth_utils import keccak
    from py_order_utils.utils import prepend_zx
except ImportError as e:
    print(f"SDK import error: {e}")
    sys.exit(1)

pk = os.getenv("PRIVATE_KEY", "").strip()
proxy = os.getenv("PROXY_WALLET", "").strip()
host = os.getenv("CLOB_HOST", "https://clob.polymarket.com")

if not pk:
    print("ERROR: PRIVATE_KEY not set in .env")
    sys.exit(1)

if not pk.startswith("0x"):
    pk = "0x" + pk
if proxy and not proxy.startswith("0x"):
    proxy = "0x" + proxy

signer = Signer(pk, POLYGON)
eoa = signer.address()

print("=" * 60)
print("DEBUG AUTH DIAGNOSTICS")
print("=" * 60)
print(f"EOA address (from key): {eoa}")
print(f"Proxy wallet:           {proxy or '(not set)'}")
print(f"CLOB host:              {host}")
print(f"Chain ID:               {POLYGON}")
print(f"Private key:            {pk[:6]}...{pk[-4:]}")
print(f"Key length:             {len(pk)} chars")


# ---- 1. Clock skew check ----
print("\n--- CLOCK SKEW CHECK ---")
local_time = int(time.time())
print(f"Local timestamp:  {local_time}")
print(f"Local datetime:   {datetime.now().isoformat()}")

try:
    r = requests.get(f"{host}/", timeout=5)
    server_date = r.headers.get("Date", "")
    print(f"Server Date header: {server_date}")
except Exception as e:
    print(f"Could not get server Date header: {e}")

try:
    r = requests.get(f"{host}/time", timeout=5)
    if r.status_code == 200:
        print(f"Server /time response: {r.text[:200]}")
except Exception:
    pass


def sign_with_address(address_in_struct, timestamp, nonce):
    """Sign EIP-712 ClobAuth with a specific address in the struct."""
    clob_auth_msg = ClobAuth(
        address=address_in_struct,
        timestamp=str(timestamp),
        nonce=nonce,
        message=MSG_TO_SIGN,
    )
    chain_id = signer.get_chain_id()
    auth_struct_hash = prepend_zx(
        keccak(clob_auth_msg.signable_bytes(get_clob_auth_domain(chain_id))).hex()
    )
    return prepend_zx(signer.sign(auth_struct_hash))


def try_auth(label, method, url, poly_address, struct_address, nonce=0):
    """Try auth with specific address in header AND in EIP-712 struct."""
    ts = int(time.time())
    sig = sign_with_address(struct_address, ts, nonce)

    headers = {
        "POLY_ADDRESS": poly_address,
        "POLY_SIGNATURE": sig,
        "POLY_TIMESTAMP": str(ts),
        "POLY_NONCE": str(nonce),
        "User-Agent": "py_clob_client",
        "Accept": "*/*",
        "Connection": "keep-alive",
        "Content-Type": "application/json",
        "Accept-Encoding": "gzip",
    }

    print(f"\n[{label}]")
    print(f"  {method} {url}")
    print(f"  POLY_ADDRESS (header): {poly_address}")
    print(f"  EIP-712 struct addr:   {struct_address}")
    print(f"  POLY_TIMESTAMP: {ts}")

    try:
        if method == "POST":
            resp = requests.post(url, headers=headers, timeout=10)
        else:
            resp = requests.get(url, headers=headers, timeout=10)

        print(f"  Status: {resp.status_code}")
        body = resp.text[:300]
        print(f"  Response: {body}")

        if resp.status_code == 200:
            print("  ✅ SUCCESS!")
            return resp.json()
    except Exception as e:
        print(f"  Error: {e}")

    return None


print("\n--- TESTING AUTH ENDPOINTS ---")
print("(Testing all combinations of header address + struct address)\n")

result = None

# ---- Standard SDK approach: EOA in header, EOA in struct ----
if not result:
    result = try_auth(
        "Test 1: POST create — EOA header, EOA struct",
        "POST", f"{host}/auth/api-key", eoa, eoa
    )

if not result:
    result = try_auth(
        "Test 2: GET derive — EOA header, EOA struct",
        "GET", f"{host}/auth/derive-api-key", eoa, eoa
    )

# ---- Proxy in header, EOA in struct (previous manual_derive approach) ----
if not result and proxy:
    result = try_auth(
        "Test 3: POST create — PROXY header, EOA struct",
        "POST", f"{host}/auth/api-key", proxy, eoa
    )

# ---- Proxy in BOTH header and struct (email account theory) ----
if not result and proxy:
    result = try_auth(
        "Test 4: POST create — PROXY header, PROXY struct",
        "POST", f"{host}/auth/api-key", proxy, proxy
    )

if not result and proxy:
    result = try_auth(
        "Test 5: GET derive — PROXY header, PROXY struct",
        "GET", f"{host}/auth/derive-api-key", proxy, proxy
    )

# ---- EOA header, Proxy in struct ----
if not result and proxy:
    result = try_auth(
        "Test 6: POST create — EOA header, PROXY struct",
        "POST", f"{host}/auth/api-key", eoa, proxy
    )

if not result and proxy:
    result = try_auth(
        "Test 7: GET derive — EOA header, PROXY struct",
        "GET", f"{host}/auth/derive-api-key", eoa, proxy
    )

# ---- Try nonce=1 with the most likely combos ----
if not result:
    result = try_auth(
        "Test 8: POST create — EOA/EOA, nonce=1",
        "POST", f"{host}/auth/api-key", eoa, eoa, nonce=1
    )

if not result and proxy:
    result = try_auth(
        "Test 9: POST create — PROXY/PROXY, nonce=1",
        "POST", f"{host}/auth/api-key", proxy, proxy, nonce=1
    )

# ---- Print results ----
if result:
    print("\n" + "=" * 60)
    print("✅ SUCCESS! Add these to your .env file:")
    print("=" * 60)
    print(f"POLYMARKET_API_KEY={result.get('apiKey')}")
    print(f"POLYMARKET_API_SECRET={result.get('secret')}")
    print(f"POLYMARKET_API_PASSPHRASE={result.get('passphrase')}")
else:
    print("\n" + "=" * 60)
    print("❌ ALL ATTEMPTS FAILED")
    print("=" * 60)
    print("\nPossible causes:")
    print("  1. Clock skew — sync with: sudo ntpdate pool.ntp.org")
    print("  2. Wrong private key — the exported key might not be the CLOB signing key")
    print("  3. Account not CLOB-enabled — try placing a trade on polymarket.com first")
    print("  4. Email account auth flow differs from SDK expectations")
    print(f"\n  Your EOA from this key: {eoa}")
    print(f"  Your proxy wallet:      {proxy or '(not set)'}")
