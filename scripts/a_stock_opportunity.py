#!/usr/bin/env python
"""Xinwei opportunity layer.

This module turns the existing local research ledger into a product-facing
"next Xinwei Communication" opportunity radar. It is deliberately conservative:
market score never creates a buy label by itself, and needs_review never counts
as verified evidence.
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any


CN_TZ = timezone(timedelta(hours=8))
OPPORTUNITY_VERSION = "xinwei-opportunity-v0.6"
EVENT_SOURCE = "evidence_gate_v0.6"
EXPECTED_DIMENSIONS = 6
PROBABILITY_RETURNS = [-50.0, -30.0, -10.0, 20.0, 50.0, 100.0, 200.0]

DIMENSION_LABELS = {
    "industry_inflection": "产业拐点",
    "scarcity_position": "稀缺卡位",
    "leader_customer_binding": "龙头客户绑定",
    "capacity_order_expansion": "订单/产能扩张",
    "earnings_inflection": "业绩拐点",
    "expectation_gap": "预期差",
}

EVENT_TYPE_BY_DIMENSION = {
    "industry_inflection": "industry_inflection",
    "scarcity_position": "scarcity_position",
    "leader_customer_binding": "customer_validation",
    "capacity_order_expansion": "order_capacity",
    "earnings_inflection": "earnings_validation",
    "expectation_gap": "expectation_gap",
}

GRADE_EVENT_WEIGHT = {
    "S": 35.0,
    "A": 25.0,
    "B": 14.0,
    "C": 8.0,
}


OPPORTUNITY_SCHEMA = """
CREATE TABLE IF NOT EXISTS stock_case_library (
    case_id TEXT PRIMARY KEY,
    case_name TEXT NOT NULL,
    code TEXT,
    name TEXT,
    period_start TEXT,
    period_end TEXT,
    thesis TEXT NOT NULL,
    industry_path TEXT,
    customer_binding TEXT,
    order_capacity_path TEXT,
    earnings_path TEXT,
    valuation_path TEXT,
    max_drawdown_pct REAL,
    max_return_pct REAL,
    realization_days INTEGER,
    event_pattern_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stock_thesis_events (
    event_date TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    event_type TEXT NOT NULL,
    event_grade TEXT NOT NULL,
    title TEXT NOT NULL,
    detail TEXT,
    source TEXT NOT NULL,
    source_url TEXT,
    catalyst_date TEXT,
    dimension_id TEXT,
    raw_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(event_date, code, event_type, title, source)
);

CREATE INDEX IF NOT EXISTS idx_stock_thesis_events_code_date
    ON stock_thesis_events(code, event_date, event_type);

CREATE INDEX IF NOT EXISTS idx_stock_thesis_events_catalyst
    ON stock_thesis_events(catalyst_date, event_grade);

CREATE TABLE IF NOT EXISTS stock_probability_models (
    trade_date TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    model_version TEXT NOT NULL,
    p1 REAL NOT NULL,
    p2 REAL NOT NULL,
    p3 REAL NOT NULL,
    p4 REAL NOT NULL,
    p5 REAL NOT NULL,
    p6 REAL NOT NULL,
    p7 REAL NOT NULL,
    ev_pct REAL NOT NULL,
    win_rate REAL NOT NULL,
    odds REAL,
    half_kelly REAL NOT NULL,
    position_cap_pct REAL NOT NULL,
    decision_label TEXT NOT NULL,
    missing_triggers TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (trade_date, code)
);

CREATE INDEX IF NOT EXISTS idx_stock_probability_models_decision
    ON stock_probability_models(trade_date, decision_label, ev_pct);

CREATE TABLE IF NOT EXISTS stock_opportunity_scores (
    trade_date TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    model_version TEXT NOT NULL,
    opportunity_rank INTEGER,
    opportunity_score REAL NOT NULL,
    evidence_score REAL NOT NULL,
    case_similarity_score REAL NOT NULL,
    catalyst_score REAL NOT NULL,
    ev_score REAL NOT NULL,
    risk_penalty REAL NOT NULL,
    decision_label TEXT NOT NULL,
    position_cap_pct REAL NOT NULL,
    thesis TEXT,
    key_gap TEXT,
    catalyst_summary TEXT,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (trade_date, code)
);

CREATE INDEX IF NOT EXISTS idx_stock_opportunity_scores_rank
    ON stock_opportunity_scores(trade_date, opportunity_rank);

CREATE INDEX IF NOT EXISTS idx_stock_opportunity_scores_decision
    ON stock_opportunity_scores(trade_date, decision_label, opportunity_score DESC);
"""


def now_text() -> str:
    return datetime.now(CN_TZ).isoformat(timespec="seconds")


def parse_json(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def as_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except ValueError:
        return None


def plus_days(value: str | None, days: int) -> str | None:
    parsed = parse_date(value)
    if not parsed:
        return None
    return (parsed + timedelta(days=days)).isoformat()


def ensure_opportunity_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(OPPORTUNITY_SCHEMA)
    seed_case_library(conn)


def seed_case_library(conn: sqlite3.Connection) -> None:
    now = now_text()
    pattern = {
        "definition": "产业从0到1，证据逐步闭环，市场认知滞后，90天内有可验证催化。",
        "required_dimensions": list(DIMENSION_LABELS.keys()),
        "milestones": [
            "产业主题出现",
            "稀缺卡位被验证",
            "龙头客户或核心供应链绑定",
            "订单/产能进入兑现",
            "扣非利润或收入斜率改善",
            "市场预期仍未充分定价",
        ],
        "risk_lessons": [
            "没有客户/订单验证时不把题材当买点",
            "needs_review只能进入观察或等证据",
            "高估值阶段必须用仓位和止损约束",
        ],
    }
    conn.execute(
        """
        INSERT INTO stock_case_library(
            case_id, case_name, code, name, period_start, period_end, thesis,
            industry_path, customer_binding, order_capacity_path, earnings_path,
            valuation_path, max_drawdown_pct, max_return_pct, realization_days,
            event_pattern_json, created_at, updated_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(case_id) DO UPDATE SET
            case_name=excluded.case_name,
            thesis=excluded.thesis,
            industry_path=excluded.industry_path,
            customer_binding=excluded.customer_binding,
            order_capacity_path=excluded.order_capacity_path,
            earnings_path=excluded.earnings_path,
            valuation_path=excluded.valuation_path,
            event_pattern_json=excluded.event_pattern_json,
            updated_at=excluded.updated_at
        """,
        (
            "xinwei_communication_0_to_1",
            "信维通信0到1产业拐点样本",
            "300136",
            "信维通信",
            "2012-01-01",
            "2015-12-31",
            "射频/天线小型化产业趋势下，客户、订单、产能和业绩逐步闭环，市场预期随后重估。",
            "消费电子射频/天线从非核心件变成高价值增量环节。",
            "绑定头部终端客户和核心供应链认证。",
            "订单放量后扩产，产能兑现支撑收入斜率。",
            "收入和扣非利润拐点验证产业命题。",
            "证据闭环后估值从低认知向成长股重估。",
            -35.0,
            800.0,
            720,
            json_dumps(pattern),
            now,
            now,
        ),
    )


def latest_model_date(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT MAX(trade_date) AS trade_date FROM stock_model_scores").fetchone()
    return row["trade_date"] if row and row["trade_date"] else None


def latest_gate_date(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT MAX(snapshot_date) AS snapshot_date FROM xinwei_gate_snapshots").fetchone()
    return row["snapshot_date"] if row and row["snapshot_date"] else None


def latest_opportunity_date(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT MAX(trade_date) AS trade_date FROM stock_opportunity_scores").fetchone()
    return row["trade_date"] if row and row["trade_date"] else None


def event_type_for_dimension(dimension_id: str | None) -> str:
    return EVENT_TYPE_BY_DIMENSION.get(dimension_id or "", "evidence_event")


def catalyst_date_for_evidence(snapshot_date: str, evidence_date: str | None) -> str:
    snapshot_day = parse_date(snapshot_date) or datetime.now(CN_TZ).date()
    evidence_day = parse_date(evidence_date)
    if evidence_day and evidence_day > snapshot_day:
        return evidence_day.isoformat()
    return (snapshot_day + timedelta(days=90)).isoformat()


def refresh_thesis_events(
    conn: sqlite3.Connection,
    snapshot_date: str | None = None,
    codes: list[str] | None = None,
) -> int:
    ensure_opportunity_schema(conn)
    snapshot_date = snapshot_date or latest_gate_date(conn)
    if not snapshot_date:
        return 0
    params: list[Any] = [snapshot_date]
    code_clause = ""
    if codes:
        code_clause = f"AND code IN ({','.join('?' for _ in codes)})"
        params.extend(codes)
    gate_rows = conn.execute(
        f"""
        SELECT snapshot_date, code, name, blocking_dimensions, dimension_status_json
        FROM xinwei_gate_snapshots
        WHERE snapshot_date = ?
          {code_clause}
        """,
        params,
    ).fetchall()
    if not gate_rows:
        return 0

    code_values = [row["code"] for row in gate_rows]
    placeholders = ",".join("?" for _ in code_values)
    links = conn.execute(
        f"""
        SELECT
            code, name, dimension_id, dimension_name, evidence_grade,
            evidence_status, match_reason, match_keywords, source,
            evidence_type, title, evidence_date, source_url, is_manual_confirmed
        FROM xinwei_evidence_links
        WHERE code IN ({placeholders})
        ORDER BY code, dimension_id,
            CASE evidence_grade WHEN 'S' THEN 0 WHEN 'A' THEN 1 WHEN 'B' THEN 2 ELSE 9 END,
            evidence_date DESC
        """,
        code_values,
    ).fetchall()
    now = now_text()
    event_rows: list[tuple[Any, ...]] = []
    seen_links: set[tuple[str, str, str]] = set()
    for link in links:
        key = (link["code"], link["dimension_id"], link["title"])
        if key in seen_links:
            continue
        seen_links.add(key)
        status = link["evidence_status"] or "needs_review"
        grade = link["evidence_grade"] or "C"
        event_grade = grade if status == "verified" or int(link["is_manual_confirmed"] or 0) else "needs_review"
        event_date = link["evidence_date"] or snapshot_date
        detail = (
            f"{link['dimension_name']} evidence_status={status}; "
            f"needs_review不算通过；{link['match_reason'] or ''}"
        )
        raw = {
            "source_module": OPPORTUNITY_VERSION,
            "dimension_id": link["dimension_id"],
            "dimension_name": link["dimension_name"],
            "evidence_status": status,
            "evidence_grade": grade,
            "match_keywords": parse_json(link["match_keywords"], []),
            "original_source": link["source"],
            "evidence_type": link["evidence_type"],
        }
        event_rows.append(
            (
                event_date,
                link["code"],
                link["name"] or "",
                event_type_for_dimension(link["dimension_id"]),
                event_grade,
                (link["title"] or "").strip()[:180] or f"{link['dimension_name']}证据",
                detail[:800],
                EVENT_SOURCE,
                link["source_url"],
                catalyst_date_for_evidence(snapshot_date, link["evidence_date"]),
                link["dimension_id"],
                json_dumps(raw),
                now,
            )
        )

    for gate in gate_rows:
        blocking = parse_json(gate["blocking_dimensions"], []) or []
        dimensions = {
            item.get("dimension_id"): item
            for item in (parse_json(gate["dimension_status_json"], []) or [])
            if isinstance(item, dict)
        }
        for dimension_id in blocking:
            dimension = dimensions.get(dimension_id, {})
            label = dimension.get("dimension_name") or DIMENSION_LABELS.get(dimension_id, dimension_id)
            raw = {
                "source_module": OPPORTUNITY_VERSION,
                "dimension_id": dimension_id,
                "dimension_status": dimension.get("status", "pending"),
                "reason": "blocking_dimension",
            }
            event_rows.append(
                (
                    snapshot_date,
                    gate["code"],
                    gate["name"],
                    "evidence_gap",
                    "gap",
                    f"等待{label}S/A证据",
                    "该维度未形成verified闭环，只能等证据/观察，不能作为买入资格。",
                    EVENT_SOURCE,
                    None,
                    plus_days(snapshot_date, 30),
                    dimension_id,
                    json_dumps(raw),
                    now,
                )
            )

    if not event_rows:
        return 0
    conn.executemany(
        """
        INSERT INTO stock_thesis_events(
            event_date, code, name, event_type, event_grade, title, detail,
            source, source_url, catalyst_date, dimension_id, raw_json, created_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(event_date, code, event_type, title, source) DO UPDATE SET
            name=excluded.name,
            event_grade=excluded.event_grade,
            detail=excluded.detail,
            source_url=excluded.source_url,
            catalyst_date=excluded.catalyst_date,
            dimension_id=excluded.dimension_id,
            raw_json=excluded.raw_json,
            created_at=excluded.created_at
        """,
        event_rows,
    )
    return len(event_rows)


def normalize_probabilities(values: list[float]) -> list[float]:
    clean = [max(0.0, float(value)) for value in values]
    total = sum(clean)
    if total <= 0:
        return [1 / len(values)] * len(values)
    return [value / total for value in clean]


def shift_probability(values: list[float], from_indexes: list[int], to_indexes: list[int], amount: float) -> list[float]:
    values = values[:]
    amount = max(0.0, amount)
    available = min(amount, sum(values[index] for index in from_indexes))
    if available <= 0:
        return values
    from_total = sum(values[index] for index in from_indexes) or 1.0
    to_total = len(to_indexes) or 1
    for index in from_indexes:
        values[index] -= available * values[index] / from_total
    for index in to_indexes:
        values[index] += available / to_total
    return normalize_probabilities(values)


def compute_probability_model(
    row: sqlite3.Row,
    provisional_label: str,
    catalyst_count: int,
    catalyst_score: float,
) -> dict[str, Any]:
    risk_score = as_float(row["risk_score"], 50.0)
    formula_score = as_float(row["formula_verification_score"], 0.0)
    evidence_score = as_float(row["evidence_availability_score"], 0.0)
    if provisional_label == "可买":
        probabilities = [0.04, 0.06, 0.10, 0.25, 0.25, 0.20, 0.10]
    elif provisional_label == "等证据":
        probabilities = [0.08, 0.12, 0.20, 0.28, 0.18, 0.10, 0.04]
    elif provisional_label == "只观察":
        probabilities = [0.12, 0.18, 0.30, 0.25, 0.10, 0.04, 0.01]
    else:
        probabilities = [0.20, 0.25, 0.30, 0.18, 0.05, 0.015, 0.005]

    probabilities = normalize_probabilities(probabilities)
    if formula_score >= 80 and evidence_score >= 60:
        probabilities = shift_probability(probabilities, [0, 1, 2], [4, 5, 6], 0.04)
    if catalyst_count > 0 and catalyst_score >= 50:
        probabilities = shift_probability(probabilities, [2], [4, 5], 0.03)
    if risk_score < 55:
        probabilities = shift_probability(probabilities, [4, 5, 6], [0, 1], 0.05)
    if risk_score < 40:
        probabilities = shift_probability(probabilities, [3, 4, 5, 6], [0, 1, 2], 0.08)

    ev_pct = sum(p * r for p, r in zip(probabilities, PROBABILITY_RETURNS))
    win_rate = sum(probabilities[index] for index, value in enumerate(PROBABILITY_RETURNS) if value > 0)
    positive_expected = sum(
        probabilities[index] * value for index, value in enumerate(PROBABILITY_RETURNS) if value > 0
    )
    negative_expected = abs(
        sum(probabilities[index] * value for index, value in enumerate(PROBABILITY_RETURNS) if value < 0)
    )
    avg_win = positive_expected / win_rate if win_rate > 0 else 0.0
    loss_rate = max(0.0, 1.0 - win_rate)
    avg_loss = negative_expected / loss_rate if loss_rate > 0 else 0.0
    odds = avg_win / avg_loss if avg_loss > 0 else None
    kelly = ((odds * win_rate - loss_rate) / odds) if odds and odds > 0 else 0.0
    half_kelly = max(0.0, min(kelly / 2.0, 0.30))
    raw_position_cap_pct = min(15.0, half_kelly * 100.0)
    return {
        "p1": round(probabilities[0], 4),
        "p2": round(probabilities[1], 4),
        "p3": round(probabilities[2], 4),
        "p4": round(probabilities[3], 4),
        "p5": round(probabilities[4], 4),
        "p6": round(probabilities[5], 4),
        "p7": round(probabilities[6], 4),
        "ev_pct": round(ev_pct, 2),
        "win_rate": round(win_rate, 4),
        "odds": round(odds, 4) if odds is not None and math.isfinite(odds) else None,
        "half_kelly": round(half_kelly, 4),
        "raw_position_cap_pct": round(raw_position_cap_pct, 2),
    }


def build_missing_triggers(
    row: sqlite3.Row,
    catalyst_count: int,
    ev_pct: float,
    risk_score: float,
) -> list[str]:
    triggers: list[str] = []
    eligible = bool(row["eligible_for_buy"])
    if not eligible:
        triggers.append("六项公式未全部verified")
    if int(row["review_needs_review_count"] or 0) > 0:
        triggers.append("存在needs_review，不能当作通过")
    if int(row["s_evidence_count"] or 0) + int(row["a_evidence_count"] or 0) <= 0:
        triggers.append("缺少S/A级证据")
    if catalyst_count <= 0:
        triggers.append("未来90天催化不足")
    if ev_pct <= 0:
        triggers.append("EV不为正")
    if risk_score < 55:
        triggers.append("风险分未达标")
    if row["action_bucket"] in {"risk_watch", "archive_watch"}:
        triggers.append("模型列为风险观察或低优先级")
    return triggers


def provisional_decision(row: sqlite3.Row, catalyst_count: int) -> str:
    eligible = bool(row["eligible_for_buy"])
    risk_score = as_float(row["risk_score"], 50.0)
    action_bucket = row["action_bucket"]
    formula_score = as_float(row["formula_verification_score"], 0.0)
    evidence_score = as_float(row["evidence_availability_score"], 0.0)
    if action_bucket in {"risk_watch", "archive_watch"} or risk_score < 35:
        return "排除"
    if eligible and catalyst_count > 0 and risk_score >= 55:
        return "可买"
    if formula_score > 0 or evidence_score >= 35 or action_bucket in {"blocked_by_evidence", "deep_research"}:
        return "等证据"
    return "只观察"


def final_decision(row: sqlite3.Row, catalyst_count: int, ev_pct: float, risk_score: float) -> str:
    action_bucket = row["action_bucket"]
    eligible = bool(row["eligible_for_buy"])
    if action_bucket in {"risk_watch", "archive_watch"} or risk_score < 35:
        return "排除"
    if eligible and catalyst_count > 0 and ev_pct > 0 and risk_score >= 55:
        return "可买"
    if (
        int(row["review_needs_review_count"] or 0) > 0
        or as_float(row["formula_verification_score"], 0.0) > 0
        or as_float(row["evidence_availability_score"], 0.0) >= 35
        or action_bucket in {"blocked_by_evidence", "deep_research"}
    ):
        return "等证据"
    return "只观察"


def evidence_grade_counts(conn: sqlite3.Connection, codes: list[str]) -> dict[str, dict[str, int]]:
    if not codes:
        return {}
    rows = conn.execute(
        f"""
        SELECT code, evidence_grade, COUNT(*) AS n
        FROM stock_evidence_items
        WHERE code IN ({','.join('?' for _ in codes)})
        GROUP BY code, evidence_grade
        """,
        codes,
    ).fetchall()
    result: dict[str, dict[str, int]] = {}
    for row in rows:
        result.setdefault(row["code"], {})[row["evidence_grade"] or ""] = int(row["n"] or 0)
    return result


def thesis_event_stats(conn: sqlite3.Connection, codes: list[str], trade_date: str) -> dict[str, dict[str, Any]]:
    if not codes:
        return {}
    end_date = plus_days(trade_date, 90) or trade_date
    rows = conn.execute(
        f"""
        SELECT code, event_type, event_grade, title, catalyst_date, dimension_id
        FROM stock_thesis_events
        WHERE code IN ({','.join('?' for _ in codes)})
          AND catalyst_date IS NOT NULL
          AND catalyst_date BETWEEN ? AND ?
          AND event_type <> 'evidence_gap'
        ORDER BY catalyst_date ASC,
            CASE event_grade WHEN 'S' THEN 0 WHEN 'A' THEN 1 WHEN 'B' THEN 2 ELSE 9 END
        """,
        [*codes, trade_date, end_date],
    ).fetchall()
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        code = row["code"]
        item = result.setdefault(code, {"count": 0, "score": 0.0, "titles": [], "events": []})
        item["count"] += 1
        item["score"] += GRADE_EVENT_WEIGHT.get(row["event_grade"], 0.0)
        if len(item["titles"]) < 3:
            item["titles"].append(row["title"])
        if len(item["events"]) < 8:
            item["events"].append(dict(row))
    for item in result.values():
        item["score"] = round(min(100.0, item["score"]), 2)
    return result


def load_opportunity_inputs(conn: sqlite3.Connection, trade_date: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT
            sm.*,
            COALESCE(xgs.eligible_for_buy, 0) AS eligible_for_buy,
            COALESCE(xgs.gate_status, '') AS gate_status,
            COALESCE(xgs.supported_count, 0) AS gate_supported_count,
            COALESCE(xgs.required_count, 6) AS gate_required_count,
            COALESCE(xgs.needs_review_count, 0) AS gate_needs_review_count,
            COALESCE(xgs.pending_count, 0) AS gate_pending_count,
            COALESCE(xgs.failed_count, 0) AS gate_failed_count,
            COALESCE(xgs.blocking_dimensions, '[]') AS blocking_dimensions,
            wdm.industry,
            sw.thesis,
            sw.risk_flags
        FROM stock_model_scores sm
        LEFT JOIN xinwei_gate_snapshots xgs
          ON xgs.code = sm.code
         AND xgs.snapshot_date = sm.trade_date
        LEFT JOIN watchlist_daily_metrics wdm
          ON wdm.code = sm.code
         AND wdm.trade_date = sm.trade_date
        LEFT JOIN stock_watchlist sw
          ON sw.code = sm.code
        WHERE sm.trade_date = ?
        """,
        (trade_date,),
    ).fetchall()


def opportunity_sort_key(row: dict[str, Any]) -> tuple[int, float]:
    order = {"可买": 0, "等证据": 1, "只观察": 2, "排除": 3}
    return (order.get(row["decision_label"], 9), -float(row["opportunity_score"]))


def build_opportunity_record(
    row: sqlite3.Row,
    event_stat: dict[str, Any],
    grade_counts: dict[str, int],
) -> dict[str, Any]:
    risk_score = as_float(row["risk_score"], 50.0)
    formula_score = as_float(row["formula_verification_score"], 0.0)
    availability_score = as_float(row["evidence_availability_score"], 0.0)
    model_evidence_score = as_float(row["evidence_score"], 0.0)
    evidence_score = round((availability_score * 0.55) + (model_evidence_score * 0.45), 2)
    catalyst_count = int(event_stat.get("count") or 0)
    catalyst_score = as_float(event_stat.get("score"), 0.0)
    grade_sa_score = min(100.0, grade_counts.get("S", 0) * 30.0 + grade_counts.get("A", 0) * 18.0)
    coverage_score = min(
        100.0,
        as_float(row["research_report_count_180d"], 0.0) * 5.0 + as_float(row["research_org_count"], 0.0) * 8.0,
    )
    needs_review_penalty = 10.0 if int(row["review_needs_review_count"] or 0) > 0 else 0.0
    case_similarity_score = round(
        max(
            0.0,
            min(
                100.0,
                formula_score * 0.35
                + grade_sa_score * 0.25
                + catalyst_score * 0.20
                + coverage_score * 0.10
                + risk_score * 0.10
                - needs_review_penalty,
            ),
        ),
        2,
    )
    provisional = provisional_decision(row, catalyst_count)
    probability = compute_probability_model(row, provisional, catalyst_count, catalyst_score)
    decision_label = final_decision(row, catalyst_count, probability["ev_pct"], risk_score)
    if decision_label != provisional:
        probability = compute_probability_model(row, decision_label, catalyst_count, catalyst_score)
    missing_triggers = build_missing_triggers(row, catalyst_count, probability["ev_pct"], risk_score)
    position_cap_pct = probability["raw_position_cap_pct"] if decision_label == "可买" else 0.0
    ev_score = round(clamp((probability["ev_pct"] + 20.0) / 80.0) * 100.0, 2)
    risk_penalty = round(max(0.0, 100.0 - risk_score), 2)
    opportunity_score = round(
        formula_score * 0.25
        + evidence_score * 0.20
        + case_similarity_score * 0.15
        + catalyst_score * 0.15
        + ev_score * 0.15
        + risk_score * 0.10,
        2,
    )
    thesis = row["thesis"] or (
        f"{row['industry']}产业趋势候选，产业命题仍待补全。"
        if row["industry"]
        else "产业命题待补全，暂不能只凭市场分买入。"
    )
    titles = event_stat.get("titles") or []
    catalyst_summary = (
        f"未来90天有{catalyst_count}条可跟踪验证：{' / '.join(titles)}"
        if catalyst_count > 0
        else "未来90天缺少明确催化，先等证据。"
    )
    key_gap = missing_triggers[0] if missing_triggers else "证据、催化、EV和风险均达标，仍需人工复核买入条件。"
    detail = {
        "version": OPPORTUNITY_VERSION,
        "formula_closed_score": formula_score,
        "formula_gate": {
            "status": row["gate_status"],
            "eligible_for_buy": bool(row["eligible_for_buy"]),
            "supported_count": int(row["gate_supported_count"] or 0),
            "required_count": int(row["gate_required_count"] or EXPECTED_DIMENSIONS),
            "needs_review_count": int(row["gate_needs_review_count"] or 0),
            "pending_count": int(row["gate_pending_count"] or 0),
            "failed_count": int(row["gate_failed_count"] or 0),
            "blocking_dimensions": parse_json(row["blocking_dimensions"], []) or [],
        },
        "evidence_validation": {
            "S": grade_counts.get("S", 0),
            "A": grade_counts.get("A", 0),
            "B": grade_counts.get("B", 0),
            "C": grade_counts.get("C", 0),
            "needs_review_count": int(row["review_needs_review_count"] or 0),
        },
        "probability": probability,
        "missing_triggers": missing_triggers,
        "buy_conditions": [
            "六项信维公式均为verified",
            "S/A级证据形成闭环",
            "未来90天存在可验证催化",
            "EV>0且风险分达标",
            "单票仓位不超过总资金15%",
        ],
        "do_not_chase": [
            "涨停或大幅冲高后不追",
            "没有客户/订单/产能证据不买",
            "needs_review未人工确认前不买",
            "估值过热且EV转负时不买",
        ],
        "risk_triggers": [
            "核心客户或订单被证伪则清仓复核",
            "任一关键维度由verified转failed/stale则仓位回到0%",
            "连续两个季度收入/订单斜率走平则减仓复核",
            "回撤触发既定止损线则先执行纪律再复盘",
        ],
        "case_library_reference": "xinwei_communication_0_to_1",
        "case_similarity_inputs": {
            "formula_score": formula_score,
            "grade_sa_score": round(grade_sa_score, 2),
            "catalyst_score": catalyst_score,
            "coverage_score": round(coverage_score, 2),
            "risk_score": risk_score,
        },
    }
    return {
        "trade_date": row["trade_date"],
        "code": row["code"],
        "name": row["name"],
        "opportunity_rank": None,
        "opportunity_score": opportunity_score,
        "evidence_score": evidence_score,
        "case_similarity_score": case_similarity_score,
        "catalyst_score": catalyst_score,
        "ev_score": ev_score,
        "risk_penalty": risk_penalty,
        "decision_label": decision_label,
        "position_cap_pct": round(min(15.0, max(0.0, position_cap_pct)), 2),
        "thesis": thesis,
        "key_gap": key_gap,
        "catalyst_summary": catalyst_summary,
        "probability": probability | {"position_cap_pct": round(min(15.0, max(0.0, position_cap_pct)), 2)},
        "missing_triggers": missing_triggers,
        "detail": detail,
    }


def save_opportunity_records(conn: sqlite3.Connection, records: list[dict[str, Any]]) -> None:
    if not records:
        return
    now = now_text()
    opportunity_rows = []
    probability_rows = []
    for record in records:
        probability = record["probability"]
        detail_json = json_dumps(record["detail"])
        missing_json = json_dumps(record["missing_triggers"])
        opportunity_rows.append(
            (
                record["trade_date"],
                record["code"],
                record["name"],
                OPPORTUNITY_VERSION,
                record["opportunity_rank"],
                record["opportunity_score"],
                record["evidence_score"],
                record["case_similarity_score"],
                record["catalyst_score"],
                record["ev_score"],
                record["risk_penalty"],
                record["decision_label"],
                record["position_cap_pct"],
                record["thesis"],
                record["key_gap"],
                record["catalyst_summary"],
                detail_json,
                now,
            )
        )
        probability_rows.append(
            (
                record["trade_date"],
                record["code"],
                record["name"],
                OPPORTUNITY_VERSION,
                probability["p1"],
                probability["p2"],
                probability["p3"],
                probability["p4"],
                probability["p5"],
                probability["p6"],
                probability["p7"],
                probability["ev_pct"],
                probability["win_rate"],
                probability["odds"],
                probability["half_kelly"],
                record["position_cap_pct"],
                record["decision_label"],
                missing_json,
                detail_json,
                now,
            )
        )
    conn.executemany(
        """
        INSERT INTO stock_opportunity_scores(
            trade_date, code, name, model_version, opportunity_rank,
            opportunity_score, evidence_score, case_similarity_score,
            catalyst_score, ev_score, risk_penalty, decision_label,
            position_cap_pct, thesis, key_gap, catalyst_summary,
            detail_json, created_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(trade_date, code) DO UPDATE SET
            name=excluded.name,
            model_version=excluded.model_version,
            opportunity_rank=excluded.opportunity_rank,
            opportunity_score=excluded.opportunity_score,
            evidence_score=excluded.evidence_score,
            case_similarity_score=excluded.case_similarity_score,
            catalyst_score=excluded.catalyst_score,
            ev_score=excluded.ev_score,
            risk_penalty=excluded.risk_penalty,
            decision_label=excluded.decision_label,
            position_cap_pct=excluded.position_cap_pct,
            thesis=excluded.thesis,
            key_gap=excluded.key_gap,
            catalyst_summary=excluded.catalyst_summary,
            detail_json=excluded.detail_json,
            created_at=excluded.created_at
        """,
        opportunity_rows,
    )
    conn.executemany(
        """
        INSERT INTO stock_probability_models(
            trade_date, code, name, model_version, p1, p2, p3, p4, p5, p6, p7,
            ev_pct, win_rate, odds, half_kelly, position_cap_pct,
            decision_label, missing_triggers, detail_json, created_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(trade_date, code) DO UPDATE SET
            name=excluded.name,
            model_version=excluded.model_version,
            p1=excluded.p1,
            p2=excluded.p2,
            p3=excluded.p3,
            p4=excluded.p4,
            p5=excluded.p5,
            p6=excluded.p6,
            p7=excluded.p7,
            ev_pct=excluded.ev_pct,
            win_rate=excluded.win_rate,
            odds=excluded.odds,
            half_kelly=excluded.half_kelly,
            position_cap_pct=excluded.position_cap_pct,
            decision_label=excluded.decision_label,
            missing_triggers=excluded.missing_triggers,
            detail_json=excluded.detail_json,
            created_at=excluded.created_at
        """,
        probability_rows,
    )


def refresh_opportunity_layer(conn: sqlite3.Connection, trade_date: str | None = None) -> list[dict[str, Any]]:
    ensure_opportunity_schema(conn)
    trade_date = trade_date or latest_model_date(conn)
    if not trade_date:
        return []
    refresh_thesis_events(conn, trade_date)
    rows = load_opportunity_inputs(conn, trade_date)
    codes = [row["code"] for row in rows]
    event_stats = thesis_event_stats(conn, codes, trade_date)
    grade_counts_by_code = evidence_grade_counts(conn, codes)
    records = [
        build_opportunity_record(row, event_stats.get(row["code"], {}), grade_counts_by_code.get(row["code"], {}))
        for row in rows
    ]
    records.sort(key=opportunity_sort_key)
    for idx, record in enumerate(records, start=1):
        record["opportunity_rank"] = idx
    save_opportunity_records(conn, records)
    return records


def mask_opportunity_row(row: dict[str, Any], tier: str = "free") -> dict[str, Any]:
    tier = tier if tier in {"free", "research", "pro", "team"} else "free"
    detail = parse_json(row.get("detail_json"), {}) or {}
    missing = parse_json(row.get("missing_triggers"), []) or detail.get("missing_triggers") or []
    item = {
        "trade_date": row.get("trade_date"),
        "code": row.get("code"),
        "name": row.get("name"),
        "opportunity_rank": row.get("opportunity_rank"),
        "opportunity_score": row.get("opportunity_score"),
        "decision_label": row.get("decision_label"),
        "thesis": row.get("thesis"),
        "key_gap": row.get("key_gap"),
        "catalyst_summary": row.get("catalyst_summary"),
        "evidence_score": row.get("evidence_score"),
        "case_similarity_score": row.get("case_similarity_score"),
        "catalyst_score": row.get("catalyst_score"),
        "risk_penalty": row.get("risk_penalty"),
        "missing_triggers": missing,
        "formula_gate": detail.get("formula_gate", {}),
        "evidence_validation": detail.get("evidence_validation", {}),
    }
    if tier in {"pro", "team"}:
        item.update(
            {
                "ev_score": row.get("ev_score"),
                "ev_pct": row.get("ev_pct"),
                "win_rate": row.get("win_rate"),
                "odds": row.get("odds"),
                "half_kelly": row.get("half_kelly"),
                "position_cap_pct": row.get("position_cap_pct"),
                "probability": {
                    "p1": row.get("p1"),
                    "p2": row.get("p2"),
                    "p3": row.get("p3"),
                    "p4": row.get("p4"),
                    "p5": row.get("p5"),
                    "p6": row.get("p6"),
                    "p7": row.get("p7"),
                },
                "detail": detail,
                "locked_fields": [],
            }
        )
    elif tier == "research":
        item.update(
            {
                "ev_pct": None,
                "position_cap_pct": None,
                "locked_fields": ["EV", "胜率", "赔率", "半凯利仓位", "完整复盘提醒"],
                "detail": {
                    "buy_conditions": detail.get("buy_conditions", []),
                    "do_not_chase": detail.get("do_not_chase", []),
                    "risk_triggers": detail.get("risk_triggers", []),
                },
            }
        )
    else:
        item.update(
            {
                "ev_pct": None,
                "position_cap_pct": None,
                "evidence_validation": {},
                "locked_fields": ["完整股票池", "S/A证据链", "90天催化", "EV/仓位模型"],
            }
        )
    return item


def latest_opportunity_radar(
    conn: sqlite3.Connection,
    limit: int = 20,
    tier: str = "free",
) -> dict[str, Any]:
    ensure_opportunity_schema(conn)
    trade_date = latest_opportunity_date(conn)
    if not trade_date:
        return {"trade_date": None, "tier": tier, "rows": [], "decision_counts": {}, "version": OPPORTUNITY_VERSION}
    max_limit = 3 if tier == "free" else max(1, min(limit, 80))
    rows = conn.execute(
        """
        SELECT
            sos.*,
            spm.p1, spm.p2, spm.p3, spm.p4, spm.p5, spm.p6, spm.p7,
            spm.ev_pct, spm.win_rate, spm.odds, spm.half_kelly,
            spm.missing_triggers
        FROM stock_opportunity_scores sos
        LEFT JOIN stock_probability_models spm
          ON spm.trade_date = sos.trade_date
         AND spm.code = sos.code
        WHERE sos.trade_date = ?
        ORDER BY sos.opportunity_rank ASC
        LIMIT ?
        """,
        (trade_date, max_limit),
    ).fetchall()
    counts = {
        row["decision_label"]: row["n"]
        for row in conn.execute(
            """
            SELECT decision_label, COUNT(*) AS n
            FROM stock_opportunity_scores
            WHERE trade_date = ?
            GROUP BY decision_label
            """,
            (trade_date,),
        ).fetchall()
    }
    return {
        "trade_date": trade_date,
        "tier": tier,
        "rows": [mask_opportunity_row(dict(row), tier) for row in rows],
        "decision_counts": counts,
        "version": OPPORTUNITY_VERSION,
        "membership_note": "普通账号看摘要和3只样本；研究会员看完整证据链；高级会员看EV、胜率、赔率、仓位和触发提醒。",
    }


def opportunity_for_code(conn: sqlite3.Connection, code: str, tier: str = "free") -> dict[str, Any] | None:
    ensure_opportunity_schema(conn)
    trade_date = latest_opportunity_date(conn)
    if not trade_date:
        return None
    row = conn.execute(
        """
        SELECT
            sos.*,
            spm.p1, spm.p2, spm.p3, spm.p4, spm.p5, spm.p6, spm.p7,
            spm.ev_pct, spm.win_rate, spm.odds, spm.half_kelly,
            spm.missing_triggers
        FROM stock_opportunity_scores sos
        LEFT JOIN stock_probability_models spm
          ON spm.trade_date = sos.trade_date
         AND spm.code = sos.code
        WHERE sos.trade_date = ?
          AND sos.code = ?
        """,
        (trade_date, code),
    ).fetchone()
    if not row:
        return None
    payload = mask_opportunity_row(dict(row), tier)
    end_date = plus_days(trade_date, 90) or trade_date
    events = conn.execute(
        """
        SELECT event_date, event_type, event_grade, title, detail, source, source_url, catalyst_date, dimension_id
        FROM stock_thesis_events
        WHERE code = ?
          AND catalyst_date BETWEEN ? AND ?
        ORDER BY catalyst_date ASC,
            CASE event_grade WHEN 'S' THEN 0 WHEN 'A' THEN 1 WHEN 'B' THEN 2 ELSE 9 END
        LIMIT ?
        """,
        (code, trade_date, end_date, 5 if tier == "free" else 30),
    ).fetchall()
    payload["thesis_events"] = [dict(event) for event in events] if tier in {"research", "pro", "team"} else []
    payload["case_reference"] = "xinwei_communication_0_to_1" if tier in {"research", "pro", "team"} else None
    return payload
