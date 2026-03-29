"""
diagnose_allowance.py – Check USDC balance and allowance state for both exchange contracts.
Run this to understand why orders fail with "not enough balance / allowance".
"""

import os
from dotenv import load_dotenv
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware
from eth_account import Account

load_dotenv()

# ── Setup ─────────────────────────────────────────────────────────────────────

RPC = os.getenv("POLYGON_RPC", "https://polygon-bor-rpc.publicnode.com")
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
PROXY_WALLET = os.getenv("PROXY_WALLET", "")

# Polymarket contract addresses (Polygon mainnet)
USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"           # regular exchange
NEG_RISK_CTF_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"    # neg-risk exchange
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"

# Minimal ERC20 ABI for balanceOf + allowance
ERC20_ABI = [
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]


def main():
    w3 = Web3(Web3.HTTPProvider(RPC, request_kwargs={"timeout": 20}))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

    if not w3.is_connected():
        print("ERROR: Cannot connect to Polygon RPC")
        return

    acct = Account.from_key(PRIVATE_KEY)
    eoa = acct.address
    proxy = Web3.to_checksum_address(PROXY_WALLET) if PROXY_WALLET else None

    usdc = w3.eth.contract(address=Web3.to_checksum_address(USDC_E), abi=ERC20_ABI)

    print("=" * 65)
    print("  Polymarket Allowance Diagnostic")
    print("=" * 65)

    # ── EOA info ──────────────────────────────────────────────────────────────
    print(f"\n  EOA address:   {eoa}")
    pol_bal = w3.from_wei(w3.eth.get_balance(eoa), "ether")
    eoa_usdc = usdc.functions.balanceOf(eoa).call() / 1e6
    print(f"  EOA POL:       {pol_bal:.4f}")
    print(f"  EOA USDC:      ${eoa_usdc:.2f}")

    is_eoa_contract = w3.eth.get_code(eoa)
    print(f"  EOA is contract: {len(is_eoa_contract) > 0}")

    # ── Proxy wallet info ─────────────────────────────────────────────────────
    if proxy:
        print(f"\n  Proxy address: {proxy}")
        proxy_pol = w3.from_wei(w3.eth.get_balance(proxy), "ether")
        proxy_usdc = usdc.functions.balanceOf(proxy).call() / 1e6
        print(f"  Proxy POL:     {proxy_pol:.4f}")
        print(f"  Proxy USDC:    ${proxy_usdc:.2f}")

        is_contract = w3.eth.get_code(proxy)
        print(f"  Proxy is contract: {len(is_contract) > 0}")

        # ── Allowances from proxy wallet ──────────────────────────────────────
        print(f"\n  --- USDC Allowances (from proxy wallet) ---")

        for label, spender in [
            ("CTF Exchange", CTF_EXCHANGE),
            ("NegRisk CTF Exchange", NEG_RISK_CTF_EXCHANGE),
            ("NegRisk Adapter", NEG_RISK_ADAPTER),
        ]:
            allow = usdc.functions.allowance(proxy, Web3.to_checksum_address(spender)).call()
            allow_usd = allow / 1e6
            if allow > 1e30:
                print(f"  {label:25s} → UNLIMITED (max uint256)")
            elif allow == 0:
                print(f"  {label:25s} → $0.00  ⚠️  NOT APPROVED")
            else:
                print(f"  {label:25s} → ${allow_usd:.2f}")

        # ── Allowances from EOA ───────────────────────────────────────────────
        print(f"\n  --- USDC Allowances (from EOA) ---")

        for label, spender in [
            ("CTF Exchange", CTF_EXCHANGE),
            ("NegRisk CTF Exchange", NEG_RISK_CTF_EXCHANGE),
            ("NegRisk Adapter", NEG_RISK_ADAPTER),
        ]:
            allow = usdc.functions.allowance(eoa, Web3.to_checksum_address(spender)).call()
            allow_usd = allow / 1e6
            if allow > 1e30:
                print(f"  {label:25s} → UNLIMITED (max uint256)")
            elif allow == 0:
                print(f"  {label:25s} → $0.00  ⚠️  NOT APPROVED")
            else:
                print(f"  {label:25s} → ${allow_usd:.2f}")
    else:
        print("\n  No PROXY_WALLET set in .env")

    print()


if __name__ == "__main__":
    main()
