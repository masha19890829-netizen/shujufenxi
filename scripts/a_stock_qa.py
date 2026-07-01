#!/usr/bin/env python
"""Continuous QA and quantitative references for the local A-share stack.

The QA layer validates local data, model output, website endpoints, and Xinwei
formula gates. Quantitative rows are reference-only; they never open a buy gate
or change recommendation/model output.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))

from a_stock_daily import DEFAULT_DB, XINWEI_DIMENSIONS, as_float, connect, init_db, now_cn, today_cn


QA_VERSION = "xinwei-qa-v0.1"
DEFAULT_WEBSITE_URL = "http://127.0.0.1:8765"
DEFAULT_TOP = 20
FAILURE_RATE_THRESHOLD = 0.01
MISSING_FIELD_RATE_THRESHOLD = 0.02
API_P95_THRESHOLD_MS = 2500.0
RETENTION_DAYS = 30
CORE_ENDPOINTS = [
    ("/", "text"),
    ("/api/summary", "json"),
    ("/api/morning-report", "json"),
    ("/api/gate-matrix?limit=5", "json"),
    ("/api/research-tasks?limit=5", "json"),
    ("/api/qa-report", "json"),
    ("/api/weekly-review", "json"),
]
PAYOFFS = [-50.0, -30.0, -10.0, 20.0, 50.0, 100.0, 200.0]
QA_TASKS = [
    ("data-freshness", "Data QA", "Latest table dates are present and consistent.", "core tables", "Dates are non-empty and cache status can be disclosed.", 1),
    ("data-field-completeness", "Data QA", "Critical fields are populated.", "model + watchlist tables", "Missing-field rate stays below 2%.", 1),
    ("model-bucket-stability", "Model QA", "Action bucket distribution and topN shape are stable.", "latest model rows", "Duplicate ranks/codes and large bucket drift are surfaced.", 2),
    ("website-core-api", "Website QA", "Dashboard and core API endpoints are reachable.", DEFAULT_WEBSITE_URL, "Core endpoints return 200 and P95 stays below 2.5s.", 1),
    ("formula-gate-integrity", "Formula QA", "Xinwei gate is strict about verified S/A evidence.", "review + gate + evidence tables", "needs_review is never counted as verified.", 1),
    ("quant-reference", "Formula QA", "EV/Kelly reference rows are generated.", "latest top model rows", "Incomplete formula rows stay observe_only with 0% position.", 2),
]


@dataclass
class CheckResult:
    role: str
    check_id: str
    severity: str
    status: str
    message: str
    metric_value: float | None = None
    threshold_value: float | None = None
    detail: dict[str, Any] | None = None


@dataclass
class QuantReference:
    trade_date: str
    code: str
    name: str
    action_bucket: str
    reference_status: str
    probabilities: list[float]
    ev_pct: float
    win_rate: float
    odds: float | None
    half_kelly: float
    position_cap_pct: float
    missing_triggers: list[str]
    detail: dict[str, Any]


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def parse_json(value: str | None) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def init_qa_schema(conn: sqlite3.Connection) -> None:
    init_db(conn)


def seed_qa_tasks(conn: sqlite3.Connection) -> None:
    now_text = now_cn().isoformat(timespec="seconds")
    rows = [
        (task_id, role, scenario, input_ref, expected, priority, "internal-qa", "open", now_text, now_text)
        for task_id, role, scenario, input_ref, expected, priority in QA_TASKS
    ]
    conn.executemany(
        """
        INSERT INTO qa_tasks(
            task_id, role, scenario, input_ref, expected, priority, owner, status, created_at, updated_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(task_id) DO UPDATE SET
            role=excluded.role,
            scenario=excluded.scenario,
            input_ref=excluded.input_ref,
            expected=excluded.expected,
            priority=excluded.priority,
            owner=excluded.owner,
            updated_at=excluded.updated_at
        """,
        rows,
    )


def latest_value(conn: sqlite3.Connection, table: str, column: str) -> str | None:
    row = conn.execute(f"SELECT MAX({column}) AS value FROM {table}").fetchone()
    return row["value"] if row and row["value"] else None


def date_age_days(date_text: str | None) -> int | None:
    if not date_text:
        return None
    try:
        day = datetime.fromisoformat(date_text[:10]).date()
    except ValueError:
        return None
    return (now_cn().date() - day).days


def missing_ratio_for_rows(rows: list[sqlite3.Row], fields: list[str]) -> tuple[float, int, int, dict[str, int]]:
    if not rows or not fields:
        return 1.0, 0, 0, {field: 0 for field in fields}
    missing_by_field = {field: 0 for field in fields}
    total_cells = len(rows) * len(fields)
    missing_cells = 0
    for row in rows:
        for field in fields:
            if row[field] in (None, "", "null"):
                missing_cells += 1
                missing_by_field[field] += 1
    return missing_cells / max(1, total_cells), missing_cells, total_cells, missing_by_field


def check_data_freshness(conn: sqlite3.Connection) -> CheckResult:
    dates = {
        "market_snapshot": latest_value(conn, "market_snapshot", "trade_date"),
        "stock_model_scores": latest_value(conn, "stock_model_scores", "trade_date"),
        "watchlist_daily_metrics": latest_value(conn, "watchlist_daily_metrics", "trade_date"),
        "stock_research_coverage": latest_value(conn, "stock_research_coverage", "as_of_date"),
        "xinwei_gate_snapshots": latest_value(conn, "xinwei_gate_snapshots", "snapshot_date"),
    }
    missing = [table for table, value in dates.items() if not value]
    ages = {table: date_age_days(value) for table, value in dates.items()}
    status = "pass"
    message = "Core data dates are available."
    if missing:
        status = "fail"
        message = f"Missing latest dates for: {', '.join(missing)}"
    elif any((age is not None and age > 7) for age in ages.values()):
        status = "warn"
        message = "Some local data dates are older than 7 calendar days."
    elif dates["market_snapshot"] != dates["stock_model_scores"]:
        status = "warn"
        message = "Market snapshot and model dates differ; report must disclose local-cache status."
    return CheckResult("Data QA", "data_freshness", "critical", status, message, detail={"dates": dates, "ages_days": ages})


def check_field_completeness(conn: sqlite3.Connection) -> CheckResult:
    model_date = latest_value(conn, "stock_model_scores", "trade_date")
    metric_date = latest_value(conn, "watchlist_daily_metrics", "trade_date")
    model_rows = conn.execute(
        """
        SELECT code, name, action_bucket, total_score, market_score, evidence_score,
               factor_score, risk_score, score_json
        FROM stock_model_scores
        WHERE trade_date = ?
        """,
        (model_date,),
    ).fetchall() if model_date else []
    metric_rows = conn.execute(
        """
        SELECT code, name, status, price, latest_score, industry
        FROM watchlist_daily_metrics
        WHERE trade_date = ?
        """,
        (metric_date,),
    ).fetchall() if metric_date else []
    model_ratio, model_missing, model_cells, model_fields = missing_ratio_for_rows(
        model_rows,
        ["code", "name", "action_bucket", "total_score", "market_score", "evidence_score", "factor_score", "risk_score", "score_json"],
    )
    metric_ratio, metric_missing, metric_cells, metric_fields = missing_ratio_for_rows(
        metric_rows,
        ["code", "name", "status", "price", "latest_score", "industry"],
    )
    total_missing = model_missing + metric_missing
    total_cells = model_cells + metric_cells
    ratio = total_missing / max(1, total_cells)
    status = "pass"
    if ratio > MISSING_FIELD_RATE_THRESHOLD:
        status = "fail"
    elif ratio > MISSING_FIELD_RATE_THRESHOLD / 2:
        status = "warn"
    detail = {
        "model_date": model_date,
        "metric_date": metric_date,
        "model_rows": len(model_rows),
        "metric_rows": len(metric_rows),
        "model_missing_by_field": model_fields,
        "metric_missing_by_field": metric_fields,
        "model_missing_ratio": model_ratio,
        "metric_missing_ratio": metric_ratio,
    }
    return CheckResult(
        "Data QA",
        "critical_field_completeness",
        "critical",
        status,
        f"Critical missing-field rate is {ratio:.2%}.",
        metric_value=ratio,
        threshold_value=MISSING_FIELD_RATE_THRESHOLD,
        detail=detail,
    )


def check_data_consistency(conn: sqlite3.Connection) -> CheckResult:
    model_date = latest_value(conn, "stock_model_scores", "trade_date")
    metric_date = latest_value(conn, "watchlist_daily_metrics", "trade_date")
    if not model_date or not metric_date:
        return CheckResult("Data QA", "cross_table_consistency", "critical", "fail", "Model or metric date is missing.")
    unmatched = conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM stock_model_scores sm
        JOIN stock_watchlist sw
          ON sw.code = sm.code
        LEFT JOIN watchlist_daily_metrics wm
          ON wm.code = sm.code
         AND wm.trade_date = ?
        WHERE sm.trade_date = ?
          AND sw.status = 'active'
          AND wm.code IS NULL
        """,
        (metric_date, model_date),
    ).fetchone()["n"]
    name_mismatch = conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM stock_model_scores sm
        JOIN stock_watchlist sw
          ON sw.code = sm.code
        JOIN watchlist_daily_metrics wm
          ON wm.code = sm.code
         AND wm.trade_date = ?
        WHERE sm.trade_date = ?
          AND sw.status = 'active'
          AND TRIM(sm.name) <> TRIM(wm.name)
        """,
        (metric_date, model_date),
    ).fetchone()["n"]
    missing_by_bucket_rows = conn.execute(
        """
        SELECT sm.action_bucket, COUNT(*) AS n
        FROM stock_model_scores sm
        JOIN stock_watchlist sw
          ON sw.code = sm.code
        LEFT JOIN watchlist_daily_metrics wm
          ON wm.code = sm.code
         AND wm.trade_date = ?
        WHERE sm.trade_date = ?
          AND sw.status = 'active'
          AND wm.code IS NULL
        GROUP BY sm.action_bucket
        ORDER BY n DESC
        """,
        (metric_date, model_date),
    ).fetchall()
    missing_by_bucket = {row["action_bucket"]: int(row["n"] or 0) for row in missing_by_bucket_rows}
    top_missing_rows = conn.execute(
        """
        SELECT sm.priority_rank, sm.code, sm.name, sm.action_bucket, sm.total_score
        FROM stock_model_scores sm
        JOIN stock_watchlist sw
          ON sw.code = sm.code
        LEFT JOIN watchlist_daily_metrics wm
          ON wm.code = sm.code
         AND wm.trade_date = ?
        WHERE sm.trade_date = ?
          AND sw.status = 'active'
          AND wm.code IS NULL
        ORDER BY sm.priority_rank ASC
        LIMIT 15
        """,
        (metric_date, model_date),
    ).fetchall()
    top_missing = [dict(row) for row in top_missing_rows]
    high_priority_missing = sum(
        1
        for row in top_missing
        if row.get("action_bucket") in {"formula_supported", "deep_research", "blocked_by_evidence"}
    )
    status = "pass"
    if high_priority_missing or unmatched > 0 and unmatched / max(1, int(conn.execute("SELECT COUNT(*) AS n FROM stock_watchlist WHERE status = 'active'").fetchone()["n"] or 0)) > 0.2:
        status = "fail"
    elif unmatched or name_mismatch:
        status = "warn"
    return CheckResult(
        "Data QA",
        "cross_table_consistency",
        "major",
        status,
        (
            f"Cross-table consistency: unmatched={unmatched}, name_mismatch={name_mismatch}, "
            f"high_priority_missing={high_priority_missing}."
        ),
        metric_value=float(unmatched + name_mismatch),
        threshold_value=0.0,
        detail={
            "model_date": model_date,
            "metric_date": metric_date,
            "scope": "active stock_watchlist rows only",
            "unmatched": unmatched,
            "name_mismatch": name_mismatch,
            "high_priority_missing": high_priority_missing,
            "missing_by_bucket": missing_by_bucket,
            "top_missing": top_missing,
        },
    )


def check_score_ranges(conn: sqlite3.Connection) -> CheckResult:
    model_date = latest_value(conn, "stock_model_scores", "trade_date")
    if not model_date:
        return CheckResult("Data QA", "score_ranges", "critical", "fail", "No stock_model_scores date found.")
    bad = conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM stock_model_scores
        WHERE trade_date = ?
          AND (
            total_score < 0 OR total_score > 100 OR market_score < 0 OR market_score > 100 OR
            evidence_score < 0 OR evidence_score > 100 OR factor_score < 0 OR factor_score > 100 OR
            risk_score < 0 OR risk_score > 100 OR formula_verification_score < 0 OR formula_verification_score > 100
          )
        """,
        (model_date,),
    ).fetchone()["n"]
    return CheckResult(
        "Data QA",
        "score_ranges",
        "critical",
        "fail" if bad else "pass",
        f"Out-of-range model score rows: {bad}.",
        metric_value=float(bad),
        threshold_value=0.0,
        detail={"model_date": model_date},
    )


