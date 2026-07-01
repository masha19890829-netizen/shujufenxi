#!/usr/bin/env python
"""Score the local A-share watchlist as a research-priority queue.

The score is not a buy signal. It ranks which names deserve deeper manual
verification under the Xinwei formula: market signal, S/A evidence, post-entry
behavior, and risk controls are stored separately so the model stays auditable.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from a_stock_daily import (
    DEFAULT_DB,
    as_float,
    clamp,
    connect,
    init_db,
    now_cn,
    refresh_watchlist_metrics,
    sync_watchlist_from_recommendations,
)
from a_stock_evidence_gate import refresh_evidence_gate
from a_stock_factors import refresh_stock_factors
from a_stock_opportunity import refresh_opportunity_layer


MODEL_VERSION = "xinwei-research-priority-v0.6-opportunity"
EXPECTED_DIMENSIONS = 6


@dataclass
class ModelScore:
    trade_date: str
    code: str
    name: str
    action_bucket: str
    total_score: float
    market_score: float
    evidence_score: float
    evidence_availability_score: float
    formula_verification_score: float
    behavior_score: float
    factor_score: float
    factor_quality_score: float
    risk_score: float
    latest_candidate_score: float | None
    latest_return_pct: float | None
    latest_drawdown_pct: float | None
    evidence_item_count: int
    s_evidence_count: int
    a_evidence_count: int
    review_supported_count: int
    review_needs_review_count: int
    review_pending_count: int
    review_failed_count: int
    research_org_count: int | None
    research_report_count_180d: int | None
    detail: dict[str, Any]
    priority_rank: int | None = None


def latest_metric_date(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        """
        SELECT COALESCE(
            (SELECT MAX(trade_date) FROM watchlist_daily_metrics),
            (SELECT MAX(trade_date) FROM market_snapshot)
        ) AS trade_date
        """
    ).fetchone()
    return row["trade_date"] if row and row["trade_date"] else None


def latest_model_date(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT MAX(trade_date) AS trade_date FROM stock_model_scores").fetchone()
    return row["trade_date"] if row and row["trade_date"] else None


def load_model_inputs(conn: sqlite3.Connection, trade_date: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        WITH latest_candidate AS (
            SELECT *
            FROM (
                SELECT
                    rc.code,
                    rc.score,
                    rc.risk_flags,
                    ROW_NUMBER() OVER (
                        PARTITION BY rc.code
                        ORDER BY rc.run_id DESC, rc.rank ASC
                    ) AS rn
                FROM recommendation_candidates rc
            )
            WHERE rn = 1
        ),
        review_stats AS (
            SELECT
                code,
                SUM(CASE WHEN status IN ('supported', 'verified', 'pass') THEN 1 ELSE 0 END) AS supported_count,
                SUM(CASE WHEN status = 'needs_review' THEN 1 ELSE 0 END) AS needs_review_count,
                SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending_count,
                SUM(CASE WHEN status IN ('failed', 'rejected') THEN 1 ELSE 0 END) AS failed_count,
                SUM(
                    CASE
                        WHEN status IN ('supported', 'verified', 'pass') THEN 1.0
                        WHEN status = 'needs_review' THEN COALESCE(score, 0.4)
                        WHEN status IN ('failed', 'rejected') THEN -0.4
                        ELSE 0
                    END
                ) AS review_points,
                SUM(
                    CASE
                        WHEN dimension_id IN ('scarcity_position', 'leader_customer_binding')
                         AND status = 'pending' THEN 1
                        ELSE 0
                    END
                ) AS critical_pending_count,
                GROUP_CONCAT(CASE WHEN status IN ('supported', 'verified', 'pass') THEN dimension_name END, '|')
                    AS supported_dimensions,
                GROUP_CONCAT(CASE WHEN status = 'needs_review' THEN dimension_name END, '|')
                    AS needs_review_dimensions,
                GROUP_CONCAT(CASE WHEN status = 'pending' THEN dimension_name END, '|')
                    AS pending_dimensions,
                GROUP_CONCAT(CASE WHEN status IN ('failed', 'rejected') THEN dimension_name END, '|')
                    AS failed_dimensions
            FROM stock_xinwei_reviews
            GROUP BY code
        ),
        evidence_stats AS (
            SELECT
                code,
                COUNT(*) AS evidence_item_count,
                SUM(CASE WHEN evidence_grade = 'S' THEN 1 ELSE 0 END) AS s_evidence_count,
                SUM(CASE WHEN evidence_grade = 'A' THEN 1 ELSE 0 END) AS a_evidence_count
            FROM stock_evidence_items
            GROUP BY code
        ),
        latest_coverage AS (
            SELECT *
            FROM (
                SELECT
                    code,
                    report_count_180d,
                    org_count,
                    ROW_NUMBER() OVER (
                        PARTITION BY code
                        ORDER BY as_of_date DESC
                    ) AS rn
                FROM stock_research_coverage
            )
            WHERE rn = 1
        ),
        latest_gate AS (
            SELECT *
            FROM xinwei_gate_snapshots
            WHERE snapshot_date = ?
        )
        SELECT
            w.code,
            w.name,
            w.status,
            w.first_recommended_date,
            w.first_price,
            w.risk_flags AS watch_risk_flags,
            wm.trade_date,
            wm.price,
            wm.return_pct,
            wm.max_drawdown_pct,
            wm.days_since_first,
            wm.change_pct,
            wm.amount,
            wm.turnover_pct,
            wm.main_net_inflow,
            wm.pe_ttm,
            wm.pb,
            wm.latest_score,
            wm.industry,
            lc.score AS candidate_score,
            lc.risk_flags AS latest_risk_flags,
            COALESCE(xg.supported_count, rs.supported_count, 0) AS supported_count,
            COALESCE(xg.needs_review_count, rs.needs_review_count, 0) AS needs_review_count,
            COALESCE(xg.pending_count, rs.pending_count, 0) AS pending_count,
            COALESCE(xg.failed_count, rs.failed_count, 0) AS failed_count,
            COALESCE(xg.stale_count, 0) AS stale_count,
            COALESCE(xg.required_count, 6) AS required_count,
            xg.gate_status,
            xg.eligible_for_buy,
            xg.blocking_dimensions,
            xg.dimension_status_json,
            xg.evidence_chain_json,
            COALESCE(rs.review_points, 0) AS review_points,
            COALESCE(rs.critical_pending_count, 0) AS critical_pending_count,
            rs.supported_dimensions,
            rs.needs_review_dimensions,
            rs.pending_dimensions,
            rs.failed_dimensions,
            COALESCE(es.evidence_item_count, 0) AS evidence_item_count,
            COALESCE(es.s_evidence_count, 0) AS s_evidence_count,
            COALESCE(es.a_evidence_count, 0) AS a_evidence_count,
            lcvg.report_count_180d,
            lcvg.org_count,
            sf.composite_score AS factor_composite_score,
            sf.quality_score AS factor_quality_score,
            sf.trend_score AS factor_trend_score,
            sf.liquidity_score AS factor_liquidity_score,
            sf.flow_score AS factor_flow_score,
            sf.volatility_score AS factor_volatility_score,
            sf.valuation_score AS factor_valuation_score,
            sf.technical_score AS factor_technical_score,
            sf.rsi14 AS factor_rsi14,
            sf.macd_hist AS factor_macd_hist,
            sf.boll_position_pct AS factor_boll_position_pct,
            sf.atr14_pct AS factor_atr14_pct,
            sf.sample_days AS factor_sample_days,
            sf.return_1d_pct AS factor_return_1d_pct,
            sf.momentum_5d_pct AS factor_momentum_5d_pct,
            sf.drawdown_20d_pct AS factor_drawdown_20d_pct,
            sf.factor_json
        FROM stock_watchlist w
        LEFT JOIN watchlist_daily_metrics wm
          ON wm.code = w.code
         AND wm.trade_date = ?
        LEFT JOIN latest_candidate lc
          ON lc.code = w.code
        LEFT JOIN review_stats rs
          ON rs.code = w.code
        LEFT JOIN evidence_stats es
          ON es.code = w.code
        LEFT JOIN latest_coverage lcvg
          ON lcvg.code = w.code
        LEFT JOIN latest_gate xg
          ON xg.code = w.code
        LEFT JOIN stock_factor_daily sf
          ON sf.code = w.code
         AND sf.trade_date = ?
        ORDER BY
            CASE WHEN w.status = 'active' THEN 0 ELSE 1 END,
            w.first_recommended_date DESC,
            w.first_rank ASC
        """,
        (trade_date, trade_date, trade_date),
    ).fetchall()


