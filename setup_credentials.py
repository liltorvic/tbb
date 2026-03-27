#!/usr/bin/env python3
"""
setup_credentials.py – Derive your Polymarket L2 API credentials.

Run this ONCE after creating your Polymarket account and funding your wallet.
It will print your API key, secret, and passphrase. Copy them into your .env file.

For email‑linked accounts (proxy wallet), you MUST set:
    - PRIVATE_KEY   = the owner private key (exported from Polymarket settings)
    - PROXY_WALLET  = the proxy address shown in your Polymarket profile
"""

import os
import sys
from dotenv import load_dotenv

load_dotenv()


def main():
    # 1. Validate .env settings
    pk = os.getenv("PRIVATE_KEY", "").strip()
    proxy = os.getenv("PROXY_WALLET", "").strip()

    if not pk:
        print("\n[ERROR] PRIVATE_KEY is not set in your .env file.")
        print("Add it like:  PRIVATE_KEY=0xYOUR_PRIVATE_KEY_HERE\n")
        sys.exit(1)

    if not proxy:
        print("\n[ERROR] PROXY_WALLET is not set in your .env file.")
        print("Add it like:  PROXY_WALLET=0xYOUR_PROXY_WALLET_ADDRESS\n")
        sys.exit(1)

    # Ensure private key has 0x prefix and correct length
    if not pk.startswith("0x"):
        pk = "0x" + pk
        print("[INFO] Added 0x prefix to private key")

    if len(pk) != 66:
        print(f"\n[ERROR] Private key length is {len(pk)} (expected 66).")
        print("Make sure you copied the full 64‑character hex string.\n")
        sys.exit(1)

    # Ensure proxy address has 0x prefix
    if not proxy.startswith("0x"):
        proxy = "0x" + proxy
        print("[INFO] Added 0x prefix to proxy wallet")

    # 2. Import SDK
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.constants import POLYGON
    except ImportError as e:
        print(f"\n[ERROR] Could not import py_clob_client: {e}")
        print("Run:  pip install py-clob-client")
        sys.exit(1)

    host = os.getenv("CLOB_HOST", "https://clob.polymarket.com")

    print(f"\n[INFO] CLOB Host       = {host}")
    print(f"[INFO] Chain ID        = {POLYGON}")
    print(f"[INFO] Proxy Wallet    = {proxy}")
    print(f"[INFO] Private Key     = {pk[:6]}...{pk[-4:]}")

    # 3. Build client with signature_type=1 and funder
    print("\nConnecting to Polymarket CLOB…")
    try:
        client = ClobClient(
            host=host,
            chain_id=POLYGON,
            key=pk,
            signature_type=1,      # POLY_PROXY mode – required for email accounts
            funder=proxy,          # the proxy wallet address
        )
    except Exception as e:
        print(f"\n[ERROR] Failed to create ClobClient: {e}")
        sys.exit(1)

    # Verify the client is in L1 mode
    if client.mode < 1:
        print("\n[ERROR] Client mode is L0 – signing not available.")
        print("Check your private key and network connection.")
        sys.exit(1)

    print(f"[INFO] Client mode    = {client.mode} (L1 or higher)")
    print(f"[INFO] Signer address = {client.signer.address()}")

    # 4. Derive API credentials
    print("\nDeriving API credentials…")
    try:
        creds = client.create_or_derive_api_creds()
    except Exception as e:
        print(f"\n[ERROR] Could not derive credentials: {e}")
        print("\nTroubleshooting:")
        print("  1. Have you signed into Polymarket with this wallet and accepted terms?")
        print("  2. Has this wallet performed at least one on‑chain action (deposit / trade)?")
        print("  3. Is your internet connection stable?")
        sys.exit(1)

    if not creds:
        print("\n[ERROR] Credentials returned empty. Try again.")
        sys.exit(1)

    # 5. Output the credentials
    print("\n✅ SUCCESS! Add these to your .env file:\n")
    print(f"API_KEY={creds.api_key}")
    print(f"API_SECRET={creds.api_secret}")
    print(f"API_PASSPHRASE={creds.api_passphrase}")

    print("\n[INFO] Your proxy wallet address (already in .env):")
    print(f"PROXY_WALLET={proxy}")

    print("\n⚠️  Keep these secret – never commit them to GitHub.")


if __name__ == "__main__":
    main()