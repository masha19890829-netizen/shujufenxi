#!/usr/bin/env python
"""Build weekly post-mortem reviews for A-share recommendation logic.

The weekly review summarizes last week's recommendation runs, subsequent paper
replay performance, evidence-gate state, and logic improvement notes. It is a
learning layer for future recommendation logic, not a trading executor.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from a_stock_daily import DEFAULT_DB, as_float, connect, init_db, now_cn, today_cn
from a_stock_replay import refresh_replay_results


WEEKLY_REVIEW_VERSION = "xinwei-weekly-review-v0.1"


@dataclass
class ReviewStock:
    run_id: int
    run_date: str
    rank: int
    code: str
    name: str
    candidate_score: float
    occurrence_count: int
    action_bucket: str | None
    industry: str | None
    entry_date: str | None
    latest_date: str | None
    trading_days_observed: int
    latest_return_pct: float | None
    return_1d_pct: float | None
    return_3d_pct: float | None
    return_5d_pct: float | None
    max_intraday_return_pct: float | None
    worst_intraday_return_pct: float | None
    max_drawdown_pct: float | None
    take_profit_5_hit: int
    stop_loss_5_hit: int
    replay_status: str
    formula_gate_status: str | None
    formula_supported_count: int | None
    formula_required_count: int | None
    evidence_availability_score: float | None
    formula_verification_score: float | None
    lesson_tag: str
    lesson_note: str
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


def parse_date(value: str) -> date:
    return datetime.strptime(value[:10], "%Y-%m-%d").date()


def previous_calendar_week(today_text: str | None = None) -> tuple[str, str]:
    anchor = parse_date(today_text or today_cn())
    this_monday = anchor - timedelta(days=anchor.weekday())
    last_monday = this_monday - timedelta(days=7)
    last_sunday = this_monday - timedelta(days=1)
    return last_monday.isoformat(), last_sunday.isoformat()


def pct_rate(numerator: int | float | None, denominator: int | float | None) -> float:
    if not denominator:
        return 0.0
    return round(float(numerator or 0) / float(denominator) * 100.0, 2)


def parse_metrics(metrics_json: str | None) -> dict[str, Any]:
    value = parse_json(metrics_json)
    return value if isinstance(value, dict) else {}


def latest_model_date_for_run(conn: sqlite3.Connection, run_date: str) -> str | None:
    row = conn.execute(
        """
        SELECT MIN(trade_date) AS trade_date
        FROM stock_model_scores
        WHERE trade_date >= ?
        """,
        (run_date,),
    ).fetchone()
    if row and row["trade_date"]:
        return row["trade_date"]
    row = conn.execute("SELECT MAX(trade_date) AS trade_date FROM stock_model_scores").fetchone()
    return row["trade_date"] if row and row["trade_date"] else None


def classify_lesson(row: sqlite3.Row, model: sqlite3.Row | None, gate: sqlite3.Row | None) -> tuple[str, str]:
    latest_return = as_float(row["latest_return_pct"])
    worst_intraday = as_float(row["worst_intraday_return_pct"])
    take_profit = int(row["take_profit_5_hit"] or 0)
    stop_loss = int(row["stop_loss_5_hit"] or 0)
    evidence_avail = as_float(model["evidence_availability_score"] if model else None)
    verify_score = as_float(model["formula_verification_score"] if model else None)
    action_bucket = model["action_bucket"] if model else None
    gate_status = gate["gate_status"] if gate else None

    if row["replay_status"] not in {"open", "complete_20d"}:
        return "data_gap", "Replay is not actionable yet; improve K-line and entry coverage before judging logic."
    if take_profit and latest_return is not None and latest_return > 0:
        if verify_score == 0:
            return "market_right_evidence_weak", "Market moved favorably, but formula verification stayed weak; do not upgrade without S/A closure."
        return "validated_momentum", "Recommendation showed positive follow-through; keep checking whether industry evidence explains the move."
    if stop_loss or (worst_intraday is not None and worst_intraday <= -5):
        if evidence_avail is not None and evidence_avail < 20:
            return "thin_evidence_drawdown", "Weak evidence names suffered drawdown; tighten wait_evidence filtering and avoid pure market-score dependence."
        return "risk_control_needed", "Drawdown exceeded review threshold; inspect valuation, liquidity, and event-risk conditions."
    if latest_return is not None and latest_return > 0:
        return "modest_positive", "Positive result but not enough to prove logic; keep as sample for factor/evidence attribution."
    if latest_return is not None and latest_return < 0:
        if action_bucket == "wait_evidence" or gate_status in {"missing_evidence", None}:
            return "wait_evidence_underperformed", "Evidence gap coincided with weak performance; keep these names out of buy-style language."
        return "negative_watch", "Negative follow-through; review whether entry timing or thesis evidence was stale."
    return "insufficient_horizon", "Observation window is still short; keep in weekly tracking without changing logic."


def load_review_stocks(conn: sqlite3.Connection, week_start: str, week_end: str) -> list[ReviewStock]:
    rows = conn.execute(
        """
        SELECT
            rr.id AS run_id,
            rr.run_date,
            rc.rank,
            rc.code,
            rc.name,
            rc.score AS candidate_score,
            rc.metrics_json,
            prr.entry_date,
            prr.latest_date,
            prr.trading_days_observed,
            prr.latest_return_pct,
            prr.return_1d_pct,
            prr.return_3d_pct,
            prr.return_5d_pct,
            prr.max_intraday_return_pct,
            prr.worst_intraday_return_pct,
            prr.max_drawdown_pct,
            prr.take_profit_5_hit,
            prr.stop_loss_5_hit,
            prr.status AS replay_status
        FROM recommendation_runs rr
        JOIN recommendation_candidates rc
          ON rc.run_id = rr.id
        LEFT JOIN paper_replay_results prr
          ON prr.run_id = rr.id
         AND prr.rank = rc.rank
        WHERE rr.run_date BETWEEN ? AND ?
        ORDER BY rr.run_date ASC, rr.id ASC, rc.rank ASC
        """,
        (week_start, week_end),
    ).fetchall()
    raw: list[ReviewStock] = []
    model_date_cache: dict[str, str | None] = {}
    for row in rows:
        metrics = parse_metrics(row["metrics_json"])
        model_date = model_date_cache.get(row["run_date"])
        if row["run_date"] not in model_date_cache:
            model_date = latest_model_date_for_run(conn, row["run_date"])
            model_date_cache[row["run_date"]] = model_date
        model = (
            conn.execute(
                """
                SELECT action_bucket, evidence_availability_score, formula_verification_score,
                       market_score, factor_score, behavior_score, risk_score
                FROM stock_model_scores
                WHERE code = ? AND trade_date = ?
                """,
                (row["code"], model_date),
            ).fetchone()
            if model_date
            else None
        )
        gate = (
            conn.execute(
                """
                SELECT gate_status, supported_count, required_count, blocking_dimensions
                FROM xinwei_gate_snapshots
                WHERE code = ? AND snapshot_date = ?
                """,
                (row["code"], model_date),
            ).fetchone()
            if model_date
            else None
        )
        lesson_tag, lesson_note = classify_lesson(row, model, gate)
        detail = {
            "weekly_review_version": WEEKLY_REVIEW_VERSION,
            "model_date_used": model_date,
            "candidate_metrics": metrics,
            "model_scores": dict(model) if model else None,
            "gate": dict(gate) if gate else None,
        }
        raw.append(
            ReviewStock(
                run_id=row["run_id"],
                run_date=row["run_date"],
                rank=row["rank"],
                code=row["code"],
                name=row["name"],
                candidate_score=row["candidate_score"],
                occurrence_count=1,
                action_bucket=model["action_bucket"] if model else None,
                industry=metrics.get("industry"),
                entry_date=row["entry_date"],
                latest_date=row["latest_date"],
                trading_days_observed=int(row["trading_days_observed"] or 0),
                latest_return_pct=as_float(row["latest_return_pct"]),
                return_1d_pct=as_float(row["return_1d_pct"]),
                return_3d_pct=as_float(row["return_3d_pct"]),
                return_5d_pct=as_float(row["return_5d_pct"]),
                max_intraday_return_pct=as_float(row["max_intraday_return_pct"]),
                worst_intraday_return_pct=as_float(row["worst_intraday_return_pct"]),
                max_drawdown_pct=as_float(row["max_drawdown_pct"]),
                take_profit_5_hit=int(row["take_profit_5_hit"] or 0),
                stop_loss_5_hit=int(row["stop_loss_5_hit"] or 0),
                replay_status=row["replay_status"] or "missing_replay",
                formula_gate_status=gate["gate_status"] if gate else None,
                formula_supported_count=gate["supported_count"] if gate else None,
                formula_required_count=gate["required_count"] if gate else None,
                evidence_availability_score=as_float(model["evidence_availability_score"] if model else None),
                formula_verification_score=as_float(model["formula_verification_score"] if model else None),
                lesson_tag=lesson_tag,
                lesson_note=lesson_note,
                detail=detail,
            )
        )
    result: dict[str, ReviewStock] = {}
    for item in raw:
        occurrence = {
            "run_id": item.run_id,
            "run_date": item.run_date,
            "rank": item.rank,
            "candidate_score": item.candidate_score,
        }
        existing = result.get(item.code)
        if existing is None:
            item.detail["weekly_occurrence_count"] = 1
            item.detail["weekly_occurrences"] = [occurrence]
            result[item.code] = item
            continue
        existing.occurrence_count += 1
        existing.detail["weekly_occurrence_count"] = existing.occurrence_count
        existing.detail.setdefault("weekly_occurrences", []).append(occurrence)
    return list(result.values())


def build_summary(stocks: list[ReviewStock], run_count: int) -> dict[str, Any]:
    actionable = [item for item in stocks if item.replay_status in {"open", "complete_20d"}]
    returns = [item.latest_return_pct for item in actionable if item.latest_return_pct is not None]
    drawdowns = [item.max_drawdown_pct for item in actionable if item.max_drawdown_pct is not None]
    by_bucket: dict[str, dict[str, Any]] = {}
    by_industry: dict[str, dict[str, Any]] = {}
    for item in stocks:
        bucket = item.action_bucket or "unknown"
        industry = item.industry or "unknown"
        for target, key in ((by_bucket, bucket), (by_industry, industry)):
            row = target.setdefault(key, {"count": 0, "returns": [], "take_profit_5": 0, "stop_loss_5": 0})
            row["count"] += 1
            row["event_count"] = row.get("event_count", 0) + item.occurrence_count
            if item.latest_return_pct is not None:
                row["returns"].append(item.latest_return_pct)
            row["take_profit_5"] += item.take_profit_5_hit
            row["stop_loss_5"] += item.stop_loss_5_hit
    def finalize(grouped: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for key, row in grouped.items():
            values = row.pop("returns")
            rows.append(
                {
                    "key": key,
                    "count": row["count"],
                    "event_count": row.get("event_count", row["count"]),
                    "avg_return_pct": round(sum(values) / len(values), 2) if values else None,
                    "positive_rate": pct_rate(sum(1 for value in values if value > 0), len(values)) if values else 0.0,
                    "take_profit_5_rate": pct_rate(row["take_profit_5"], row["count"]),
                    "stop_loss_5_rate": pct_rate(row["stop_loss_5"], row["count"]),
                }
            )
        return sorted(rows, key=lambda item: (item["avg_return_pct"] is None, -(item["avg_return_pct"] or -999), -item["count"]))

    best = max((item for item in actionable if item.latest_return_pct is not None), key=lambda item: item.latest_return_pct, default=None)
    worst = min((item for item in actionable if item.latest_return_pct is not None), key=lambda item: item.latest_return_pct, default=None)
    replay_coverage = pct_rate(len(actionable), len(stocks))
    return {
        "recommendation_run_count": run_count,
        "candidate_count": len(stocks),
        "candidate_event_count": sum(item.occurrence_count for item in stocks),
        "actionable_replay_count": len(actionable),
        "replay_coverage_rate": replay_coverage,
        "avg_latest_return_pct": round(sum(returns) / len(returns), 2) if returns else None,
        "median_latest_return_pct": round(median(returns), 2) if returns else None,
        "positive_rate": pct_rate(sum(1 for value in returns if value > 0), len(returns)) if returns else 0.0,
        "avg_max_drawdown_pct": round(sum(drawdowns) / len(drawdowns), 2) if drawdowns else None,
        "take_profit_5_rate": pct_rate(sum(item.take_profit_5_hit for item in actionable), len(actionable)),
        "stop_loss_5_rate": pct_rate(sum(item.stop_loss_5_hit for item in actionable), len(actionable)),
        "best_stock": f"{best.code} {best.name} {best.latest_return_pct}%" if best else None,
        "worst_stock": f"{worst.code} {worst.name} {worst.latest_return_pct}%" if worst else None,
        "by_bucket": finalize(by_bucket),
        "by_industry": finalize(by_industry)[:12],
    }


def build_logic_insights(stocks: list[ReviewStock], summary: dict[str, Any]) -> list[dict[str, Any]]:
    insights: list[dict[str, Any]] = []
    tag_counts: dict[str, int] = {}
    for item in stocks:
        tag_counts[item.lesson_tag] = tag_counts.get(item.lesson_tag, 0) + 1
    if summary["candidate_count"] == 0:
        insights.append(
            {
                "insight_type": "data_gap",
                "severity": "major",
                "title": "No weekly recommendation candidates",
                "detail": "No recommendation candidates were found for the review window; weekly learning is blocked by missing inputs.",
                "metric": {"candidate_count": 0},
            }
        )
        return insights
    if summary["replay_coverage_rate"] < 90:
        insights.append(
            {
                "insight_type": "data_coverage",
                "severity": "major",
                "title": "Replay coverage below target",
                "detail": "Paper replay coverage is below 90%; improve K-line coverage before using performance to tune logic.",
                "metric": {"replay_coverage_rate": summary["replay_coverage_rate"]},
            }
        )
    if summary["stop_loss_5_rate"] >= 20:
        insights.append(
            {
                "insight_type": "risk_control",
                "severity": "major",
                "title": "5% stop-loss hit rate is elevated",
                "detail": "A high stop-loss hit rate suggests stricter valuation/liquidity/risk filters before adding new candidates.",
                "metric": {"stop_loss_5_rate": summary["stop_loss_5_rate"]},
            }
        )
    if tag_counts.get("thin_evidence_drawdown", 0) or tag_counts.get("wait_evidence_underperformed", 0):
        insights.append(
            {
                "insight_type": "evidence_quality",
                "severity": "major",
                "title": "Weak-evidence names need stricter language",
                "detail": "Names without formula closure should remain in observation buckets; do not let market score become a buy reason.",
                "metric": {
                    "thin_evidence_drawdown": tag_counts.get("thin_evidence_drawdown", 0),
                    "wait_evidence_underperformed": tag_counts.get("wait_evidence_underperformed", 0),
                },
            }
        )
    if tag_counts.get("market_right_evidence_weak", 0):
        insights.append(
            {
                "insight_type": "missed_evidence",
                "severity": "minor",
                "title": "Positive movers still lacked verified formula evidence",
                "detail": "For positive movers with weak evidence, prioritize evidence collection rather than retroactively upgrading the logic.",
                "metric": {"market_right_evidence_weak": tag_counts.get("market_right_evidence_weak", 0)},
            }
        )
    if not insights:
        insights.append(
            {
                "insight_type": "stable",
                "severity": "info",
                "title": "Weekly logic did not trigger major changes",
                "detail": "Performance and replay coverage did not breach review thresholds; keep accumulating samples.",
                "metric": tag_counts,
            }
        )
    return insights


def build_markdown(week_start: str, week_end: str, summary: dict[str, Any], insights: list[dict[str, Any]], stocks: list[ReviewStock]) -> str:
    lines = [
        f"# Xinwei weekly review {week_start} to {week_end}",
        "",
        f"- version: {WEEKLY_REVIEW_VERSION}",
        f"- recommendation_runs: {summary['recommendation_run_count']}",
        f"- unique_candidates: {summary['candidate_count']}",
        f"- candidate_events: {summary['candidate_event_count']}",
        f"- replay_coverage_rate: {summary['replay_coverage_rate']}%",
        f"- avg_latest_return_pct: {summary['avg_latest_return_pct']}",
        f"- positive_rate: {summary['positive_rate']}%",
        f"- avg_max_drawdown_pct: {summary['avg_max_drawdown_pct']}",
        f"- take_profit_5_rate: {summary['take_profit_5_rate']}%",
        f"- stop_loss_5_rate: {summary['stop_loss_5_rate']}%",
        f"- best_stock: {summary['best_stock']}",
        f"- worst_stock: {summary['worst_stock']}",
        "",
        "## Logic insights",
    ]
    for item in insights:
        lines.append(f"- [{item['severity']}] {item['title']}: {item['detail']}")
    lines.extend(["", "## Top reviewed stocks"])
    ordered = sorted(stocks, key=lambda item: (item.latest_return_pct is None, -(item.latest_return_pct or -999)))[:15]
    for item in ordered:
        lines.append(
            f"- {item.run_date} #{item.rank} {item.code} {item.name} "
            f"occurrences={item.occurrence_count} bucket={item.action_bucket or '-'} latest={item.latest_return_pct} "
            f"drawdown={item.max_drawdown_pct} lesson={item.lesson_tag}"
        )
    return "\n".join(lines)


def save_weekly_review(
    conn: sqlite3.Connection,
    week_start: str,
    week_end: str,
    stocks: list[ReviewStock],
    summary: dict[str, Any],
    insights: list[dict[str, Any]],
    markdown: str,
) -> int:
    generated_at = now_cn().isoformat(timespec="seconds")
    status = "complete" if stocks else "empty"
    existing_runs = conn.execute(
        "SELECT id FROM weekly_review_runs WHERE week_start = ? AND week_end = ?",
        (week_start, week_end),
    ).fetchall()
    for row in existing_runs:
        conn.execute("DELETE FROM weekly_logic_insights WHERE review_run_id = ?", (row["id"],))
        conn.execute("DELETE FROM weekly_review_stocks WHERE review_run_id = ?", (row["id"],))
        conn.execute("DELETE FROM weekly_review_runs WHERE id = ?", (row["id"],))
    cur = conn.execute(
        """
        INSERT INTO weekly_review_runs(
            week_start, week_end, generated_at, status, recommendation_run_count,
            candidate_count, candidate_event_count, replay_coverage_rate, avg_latest_return_pct,
            median_latest_return_pct, positive_rate, avg_max_drawdown_pct,
            take_profit_5_rate, stop_loss_5_rate, alert_count, best_stock, worst_stock,
            summary_json, logic_review_json, markdown_report
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            week_start,
            week_end,
            generated_at,
            status,
            summary["recommendation_run_count"],
            summary["candidate_count"],
            summary["candidate_event_count"],
            summary["replay_coverage_rate"],
            summary["avg_latest_return_pct"],
            summary["median_latest_return_pct"],
            summary["positive_rate"],
            summary["avg_max_drawdown_pct"],
            summary["take_profit_5_rate"],
            summary["stop_loss_5_rate"],
            sum(1 for item in insights if item["severity"] == "major"),
            summary["best_stock"],
            summary["worst_stock"],
            json_dumps(summary),
            json_dumps(insights),
            markdown,
        ),
    )
    review_run_id = int(cur.lastrowid)
    conn.executemany(
        """
        INSERT INTO weekly_review_stocks(
            review_run_id, run_id, run_date, rank, code, name, candidate_score,
            occurrence_count, action_bucket, industry, entry_date, latest_date, trading_days_observed,
            latest_return_pct, return_1d_pct, return_3d_pct, return_5d_pct,
            max_intraday_return_pct, worst_intraday_return_pct, max_drawdown_pct,
            take_profit_5_hit, stop_loss_5_hit, replay_status, formula_gate_status,
            formula_supported_count, formula_required_count, evidence_availability_score,
            formula_verification_score, lesson_tag, lesson_note, detail_json, created_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                review_run_id,
                item.run_id,
                item.run_date,
                item.rank,
                item.code,
                item.name,
                item.candidate_score,
                item.occurrence_count,
                item.action_bucket,
                item.industry,
                item.entry_date,
                item.latest_date,
                item.trading_days_observed,
                item.latest_return_pct,
                item.return_1d_pct,
                item.return_3d_pct,
                item.return_5d_pct,
                item.max_intraday_return_pct,
                item.worst_intraday_return_pct,
                item.max_drawdown_pct,
                item.take_profit_5_hit,
                item.stop_loss_5_hit,
                item.replay_status,
                item.formula_gate_status,
                item.formula_supported_count,
                item.formula_required_count,
                item.evidence_availability_score,
                item.formula_verification_score,
                item.lesson_tag,
                item.lesson_note,
                json_dumps(item.detail),
                generated_at,
            )
            for item in stocks
        ],
    )
    conn.executemany(
        """
        INSERT INTO weekly_logic_insights(
            review_run_id, insight_type, severity, title, detail, metric_json, created_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                review_run_id,
                item["insight_type"],
                item["severity"],
                item["title"],
                item["detail"],
                json_dumps(item.get("metric") or {}),
                generated_at,
            )
            for item in insights
        ],
    )
    return review_run_id


