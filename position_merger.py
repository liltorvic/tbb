"""
position_merger.py – Gas-efficient position merging via Polymarket's NegRiskAdapter.

When you hold offsetting YES + NO shares in the same market they can be "merged"
back into 1 USDC per pair by calling mergePositions() on the NegRiskAdapter.
This runs as a periodic background task (default: every hour).

Contract addresses (Polygon mainnet) verified against Polymarket docs / Etherscan.
ALWAYS re-confirm these in https://docs.polymarket.com/#contracts before deploying.
"""

import json
import logging
import time
from typing import Dict, List, Optional

from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

logger = logging.getLogger(__name__)


# ── Contract addresses (Polygon mainnet) ───────────────────────────────────────
# Sources:
#   https://docs.polymarket.com/#contracts
#   https://polygonscan.com – search "NegRiskAdapter" to confirm
NEG_RISK_ADAPTER   = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
CTF_EXCHANGE       = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
CONDITIONAL_TOKENS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_E             = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"


# ── Minimal ABIs ───────────────────────────────────────────────────────────────

NEG_RISK_ABI = json.loads("""[
  {
    "inputs": [
      {"internalType": "bytes32", "name": "conditionId", "type": "bytes32"},
      {"internalType": "uint256", "name": "amount",      "type": "uint256"}
    ],
    "name": "mergePositions",
    "outputs": [],
    "stateMutability": "nonpayable",
    "type": "function"
  },
  {
    "inputs": [
      {"internalType": "address", "name": "account",     "type": "address"},
      {"internalType": "bytes32", "name": "conditionId", "type": "bytes32"}
    ],
    "name": "balanceOf",
    "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
    "stateMutability": "view",
    "type": "function"
  }
]""")

CTF_ABI = json.loads("""[
  {
    "inputs": [
      {"internalType": "address[]", "name": "accounts", "type": "address[]"},
      {"internalType": "uint256[]", "name": "ids",      "type": "uint256[]"}
    ],
    "name": "balanceOfBatch",
    "outputs": [{"internalType": "uint256[]", "name": "", "type": "uint256[]"}],
    "stateMutability": "view",
    "type": "function"
  }
]""")