def pct_score_from_candidate(value: float | None) -> float:
    if value is None:
        return 0.0
    return round(clamp(value / 100.0, 0.0, 1.0) * 100.0, 2)


def score_evidence(row: sqlite3.Row) -> tuple[float, float, float, dict[str, Any]]:
    s_count = int(row["s_evidence_count"] or 0)
    a_count = int(row["a_evidence_count"] or 0)
    coverage_reports = int(row["report_count_180d"] or 0)
    coverage_orgs = int(row["org_count"] or 0)
    supported_count = int(row["supported_count"] or 0)
    required_count = int(row["required_count"] or EXPECTED_DIMENSIONS)
    availability_score = clamp((s_count * 8 + a_count * 4 + coverage_reports * 5 + coverage_orgs * 3) / 100.0) * 100.0
    verification_score = clamp(supported_count / max(1, required_count), 0.0, 1.0) * 100.0

    evidence_score = round(0.65 * availability_score + 0.35 * verification_score, 2)
    return evidence_score, round(availability_score, 2), round(verification_score, 2), {
        "availability_score": round(availability_score, 2),
        "formula_verification_score": round(verification_score, 2),
        "supported_count": supported_count,
        "required_count": required_count,
        "s_evidence_count": s_count,
        "a_evidence_count": a_count,
        "report_count_180d": coverage_reports,
        "org_count": coverage_orgs,
    }


