"""
market_selector.py – Identifies the best markets to provide liquidity on.

Scoring criteria (higher = better opportunity):
  1. 24h volume   – high volume = active market, real spread earnings
  2. Spread width – wider existing spread = more room for profit
  3. Price range  – avoid extreme (near-resolved) markets
  4. Competition  – fewer active makers = less queue pressure
  5. Rewards      – liquidity mining programmes add free alpha
"""

import logging
import time
from typing import Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)


class MarketSelector:
    """
    Fetches market data from Polymarket's Gamma API (the richer metadata source)
    and returns a ranked, filtered list of markets suitable for market making.
    """

    _GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
    _REQUEST_TIMEOUT = 15

    def __init__(self, client, config):
        self.client = client
        self.config = config
        self._last_selected: List[Dict] = []
        self._last_run: float = 0.0

    # ── Public ─────────────────────────────────────────────────────────────────

    def select_markets(self, force: bool = False) -> List[Dict]:
        """
        Return the current list of selected markets.
        Cached for MARKET_REFRESH_INTERVAL seconds unless force=True.

        Each market dict contains:
            condition_id, token_ids, label, score, reason, volume24h
        """
        age = time.time() - self._last_run
        if not force and self._last_selected and age < self.config.MARKET_REFRESH_INTERVAL:
            return self._last_selected

        logger.info("Scanning Polymarket for suitable markets…")

        raw = self._fetch_gamma_markets()
        if not raw:
            logger.warning("Gamma API returned no markets – falling back to CLOB")
            raw = self._fetch_clob_markets()

        scored: List[Tuple[float, Dict, str]] = []
        for m in raw:
            if not self._is_eligible(m):
                continue
            score, reason = self._score(m)
            if score > 0:
                scored.append((score, m, reason))

        scored.sort(key=lambda x: -x[0])
        top = scored[: self.config.MAX_MARKETS]

        selected = []
        for score, m, reason in top:
            entry = self._normalise(m, score, reason)
            if entry:
                selected.append(entry)
                logger.info(
                    f"  ✓  {entry['label'][:55]:<55}  "
                    f"score={score:.2f}  {reason}"
                )

        if not selected:
            logger.warning("No markets passed filters – loosen MIN_VOLUME_24H or MIN_SPREAD_TO_ENTER")

        self._last_selected = selected
        self._last_run = time.time()
        logger.info(f"Selected {len(selected)} market(s) to trade.")
        return selected

    # ── Fetchers ───────────────────────────────────────────────────────────────

    def _fetch_gamma_markets(self) -> List[Dict]:
        """Gamma API returns richer metadata than the CLOB."""
        results: List[Dict] = []
        params = {
            "active": "true",
            "closed": "false",
            "enableOrderBook": "true",
            "limit": 200,
            "offset": 0,
            "order": "volume24hr",
            "ascending": "false",
        }
        try:
            while True:
                resp = requests.get(
                    self._GAMMA_MARKETS_URL,
                    params=params,
                    timeout=self._REQUEST_TIMEOUT,
                )
                resp.raise_for_status()
                data = resp.json()

                batch = data if isinstance(data, list) else data.get("markets", [])
                if not batch:
                    break
                results.extend(batch)

                # Stop once we have enough candidates to evaluate
                if len(results) >= 500:
                    break

                # Some deployments paginate; break if less than limit came back
                if len(batch) < params["limit"]:
                    break

                params["offset"] += params["limit"]
                time.sleep(0.2)

        except requests.RequestException as exc:
            logger.error(f"Gamma API error: {exc}")

        logger.info(f"Fetched {len(results)} raw markets from Gamma API")
        return results

    def _fetch_clob_markets(self) -> List[Dict]:
        """Fallback: use CLOB API (less metadata)."""
        try:
            return self.client.get_all_active_markets()
        except Exception as exc:
            logger.error(f"CLOB market fallback failed: {exc}")
            return []

    # ── Filtering & Scoring ────────────────────────────────────────────────────

    def _is_eligible(self, m: Dict) -> bool:
        """Hard eligibility gates – failing any disqualifies the market."""
        if m.get("closed") or m.get("resolved"):
            return False
        if not m.get("active", True):
            return False
        if not m.get("enableOrderBook", True):
            return False
        # Binary markets only (YES/NO)
        tokens = m.get("tokens") or m.get("clobTokenIds") or []
        if len(tokens) < 2:
            return False
        return True

    def _score(self, m: Dict) -> Tuple[float, str]:
        """
        Returns (score, human_readable_reason).
        score <= 0 means the market should be skipped.
        """
        tags: List[str] = []
        score = 0.0

        # 1. Volume
        volume = float(m.get("volume24hr") or m.get("volume") or 0)
        if volume < self.config.MIN_VOLUME_24H:
            return -1.0, f"vol_too_low({volume:.0f})"
        vol_pts = min(volume / 100_000, 5.0)
        score += vol_pts
        tags.append(f"vol=${volume:,.0f}")

        # 2. Price extremity
        best_bid = float(m.get("bestBid") or 0)
        best_ask = float(m.get("bestAsk") or 0)
        if best_bid > 0 and best_ask > 0:
            mid = (best_bid + best_ask) / 2.0
            thresh = self.config.PRICE_EXTREME_THRESHOLD
            if mid < thresh or mid > (1.0 - thresh):
                return -1.0, f"price_extreme(mid={mid:.3f})"

            # 3. Spread opportunity
            spread = best_ask - best_bid
            spread_pct = spread / mid if mid > 0 else 0.0
            if spread_pct < self.config.MIN_SPREAD_TO_ENTER:
                return -1.0, f"spread_too_tight({spread_pct:.3%})"

            spread_pts = min(spread_pct * 20, 4.0)
            score += spread_pts
            tags.append(f"spread={spread_pct:.2%}")

        # 4. Liquidity rewards
        has_rewards = bool(
            m.get("rewardsMinSize")
            or m.get("clobRewards")
            or m.get("liquidityReward")
        )
        if has_rewards:
            score += 2.0
            tags.append("rewards✓")

        # 5. Competition (order count as proxy)
        order_count = int(m.get("orderCount") or 0)
        if order_count > 1_000:
            score -= 2.0
            tags.append("high_competition")
        elif order_count > 500:
            score -= 0.5
            tags.append("medium_competition")

        return score, " | ".join(tags)

    # ── Normalisation ──────────────────────────────────────────────────────────

    def _normalise(self, m: Dict, score: float, reason: str) -> Optional[Dict]:
        """
        Build a uniform market dict regardless of which API the data came from.
        Returns None if essential fields are missing.
        """
        condition_id = (
            m.get("conditionId")
            or m.get("condition_id")
            or ""
        )
        if not condition_id:
            logger.debug("Skipping market with no conditionId")
            return None

        # Extract token IDs
        tokens_raw = m.get("tokens") or m.get("clobTokenIds") or []
        token_ids: List[str] = []

        if tokens_raw and isinstance(tokens_raw[0], dict):
            # Gamma format: [{"token_id": "...", "outcome": "Yes"}, …]
            # Ensure YES comes first
            yes_tokens = [t for t in tokens_raw if t.get("outcome", "").lower() == "yes"]
            no_tokens  = [t for t in tokens_raw if t.get("outcome", "").lower() == "no"]
            ordered = yes_tokens + no_tokens + [
                t for t in tokens_raw if t not in yes_tokens + no_tokens
            ]
            token_ids = [t["token_id"] for t in ordered if t.get("token_id")]
        else:
            # CLOB format: plain list of strings
            token_ids = [str(t) for t in tokens_raw]

        if not token_ids:
            logger.debug(f"Skipping {condition_id[:12]} – no token IDs found")
            return None

        return {
            "condition_id": condition_id,
            "token_ids": token_ids,           # [YES_token, NO_token, …]
            "label": m.get("question") or condition_id[:20],
            "score": round(score, 3),
            "reason": reason,
            "volume24h": float(m.get("volume24hr") or 0),
            "best_bid": float(m.get("bestBid") or 0),
            "best_ask": float(m.get("bestAsk") or 0),
        }