class PositionMerger:
    """
    Interacts with the NegRiskAdapter contract to merge opposing positions.

    Cost of merging: one Polygon transaction (~$0.01–0.05 in POL at normal gas).
    Benefit: recover USDC from offsetting YES+NO balances (saves on redemption later).
    """

    _MIN_MERGE_SHARES = 1.0    # Don't waste gas on tiny amounts (< 1 share ≈ < $1)

    def __init__(self, config):
        self.config = config
        self.w3: Optional[Web3] = None
        self.account = None
        self.adapter = None
        self.ctf = None
        self._init()

    # ── Initialisation ─────────────────────────────────────────────────────────

    def _init(self):
        if not self.config.PRIVATE_KEY:
            logger.warning("No PRIVATE_KEY – position merging disabled")
            return

        try:
            self.w3 = Web3(Web3.HTTPProvider(self.config.POLYGON_RPC, request_kwargs={"timeout": 20}))
            self.w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

            if not self.w3.is_connected():
                logger.error("Cannot connect to Polygon RPC – merging disabled")
                self.w3 = None
                return

            self.account = self.w3.eth.account.from_key(self.config.PRIVATE_KEY)
            self.adapter = self.w3.eth.contract(
                address=Web3.to_checksum_address(NEG_RISK_ADAPTER),
                abi=NEG_RISK_ABI,
            )
            self.ctf = self.w3.eth.contract(
                address=Web3.to_checksum_address(CONDITIONAL_TOKENS),
                abi=CTF_ABI,
            )

            block = self.w3.eth.block_number
            logger.info(
                f"PositionMerger ready  "
                f"wallet={self.account.address[:10]}…  "
                f"block=#{block}"
            )

        except Exception as exc:
            logger.error(f"PositionMerger init failed: {exc}")
            self.w3 = None

    # ── Public API ─────────────────────────────────────────────────────────────

    def pol_balance(self) -> float:
        """Current POL (gas token) balance in ether units."""
        if not self.w3 or not self.account:
            return 0.0
        try:
            wei = self.w3.eth.get_balance(self.account.address)
            return float(self.w3.from_wei(wei, "ether"))
        except Exception as exc:
            logger.error(f"pol_balance error: {exc}")
            return 0.0

    def batch_merge_all(self, client, markets: List[Dict]) -> Dict[str, Optional[str]]:
        """
        Inspect all markets for offsetting positions and merge where worthwhile.
        Returns {condition_id: tx_hash_or_None}.
        """
        results: Dict[str, Optional[str]] = {}

        if not self.w3 or not self.adapter:
            logger.info("Merger not available (no Web3 or contract)")
            return results

        pol = self.pol_balance()
        if pol < self.config.MIN_POL_BALANCE:
            logger.warning(
                f"Low POL: {pol:.4f}  (need ≥{self.config.MIN_POL_BALANCE}) – skipping merge"
            )
            return results

        # Pull positions from the API to find candidates
        try:
            raw_positions = client.get_positions()
        except Exception as exc:
            logger.error(f"batch_merge_all: failed to get positions: {exc}")
            return results

        # Group by condition_id
        by_condition: Dict[str, List[Dict]] = {}
        for p in raw_positions:
            cid = p.get("conditionId") or p.get("condition_id", "")
            if cid:
                by_condition.setdefault(cid, []).append(p)

        for market in markets:
            cid = market["condition_id"]
            pos_list = by_condition.get(cid, [])

            if len(pos_list) < 2:
                continue  # need both YES and NO to merge

            sizes = [float(p.get("size") or 0) for p in pos_list]
            mergeable_shares = min(sizes)

            if mergeable_shares < self._MIN_MERGE_SHARES:
                continue

            logger.info(
                f"Merging {mergeable_shares:.2f} shares  "
                f"market={market['label'][:40]}"
            )
            tx = self._merge(cid, mergeable_shares)
            results[cid] = tx
            time.sleep(2)  # brief pause between transactions

        merged_ok = sum(1 for v in results.values() if v)
        if results:
            logger.info(f"Merge cycle complete: {merged_ok}/{len(results)} successful")

        return results

    # ── Internal ───────────────────────────────────────────────────────────────

    def _merge(self, condition_id: str, shares: float) -> Optional[str]:
        """
        Call mergePositions(conditionId, amount) on the NegRiskAdapter.
        amount is in USDC.e wei (6 decimals).
        """
        if self.config.DRY_RUN:
            logger.info(
                f"[DRY-RUN] merge  condition={condition_id[:14]}  "
                f"shares={shares:.2f}"
            )
            return "0x_dry_run"

        if not self.w3 or not self.adapter or not self.account:
            return None

        try:
            # Encode conditionId as bytes32
            cid_hex = condition_id if condition_id.startswith("0x") else "0x" + condition_id
            cid_bytes = Web3.to_bytes(hexstr=cid_hex)

            # USDC.e has 6 decimals; shares are 1:1 with USDC at resolution
            amount_wei = int(shares * 1_000_000)

            gas_price = self._get_gas_price()
            nonce     = self.w3.eth.get_transaction_count(self.account.address, "pending")

            tx = self.adapter.functions.mergePositions(
                cid_bytes,
                amount_wei,
            ).build_transaction({
                "from":     self.account.address,
                "gas":      self.config.GAS_LIMIT_MERGE,
                "gasPrice": gas_price,
                "nonce":    nonce,
                "chainId":  self.config.CHAIN_ID,
            })

            signed  = self.w3.eth.account.sign_transaction(tx, self.config.PRIVATE_KEY)
            tx_hash = self.w3.eth.send_raw_transaction(signed.rawTransaction)

            logger.info(f"Merge tx sent: {tx_hash.hex()[:18]}…  Waiting for receipt…")
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

            if receipt.status == 1:
                gas_used = receipt.gasUsed
                logger.info(
                    f"Merge confirmed ✓  "
                    f"condition={condition_id[:14]}  "
                    f"shares={shares:.2f}  "
                    f"gas_used={gas_used}  "
                    f"tx={tx_hash.hex()[:18]}…"
                )
                return tx_hash.hex()
            else:
                logger.error(
                    f"Merge REVERTED  condition={condition_id[:14]}  "
                    f"tx={tx_hash.hex()[:18]}…"
                )
                return None

        except Exception as exc:
            logger.error(f"_merge error [{condition_id[:14]}]: {exc}")
            return None

    def _get_gas_price(self) -> int:
        """EIP-1559 aware gas pricing with a buffer multiplier."""
        try:
            latest = self.w3.eth.get_block("latest")
            base_fee = latest.get("baseFeePerGas")
            if base_fee:
                # Polygon tip: ~30 Gwei is usually sufficient
                priority = self.w3.to_wei(30, "gwei")
                max_fee  = int(base_fee * self.config.GAS_PRICE_BUFFER) + priority
                return max_fee
            # Legacy fallback
            return int(self.w3.eth.gas_price * self.config.GAS_PRICE_BUFFER)
        except Exception:
            return self.w3.to_wei(100, "gwei")   # hard fallback