def split_dimensions(value: str | None) -> list[str]:
    if not value:
        return []
    seen: set[str] = set()
    dimensions: list[str] = []
    for part in str(value).split("|"):
        cleaned = part.strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            dimensions.append(cleaned)
    return dimensions


def formula_gate(row: sqlite3.Row) -> dict[str, Any]:
    dimensions = []
    if row["dimension_status_json"]:
        try:
            dimensions = json.loads(row["dimension_status_json"])
        except json.JSONDecodeError:
            dimensions = []
    supported = [d["dimension_name"] for d in dimensions if d.get("status") == "verified"]
    needs_review = [d["dimension_name"] for d in dimensions if d.get("status") == "needs_review"]
    pending = [d["dimension_name"] for d in dimensions if d.get("status") == "pending"]
    failed = [d["dimension_name"] for d in dimensions if d.get("status") == "failed"]
    stale = [d["dimension_name"] for d in dimensions if d.get("status") == "stale"]
    blocking_ids = []
    if row["blocking_dimensions"]:
        try:
            blocking_ids = json.loads(row["blocking_dimensions"])
        except json.JSONDecodeError:
            blocking_ids = []
    critical_unresolved = [
        d["dimension_name"]
        for d in dimensions
        if d.get("dimension_id") in {"scarcity_position", "leader_customer_binding"}
        and d.get("status") != "verified"
    ]
    status = row["gate_status"] or "missing_evidence"
    labels = {
        "formula_supported": "six Xinwei dimensions verified",
        "needs_manual_review": "S/A leads exist but manual verification is required",
        "missing_evidence": "S/A evidence is missing",
        "rejected": "evidence conflict or failed verification",
    }
    return {
        "status": status,
        "label": labels.get(status, status),
        "eligible_for_buy": bool(row["eligible_for_buy"]),
        "supported_dimensions": supported,
        "needs_review_dimensions": needs_review,
        "pending_dimensions": pending,
        "failed_dimensions": failed,
        "stale_dimensions": stale,
        "critical_unresolved_dimensions": critical_unresolved,
        "blocking_dimensions": blocking_ids,
        "missing_dimension_count": len(needs_review) + len(pending) + len(failed) + len(stale),
        "supported_count": int(row["supported_count"] or 0),
        "required_count": int(row["required_count"] or EXPECTED_DIMENSIONS),
        "dimension_status": dimensions,
    }


