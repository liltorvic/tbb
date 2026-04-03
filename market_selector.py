"""
market_selector.py – execution-aware market selector for the Polymarket MM bot.

Design:
  1) Coarse ranking from cheap Gamma metadata
  2) Final scoring from live YES-token order books for shortlisted markets

Keeps compatibility with the existing bot contract:
  condition_id, token_ids (YES first), label
"""

import json
import logging
import math
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)


class MarketSelector:
    _GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
    _REQUEST_TIMEOUT = 15

    def __init__(self, client, config):
        self.client = client
        self.config = config
        self._last_selected: List[Dict] = []
        self._last_run: float = 0.0
        self._book_cache: Dict[str, Dict[str, Any]] = {}
        self._book_cache_ttl: float = float(
            self._cfg_float("SELECTION_BOOK_CACHE_TTL_SECONDS", 60.0)
        )

    # ── Public ───────────────────────────────────────────────────────────────

    def select_markets(self, force: bool = False) -> List[Dict]:
        age = time.time() - self._last_run
        if not force and self._last_selected and age < self.config.MARKET_REFRESH_INTERVAL:
            return self._last_selected

        logger.info("Scanning Polymarket for execution-worthy markets…")

        raw = self._fetch_gamma_markets()
        if not raw:
            logger.warning("Gamma API returned no markets – falling back to CLOB")
            raw = self._fetch_clob_markets()

        coarse_ranked: List[Tuple[float, Dict, Dict[str, Any]]] = []
        for market in raw:
            if not self._is_eligible(market):
                continue

            coarse_score, coarse_meta = self._coarse_score(market)
            if coarse_score <= 0:
                continue

            coarse_ranked.append((coarse_score, market, coarse_meta))

        if not coarse_ranked:
            logger.warning("No markets passed the coarse selector")
            self._last_selected = []
            self._last_run = time.time()
            return []

        coarse_ranked.sort(key=lambda x: x[0], reverse=True)

        shortlist_size_cfg = self._cfg_int("SELECTION_SHORTLIST_SIZE", 0)
        shortlist_size = max(
            self.config.MAX_MARKETS,
            shortlist_size_cfg
            if shortlist_size_cfg > 0
            else self.config.MAX_MARKETS * self._cfg_int("SELECTION_SHORTLIST_MULTIPLIER", 4),
        )
        shortlist = coarse_ranked[:shortlist_size]

        final_ranked: List[Dict] = []
        for _, market, coarse_meta in shortlist:
            book_meta = self._get_yes_book_metrics_cached(market)
            if not book_meta or not book_meta.get("tradable"):
                continue

            final_score, reason, breakdown = self._final_score(coarse_meta, book_meta)
            if final_score <= 0:
                continue

            entry = self._normalise(
                market=market,
                score=final_score,
                reason=reason,
                coarse_meta=coarse_meta,
                book_meta=book_meta,
                breakdown=breakdown,
            )
            if entry:
                final_ranked.append(entry)
                logger.info(
                    "  ✓  %-55s score=%6.2f  %s",
                    entry["label"][:55],
                    entry["score"],
                    reason,
                )

        final_ranked.sort(key=lambda x: x["score"], reverse=True)
        selected = final_ranked[: self.config.MAX_MARKETS]

        if not selected:
            logger.warning(
                "No markets passed the execution-aware selector. "
                "Consider loosening MIN_VOLUME_24H, MIN_SPREAD_TO_ENTER, or "
                "SELECTION_* thresholds."
            )

        self._last_selected = selected
        self._last_run = time.time()
        logger.info("Selected %d market(s) to trade.", len(selected))
        return selected

    # ── Fetchers ─────────────────────────────────────────────────────────────

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

                if len(results) >= 500:
                    break

                if len(batch) < params["limit"]:
                    break

                params["offset"] += params["limit"]
                time.sleep(0.2)

        except requests.RequestException as exc:
            logger.error("Gamma API error: %s", exc)

        logger.info("Fetched %d raw markets from Gamma API", len(results))
        return results

    def _fetch_clob_markets(self) -> List[Dict]:
        """Fallback: use CLOB API (less metadata)."""
        try:
            return self.client.get_all_active_markets()
        except Exception as exc:
            logger.error("CLOB market fallback failed: %s", exc)
            return []

    # ── Eligibility & coarse scoring ────────────────────────────────────────

    def _is_eligible(self, market: Dict) -> bool:
        if market.get("closed") or market.get("resolved"):
            return False
        if not market.get("active", True):
            return False
        if not market.get("enableOrderBook", True):
            return False

        tokens = market.get("tokens") or market.get("clobTokenIds") or []
        if isinstance(tokens, str):
            try:
                tokens = json.loads(tokens)
            except Exception:
                tokens = []

        if len(tokens) < 2:
            return False

        return bool(self._extract_yes_token_id(market))

    def _coarse_score(self, market: Dict) -> Tuple[float, Dict[str, Any]]:
        volume = float(market.get("volume24hr") or market.get("volume") or 0.0)
        if volume < self.config.MIN_VOLUME_24H:
            return 0.0, {"reject_reason": f"vol_too_low({volume:.0f})"}

        vol_score = self._clamp(math.log10(volume + 1.0) / 6.0)

        best_bid = self._to_float(market.get("bestBid"))
        best_ask = self._to_float(market.get("bestAsk"))
        if best_bid <= 0 or best_ask <= 0 or best_ask <= best_bid:
            return 0.0, {"reject_reason": "invalid_bid_ask"}

        mid = (best_bid + best_ask) / 2.0
        spread_pct = (best_ask - best_bid) / max(mid, 1e-6)

        mid_score = self._mid_score(mid)
        spread_score = self._spread_score(spread_pct)

        has_rewards = bool(
            market.get("rewardsMinSize")
            or market.get("clobRewards")
            or market.get("liquidityReward")
        )
        reward_score = 1.0 if has_rewards else 0.0

        order_count = int(market.get("orderCount") or 0)
        competition_score = 1.0 - self._clamp(
            order_count / self._cfg_float("SELECTION_MAX_ORDERCOUNT", 1200.0)
        )

        time_to_resolution_s = self._time_to_resolution_seconds(market)
        lifecycle_score = self._lifecycle_score(time_to_resolution_s)

        min_ttr = self._cfg_int("SELECTION_MIN_TIME_TO_RESOLUTION_SECONDS", 3600)
        if time_to_resolution_s is not None and time_to_resolution_s < min_ttr:
            return 0.0, {"reject_reason": f"resolves_too_soon({time_to_resolution_s:.0f}s)"}

        coarse_score = 100.0 * (
            0.30 * vol_score
            + 0.20 * spread_score
            + 0.15 * mid_score
            + 0.15 * competition_score
            + 0.15 * lifecycle_score
            + 0.05 * reward_score
        )

        meta = {
            "volume": volume,
            "vol_score": vol_score,
            "mid": mid,
            "mid_score": mid_score,
            "spread_pct": spread_pct,
            "spread_score": spread_score,
            "has_rewards": has_rewards,
            "reward_score": reward_score,
            "order_count": order_count,
            "competition_score": competition_score,
            "time_to_resolution_s": time_to_resolution_s,
            "lifecycle_score": lifecycle_score,
            "coarse_score": coarse_score,
        }
        return coarse_score, meta

    # ── Stage 2: live order-book enrichment ─────────────────────────────────

    def _get_yes_book_metrics_cached(self, market: Dict) -> Optional[Dict[str, Any]]:
        yes_token = self._extract_yes_token_id(market)
        if not yes_token:
            return None

        now = time.time()
        cached = self._book_cache.get(yes_token)
        if cached and now - cached["timestamp"] < self._book_cache_ttl:
            return cached["data"]

        data = self._fetch_yes_book_metrics(market, yes_token=yes_token)
        self._book_cache[yes_token] = {"timestamp": now, "data": data}
        return data

    def _fetch_yes_book_metrics(
        self,
        market: Dict,
        yes_token: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        yes_token = yes_token or self._extract_yes_token_id(market)
        if not yes_token:
            return None

        try:
            raw_book = self.client.get_orderbook(yes_token)
        except Exception as exc:
            logger.debug("Orderbook fetch failed for %s: %s", yes_token[:16], exc)
            return None

        bids = self._parse_book_levels(self._book_side(raw_book, "bids"))
        asks = self._parse_book_levels(self._book_side(raw_book, "asks"))
        bids.sort(key=lambda x: x[0], reverse=True)
        asks.sort(key=lambda x: x[0])

        if not bids or not asks:
            return {"tradable": False, "yes_token": yes_token}

        best_bid = bids[0][0]
        best_ask = asks[0][0]
        if best_bid <= 0 or best_ask <= 0 or best_ask <= best_bid:
            return {"tradable": False, "yes_token": yes_token}

        mid = (best_bid + best_ask) / 2.0
        spread = best_ask - best_bid
        spread_pct = spread / max(mid, 1e-6)

        target_order_usd = float(self.config.ORDER_SIZE_USD)
        target_shares = max(target_order_usd / max(mid, 0.05), 1.0)

        depth_bps = self._cfg_float("SELECTION_DEPTH_BPS", 15.0)
        competition_bps = self._cfg_float("SELECTION_COMPETITION_BPS", 12.0)
        half_spread_bps = (spread_pct * 10_000.0) / 2.0
        adaptive_depth_bps = min(
            max(depth_bps, half_spread_bps + 1.0),
            self._cfg_float("SELECTION_MAX_ADAPTIVE_DEPTH_BPS", 400.0),
        )
        adaptive_competition_bps = min(
            max(competition_bps, half_spread_bps + 1.0),
            self._cfg_float("SELECTION_MAX_ADAPTIVE_COMPETITION_BPS", 400.0),
        )

        bid_depth_window = self._depth_within_bps(bids, mid, "bid", adaptive_depth_bps)
        ask_depth_window = self._depth_within_bps(asks, mid, "ask", adaptive_depth_bps)

        near_touch_bid_levels = self._levels_within_bps(
            bids, mid, "bid", adaptive_competition_bps
        )
        near_touch_ask_levels = self._levels_within_bps(
            asks, mid, "ask", adaptive_competition_bps
        )
        near_touch_levels = near_touch_bid_levels + near_touch_ask_levels

        bid_depth_5 = sum(size for _, size in bids[:5])
        ask_depth_5 = sum(size for _, size in asks[:5])
        total_top5 = bid_depth_5 + ask_depth_5
        depth_imbalance = (bid_depth_5 - ask_depth_5) / total_top5 if total_top5 > 0 else 0.0

        last_trade_price = self._to_float(self._book_field(raw_book, "last_trade_price"))
        last_trade_gap_pct = (
            abs(last_trade_price - mid) / mid
            if last_trade_price > 0 and mid > 0
            else 0.0
        )

        return {
            "tradable": True,
            "yes_token": yes_token,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "mid": mid,
            "spread": spread,
            "spread_pct": spread_pct,
            "target_shares": target_shares,
            "bid_depth_window": bid_depth_window,
            "ask_depth_window": ask_depth_window,
            "bid_depth_5": bid_depth_5,
            "ask_depth_5": ask_depth_5,
            "depth_imbalance": depth_imbalance,
            "adaptive_depth_bps": adaptive_depth_bps,
            "adaptive_competition_bps": adaptive_competition_bps,
            "near_touch_bid_levels": near_touch_bid_levels,
            "near_touch_ask_levels": near_touch_ask_levels,
            "near_touch_levels": near_touch_levels,
            "last_trade_price": last_trade_price,
            "last_trade_gap_pct": last_trade_gap_pct,
        }

    def _final_score(
        self,
        coarse_meta: Dict[str, Any],
        book_meta: Dict[str, Any],
    ) -> Tuple[float, str, Dict[str, float]]:
        spread_pct = book_meta["spread_pct"]
        target_quote_spread = self.config.TARGET_SPREAD_BPS / 10_000.0
        min_enter_spread = float(self.config.MIN_SPREAD_TO_ENTER)

        spread_score = self._spread_score(spread_pct)
        headroom = spread_pct - max(target_quote_spread, min_enter_spread * 0.75)
        headroom_score = self._clamp(
            (headroom + 0.01)
            / self._cfg_float("SELECTION_MAX_REASONABLE_SPREAD", 0.08)
        )
        edge_score = 0.55 * spread_score + 0.45 * headroom_score
        max_reasonable_spread = self._cfg_float("SELECTION_MAX_REASONABLE_SPREAD", 0.08)
        if spread_pct > max_reasonable_spread and spread_pct > 0:
            edge_score *= max(0.10, max_reasonable_spread / spread_pct)

        target_depth_mult = self._cfg_float("SELECTION_TARGET_DEPTH_MULTIPLIER", 3.0)
        depth_capacity = min(book_meta["bid_depth_window"], book_meta["ask_depth_window"])
        depth_score = self._clamp(
            depth_capacity / max(book_meta["target_shares"] * target_depth_mult, 1e-6)
        )

        max_comp_levels = self._cfg_float("SELECTION_MAX_COMPETITION_LEVELS", 12.0)
        if depth_capacity <= 0:
            local_competition_score = 0.0
        else:
            local_competition_score = 1.0 - self._clamp(
                book_meta["near_touch_levels"] / max_comp_levels
            )

        fill_quality_score = 0.65 * depth_score + 0.35 * local_competition_score
        if depth_capacity <= 0:
            fill_quality_score *= 0.20

        imbalance_penalty = min(abs(book_meta["depth_imbalance"]), 1.0) * 0.10
        last_trade_gap_penalty = min(book_meta["last_trade_gap_pct"] / 0.02, 1.0) * 0.15
        suspicious_wide_penalty = 0.10 if spread_pct > 0.10 else 0.0
        toxicity_penalty = imbalance_penalty + last_trade_gap_penalty + suspicious_wide_penalty

        # Soft guardrails to discourage pathological markets without eliminating all candidates.
        soft_max_spread = self._cfg_float("SELECTION_SOFT_MAX_SPREAD_PCT", 0.35)
        soft_min_depth = self._cfg_float("SELECTION_SOFT_MIN_DEPTH_SHARES", 1.0)

        spread_guard_penalty = 0.0
        if soft_max_spread > 0 and spread_pct > soft_max_spread:
            spread_guard_penalty = min((spread_pct - soft_max_spread) / soft_max_spread, 1.0) * 0.30

        depth_guard_penalty = 0.0
        if soft_min_depth > 0 and depth_capacity < soft_min_depth:
            depth_guard_penalty = min((soft_min_depth - depth_capacity) / soft_min_depth, 1.0) * 0.30

        guard_penalty = spread_guard_penalty + depth_guard_penalty

        net_edge_score = max(0.0, edge_score - toxicity_penalty - guard_penalty)
        liquidity_score = 0.50 * coarse_meta["vol_score"] + 0.50 * depth_score

        final_score = 100.0 * (
            0.25 * liquidity_score
            + 0.30 * net_edge_score
            + 0.20 * fill_quality_score
            + 0.10 * coarse_meta["competition_score"]
            + 0.10 * coarse_meta["lifecycle_score"]
            + 0.05 * coarse_meta["reward_score"]
        )

        breakdown = {
            "liquidity_score": round(liquidity_score, 4),
            "edge_score": round(edge_score, 4),
            "net_edge_score": round(net_edge_score, 4),
            "depth_score": round(depth_score, 4),
            "fill_quality_score": round(fill_quality_score, 4),
            "competition_score": round(coarse_meta["competition_score"], 4),
            "local_competition_score": round(local_competition_score, 4),
            "lifecycle_score": round(coarse_meta["lifecycle_score"], 4),
            "reward_score": round(coarse_meta["reward_score"], 4),
            "toxicity_penalty": round(toxicity_penalty, 4),
            "spread_guard_penalty": round(spread_guard_penalty, 4),
            "depth_guard_penalty": round(depth_guard_penalty, 4),
        }

        reason = " | ".join(
            [
                f"liq={liquidity_score:.2f}",
                f"edge={net_edge_score:.2f}",
                f"fill={fill_quality_score:.2f}",
                f"comp={local_competition_score:.2f}",
                f"life={coarse_meta['lifecycle_score']:.2f}",
                f"spread={spread_pct:.2%}",
                f"depth={depth_capacity:.1f}sh",
                "rewards✓" if coarse_meta["has_rewards"] else "rewards×",
            ]
        )

        return final_score, reason, breakdown

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _extract_yes_token_id(self, market: Dict) -> Optional[str]:
        tokens_raw = market.get("tokens") or market.get("clobTokenIds") or []

        if isinstance(tokens_raw, str):
            try:
                tokens_raw = json.loads(tokens_raw)
            except Exception:
                tokens_raw = []

        if not tokens_raw:
            return None

        if isinstance(tokens_raw[0], dict):
            for token in tokens_raw:
                if str(token.get("outcome", "")).lower() == "yes":
                    token_id = token.get("token_id")
                    if token_id:
                        return str(token_id)
            first = tokens_raw[0].get("token_id")
            return str(first) if first else None

        return str(tokens_raw[0])

    def _book_side(self, raw_book: Any, side: str) -> Any:
        if isinstance(raw_book, dict):
            return raw_book.get(side) or []
        return getattr(raw_book, side, [])

    def _book_field(self, raw_book: Any, field: str) -> Any:
        if isinstance(raw_book, dict):
            return raw_book.get(field)
        return getattr(raw_book, field, None)

    def _parse_book_levels(self, levels: Any) -> List[Tuple[float, float]]:
        parsed: List[Tuple[float, float]] = []
        for level in levels or []:
            price = 0.0
            size = 0.0

            if isinstance(level, dict):
                price = self._to_float(level.get("price") or level.get("p"))
                size = self._to_float(level.get("size") or level.get("s") or level.get("quantity"))
            elif isinstance(level, (list, tuple)) and len(level) >= 2:
                price = self._to_float(level[0])
                size = self._to_float(level[1])
            else:
                price = self._to_float(getattr(level, "price", 0))
                size = self._to_float(getattr(level, "size", 0))

            if price > 0 and size > 0:
                parsed.append((price, size))

        return parsed

    def _depth_within_bps(
        self,
        levels: List[Tuple[float, float]],
        mid: float,
        side: str,
        bps: float,
    ) -> float:
        limit = bps / 10_000.0
        total = 0.0

        ordered = levels if side == "bid" else sorted(levels, key=lambda x: x[0])
        for price, size in ordered:
            dist = ((mid - price) / mid) if side == "bid" else ((price - mid) / mid)
            if dist <= limit:
                total += size
            else:
                break

        return total

    def _levels_within_bps(
        self,
        levels: List[Tuple[float, float]],
        mid: float,
        side: str,
        bps: float,
    ) -> int:
        limit = bps / 10_000.0
        count = 0

        ordered = levels if side == "bid" else sorted(levels, key=lambda x: x[0])
        for price, _ in ordered:
            dist = ((mid - price) / mid) if side == "bid" else ((price - mid) / mid)
            if dist <= limit:
                count += 1
            else:
                break

        return count

    def _mid_score(self, mid: Optional[float]) -> float:
        if mid is None or mid <= 0 or mid >= 1:
            return 0.0

        thresh = float(self.config.PRICE_EXTREME_THRESHOLD)
        center_distance = min(mid, 1.0 - mid)

        if center_distance <= thresh:
            return 0.0

        return self._clamp((center_distance - thresh) / max(0.20 - thresh, 1e-6))

    def _spread_score(self, spread_pct: Optional[float]) -> float:
        if spread_pct is None or spread_pct <= 0:
            return 0.0

        floor = float(self.config.MIN_SPREAD_TO_ENTER) * 0.5
        ceiling = self._cfg_float("SELECTION_MAX_REASONABLE_SPREAD", 0.08)
        return self._clamp((spread_pct - floor) / max(ceiling - floor, 1e-6))

    def _lifecycle_score(self, time_to_resolution_s: Optional[float]) -> float:
        if time_to_resolution_s is None:
            return 0.55

        if time_to_resolution_s < 3600:
            return 0.0
        if time_to_resolution_s < 6 * 3600:
            return 0.25
        if time_to_resolution_s < 24 * 3600:
            return 0.60
        if time_to_resolution_s < 14 * 24 * 3600:
            return 1.0
        if time_to_resolution_s < 45 * 24 * 3600:
            return 0.70
        return 0.45

    def _time_to_resolution_seconds(self, market: Dict) -> Optional[float]:
        raw = (
            market.get("endDate")
            or market.get("end_date")
            or market.get("endTime")
            or market.get("end_time")
        )
        if not raw:
            return None

        dt = self._parse_datetime(raw)
        if not dt:
            return None

        return max(0.0, dt.timestamp() - time.time())

    def _parse_datetime(self, raw: Any) -> Optional[datetime]:
        if raw is None:
            return None

        if isinstance(raw, (int, float)):
            ts = float(raw)
            if ts > 4_000_000_000:
                ts /= 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc)

        if isinstance(raw, str):
            raw = raw.strip()
            if not raw:
                return None

            try:
                return datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                pass

            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
                except ValueError:
                    continue

        return None

    def _to_float(self, value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def _cfg_float(self, key: str, default: float) -> float:
        value = getattr(self.config, key, None)
        if value is None:
            value = os.getenv(key, str(default))
        return self._to_float(value) if value != "" else default

    def _cfg_int(self, key: str, default: int) -> int:
        value = getattr(self.config, key, None)
        if value is None:
            value = os.getenv(key, str(default))
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _clamp(self, value: float, lo: float = 0.0, hi: float = 1.0) -> float:
        return max(lo, min(value, hi))

    # ── Output shaping ───────────────────────────────────────────────────────

    def _normalise(
        self,
        market: Dict,
        score: float,
        reason: str,
        coarse_meta: Dict[str, Any],
        book_meta: Dict[str, Any],
        breakdown: Dict[str, float],
    ) -> Optional[Dict]:
        condition_id = market.get("conditionId") or market.get("condition_id") or ""
        if not condition_id:
            logger.debug("Skipping market with no conditionId")
            return None

        tokens_raw = market.get("tokens") or market.get("clobTokenIds") or []
        token_ids: List[str] = []

        if isinstance(tokens_raw, str):
            try:
                tokens_raw = json.loads(tokens_raw)
            except Exception:
                tokens_raw = []

        if tokens_raw and isinstance(tokens_raw[0], dict):
            yes_tokens = [t for t in tokens_raw if str(t.get("outcome", "")).lower() == "yes"]
            no_tokens = [t for t in tokens_raw if str(t.get("outcome", "")).lower() == "no"]
            ordered = yes_tokens + no_tokens + [
                t for t in tokens_raw if t not in yes_tokens + no_tokens
            ]
            token_ids = [str(t["token_id"]) for t in ordered if t.get("token_id")]
        else:
            token_ids = [str(t) for t in tokens_raw]

        if not token_ids:
            logger.debug("Skipping %s – no token IDs found", condition_id[:12])
            return None

        return {
            "condition_id": condition_id,
            "token_ids": token_ids,
            "label": market.get("question") or condition_id[:20],
            "score": round(score, 3),
            "reason": reason,
            "volume24h": coarse_meta["volume"],
            "best_bid": round(book_meta["best_bid"], 6),
            "best_ask": round(book_meta["best_ask"], 6),
            "time_to_resolution_s": coarse_meta["time_to_resolution_s"],
            "selection_meta": {
                "coarse_score": round(coarse_meta["coarse_score"], 3),
                "coarse": {
                    "vol_score": round(coarse_meta["vol_score"], 4),
                    "spread_score": round(coarse_meta["spread_score"], 4),
                    "mid_score": round(coarse_meta["mid_score"], 4),
                    "competition_score": round(coarse_meta["competition_score"], 4),
                    "reward_score": round(coarse_meta["reward_score"], 4),
                    "lifecycle_score": round(coarse_meta["lifecycle_score"], 4),
                },
                "book": {
                    "mid": round(book_meta["mid"], 6),
                    "spread_pct": round(book_meta["spread_pct"], 6),
                    "target_shares": round(book_meta["target_shares"], 3),
                    "bid_depth_window": round(book_meta["bid_depth_window"], 3),
                    "ask_depth_window": round(book_meta["ask_depth_window"], 3),
                    "bid_depth_5": round(book_meta["bid_depth_5"], 3),
                    "ask_depth_5": round(book_meta["ask_depth_5"], 3),
                    "depth_imbalance": round(book_meta["depth_imbalance"], 4),
                    "adaptive_depth_bps": round(book_meta["adaptive_depth_bps"], 2),
                    "adaptive_competition_bps": round(
                        book_meta["adaptive_competition_bps"], 2
                    ),
                    "near_touch_levels": int(book_meta["near_touch_levels"]),
                    "last_trade_gap_pct": round(book_meta["last_trade_gap_pct"], 6),
                },
                "score_breakdown": breakdown,
            },
        }