def refresh_weekly_review(conn: sqlite3.Connection, week_start: str, week_end: str) -> int:
    refresh_replay_results(conn, since=week_start)
    run_count = conn.execute(
        "SELECT COUNT(*) AS n FROM recommendation_runs WHERE run_date BETWEEN ? AND ? AND candidate_count > 0",
        (week_start, week_end),
    ).fetchone()["n"]
    stocks = load_review_stocks(conn, week_start, week_end)
    summary = build_summary(stocks, int(run_count or 0))
    insights = build_logic_insights(stocks, summary)
    markdown = build_markdown(week_start, week_end, summary, insights, stocks)
    return save_weekly_review(conn, week_start, week_end, stocks, summary, insights, markdown)


def latest_weekly_review_payload(conn: sqlite3.Connection) -> dict[str, Any]:
    run = conn.execute("SELECT * FROM weekly_review_runs ORDER BY week_end DESC, id DESC LIMIT 1").fetchone()
    if not run:
        return {"run": None, "stocks": [], "insights": []}
    stocks = conn.execute(
        """
        SELECT *
        FROM weekly_review_stocks
        WHERE review_run_id = ?
        ORDER BY run_date ASC, run_id ASC, rank ASC
        LIMIT 300
        """,
        (run["id"],),
    ).fetchall()
    insights = conn.execute(
        """
        SELECT insight_type, severity, title, detail, metric_json, created_at
        FROM weekly_logic_insights
        WHERE review_run_id = ?
        ORDER BY CASE severity WHEN 'major' THEN 0 WHEN 'minor' THEN 1 ELSE 2 END, id ASC
        """,
        (run["id"],),
    ).fetchall()
    run_payload = dict(run)
    run_payload["summary"] = parse_json(run_payload.pop("summary_json")) or {}
    run_payload["logic_review"] = parse_json(run_payload.pop("logic_review_json")) or []
    stock_payload = []
    for row in stocks:
        item = dict(row)
        item["detail"] = parse_json(item.pop("detail_json")) or {}
        stock_payload.append(item)
    insight_payload = []
    for row in insights:
        item = dict(row)
        item["metric"] = parse_json(item.pop("metric_json")) or {}
        insight_payload.append(item)
    return {"run": run_payload, "stocks": stock_payload, "insights": insight_payload}