def score_behavior(row: sqlite3.Row) -> tuple[float, dict[str, Any]]:
    return_pct = as_float(row["return_pct"])
    drawdown_pct = as_float(row["max_drawdown_pct"])
    change_pct = as_float(row["change_pct"])
    main_net_inflow = as_float(row["main_net_inflow"])

    score = 50.0
    if return_pct is not None:
        if return_pct >= 0:
            score += min(30.0, return_pct * 1.2)
        else:
            score -= min(28.0, abs(return_pct) * 1.5)
    if drawdown_pct is not None:
        if drawdown_pct >= -3:
            score += 5.0
        else:
            score -= min(30.0, abs(drawdown_pct) * 1.35)
    if change_pct is not None:
        score += max(-5.0, min(5.0, change_pct * 0.4))
    if main_net_inflow is not None:
        score += max(-4.0, min(4.0, main_net_inflow / 50_000_000))

    return round(clamp(score, 0.0, 100.0), 2), {
        "return_pct": return_pct,
        "max_drawdown_pct": drawdown_pct,
        "change_pct": change_pct,
        "main_net_inflow": main_net_inflow,
    }


def score_factor(row: sqlite3.Row) -> tuple[float, float, dict[str, Any]]:
    raw_factor_score = as_float(row["factor_composite_score"])
    quality_score = as_float(row["factor_quality_score"])
    if raw_factor_score is None:
        return 50.0, 0.0, {"available": False, "reason": "no factor row"}
    quality_weight = clamp((quality_score or 0.0) / 100.0, 0.0, 1.0)
    factor_score = 50.0 + (raw_factor_score - 50.0) * quality_weight
    detail = {
        "available": True,
        "raw_composite_score": round(raw_factor_score, 2),
        "quality_adjusted_score": round(factor_score, 2),
        "quality_score": quality_score or 0.0,
        "sample_days": row["factor_sample_days"],
        "trend_score": row["factor_trend_score"],
        "liquidity_score": row["factor_liquidity_score"],
        "flow_score": row["factor_flow_score"],
        "volatility_score": row["factor_volatility_score"],
        "valuation_score": row["factor_valuation_score"],
        "technical_score": row["factor_technical_score"],
        "rsi14": row["factor_rsi14"],
        "macd_hist": row["factor_macd_hist"],
        "boll_position_pct": row["factor_boll_position_pct"],
        "atr14_pct": row["factor_atr14_pct"],
        "return_1d_pct": row["factor_return_1d_pct"],
        "momentum_5d_pct": row["factor_momentum_5d_pct"],
        "drawdown_20d_pct": row["factor_drawdown_20d_pct"],
    }
    parsed = None
    if row["factor_json"]:
        try:
            parsed = json.loads(row["factor_json"])
        except json.JSONDecodeError:
            parsed = None
    if parsed:
        detail["factor_json"] = parsed
    return round(factor_score, 2), round(quality_score or 0.0, 2), detail


def score_risk(row: sqlite3.Row, market_score: float) -> tuple[float, list[str]]:
    score = 100.0
    reasons: list[str] = []
    return_pct = as_float(row["return_pct"])
    drawdown_pct = as_float(row["max_drawdown_pct"])
    pe_ttm = as_float(row["pe_ttm"])
    pb = as_float(row["pb"])
    pending_count = int(row["pending_count"] or 0)
    critical_pending = int(row["critical_pending_count"] or 0)
    failed_count = int(row["failed_count"] or 0)
    evidence_count = int(row["evidence_item_count"] or 0)
    risk_text = " ".join(
        str(v or "")
        for v in (row["watch_risk_flags"], row["latest_risk_flags"])
    )

    if failed_count:
        penalty = min(60.0, failed_count * 30.0)
        score -= penalty
        reasons.append(f"{failed_count} dimensions failed verification")
    if critical_pending:
        penalty = critical_pending * 8.0
        score -= penalty
        reasons.append("scarcity/customer binding still pending")
    elif pending_count >= 5:
        score -= 10.0
        reasons.append("most Xinwei dimensions still pending")
    elif pending_count >= 3:
        score -= 5.0
        reasons.append("several Xinwei dimensions still pending")

    if evidence_count == 0:
        score -= 8.0
        reasons.append("no S/A evidence collected")
    if return_pct is not None and return_pct < -10:
        score -= 15.0
        reasons.append("post-entry return below -10%")
    elif return_pct is not None and return_pct < 0:
        score -= 5.0
        reasons.append("post-entry return below 0")
    if drawdown_pct is not None and drawdown_pct < -15:
        score -= 20.0
        reasons.append("drawdown worse than -15%")
    elif drawdown_pct is not None and drawdown_pct < -8:
        score -= 10.0
        reasons.append("drawdown worse than -8%")
    if pe_ttm is not None and (pe_ttm <= 0 or pe_ttm > 150):
        score -= 10.0
        reasons.append("PE valuation needs caution")
    if pb is not None and pb > 15:
        score -= 10.0
        reasons.append("PB above 15")
    elif pb is not None and pb > 10:
        score -= 6.0
        reasons.append("PB above 10")
    if market_score < 45:
        score -= 8.0
        reasons.append("latest market score below 45")
    if "ST" in risk_text.upper() or "*ST" in risk_text.upper():
        score -= 30.0
        reasons.append("ST risk flag")

    return round(clamp(score, 0.0, 100.0), 2), reasons