def check_model_stability(conn: sqlite3.Connection) -> CheckResult:
    model_date = latest_value(conn, "stock_model_scores", "trade_date")
    if not model_date:
        return CheckResult("Model QA", "model_bucket_stability", "critical", "fail", "No latest model score date found.")
    current = conn.execute(
        """
        SELECT action_bucket, COUNT(*) AS n, AVG(total_score) AS avg_score
        FROM stock_model_scores
        WHERE trade_date = ?
        GROUP BY action_bucket
        """,
        (model_date,),
    ).fetchall()
    current_counts = {row["action_bucket"]: int(row["n"] or 0) for row in current}
    current_avg = {row["action_bucket"]: round(float(row["avg_score"] or 0), 2) for row in current}
    duplicate_ranks = conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM (
            SELECT priority_rank
            FROM stock_model_scores
            WHERE trade_date = ? AND priority_rank IS NOT NULL
            GROUP BY priority_rank
            HAVING COUNT(*) > 1
        )
        """,
        (model_date,),
    ).fetchone()["n"]
    previous_date = conn.execute(
        "SELECT MAX(trade_date) AS trade_date FROM stock_model_scores WHERE trade_date < ?",
        (model_date,),
    ).fetchone()["trade_date"]
    drift: dict[str, Any] = {}
    status = "fail" if duplicate_ranks else "pass"
    if previous_date:
        previous = conn.execute(
            """
            SELECT action_bucket, COUNT(*) AS n
            FROM stock_model_scores
            WHERE trade_date = ?
            GROUP BY action_bucket
            """,
            (previous_date,),
        ).fetchall()
        prev_counts = {row["action_bucket"]: int(row["n"] or 0) for row in previous}
        for bucket, count in current_counts.items():
            prev = prev_counts.get(bucket, 0)
            if prev:
                change = abs(count - prev) / prev
                if change > 0.5 and count + prev >= 10:
                    status = "warn" if status == "pass" else status
                    drift[bucket] = {"current": count, "previous": prev, "change_pct": round(change * 100, 2)}
    return CheckResult(
        "Model QA",
        "model_bucket_stability",
        "major",
        status,
        f"Model buckets: {current_counts}; duplicate ranks={duplicate_ranks}.",
        metric_value=float(duplicate_ranks),
        threshold_value=0.0,
        detail={"model_date": model_date, "previous_date": previous_date, "counts": current_counts, "avg_scores": current_avg, "drift": drift},
    )


def check_model_buy_gate(conn: sqlite3.Connection) -> CheckResult:
    model_date = latest_value(conn, "stock_model_scores", "trade_date")
    gate_date = latest_value(conn, "xinwei_gate_snapshots", "snapshot_date")
    if not model_date:
        return CheckResult("Model QA", "buy_gate_guardrail", "critical", "fail", "No model score date found.")
    bad_model = conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM stock_model_scores
        WHERE trade_date = ?
          AND action_bucket = 'formula_supported'
          AND formula_verification_score < 100
        """,
        (model_date,),
    ).fetchone()["n"]
    bad_gate = 0
    if gate_date:
        bad_gate = conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM xinwei_gate_snapshots
            WHERE snapshot_date = ?
              AND eligible_for_buy = 1
              AND supported_count < required_count
            """,
            (gate_date,),
        ).fetchone()["n"]
    bad = int(bad_model or 0) + int(bad_gate or 0)
    return CheckResult(
        "Model QA",
        "buy_gate_guardrail",
        "critical",
        "fail" if bad else "pass",
        f"Buy-gate guardrail violations: {bad}.",
        metric_value=float(bad),
        threshold_value=0.0,
        detail={"model_date": model_date, "gate_date": gate_date, "bad_model_rows": bad_model, "bad_gate_rows": bad_gate},
    )


def check_formula_integrity(conn: sqlite3.Connection) -> CheckResult:
    gate_date = latest_value(conn, "xinwei_gate_snapshots", "snapshot_date")
    if not gate_date:
        return CheckResult("Formula QA", "formula_gate_integrity", "critical", "fail", "No Xinwei gate snapshot date found.")
    dimension_count = len(XINWEI_DIMENSIONS)
    missing_dimension_rows = conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM (
            SELECT code, COUNT(*) AS dimension_count
            FROM stock_xinwei_reviews
            GROUP BY code
            HAVING COUNT(*) <> ?
        )
        """,
        (dimension_count,),
    ).fetchone()["n"]
    needs_review_as_verified = 0
    gate_rows = conn.execute(
        "SELECT code, dimension_status_json FROM xinwei_gate_snapshots WHERE snapshot_date = ?",
        (gate_date,),
    ).fetchall()
    for row in gate_rows:
        for dim in parse_json(row["dimension_status_json"]) or []:
            if dim.get("review_status") == "needs_review" and dim.get("status") == "verified":
                needs_review_as_verified += 1
    invalid_grade = conn.execute(
        "SELECT COUNT(*) AS n FROM stock_evidence_items WHERE evidence_grade NOT IN ('S', 'A', 'B', 'C')"
    ).fetchone()["n"]
    status = "pass"
    if needs_review_as_verified or invalid_grade:
        status = "fail"
    elif missing_dimension_rows:
        status = "warn"
    return CheckResult(
        "Formula QA",
        "formula_gate_integrity",
        "critical",
        status,
        f"Formula integrity: missing_dimension_rows={missing_dimension_rows}, needs_review_as_verified={needs_review_as_verified}, invalid_grades={invalid_grade}.",
        metric_value=float(needs_review_as_verified + invalid_grade),
        threshold_value=0.0,
        detail={"gate_date": gate_date, "missing_dimension_rows": missing_dimension_rows, "needs_review_as_verified": needs_review_as_verified, "invalid_evidence_grade": invalid_grade},
    )