def command_run(args: argparse.Namespace) -> None:
    week_start, week_end = (args.start, args.end) if args.start and args.end else previous_calendar_week(args.as_of)
    with connect(args.db) as conn:
        init_db(conn)
        review_id = refresh_weekly_review(conn, week_start, week_end)
        conn.commit()
        payload = latest_weekly_review_payload(conn)
    run = payload["run"]
    print(
        f"Weekly review #{review_id} {week_start}..{week_end} status={run['status']} "
        f"runs={run['recommendation_run_count']} candidates={run['candidate_count']} "
        f"avg={run['avg_latest_return_pct']} positive={run['positive_rate']}%"
    )
    if args.report:
        print()
        print(run["markdown_report"])


def command_show(args: argparse.Namespace) -> None:
    with connect(args.db) as conn:
        init_db(conn)
        payload = latest_weekly_review_payload(conn)
    run = payload["run"]
    if not run:
        print("No weekly review found.")
        return
    print(
        f"Weekly review #{run['id']} {run['week_start']}..{run['week_end']} "
        f"status={run['status']} candidates={run['candidate_count']} "
        f"avg={run['avg_latest_return_pct']} positive={run['positive_rate']}%"
    )
    for insight in payload["insights"]:
        print(f"{insight['severity']:>5} {insight['insight_type']}: {insight['title']}")
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build weekly A-share recommendation reviews")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Build weekly review")
    run.add_argument("--start", help="Week start date YYYY-MM-DD")
    run.add_argument("--end", help="Week end date YYYY-MM-DD")
    run.add_argument("--as-of", help="Reference date; defaults to today and reviews previous calendar week")
    run.add_argument("--report", action="store_true")

    show = sub.add_parser("show", help="Show latest weekly review")
    show.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "run":
            command_run(args)
        elif args.command == "show":
            command_show(args)
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