def choose_bucket(
    total_score: float,
    market_score: float,
    evidence_availability_score: float,
    behavior_score: float,
    factor_score: float,
    risk_score: float,
    row: sqlite3.Row,
    gate_detail: dict[str, Any],
) -> str:
    drawdown_pct = as_float(row["max_drawdown_pct"])
    return_pct = as_float(row["return_pct"])
    failed_count = int(row["failed_count"] or 0)
    gate_status = gate_detail.get("status")
    critical_unresolved = gate_detail.get("critical_unresolved_dimensions") or []
    eligible_for_buy = bool(gate_detail.get("eligible_for_buy"))
    if eligible_for_buy and gate_status == "formula_supported":
        return "formula_supported"
    if failed_count or risk_score < 40 or (
        drawdown_pct is not None and return_pct is not None and drawdown_pct < -12 and return_pct < -6
    ):
        return "risk_watch"
    if (
        critical_unresolved
        and evidence_availability_score >= 35
        and market_score >= 50
        and factor_score >= 45
        and risk_score >= 55
        and total_score >= 50
    ):
        return "blocked_by_evidence"
    if evidence_availability_score >= 35 and market_score >= 50 and factor_score >= 45 and risk_score >= 55 and total_score >= 50:
        return "deep_research"
    if market_score >= 60 and evidence_availability_score < 20 and risk_score >= 50:
        return "wait_evidence"
    if total_score >= 45 and behavior_score >= 35 and factor_score >= 35 and risk_score >= 45:
        return "track"
    return "archive_watch"


def build_score(row: sqlite3.Row, trade_date: str) -> ModelScore:
    candidate_score = as_float(row["latest_score"])
    if candidate_score is None:
        candidate_score = as_float(row["candidate_score"])
    market_score = pct_score_from_candidate(candidate_score)
    evidence_score, evidence_availability_score, formula_verification_score, evidence_detail = score_evidence(row)
    gate_detail = formula_gate(row)
    behavior_score, behavior_detail = score_behavior(row)
    factor_score, factor_quality_score, factor_detail = score_factor(row)
    risk_score, risk_reasons = score_risk(row, market_score)

    total_score = round(
        0.30 * market_score
        + 0.30 * evidence_score
        + 0.15 * factor_score
        + 0.10 * behavior_score
        + 0.15 * risk_score,
        2,
    )
    action_bucket = choose_bucket(
        total_score,
        market_score,
        evidence_availability_score,
        behavior_score,
        factor_score,
        risk_score,
        row,
        gate_detail,
    )
    detail = {
        "model_version": MODEL_VERSION,
        "weights": {
            "market_score": 0.30,
            "evidence_score": 0.30,
            "factor_score": 0.15,
            "behavior_score": 0.10,
            "risk_score": 0.15,
        },
        "evidence_detail": evidence_detail,
        "formula_gate": gate_detail,
        "factor_detail": factor_detail,
        "behavior_detail": behavior_detail,
        "risk_reasons": risk_reasons,
        "bucket_note": bucket_label(action_bucket),
    }
    return ModelScore(
        trade_date=trade_date,
        code=row["code"],
        name=row["name"],
        action_bucket=action_bucket,
        total_score=total_score,
        market_score=market_score,
        evidence_score=evidence_score,
        evidence_availability_score=evidence_availability_score,
        formula_verification_score=formula_verification_score,
        behavior_score=behavior_score,
        factor_score=factor_score,
        factor_quality_score=factor_quality_score,
        risk_score=risk_score,
        latest_candidate_score=candidate_score,
        latest_return_pct=as_float(row["return_pct"]),
        latest_drawdown_pct=as_float(row["max_drawdown_pct"]),
        evidence_item_count=int(row["evidence_item_count"] or 0),
        s_evidence_count=int(row["s_evidence_count"] or 0),
        a_evidence_count=int(row["a_evidence_count"] or 0),
        review_supported_count=int(row["supported_count"] or 0),
        review_needs_review_count=int(row["needs_review_count"] or 0),
        review_pending_count=int(row["pending_count"] or 0),
        review_failed_count=int(row["failed_count"] or 0),
        research_org_count=row["org_count"],
        research_report_count_180d=row["report_count_180d"],
        detail=detail,
    )


