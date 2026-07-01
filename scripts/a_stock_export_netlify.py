"""Export the local A-share dashboard as a Netlify-friendly static site."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from a_stock_daily import DEFAULT_DB, connect, init_db, today_cn
from a_stock_web import INDEX_HTML, summary_payload
from a_stock_qa import latest_qa_payload
from a_stock_weekly_review import latest_weekly_review_payload


STATIC_API_HELPER = """
    function staticApiPath(path) {
      if (path.startsWith('/api/stock?')) {
        const params = new URLSearchParams(path.split('?')[1] || '');
        const code = params.get('code') || '';
        return `/api/stock/${encodeURIComponent(code)}.json`;
      }
      return path;
    }
"""


def json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def write_json(root: Path, endpoint: str, payload: Any) -> None:
    target = root / endpoint.strip("/")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(json_bytes(payload))


def row_dict(row: Any | None) -> dict[str, Any]:
    return dict(row) if row else {}


def parse_json(value: str | None) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def latest_value(conn: Any, table: str, column: str) -> str | None:
    row = conn.execute(f"SELECT MAX({column}) AS value FROM {table}").fetchone()
    return row["value"] if row and row["value"] else None


def light_watchlist_rows(conn: Any, limit: int = 500) -> list[dict[str, Any]]:
    metric_date = latest_value(conn, "watchlist_daily_metrics", "trade_date")
    if not metric_date:
        return []
    rows = conn.execute(
        """
        SELECT *
        FROM watchlist_daily_metrics
        WHERE trade_date = ?
        ORDER BY return_pct DESC, latest_score DESC
        LIMIT ?
        """,
        (metric_date, limit),
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["amount_yi"] = round(float(item["amount"] or 0) / 100000000.0, 2) if item.get("amount") else None
        result.append(item)
    return result


def light_runs_payload(conn: Any, limit: int = 50) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT
            id,
            run_date,
            created_at AS generated_at,
            strategy_version AS data_source,
            run_date AS market_date,
            candidate_count,
            notes AS note
        FROM recommendation_runs
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return {"rows": [dict(row) for row in rows]}


def light_gate_matrix_payload(conn: Any, limit: int = 80) -> dict[str, Any]:
    snapshot_date = latest_value(conn, "xinwei_gate_snapshots", "snapshot_date")
    model_date = latest_value(conn, "stock_model_scores", "trade_date")
    if not snapshot_date:
        return {"snapshot_date": None, "rows": []}
    rows = conn.execute(
        """
        SELECT
            xgs.code,
            xgs.name,
            xgs.gate_status,
            xgs.eligible_for_buy,
            xgs.supported_count,
            xgs.needs_review_count,
            xgs.pending_count,
            xgs.failed_count,
            xgs.required_count,
            xgs.blocking_dimensions,
            xgs.dimension_status_json,
            sm.priority_rank,
            sm.action_bucket,
            sm.total_score,
            sm.market_score,
            sm.evidence_availability_score,
            sm.formula_verification_score,
            sm.factor_score,
            sm.risk_score
        FROM xinwei_gate_snapshots xgs
        LEFT JOIN stock_model_scores sm
          ON sm.code = xgs.code
         AND sm.trade_date = ?
        WHERE xgs.snapshot_date = ?
        ORDER BY COALESCE(sm.priority_rank, 999999), xgs.supported_count DESC
        LIMIT ?
        """,
        (model_date, snapshot_date, limit),
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["blocking_dimensions"] = parse_json(item.get("blocking_dimensions")) or []
        item["dimension_status"] = parse_json(item.pop("dimension_status_json", None)) or {}
        result.append(item)
    return {"snapshot_date": snapshot_date, "model_date": model_date, "rows": result}


def light_research_tasks_payload(conn: Any, limit: int = 120) -> dict[str, Any]:
    model_date = latest_value(conn, "stock_model_scores", "trade_date")
    if not model_date:
        return {"trade_date": None, "rows": []}
    rows = conn.execute(
        """
        SELECT
            sm.priority_rank,
            sm.code,
            sm.name,
            sm.action_bucket,
            sm.total_score,
            sm.market_score,
            sm.evidence_availability_score,
            sm.formula_verification_score,
            sm.factor_score,
            sm.risk_score,
            sm.research_report_count_180d,
            sm.review_supported_count,
            sm.review_needs_review_count,
            sm.review_pending_count,
            xgs.gate_status,
            xgs.eligible_for_buy,
            xgs.supported_count,
            xgs.required_count,
            xgs.blocking_dimensions
        FROM stock_model_scores sm
        LEFT JOIN xinwei_gate_snapshots xgs
          ON xgs.code = sm.code
         AND xgs.snapshot_date = ?
        WHERE sm.trade_date = ?
        ORDER BY sm.priority_rank ASC
        LIMIT ?
        """,
        (model_date, model_date, limit),
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["blocking_dimensions"] = parse_json(item.get("blocking_dimensions")) or []
        result.append(item)
    return {"trade_date": model_date, "rows": result}


def light_tool_registry_payload(conn: Any) -> dict[str, Any]:
    tools = conn.execute(
        """
        SELECT
            name,
            repo_full_name AS provider,
            category,
            capability_layer AS status,
            priority,
            reuse_decision AS notes,
            last_reviewed_at AS last_checked_at
        FROM external_tool_registry
        ORDER BY priority ASC, name ASC
        """
    ).fetchall()
    roadmap = conn.execute(
        """
        SELECT
            capability_name AS capability,
            status,
            priority,
            source_repos AS owner,
            next_step AS notes,
            updated_at
        FROM capability_roadmap
        ORDER BY priority ASC, capability ASC
        """
    ).fetchall()
    return {"tools": [dict(row) for row in tools], "roadmap": [dict(row) for row in roadmap]}


def light_morning_report(summary: dict[str, Any], gate: dict[str, Any], tasks: dict[str, Any]) -> dict[str, Any]:
    report_date = today_cn()
    return {
        "report_date": report_date,
        "latest_trade_date": summary.get("latest_trade_date"),
        "model_trade_date": summary.get("model_trade_date"),
        "headline": f"{report_date} 在线快照使用本地最新模型日 {summary.get('model_trade_date') or '-'}。",
        "conclusion": "这是 Netlify 只读快照，用于在线浏览和复盘，不会自动刷新或发出交易指令。",
        "data_source_health": {
            "snapshot_rows": summary.get("snapshot_rows"),
            "model_score_count": summary.get("model_score_count"),
            "qa_status": summary.get("qa_status"),
            "qa_alert_count": summary.get("qa_alert_count"),
        },
        "paper_replay": {
            "total_count": summary.get("replay_total_count"),
            "positive_count": summary.get("replay_positive_count"),
            "avg_latest_return": summary.get("replay_avg_latest_return"),
            "latest_date": summary.get("replay_latest_date"),
        },
        "position_boundary": {
            "single_stock_cap": "15%",
            "rule": "证据不足只观察；公式核验未通过不输出明确买入。",
        },
        "gate_summary": {
            "total": summary.get("formula_gate_count"),
            "formula_supported": summary.get("formula_gate_supported_count"),
            "missing_evidence": summary.get("formula_gate_missing_count"),
            "needs_review": summary.get("formula_gate_needs_review_count"),
        },
        "queues": {
            "deep_research": summary.get("deep_research_count"),
            "wait_evidence": summary.get("wait_evidence_count"),
            "risk_watch": summary.get("risk_watch_count"),
        },
        "top_candidates": tasks.get("rows", [])[:12],
        "evidence_gap_tasks": [row for row in tasks.get("rows", []) if row.get("formula_verification_score") in (None, 0)][:20],
        "gate_matrix": gate.get("rows", [])[:20],
    }


def light_stock_payload(conn: Any, code: str) -> dict[str, Any]:
    metric_date = latest_value(conn, "watchlist_daily_metrics", "trade_date")
    model_date = latest_value(conn, "stock_model_scores", "trade_date")
    metric = row_dict(
        conn.execute(
            "SELECT * FROM watchlist_daily_metrics WHERE code = ? AND trade_date = ?",
            (code, metric_date),
        ).fetchone()
        if metric_date
        else None
    )
    model = row_dict(
        conn.execute(
            "SELECT * FROM stock_model_scores WHERE code = ? AND trade_date = ?",
            (code, model_date),
        ).fetchone()
        if model_date
        else None
    )
    gate = row_dict(
        conn.execute(
            "SELECT * FROM xinwei_gate_snapshots WHERE code = ? AND snapshot_date = ?",
            (code, model_date),
        ).fetchone()
        if model_date
        else None
    )
    coverage = row_dict(
        conn.execute(
            "SELECT * FROM stock_research_coverage WHERE code = ? ORDER BY as_of_date DESC LIMIT 1",
            (code,),
        ).fetchone()
    )
    evidence = [
        dict(row)
        for row in conn.execute(
            """
            SELECT evidence_date, source, evidence_type, evidence_grade, title, summary, source_url
            FROM stock_evidence_items
            WHERE code = ?
            ORDER BY evidence_date DESC, id DESC
            LIMIT 20
            """,
            (code,),
        ).fetchall()
    ]
    reviews = [
        dict(row)
        for row in conn.execute(
            """
            SELECT dimension_id, dimension_name, status, evidence_grade, score, summary, source_url, last_checked_at
            FROM stock_xinwei_reviews
            WHERE code = ?
            ORDER BY dimension_id ASC
            """,
            (code,),
        ).fetchall()
    ]
    history = [
        dict(row)
        for row in conn.execute(
            """
            SELECT trade_date, close, change_pct, amount
            FROM stock_kline_daily
            WHERE code = ?
            ORDER BY trade_date DESC
            LIMIT 90
            """,
            (code,),
        ).fetchall()
    ]
    if gate:
        gate["blocking_dimensions"] = parse_json(gate.get("blocking_dimensions")) or []
        gate["dimension_status"] = parse_json(gate.pop("dimension_status_json", None)) or {}
        gate["evidence_chain"] = parse_json(gate.pop("evidence_chain_json", None)) or {}
        gate["status"] = gate.get("gate_status")
        gate["label"] = gate.get("gate_status")
    name = metric.get("name") or model.get("name") or gate.get("name") or code
    return {
        "code": code,
        "name": name,
        "industry": metric.get("industry"),
        "metric": metric,
        "model_score": model,
        "formula_gate": gate,
        "research_coverage": coverage,
        "evidence_items": evidence,
        "xinwei_reviews": reviews,
        "history": list(reversed(history)),
    }


def static_index_html() -> str:
    html = INDEX_HTML
    if "function staticApiPath(path)" not in html:
        html = html.replace("    async function api(path) {", STATIC_API_HELPER + "\n    async function api(path) {")
    html = html.replace(
        "const response = await fetch(path, { headers: { 'Accept': 'application/json' } });",
        "const response = await fetch(staticApiPath(path), { headers: { 'Accept': 'application/json' } });",
    )
    html = html.replace("http://127.0.0.1:8765", "")
    return html


def export_site(db_path: Path, out_dir: Path, stock_limit: int) -> dict[str, Any]:
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "index.html").write_text(static_index_html(), encoding="utf-8")
    (out_dir / "_headers").write_text("/api/*\n  Content-Type: application/json; charset=utf-8\n", encoding="utf-8")
    (out_dir / "_redirects").write_text("/* /index.html 200\n", encoding="utf-8")

    with connect(db_path) as conn:
        init_db(conn)
        summary = summary_payload(conn)
        watchlist = {"rows": light_watchlist_rows(conn)}
        runs = light_runs_payload(conn, 50)
        tools = light_tool_registry_payload(conn)
        gate = light_gate_matrix_payload(conn, 80)
        tasks = light_research_tasks_payload(conn, 120)
        morning = light_morning_report(summary, gate, tasks)
        qa = latest_qa_payload(conn)
        weekly = latest_weekly_review_payload(conn)

        write_json(out_dir, "/api/summary", summary)
        write_json(out_dir, "/api/morning-report", morning)
        write_json(out_dir, "/api/watchlist", watchlist)
        write_json(out_dir, "/api/runs", runs)
        write_json(out_dir, "/api/tool-registry", tools)
        write_json(out_dir, "/api/gate-matrix", gate)
        write_json(out_dir, "/api/research-tasks", tasks)
        write_json(out_dir, "/api/qa-report", qa)
        write_json(out_dir, "/api/weekly-review", weekly)

        codes: list[str] = []
        seen: set[str] = set()
        for source in (
            [row.get("code") for row in watchlist["rows"]],
            [row.get("code") for row in gate.get("rows", [])],
            [row.get("code") for row in tasks.get("rows", [])],
            [row.get("code") for row in morning.get("top_candidates", [])],
        ):
            for code in source:
                if code and code not in seen:
                    seen.add(code)
                    codes.append(code)
        if stock_limit > 0:
            codes = codes[:stock_limit]
        exported = 0
        for code in codes:
            try:
                write_json(out_dir, f"/api/stock/{code}.json", light_stock_payload(conn, code))
                exported += 1
            except KeyError:
                continue

    return {
        "out_dir": str(out_dir),
        "stock_detail_count": exported,
        "summary_date": summary.get("latest_trade_date"),
        "model_date": summary.get("model_trade_date"),
        "weekly_review": {
            "week_start": summary.get("weekly_review_week_start"),
            "week_end": summary.get("weekly_review_week_end"),
            "candidate_count": summary.get("weekly_review_candidate_count"),
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export static A-share dashboard for Netlify")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--out", type=Path, default=Path("dist/netlify"))
    parser.add_argument("--stock-limit", type=int, default=500)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = export_site(args.db, args.out, args.stock_limit)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