def http_get(url: str, timeout: float = 8.0) -> tuple[int, bytes, str]:
    req = Request(url, headers={"User-Agent": "xinwei-qa/0.1"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), resp.headers.get("Content-Type", "")
    except HTTPError as exc:
        return exc.code, exc.read(), exc.headers.get("Content-Type", "")


def maybe_start_website(args: argparse.Namespace) -> subprocess.Popen[str] | None:
    if not args.start_web:
        return None
    script = Path(__file__).resolve().parent / "a_stock_web.py"
    cmd = [sys.executable, str(script), "--db", str(args.db), "--host", args.host, "--port", str(args.port)]
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True)


def wait_for_website(base_url: str, timeout_seconds: float) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            status, _, _ = http_get(base_url.rstrip("/") + "/", timeout=2.0)
            if status == 200:
                return True
        except (OSError, URLError, TimeoutError):
            pass
        time.sleep(1.0)
    return False


def check_website(base_url: str) -> tuple[CheckResult, float | None]:
    latencies: list[float] = []
    failures: list[dict[str, Any]] = []
    schemas: dict[str, list[str]] = {}
    for path, expected_kind in CORE_ENDPOINTS:
        url = base_url.rstrip("/") + path
        started = time.perf_counter()
        try:
            status, body, content_type = http_get(url)
            elapsed_ms = (time.perf_counter() - started) * 1000
            latencies.append(elapsed_ms)
            if status != 200:
                failures.append({"path": path, "status": status, "elapsed_ms": round(elapsed_ms, 2)})
                continue
            if expected_kind == "json":
                payload = json.loads(body.decode("utf-8"))
                if not isinstance(payload, dict):
                    failures.append({"path": path, "status": status, "reason": "json root is not object"})
                else:
                    schemas[path] = sorted(payload.keys())[:20]
                if "application/json" not in content_type:
                    failures.append({"path": path, "status": status, "reason": f"unexpected content-type {content_type}"})
            elif not body.strip():
                failures.append({"path": path, "status": status, "reason": "empty response"})
        except (OSError, URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            elapsed_ms = (time.perf_counter() - started) * 1000
            latencies.append(elapsed_ms)
            failures.append({"path": path, "error": str(exc), "elapsed_ms": round(elapsed_ms, 2)})
    p95 = None
    if latencies:
        ordered = sorted(latencies)
        p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
    status = "fail" if failures else ("warn" if p95 is not None and p95 > API_P95_THRESHOLD_MS else "pass")
    return (
        CheckResult(
            "Website QA",
            "website_core_endpoints",
            "critical",
            status,
            f"Website endpoint failures={len(failures)}, p95={round(p95 or 0, 2)}ms.",
            metric_value=p95,
            threshold_value=API_P95_THRESHOLD_MS,
            detail={"base_url": base_url, "failures": failures, "schemas": schemas, "latencies_ms": [round(v, 2) for v in latencies]},
        ),
        p95,
    )


def normalize_score(value: Any, default: float = 0.0) -> float:
    parsed = as_float(value)
    if parsed is None:
        return default
    return max(0.0, min(1.0, parsed / 100.0))


def probability_distribution(row: sqlite3.Row, gate: dict[str, Any]) -> tuple[list[float], str, list[str]]:
    verified = normalize_score(row["formula_verification_score"])
    eligible = bool(gate.get("eligible_for_buy")) and verified >= 1.0
    missing = []
    if verified < 1.0:
        missing.append("formula_verification_score<100")
    blocking = gate.get("blocking_dimensions") or []
    if blocking:
        missing.extend(str(item) for item in blocking)
    if not eligible:
        return [0.05, 0.25, 0.70, 0.0, 0.0, 0.0, 0.0], "observe_only", sorted(set(missing or ["gate_not_eligible"]))

    market = normalize_score(row["market_score"], 0.5)
    factor = normalize_score(row["factor_score"], 0.5)
    behavior = normalize_score(row["behavior_score"], 0.5)
    risk = normalize_score(row["risk_score"], 0.5)
    win_mass = max(0.35, min(0.78, 0.20 + 0.18 * market + 0.16 * factor + 0.12 * behavior + 0.12 * risk))
    loss_mass = 1.0 - win_mass
    tail_risk = 1.0 - risk
    p1 = loss_mass * (0.12 + 0.18 * tail_risk)
    p2 = loss_mass * (0.28 + 0.12 * tail_risk)
    p3 = max(0.0, loss_mass - p1 - p2)
    growth_power = (market + factor + behavior) / 3.0
    p4 = win_mass * max(0.20, 0.45 - growth_power * 0.15)
    p6 = win_mass * max(0.08, growth_power * 0.20)
    p7 = win_mass * max(0.02, growth_power * 0.08)
    p5 = max(0.0, win_mass - p4 - p6 - p7)
    probabilities = [p1, p2, p3, p4, p5, p6, p7]
    total = sum(probabilities) or 1.0
    return [round(p / total, 6) for p in probabilities], "reference_only", []


def quant_from_row(row: sqlite3.Row) -> QuantReference:
    score_json = parse_json(row["score_json"]) or {}
    gate = score_json.get("formula_gate") or {}
    probabilities, reference_status, missing = probability_distribution(row, gate)
    ev_pct = round(sum(p * payoff for p, payoff in zip(probabilities, PAYOFFS)), 2)
    win_rate = round(sum(probabilities[3:]), 6)
    loss_rate = 1.0 - win_rate
    positive = [(p, payoff) for p, payoff in zip(probabilities[3:], PAYOFFS[3:]) if p > 0]
    negative = [(p, payoff) for p, payoff in zip(probabilities[:3], PAYOFFS[:3]) if p > 0]
    avg_gain = sum(p * payoff for p, payoff in positive) / max(1e-9, win_rate) if positive else 0.0
    avg_loss = abs(sum(p * payoff for p, payoff in negative) / max(1e-9, loss_rate)) if negative else 0.0
    odds = round(avg_gain / avg_loss, 4) if avg_loss > 0 and avg_gain > 0 else None
    kelly = max(0.0, ((odds * win_rate) - loss_rate) / odds) if odds else 0.0
    half_kelly = round(kelly / 2.0, 6)
    position_cap_pct = 0.0 if reference_status == "observe_only" else round(min(0.15, half_kelly) * 100.0, 2)
    return QuantReference(
        trade_date=row["trade_date"],
        code=row["code"],
        name=row["name"],
        action_bucket=row["action_bucket"],
        reference_status=reference_status,
        probabilities=probabilities,
        ev_pct=ev_pct,
        win_rate=win_rate,
        odds=odds,
        half_kelly=half_kelly,
        position_cap_pct=position_cap_pct,
        missing_triggers=missing,
        detail={
            "payoffs_pct": PAYOFFS,
            "scores": {
                "market_score": row["market_score"],
                "factor_score": row["factor_score"],
                "behavior_score": row["behavior_score"],
                "risk_score": row["risk_score"],
                "formula_verification_score": row["formula_verification_score"],
            },
            "avg_gain_pct": round(avg_gain, 2),
            "avg_loss_pct": round(avg_loss, 2),
            "rule": "reference only; formula-incomplete rows keep position at 0%",
        },
    )


def build_quant_references(conn: sqlite3.Connection, top: int) -> list[QuantReference]:
    model_date = latest_value(conn, "stock_model_scores", "trade_date")
    if not model_date:
        return []
    rows = conn.execute(
        "SELECT * FROM stock_model_scores WHERE trade_date = ? ORDER BY priority_rank ASC LIMIT ?",
        (model_date, max(1, top)),
    ).fetchall()
    return [quant_from_row(row) for row in rows]


def quant_check(refs: list[QuantReference]) -> CheckResult:
    bad = [ref.code for ref in refs if ref.reference_status == "observe_only" and ref.position_cap_pct != 0]
    return CheckResult(
        "Formula QA",
        "quant_reference_guardrail",
        "major",
        "fail" if bad else "pass",
        f"Quant reference rows generated={len(refs)}, guardrail violations={len(bad)}.",
        metric_value=float(len(bad)),
        threshold_value=0.0,
        detail={"violating_codes": bad, "top_refs": [ref.code for ref in refs[:10]]},
    )


def core_script_check(db_path: Path) -> CheckResult:
    script_dir = Path(__file__).resolve().parent
    scripts = ["a_stock_daily.py", "a_stock_model.py", "a_stock_web.py"]
    failures: list[dict[str, Any]] = []
    for script in scripts:
        started = time.perf_counter()
        proc = subprocess.run(
            [sys.executable, str(script_dir / script), "--help"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=20,
        )
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        if proc.returncode != 0:
            failures.append({"script": script, "returncode": proc.returncode, "stderr": proc.stderr[-500:], "elapsed_ms": elapsed_ms})
    model_show = subprocess.run(
        [sys.executable, str(script_dir / "a_stock_model.py"), "--db", str(db_path), "show", "--top", "1"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
    )
    if model_show.returncode != 0:
        failures.append({"script": "a_stock_model.py show", "returncode": model_show.returncode, "stderr": model_show.stderr[-500:]})
    return CheckResult(
        "Model QA",
        "core_script_reproducibility",
        "major",
        "fail" if failures else "pass",
        f"Core script checks failures={len(failures)}.",
        metric_value=float(len(failures)),
        threshold_value=0.0,
        detail={"failures": failures},
    )


def build_report(
    mode: str,
    checks: list[CheckResult],
    refs: list[QuantReference],
    failure_rate: float,
    missing_field_rate: float,
    api_p95_ms: float | None,
    status: str,
) -> str:
    lines = [
        f"# Xinwei QA {mode} report",
        "",
        f"- version: {QA_VERSION}",
        f"- run_date: {today_cn()}",
        f"- status: {status}",
        f"- failure_rate: {failure_rate:.2%}",
        f"- critical_missing_field_rate: {missing_field_rate:.2%}",
        f"- api_p95_ms: {round(api_p95_ms, 2) if api_p95_ms is not None else 'N/A'}",
        "",
        "## Checks",
    ]
    for check in checks:
        lines.append(f"- [{check.status.upper()}] {check.role}/{check.check_id}: {check.message}")
    lines.extend(["", "## Quant reference top"])
    for ref in refs[:10]:
        lines.append(
            f"- {ref.code} {ref.name} {ref.reference_status}: EV={ref.ev_pct}% "
            f"win={ref.win_rate:.2%} odds={ref.odds if ref.odds is not None else 'N/A'} "
            f"half_kelly={ref.half_kelly:.2%} position={ref.position_cap_pct}%"
        )
    return "\n".join(lines)


def calculate_status(checks: list[CheckResult], failure_rate: float, missing_field_rate: float, api_p95_ms: float | None) -> str:
    if any(check.status == "fail" and check.severity == "critical" for check in checks):
        return "fail"
    if failure_rate > FAILURE_RATE_THRESHOLD or missing_field_rate > MISSING_FIELD_RATE_THRESHOLD:
        return "fail"
    if api_p95_ms is not None and api_p95_ms > API_P95_THRESHOLD_MS:
        return "warn"
    if any(check.status in {"fail", "warn"} for check in checks):
        return "warn"
    return "pass"


def persist_run(
    conn: sqlite3.Connection,
    mode: str,
    started_at: str,
    finished_at: str,
    status: str,
    checks: list[CheckResult],
    refs: list[QuantReference],
    failure_rate: float,
    missing_field_rate: float,
    api_p95_ms: float | None,
    report: str,
) -> int:
    alerts = [check for check in checks if check.status == "fail" or (check.status == "warn" and check.severity in {"critical", "major"})]
    summary = {
        "qa_version": QA_VERSION,
        "mode": mode,
        "status": status,
        "checks_total": len(checks),
        "checks_failed": sum(1 for check in checks if check.status == "fail"),
        "checks_warn": sum(1 for check in checks if check.status == "warn"),
        "failure_rate": failure_rate,
        "missing_field_rate": missing_field_rate,
        "api_p95_ms": api_p95_ms,
        "quant_reference_count": len(refs),
    }
    cur = conn.execute(
        """
        INSERT INTO qa_runs(
            run_date, mode, status, started_at, finished_at, failure_rate,
            missing_field_rate, api_p95_ms, alert_count, summary_json, markdown_report
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (today_cn(), mode, status, started_at, finished_at, failure_rate, missing_field_rate, api_p95_ms, len(alerts), json_dumps(summary), report),
    )
    run_id = int(cur.lastrowid)
    now_text = now_cn().isoformat(timespec="seconds")
    conn.executemany(
        """
        INSERT INTO qa_checks(
            run_id, role, check_id, severity, status, metric_value, threshold_value,
            message, detail_json, created_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (run_id, check.role, check.check_id, check.severity, check.status, check.metric_value, check.threshold_value, check.message, json_dumps(check.detail or {}), now_text)
            for check in checks
        ],
    )
    conn.executemany(
        """
        INSERT INTO qa_quant_reference(
            run_id, trade_date, code, name, action_bucket, reference_status,
            p1, p2, p3, p4, p5, p6, p7, ev_pct, win_rate, odds, half_kelly,
            position_cap_pct, missing_triggers, detail_json, created_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                run_id,
                ref.trade_date,
                ref.code,
                ref.name,
                ref.action_bucket,
                ref.reference_status,
                *ref.probabilities,
                ref.ev_pct,
                ref.win_rate,
                ref.odds,
                ref.half_kelly,
                ref.position_cap_pct,
                json_dumps(ref.missing_triggers),
                json_dumps(ref.detail),
                now_text,
            )
            for ref in refs
        ],
    )
    conn.executemany(
        """
        INSERT INTO qa_alerts(run_id, severity, status, title, detail, created_at)
        VALUES(?, ?, 'open', ?, ?, ?)
        """,
        [(run_id, check.severity, f"{check.role}: {check.check_id}", check.message, now_text) for check in alerts],
    )
    cutoff = (now_cn() - timedelta(days=RETENTION_DAYS)).date().isoformat()
    conn.execute("DELETE FROM qa_runs WHERE run_date < ?", (cutoff,))
    return run_id


def run_qa(args: argparse.Namespace) -> int:
    started_at = now_cn().isoformat(timespec="seconds")
    web_process: subprocess.Popen[str] | None = None
    try:
        with connect(args.db) as conn:
            init_qa_schema(conn)
            seed_qa_tasks(conn)
            checks = [
                check_data_freshness(conn),
                check_field_completeness(conn),
                check_data_consistency(conn),
                check_score_ranges(conn),
                check_model_stability(conn),
                check_model_buy_gate(conn),
                check_formula_integrity(conn),
            ]
            refs = build_quant_references(conn, args.top)
            checks.append(quant_check(refs))
            conn.commit()

        checks.append(core_script_check(args.db))
        web_process = maybe_start_website(args)
        if web_process:
            wait_for_website(args.website_url, args.web_start_timeout)
        website_result, api_p95_ms = check_website(args.website_url)
        checks.append(website_result)

        with connect(args.db) as conn:
            init_qa_schema(conn)
            seed_qa_tasks(conn)
            total = len(checks)
            failed = sum(1 for check in checks if check.status == "fail")
            failure_rate = failed / max(1, total)
            missing_check = next((check for check in checks if check.check_id == "critical_field_completeness"), None)
            missing_rate = float(missing_check.metric_value or 0.0) if missing_check else 0.0
            status = calculate_status(checks, failure_rate, missing_rate, api_p95_ms)
            finished_at = now_cn().isoformat(timespec="seconds")
            report = build_report(args.mode, checks, refs, failure_rate, missing_rate, api_p95_ms, status)
            run_id = persist_run(conn, args.mode, started_at, finished_at, status, checks, refs, failure_rate, missing_rate, api_p95_ms, report)
            conn.commit()
        alertish = sum(1 for check in checks if check.status in {"fail", "warn"})
        print(f"QA run #{run_id} status={status} checks={total} failed={failed} alerts={alertish}")
        for check in checks:
            print(f"{check.status.upper():5} {check.role:10} {check.check_id}: {check.message}")
        if args.report:
            print()
            print(report)
        return 1 if status == "fail" and args.fail_on_alert else 0
    finally:
        if web_process:
            web_process.terminate()
            try:
                web_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                web_process.kill()


def latest_qa_payload(conn: sqlite3.Connection) -> dict[str, Any]:
    try:
        run = conn.execute("SELECT * FROM qa_runs ORDER BY id DESC LIMIT 1").fetchone()
    except sqlite3.OperationalError:
        return {"run": None, "checks": [], "alerts": [], "quant_reference": [], "tasks": []}
    if not run:
        return {"run": None, "checks": [], "alerts": [], "quant_reference": [], "tasks": []}
    checks = conn.execute(
        """
        SELECT role, check_id, severity, status, metric_value, threshold_value, message, detail_json, created_at
        FROM qa_checks
        WHERE run_id = ?
        ORDER BY CASE status WHEN 'fail' THEN 0 WHEN 'warn' THEN 1 ELSE 2 END, role, check_id
        """,
        (run["id"],),
    ).fetchall()
    alerts = conn.execute(
        "SELECT severity, status, title, detail, created_at FROM qa_alerts WHERE run_id = ? ORDER BY id ASC",
        (run["id"],),
    ).fetchall()
    refs = conn.execute(
        """
        SELECT *
        FROM qa_quant_reference
        WHERE run_id = ?
        ORDER BY CASE reference_status WHEN 'reference_only' THEN 0 ELSE 1 END, ev_pct DESC, code ASC
        LIMIT 30
        """,
        (run["id"],),
    ).fetchall()
    tasks = conn.execute(
        """
        SELECT task_id, role, scenario, input_ref, expected, priority, owner, status, updated_at
        FROM qa_tasks
        ORDER BY priority ASC, role ASC, task_id ASC
        """,
    ).fetchall()
    run_payload = dict(run)
    run_payload["summary"] = parse_json(run_payload.pop("summary_json")) or {}
    check_payload = []
    for row in checks:
        item = dict(row)
        item["detail"] = parse_json(item.pop("detail_json")) or {}
        check_payload.append(item)
    ref_payload = []
    for row in refs:
        item = dict(row)
        item["probabilities"] = [item.pop(f"p{i}") for i in range(1, 8)]
        item["missing_triggers"] = parse_json(item.pop("missing_triggers")) or []
        item["detail"] = parse_json(item.pop("detail_json")) or {}
        ref_payload.append(item)
    return {
        "run": run_payload,
        "checks": check_payload,
        "alerts": [dict(row) for row in alerts],
        "quant_reference": ref_payload,
        "tasks": [dict(row) for row in tasks],
        "thresholds": {
            "failure_rate": FAILURE_RATE_THRESHOLD,
            "missing_field_rate": MISSING_FIELD_RATE_THRESHOLD,
            "api_p95_ms": API_P95_THRESHOLD_MS,
            "retention_days": RETENTION_DAYS,
        },
    }


def command_init(args: argparse.Namespace) -> None:
    with connect(args.db) as conn:
        init_qa_schema(conn)
        seed_qa_tasks(conn)
        conn.commit()
    print("QA schema and task catalog initialized.")


def command_run(args: argparse.Namespace) -> int:
    return run_qa(args)


def command_show(args: argparse.Namespace) -> None:
    with connect(args.db) as conn:
        init_qa_schema(conn)
        payload = latest_qa_payload(conn)
    run = payload.get("run")
    if not run:
        print("No QA runs found.")
        return
    print(
        f"QA run #{run['id']} date={run['run_date']} mode={run['mode']} "
        f"status={run['status']} alerts={run['alert_count']} "
        f"failure_rate={run['failure_rate']:.2%} missing={run['missing_field_rate']:.2%} "
        f"api_p95={run['api_p95_ms']}"
    )
    for check in payload["checks"][: args.top]:
        print(f"{check['status']:>5} {check['role']} {check['check_id']}: {check['message']}")
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Xinwei website/data QA and quantitative references")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite database path")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Initialize QA schema and task catalog")
    run = sub.add_parser("run", help="Run daily or weekly QA")
    run.add_argument("--mode", choices=["daily", "weekly"], default="daily")
    run.add_argument("--top", type=int, default=DEFAULT_TOP, help="Top model rows for quant reference")
    run.add_argument("--website-url", default=DEFAULT_WEBSITE_URL)
    run.add_argument("--host", default="127.0.0.1")
    run.add_argument("--port", type=int, default=8765)
    run.add_argument("--start-web", action="store_true", help="Start a temporary local web server during QA")
    run.add_argument("--web-start-timeout", type=float, default=35.0)
    run.add_argument("--report", action="store_true", help="Print markdown report after checks")
    run.add_argument("--fail-on-alert", action="store_true", help="Return non-zero when status is fail")
    show = sub.add_parser("show", help="Show latest QA run")
    show.add_argument("--top", type=int, default=20)
    show.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "init":
            command_init(args)
            return 0
        if args.command == "run":
            return command_run(args)
        if args.command == "show":
            command_show(args)
            return 0
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