def bucket_label(bucket: str) -> str:
    return {
        "formula_supported": "formula supported: all six dimensions are verified, still subject to portfolio risk limits",
        "blocked_by_evidence": "blocked by evidence: strong research signal but critical Xinwei proof is unresolved",
        "deep_research": "deep research: evidence plus market signal are both worth manual verification",
        "track": "track: keep in the watchlist and wait for stronger evidence or behavior",
        "wait_evidence": "wait for evidence: market signal is present but Xinwei proof is thin",
        "risk_watch": "risk watch: verification, drawdown, or valuation risk is elevated",
        "archive_watch": "archive watch: low priority until new evidence appears",
    }.get(bucket, bucket)


def bucket_sort_key(score: ModelScore) -> tuple[int, float]:
    order = {
        "formula_supported": 0,
        "deep_research": 1,
        "blocked_by_evidence": 2,
        "track": 3,
        "wait_evidence": 4,
        "risk_watch": 5,
        "archive_watch": 6,
    }
    return (order.get(score.action_bucket, 99), -score.total_score)


def save_scores(conn: sqlite3.Connection, scores: list[ModelScore]) -> None:
    now = now_cn().isoformat(timespec="seconds")
    rows = []
    for score in scores:
        rows.append(
            (
                score.trade_date,
                score.code,
                score.name,
                MODEL_VERSION,
                score.priority_rank,
                score.action_bucket,
                score.total_score,
                score.market_score,
                score.evidence_score,
                score.evidence_availability_score,
                score.formula_verification_score,
                score.behavior_score,
                score.factor_score,
                score.factor_quality_score,
                score.risk_score,
                score.latest_candidate_score,
                score.latest_return_pct,
                score.latest_drawdown_pct,
                score.evidence_item_count,
                score.s_evidence_count,
                score.a_evidence_count,
                score.review_supported_count,
                score.review_needs_review_count,
                score.review_pending_count,
                score.review_failed_count,
                score.research_org_count,
                score.research_report_count_180d,
                json.dumps(score.detail, ensure_ascii=False, separators=(",", ":")),
                now,
            )
        )
    conn.executemany(
        """
        INSERT INTO stock_model_scores(
            trade_date, code, name, model_version, priority_rank, action_bucket,
            total_score, market_score, evidence_score,
            evidence_availability_score, formula_verification_score, behavior_score,
            factor_score, factor_quality_score, risk_score,
            latest_candidate_score, latest_return_pct, latest_drawdown_pct,
            evidence_item_count, s_evidence_count, a_evidence_count,
            review_supported_count, review_needs_review_count, review_pending_count,
            review_failed_count, research_org_count, research_report_count_180d,
            score_json, created_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(trade_date, code) DO UPDATE SET
            name=excluded.name,
            model_version=excluded.model_version,
            priority_rank=excluded.priority_rank,
            action_bucket=excluded.action_bucket,
            total_score=excluded.total_score,
            market_score=excluded.market_score,
            evidence_score=excluded.evidence_score,
            evidence_availability_score=excluded.evidence_availability_score,
            formula_verification_score=excluded.formula_verification_score,
            behavior_score=excluded.behavior_score,
            factor_score=excluded.factor_score,
            factor_quality_score=excluded.factor_quality_score,
            risk_score=excluded.risk_score,
            latest_candidate_score=excluded.latest_candidate_score,
            latest_return_pct=excluded.latest_return_pct,
            latest_drawdown_pct=excluded.latest_drawdown_pct,
            evidence_item_count=excluded.evidence_item_count,
            s_evidence_count=excluded.s_evidence_count,
            a_evidence_count=excluded.a_evidence_count,
            review_supported_count=excluded.review_supported_count,
            review_needs_review_count=excluded.review_needs_review_count,
            review_pending_count=excluded.review_pending_count,
            review_failed_count=excluded.review_failed_count,
            research_org_count=excluded.research_org_count,
            research_report_count_180d=excluded.research_report_count_180d,
            score_json=excluded.score_json,
            created_at=excluded.created_at
        """,
        rows,
    )


def refresh_model_scores(conn: sqlite3.Connection, trade_date: str | None = None) -> list[ModelScore]:
    init_db(conn)
    sync_watchlist_from_recommendations(conn)
    refresh_watchlist_metrics(conn)
    trade_date = trade_date or latest_metric_date(conn)
    if not trade_date:
        raise RuntimeError("No market snapshot or watchlist metrics are available.")
    refresh_stock_factors(conn, trade_date)
    refresh_evidence_gate(conn, trade_date)
    input_rows = load_model_inputs(conn, trade_date)
    scores = [build_score(row, trade_date) for row in input_rows]
    scores.sort(key=bucket_sort_key)
    for idx, score in enumerate(scores, start=1):
        score.priority_rank = idx
    save_scores(conn, scores)
    refresh_opportunity_layer(conn, trade_date)
    return scores


def command_refresh(args: argparse.Namespace) -> None:
    with connect(args.db) as conn:
        scores = refresh_model_scores(conn, args.date)
        conn.commit()
    buckets: dict[str, int] = {}
    for score in scores:
        buckets[score.action_bucket] = buckets.get(score.action_bucket, 0) + 1
    bucket_text = ", ".join(f"{key}={value}" for key, value in sorted(buckets.items()))
    date_text = scores[0].trade_date if scores else args.date or "-"
    print(f"Refreshed {len(scores)} model scores for {date_text}. {bucket_text}")


def command_show(args: argparse.Namespace) -> None:
    with connect(args.db) as conn:
        init_db(conn)
        date = args.date or latest_model_date(conn) or latest_metric_date(conn)
        if not date:
            print("No model score date found.")
            return
        params: list[Any] = [date]
        bucket_clause = ""
        if args.bucket:
            bucket_clause = "AND action_bucket = ?"
            params.append(args.bucket)
        params.append(args.top)
        rows = conn.execute(
            f"""
            SELECT *
            FROM stock_model_scores
            WHERE trade_date = ?
              {bucket_clause}
            ORDER BY priority_rank ASC
            LIMIT ?
            """,
            params,
        ).fetchall()
    print(f"Model scores date={date} version={MODEL_VERSION}")
    for row in rows:
        try:
            detail = json.loads(row["score_json"] or "{}")
        except json.JSONDecodeError:
            detail = {}
        gate = detail.get("formula_gate") or {}
        gate_text = gate.get("label") or gate.get("status") or "-"
        buy_text = "yes" if gate.get("eligible_for_buy") else "no"
        missing = gate.get("missing_dimension_count")
        missing_text = f" missing={missing}" if missing is not None else ""
        print(
            f"{row['priority_rank']:>3}. {row['code']} {row['name']} "
            f"{row['action_bucket']} total={row['total_score']:.2f} "
            f"market={row['market_score']:.2f} evidence={row['evidence_score']:.2f} "
            f"avail={row['evidence_availability_score']:.2f} verify={row['formula_verification_score']:.2f} "
            f"factor={row['factor_score']:.2f} quality={row['factor_quality_score']:.2f} "
            f"behavior={row['behavior_score']:.2f} risk={row['risk_score']:.2f} "
            f"gate={gate_text} buy_eligible={buy_text}{missing_text}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Refresh and inspect A-share research-priority scores")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite database path")
    parser.add_argument("--date", help="Score date, defaults to latest persisted model date")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("refresh", help="Refresh model scores for the current watchlist")

    show = sub.add_parser("show", help="Print ranked model scores")
    show.add_argument("--top", type=int, default=20, help="Number of rows to show")
    show.add_argument("--bucket", help="Optional action bucket filter")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "refresh":
            command_refresh(args)
        elif args.command == "show":
            command_show(args)
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
