#!/usr/bin/env python
"""Build a local A-share research database and daily candidate list.

This script follows the data-source discipline from simonlin1212/a-stock-data:
use broad quote snapshots for daily screening, keep Eastmoney requests serial
and slow, and store raw rows so later strategy changes remain auditable.
"""

from __future__ import annotations

import argparse
import http.client
import json
import math
import random
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    import requests
except ImportError:  # pragma: no cover - fallback for minimal Python installs.
    requests = None


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "a_stock.db"
STRATEGY_VERSION = "daily-candidate-v0.1"
CN_TZ = timezone(timedelta(hours=8))
MARKET_CLOSED_DATES_2026 = {
    # SSE/SZSE 2026 holiday schedule; weekends are also filtered separately.
    "2026-01-01",
    "2026-01-02",
    "2026-01-03",
    "2026-02-15",
    "2026-02-16",
    "2026-02-17",
    "2026-02-18",
    "2026-02-19",
    "2026-02-20",
    "2026-02-21",
    "2026-02-22",
    "2026-02-23",
    "2026-04-04",
    "2026-04-05",
    "2026-04-06",
    "2026-05-01",
    "2026-05-02",
    "2026-05-03",
    "2026-05-04",
    "2026-05-05",
    "2026-06-19",
    "2026-06-20",
    "2026-06-21",
    "2026-09-25",
    "2026-09-26",
    "2026-09-27",
    "2026-10-01",
    "2026-10-02",
    "2026-10-03",
    "2026-10-04",
    "2026-10-05",
    "2026-10-06",
    "2026-10-07",
}
XINWEI_DIMENSIONS = [
    ("industry_inflection", "产业拐点"),
    ("scarcity_position", "稀缺卡位"),
    ("leader_customer_binding", "双龙头客户绑定"),
    ("capacity_order_expansion", "产能/订单扩张"),
    ("earnings_inflection", "业绩拐点确认"),
    ("expectation_gap", "巨大预期差"),
]

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
EASTMONEY_CLIST_URLS = [
    "https://push2.eastmoney.com/api/qt/clist/get",
    "https://83.push2.eastmoney.com/api/qt/clist/get",
    "https://84.push2.eastmoney.com/api/qt/clist/get",
    "https://85.push2.eastmoney.com/api/qt/clist/get",
]
EASTMONEY_REFERER = "https://quote.eastmoney.com/center/gridlist.html"
EASTMONEY_MIN_INTERVAL = 1.7
_last_eastmoney_call = 0.0
_requests_session = requests.Session() if requests else None


def _configure_eastmoney_session() -> None:
    if _requests_session:
        _requests_session.trust_env = False
        _requests_session.headers.update({"User-Agent": UA, "Referer": EASTMONEY_REFERER})


def _reset_eastmoney_session() -> None:
    """Recover from remote disconnects caused by stale keep-alive sockets."""
    global _requests_session
    if not requests:
        return
    try:
        if _requests_session:
            _requests_session.close()
    finally:
        _requests_session = requests.Session()
        _configure_eastmoney_session()


_configure_eastmoney_session()

A_SHARE_FS = ",".join(
    [
        "m:0+t:6",          # Shenzhen main board A shares
        "m:0+t:80",         # ChiNext
        "m:1+t:2",          # Shanghai main board A shares
        "m:1+t:23",         # STAR market
        "m:0+t:81+s:2048",  # Beijing Stock Exchange
    ]
)

SNAPSHOT_FIELDS = ",".join(
    [
        "f2", "f3", "f4", "f5", "f6", "f7", "f8", "f9", "f10",
        "f12", "f13", "f14", "f15", "f16", "f17", "f18", "f20",
        "f21", "f23", "f24", "f25", "f62", "f100", "f115",
    ]
)


def now_cn() -> datetime:
    return datetime.now(CN_TZ)


def today_cn() -> str:
    return now_cn().date().isoformat()


def is_a_share_trading_day(date_text: str) -> bool:
    try:
        day = datetime.fromisoformat(date_text).date()
    except ValueError:
        return True
    if day.weekday() >= 5:
        return False
    return date_text not in MARKET_CLOSED_DATES_2026


def as_float(value: Any) -> float | None:
    if value in (None, "", "-", "--"):
        return None
    try:
        if isinstance(value, str):
            value = value.replace(",", "")
        result = float(value)
        if math.isnan(result) or math.isinf(result):
            return None
        return result
    except (TypeError, ValueError):
        return None


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def eastmoney_get_json(
    params: dict[str, str],
    timeout: int = 20,
    retries: int = 10,
) -> dict[str, Any]:
    global _last_eastmoney_call
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        wait = EASTMONEY_MIN_INTERVAL - (time.time() - _last_eastmoney_call)
        if wait > 0:
            time.sleep(wait + random.uniform(0.1, 0.5))
        base_url = EASTMONEY_CLIST_URLS[(attempt - 1) % len(EASTMONEY_CLIST_URLS)]
        try:
            if _requests_session:
                resp = _requests_session.get(base_url, params=params, timeout=timeout)
                resp.raise_for_status()
                return resp.json()
            url = base_url + "?" + urlencode(params)
            req = Request(url, headers={"User-Agent": UA, "Referer": EASTMONEY_REFERER})
            with urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (
            HTTPError,
            URLError,
            TimeoutError,
            http.client.RemoteDisconnected,
            json.JSONDecodeError,
            Exception,
        ) as exc:
            last_error = exc
            _reset_eastmoney_session()
            time.sleep((EASTMONEY_MIN_INTERVAL * attempt) + random.uniform(0.8, 2.0))
        finally:
            _last_eastmoney_call = time.time()
    raise RuntimeError(f"Eastmoney request failed after {retries} attempts: {last_error}")


def eastmoney_get_url_json(
    urls: str | list[str],
    params: dict[str, str],
    timeout: int = 20,
    retries: int = 4,
    referer: str = EASTMONEY_REFERER,
    origin: str | None = None,
) -> dict[str, Any]:
    """Eastmoney JSON request helper for non-clist endpoints with shared throttling."""
    global _last_eastmoney_call
    url_list = [urls] if isinstance(urls, str) else list(urls)
    headers = {"User-Agent": UA, "Referer": referer}
    if origin:
        headers["Origin"] = origin
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        wait = EASTMONEY_MIN_INTERVAL - (time.time() - _last_eastmoney_call)
        if wait > 0:
            time.sleep(wait)
        base_url = url_list[(attempt - 1) % len(url_list)]
        try:
            if _requests_session:
                resp = _requests_session.get(base_url, params=params, headers=headers, timeout=timeout)
                resp.raise_for_status()
                return resp.json()
            url = base_url + "?" + urlencode(params)
            req = Request(url, headers=headers)
            with urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (
            HTTPError,
            URLError,
            TimeoutError,
            http.client.RemoteDisconnected,
            json.JSONDecodeError,
            Exception,
        ) as exc:
            last_error = exc
            time.sleep((EASTMONEY_MIN_INTERVAL * attempt) + random.uniform(0.2, 0.8))
        finally:
            _last_eastmoney_call = time.time()
    raise RuntimeError(f"Eastmoney request failed after {retries} attempts: {last_error}")


def connect(db_path: Path = DEFAULT_DB) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS data_sources (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            priority INTEGER NOT NULL,
            url TEXT,
            usage TEXT,
            risk_note TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS external_tool_registry (
            repo_full_name TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            url TEXT NOT NULL,
            category TEXT NOT NULL,
            capability_layer TEXT NOT NULL,
            reuse_decision TEXT NOT NULL,
            priority INTEGER NOT NULL,
            license TEXT,
            source_level TEXT NOT NULL,
            risk_note TEXT NOT NULL,
            integration_plan TEXT NOT NULL,
            source_url TEXT NOT NULL,
            last_reviewed_at TEXT NOT NULL,
            raw_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_external_tool_registry_priority
            ON external_tool_registry(priority, reuse_decision, category);

        CREATE TABLE IF NOT EXISTS capability_roadmap (
            capability_id TEXT PRIMARY KEY,
            capability_name TEXT NOT NULL,
            status TEXT NOT NULL,
            priority INTEGER NOT NULL,
            source_repos TEXT NOT NULL,
            next_step TEXT NOT NULL,
            target_files TEXT NOT NULL,
            acceptance_criteria TEXT NOT NULL,
            raw_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_capability_roadmap_priority
            ON capability_roadmap(priority, status);

        CREATE TABLE IF NOT EXISTS market_snapshot (
            trade_date TEXT NOT NULL,
            code TEXT NOT NULL,
            name TEXT NOT NULL,
            market TEXT,
            industry TEXT,
            price REAL,
            change_pct REAL,
            change_amount REAL,
            volume_lot REAL,
            amount REAL,
            amplitude_pct REAL,
            turnover_pct REAL,
            pe_dynamic REAL,
            pe_ttm REAL,
            pb REAL,
            volume_ratio REAL,
            high REAL,
            low REAL,
            open REAL,
            last_close REAL,
            total_mcap REAL,
            float_mcap REAL,
            pct_60d REAL,
            pct_ytd REAL,
            main_net_inflow REAL,
            raw_json TEXT NOT NULL,
            source TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (trade_date, code)
        );

        CREATE INDEX IF NOT EXISTS idx_market_snapshot_date_score
            ON market_snapshot(trade_date, amount, change_pct, main_net_inflow);

        CREATE TABLE IF NOT EXISTS stock_kline_daily (
            trade_date TEXT NOT NULL,
            code TEXT NOT NULL,
            name TEXT NOT NULL,
            market TEXT,
            open REAL,
            close REAL,
            high REAL,
            low REAL,
            volume_lot REAL,
            amount REAL,
            amplitude_pct REAL,
            change_pct REAL,
            change_amount REAL,
            turnover_pct REAL,
            source TEXT NOT NULL,
            raw_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (trade_date, code)
        );

        CREATE INDEX IF NOT EXISTS idx_stock_kline_daily_code_date
            ON stock_kline_daily(code, trade_date);

        CREATE TABLE IF NOT EXISTS provider_health_checks (
            check_date TEXT NOT NULL,
            code TEXT NOT NULL,
            name TEXT NOT NULL,
            primary_source TEXT NOT NULL,
            comparison_source TEXT NOT NULL,
            primary_price REAL,
            comparison_price REAL,
            price_diff_pct REAL,
            primary_amount REAL,
            comparison_amount REAL,
            status TEXT NOT NULL,
            note TEXT,
            raw_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (check_date, code, primary_source, comparison_source)
        );

        CREATE INDEX IF NOT EXISTS idx_provider_health_checks_date_status
            ON provider_health_checks(check_date, status, price_diff_pct);

        CREATE TABLE IF NOT EXISTS stock_factor_daily (
            trade_date TEXT NOT NULL,
            code TEXT NOT NULL,
            name TEXT NOT NULL,
            source TEXT NOT NULL,
            sample_days INTEGER NOT NULL,
            quality_score REAL NOT NULL,
            price REAL,
            prev_price REAL,
            return_1d_pct REAL,
            momentum_5d_pct REAL,
            momentum_20d_pct REAL,
            ma5 REAL,
            ma20 REAL,
            ma60 REAL,
            ma5_sample_days INTEGER NOT NULL DEFAULT 0,
            ma20_sample_days INTEGER NOT NULL DEFAULT 0,
            ma60_sample_days INTEGER NOT NULL DEFAULT 0,
            volatility_5d_pct REAL,
            volatility_20d_pct REAL,
            high_20d REAL,
            low_20d REAL,
            drawdown_20d_pct REAL,
            rsi14 REAL,
            macd_dif REAL,
            macd_dea REAL,
            macd_hist REAL,
            boll_mid REAL,
            boll_upper REAL,
            boll_lower REAL,
            boll_position_pct REAL,
            atr14_pct REAL,
            amount_ma5 REAL,
            amount_ma20 REAL,
            turnover_ma5 REAL,
            main_net_inflow_ma5 REAL,
            main_net_inflow_to_amount_pct REAL,
            liquidity_score REAL NOT NULL,
            trend_score REAL NOT NULL,
            flow_score REAL NOT NULL,
            volatility_score REAL NOT NULL,
            valuation_score REAL NOT NULL,
            technical_score REAL NOT NULL DEFAULT 50,
            composite_score REAL NOT NULL,
            factor_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (trade_date, code)
        );

        CREATE INDEX IF NOT EXISTS idx_stock_factor_daily_date_score
            ON stock_factor_daily(trade_date, composite_score DESC, quality_score DESC);

        CREATE TABLE IF NOT EXISTS recommendation_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT NOT NULL,
            created_at TEXT NOT NULL,
            strategy_version TEXT NOT NULL,
            universe_count INTEGER NOT NULL,
            candidate_count INTEGER NOT NULL,
            notes TEXT
        );

        CREATE TABLE IF NOT EXISTS recommendation_candidates (
            run_id INTEGER NOT NULL,
            rank INTEGER NOT NULL,
            code TEXT NOT NULL,
            name TEXT NOT NULL,
            score REAL NOT NULL,
            action_label TEXT NOT NULL,
            reasons TEXT NOT NULL,
            risk_flags TEXT NOT NULL,
            metrics_json TEXT NOT NULL,
            PRIMARY KEY (run_id, rank),
            FOREIGN KEY (run_id) REFERENCES recommendation_runs(id)
        );

        CREATE TABLE IF NOT EXISTS paper_replay_results (
            run_id INTEGER NOT NULL,
            rank INTEGER NOT NULL,
            code TEXT NOT NULL,
            name TEXT NOT NULL,
            run_date TEXT NOT NULL,
            strategy_version TEXT NOT NULL,
            candidate_score REAL NOT NULL,
            candidate_price REAL,
            entry_date TEXT,
            entry_price REAL,
            entry_source TEXT NOT NULL,
            latest_date TEXT,
            latest_close REAL,
            trading_days_observed INTEGER NOT NULL DEFAULT 0,
            return_1d_pct REAL,
            return_3d_pct REAL,
            return_5d_pct REAL,
            return_10d_pct REAL,
            return_20d_pct REAL,
            latest_return_pct REAL,
            max_close_return_pct REAL,
            max_intraday_return_pct REAL,
            worst_intraday_return_pct REAL,
            max_drawdown_pct REAL,
            best_date TEXT,
            worst_date TEXT,
            take_profit_5_hit INTEGER NOT NULL DEFAULT 0,
            stop_loss_5_hit INTEGER NOT NULL DEFAULT 0,
            stop_loss_8_hit INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            note TEXT,
            raw_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (run_id, rank),
            FOREIGN KEY (run_id) REFERENCES recommendation_runs(id)
        );

        CREATE INDEX IF NOT EXISTS idx_paper_replay_results_status
            ON paper_replay_results(run_date, status, latest_return_pct);

        CREATE INDEX IF NOT EXISTS idx_paper_replay_results_code
            ON paper_replay_results(code, run_date);

        CREATE TABLE IF NOT EXISTS stock_watchlist (
            code TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            first_recommended_date TEXT NOT NULL,
            first_run_id INTEGER NOT NULL,
            first_rank INTEGER NOT NULL,
            first_score REAL NOT NULL,
            first_price REAL,
            first_metrics_json TEXT NOT NULL,
            thesis TEXT,
            risk_flags TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (first_run_id) REFERENCES recommendation_runs(id)
        );

        CREATE INDEX IF NOT EXISTS idx_stock_watchlist_status_date
            ON stock_watchlist(status, first_recommended_date);

        CREATE TABLE IF NOT EXISTS stock_research_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            note_date TEXT NOT NULL,
            category TEXT NOT NULL,
            evidence_grade TEXT,
            title TEXT NOT NULL,
            body TEXT,
            source_url TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (code) REFERENCES stock_watchlist(code)
        );

        CREATE INDEX IF NOT EXISTS idx_stock_research_notes_code_date
            ON stock_research_notes(code, note_date);

        CREATE TABLE IF NOT EXISTS stock_xinwei_reviews (
            code TEXT NOT NULL,
            dimension_id TEXT NOT NULL,
            dimension_name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            evidence_grade TEXT,
            score REAL,
            summary TEXT,
            source_url TEXT,
            last_checked_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (code, dimension_id),
            FOREIGN KEY (code) REFERENCES stock_watchlist(code)
        );

        CREATE INDEX IF NOT EXISTS idx_stock_xinwei_reviews_status
            ON stock_xinwei_reviews(status, dimension_id);

        CREATE TABLE IF NOT EXISTS stock_evidence_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            name TEXT,
            evidence_date TEXT,
            source TEXT NOT NULL,
            evidence_type TEXT NOT NULL,
            evidence_grade TEXT NOT NULL,
            title TEXT NOT NULL,
            summary TEXT,
            source_url TEXT,
            raw_json TEXT NOT NULL,
            collected_at TEXT NOT NULL,
            UNIQUE(code, source, evidence_type, title, evidence_date)
        );

        CREATE INDEX IF NOT EXISTS idx_stock_evidence_items_code_type_date
            ON stock_evidence_items(code, evidence_type, evidence_date);

        CREATE TABLE IF NOT EXISTS stock_research_coverage (
            code TEXT NOT NULL,
            as_of_date TEXT NOT NULL,
            report_count_total INTEGER NOT NULL,
            report_count_180d INTEGER NOT NULL,
            org_count INTEGER NOT NULL,
            latest_report_date TEXT,
            latest_rating TEXT,
            raw_json TEXT NOT NULL,
            collected_at TEXT NOT NULL,
            PRIMARY KEY (code, as_of_date)
        );

        CREATE TABLE IF NOT EXISTS xinwei_evidence_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            name TEXT,
            dimension_id TEXT NOT NULL,
            dimension_name TEXT NOT NULL,
            evidence_item_id INTEGER NOT NULL,
            evidence_grade TEXT NOT NULL,
            evidence_status TEXT NOT NULL,
            match_reason TEXT NOT NULL,
            match_keywords TEXT NOT NULL,
            source TEXT,
            evidence_type TEXT,
            title TEXT NOT NULL,
            evidence_date TEXT,
            source_url TEXT,
            is_manual_confirmed INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(code, dimension_id, evidence_item_id),
            FOREIGN KEY (evidence_item_id) REFERENCES stock_evidence_items(id)
        );

        CREATE INDEX IF NOT EXISTS idx_xinwei_evidence_links_code_dim
            ON xinwei_evidence_links(code, dimension_id, evidence_status, evidence_grade);

        CREATE TABLE IF NOT EXISTS xinwei_gate_snapshots (
            snapshot_date TEXT NOT NULL,
            code TEXT NOT NULL,
            name TEXT NOT NULL,
            gate_status TEXT NOT NULL,
            eligible_for_buy INTEGER NOT NULL DEFAULT 0,
            supported_count INTEGER NOT NULL DEFAULT 0,
            needs_review_count INTEGER NOT NULL DEFAULT 0,
            pending_count INTEGER NOT NULL DEFAULT 0,
            failed_count INTEGER NOT NULL DEFAULT 0,
            stale_count INTEGER NOT NULL DEFAULT 0,
            required_count INTEGER NOT NULL DEFAULT 6,
            blocking_dimensions TEXT NOT NULL,
            dimension_status_json TEXT NOT NULL,
            evidence_chain_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (snapshot_date, code)
        );

        CREATE INDEX IF NOT EXISTS idx_xinwei_gate_snapshots_status
            ON xinwei_gate_snapshots(snapshot_date, gate_status, eligible_for_buy);

        CREATE TABLE IF NOT EXISTS research_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_date TEXT NOT NULL,
            code TEXT NOT NULL,
            name TEXT NOT NULL,
            dimension_id TEXT NOT NULL,
            dimension_name TEXT NOT NULL,
            task_type TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 3,
            status TEXT NOT NULL DEFAULT 'open',
            title TEXT NOT NULL,
            detail TEXT NOT NULL,
            source TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(task_date, code, dimension_id, task_type)
        );

        CREATE INDEX IF NOT EXISTS idx_research_tasks_status_priority
            ON research_tasks(status, priority, task_date, code);

        CREATE TABLE IF NOT EXISTS stock_model_scores (
            trade_date TEXT NOT NULL,
            code TEXT NOT NULL,
            name TEXT NOT NULL,
            model_version TEXT NOT NULL,
            priority_rank INTEGER,
            action_bucket TEXT NOT NULL,
            total_score REAL NOT NULL,
            market_score REAL NOT NULL,
            evidence_score REAL NOT NULL,
            evidence_availability_score REAL NOT NULL DEFAULT 0,
            formula_verification_score REAL NOT NULL DEFAULT 0,
            behavior_score REAL NOT NULL,
            factor_score REAL NOT NULL DEFAULT 50,
            factor_quality_score REAL NOT NULL DEFAULT 0,
            risk_score REAL NOT NULL,
            latest_candidate_score REAL,
            latest_return_pct REAL,
            latest_drawdown_pct REAL,
            evidence_item_count INTEGER NOT NULL DEFAULT 0,
            s_evidence_count INTEGER NOT NULL DEFAULT 0,
            a_evidence_count INTEGER NOT NULL DEFAULT 0,
            review_supported_count INTEGER NOT NULL DEFAULT 0,
            review_needs_review_count INTEGER NOT NULL DEFAULT 0,
            review_pending_count INTEGER NOT NULL DEFAULT 0,
            review_failed_count INTEGER NOT NULL DEFAULT 0,
            research_org_count INTEGER,
            research_report_count_180d INTEGER,
            score_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (trade_date, code)
        );

        CREATE INDEX IF NOT EXISTS idx_stock_model_scores_date_rank
            ON stock_model_scores(trade_date, priority_rank);

        CREATE TABLE IF NOT EXISTS watchlist_daily_metrics (
            trade_date TEXT NOT NULL,
            code TEXT NOT NULL,
            name TEXT NOT NULL,
            status TEXT NOT NULL,
            first_recommended_date TEXT NOT NULL,
            first_price REAL,
            price REAL,
            return_pct REAL,
            max_drawdown_pct REAL,
            days_since_first INTEGER,
            change_pct REAL,
            amount REAL,
            turnover_pct REAL,
            main_net_inflow REAL,
            pe_ttm REAL,
            pb REAL,
            latest_score REAL,
            industry TEXT,
            created_at TEXT NOT NULL,
            PRIMARY KEY (trade_date, code),
            FOREIGN KEY (code) REFERENCES stock_watchlist(code)
        );

        CREATE INDEX IF NOT EXISTS idx_watchlist_daily_metrics_code_date
            ON watchlist_daily_metrics(code, trade_date);

        CREATE TABLE IF NOT EXISTS ingestion_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            rows INTEGER DEFAULT 0,
            message TEXT
        );

        CREATE TABLE IF NOT EXISTS qa_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT NOT NULL,
            mode TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            failure_rate REAL NOT NULL DEFAULT 0,
            missing_field_rate REAL NOT NULL DEFAULT 0,
            api_p95_ms REAL,
            alert_count INTEGER NOT NULL DEFAULT 0,
            summary_json TEXT NOT NULL,
            markdown_report TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_qa_runs_date_status
            ON qa_runs(run_date, status, id);

        CREATE TABLE IF NOT EXISTS qa_checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            check_id TEXT NOT NULL,
            severity TEXT NOT NULL,
            status TEXT NOT NULL,
            metric_value REAL,
            threshold_value REAL,
            message TEXT NOT NULL,
            detail_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (run_id) REFERENCES qa_runs(id)
        );

        CREATE INDEX IF NOT EXISTS idx_qa_checks_run_status
            ON qa_checks(run_id, status, role, severity);

        CREATE TABLE IF NOT EXISTS qa_tasks (
            task_id TEXT PRIMARY KEY,
            role TEXT NOT NULL,
            scenario TEXT NOT NULL,
            input_ref TEXT NOT NULL,
            expected TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 3,
            owner TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_qa_tasks_status_priority
            ON qa_tasks(status, priority, role);

        CREATE TABLE IF NOT EXISTS qa_quant_reference (
            run_id INTEGER NOT NULL,
            trade_date TEXT NOT NULL,
            code TEXT NOT NULL,
            name TEXT NOT NULL,
            action_bucket TEXT NOT NULL,
            reference_status TEXT NOT NULL,
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
            missing_triggers TEXT NOT NULL,
            detail_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (run_id, code),
            FOREIGN KEY (run_id) REFERENCES qa_runs(id)
        );

        CREATE INDEX IF NOT EXISTS idx_qa_quant_reference_run_ev
            ON qa_quant_reference(run_id, reference_status, ev_pct);

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

        CREATE TABLE IF NOT EXISTS qa_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            severity TEXT NOT NULL,
            status TEXT NOT NULL,
            title TEXT NOT NULL,
            detail TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (run_id) REFERENCES qa_runs(id)
        );

        CREATE INDEX IF NOT EXISTS idx_qa_alerts_run_status
            ON qa_alerts(run_id, status, severity);

        CREATE TABLE IF NOT EXISTS weekly_review_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            week_start TEXT NOT NULL,
            week_end TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            status TEXT NOT NULL,
            recommendation_run_count INTEGER NOT NULL DEFAULT 0,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            candidate_event_count INTEGER NOT NULL DEFAULT 0,
            replay_coverage_rate REAL NOT NULL DEFAULT 0,
            avg_latest_return_pct REAL,
            median_latest_return_pct REAL,
            positive_rate REAL,
            avg_max_drawdown_pct REAL,
            take_profit_5_rate REAL,
            stop_loss_5_rate REAL,
            alert_count INTEGER NOT NULL DEFAULT 0,
            best_stock TEXT,
            worst_stock TEXT,
            summary_json TEXT NOT NULL,
            logic_review_json TEXT NOT NULL,
            markdown_report TEXT NOT NULL,
            UNIQUE(week_start, week_end)
        );

        CREATE INDEX IF NOT EXISTS idx_weekly_review_runs_week
            ON weekly_review_runs(week_start, week_end, status);

        CREATE TABLE IF NOT EXISTS weekly_review_stocks (
            review_run_id INTEGER NOT NULL,
            run_id INTEGER NOT NULL,
            run_date TEXT NOT NULL,
            rank INTEGER NOT NULL,
            code TEXT NOT NULL,
            name TEXT NOT NULL,
            candidate_score REAL NOT NULL,
            occurrence_count INTEGER NOT NULL DEFAULT 1,
            action_bucket TEXT,
            industry TEXT,
            entry_date TEXT,
            latest_date TEXT,
            trading_days_observed INTEGER NOT NULL DEFAULT 0,
            latest_return_pct REAL,
            return_1d_pct REAL,
            return_3d_pct REAL,
            return_5d_pct REAL,
            max_intraday_return_pct REAL,
            worst_intraday_return_pct REAL,
            max_drawdown_pct REAL,
            take_profit_5_hit INTEGER NOT NULL DEFAULT 0,
            stop_loss_5_hit INTEGER NOT NULL DEFAULT 0,
            replay_status TEXT NOT NULL,
            formula_gate_status TEXT,
            formula_supported_count INTEGER,
            formula_required_count INTEGER,
            evidence_availability_score REAL,
            formula_verification_score REAL,
            lesson_tag TEXT NOT NULL,
            lesson_note TEXT NOT NULL,
            detail_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (review_run_id, run_id, rank),
            FOREIGN KEY (review_run_id) REFERENCES weekly_review_runs(id)
        );

        CREATE INDEX IF NOT EXISTS idx_weekly_review_stocks_code
            ON weekly_review_stocks(code, run_date);

        CREATE TABLE IF NOT EXISTS weekly_logic_insights (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            review_run_id INTEGER NOT NULL,
            insight_type TEXT NOT NULL,
            severity TEXT NOT NULL,
            title TEXT NOT NULL,
            detail TEXT NOT NULL,
            metric_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (review_run_id) REFERENCES weekly_review_runs(id)
        );

        CREATE INDEX IF NOT EXISTS idx_weekly_logic_insights_run_type
            ON weekly_logic_insights(review_run_id, insight_type, severity);
        """
    )
    ensure_schema_migrations(conn)
    seed_sources(conn)
    sync_watchlist_from_recommendations(conn)
    seed_xinwei_reviews(conn)
    conn.commit()


def ensure_schema_migrations(conn: sqlite3.Connection) -> None:
    model_existing = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(stock_model_scores)").fetchall()
    }
    model_migrations = [
        ("factor_score", "ALTER TABLE stock_model_scores ADD COLUMN factor_score REAL NOT NULL DEFAULT 50"),
        (
            "factor_quality_score",
            "ALTER TABLE stock_model_scores ADD COLUMN factor_quality_score REAL NOT NULL DEFAULT 0",
        ),
        (
            "evidence_availability_score",
            "ALTER TABLE stock_model_scores ADD COLUMN evidence_availability_score REAL NOT NULL DEFAULT 0",
        ),
        (
            "formula_verification_score",
            "ALTER TABLE stock_model_scores ADD COLUMN formula_verification_score REAL NOT NULL DEFAULT 0",
        ),
    ]
    for column, sql in model_migrations:
        if column not in model_existing:
            conn.execute(sql)

    factor_existing = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(stock_factor_daily)").fetchall()
    }
    factor_migrations = [
        ("rsi14", "ALTER TABLE stock_factor_daily ADD COLUMN rsi14 REAL"),
        ("macd_dif", "ALTER TABLE stock_factor_daily ADD COLUMN macd_dif REAL"),
        ("macd_dea", "ALTER TABLE stock_factor_daily ADD COLUMN macd_dea REAL"),
        ("macd_hist", "ALTER TABLE stock_factor_daily ADD COLUMN macd_hist REAL"),
        ("boll_mid", "ALTER TABLE stock_factor_daily ADD COLUMN boll_mid REAL"),
        ("boll_upper", "ALTER TABLE stock_factor_daily ADD COLUMN boll_upper REAL"),
        ("boll_lower", "ALTER TABLE stock_factor_daily ADD COLUMN boll_lower REAL"),
        ("boll_position_pct", "ALTER TABLE stock_factor_daily ADD COLUMN boll_position_pct REAL"),
        ("atr14_pct", "ALTER TABLE stock_factor_daily ADD COLUMN atr14_pct REAL"),
        ("technical_score", "ALTER TABLE stock_factor_daily ADD COLUMN technical_score REAL NOT NULL DEFAULT 50"),
    ]
    for column, sql in factor_migrations:
        if column not in factor_existing:
            conn.execute(sql)

    weekly_run_existing = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(weekly_review_runs)").fetchall()
    }
    weekly_run_migrations = [
        (
            "candidate_event_count",
            "ALTER TABLE weekly_review_runs ADD COLUMN candidate_event_count INTEGER NOT NULL DEFAULT 0",
        ),
        ("alert_count", "ALTER TABLE weekly_review_runs ADD COLUMN alert_count INTEGER NOT NULL DEFAULT 0"),
    ]
    for column, sql in weekly_run_migrations:
        if column not in weekly_run_existing:
            conn.execute(sql)

    weekly_stock_existing = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(weekly_review_stocks)").fetchall()
    }
    weekly_stock_migrations = [
        (
            "occurrence_count",
            "ALTER TABLE weekly_review_stocks ADD COLUMN occurrence_count INTEGER NOT NULL DEFAULT 1",
        ),
    ]
    for column, sql in weekly_stock_migrations:
        if column not in weekly_stock_existing:
            conn.execute(sql)


def seed_sources(conn: sqlite3.Connection) -> None:
    updated_at = now_cn().isoformat(timespec="seconds")
    rows = [
        (
            "a-stock-data",
            "simonlin1212/a-stock-data",
            0,
            "https://github.com/simonlin1212/a-stock-data",
            "Source map and endpoint discipline for A-share research.",
            "The project provides tools, not investment advice.",
        ),
        (
            "mootdx",
            "TDX/mootdx market data",
            1,
            "https://github.com/mootdx/mootdx",
            "Preferred future source for K-line, quotes, F10, and finance snapshots.",
            "Requires the mootdx Python dependency and stable access to TDX servers.",
        ),
        (
            "tencent-quote",
            "Tencent Finance quote API",
            2,
            "https://qt.gtimg.cn/",
            "Preferred quote and valuation enrichment source from a-stock-data.",
            "GBK response; field indexes need validation when Tencent changes payload.",
        ),
        (
            "tencent-kline",
            "Tencent Finance daily K-line API",
            3,
            "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
            "Current local fallback for daily K-line history when mootdx is unavailable.",
            "Lightweight HTTP source; amount and turnover fields need enrichment from snapshots.",
        ),
        (
            "eastmoney-clist",
            "Eastmoney full-market quote snapshot",
            4,
            EASTMONEY_CLIST_URLS[0],
            "Daily whole-market seed table: quote, turnover, valuation, market cap, main inflow.",
            "Use serial requests. Avoid high-frequency polling.",
        ),
        (
            "eastmoney-kline",
            "Eastmoney push2his daily K-line",
            5,
            "https://push2his.eastmoney.com/api/qt/stock/kline/get",
            "Last-resort daily K-line fallback for local factor depth.",
            "Use the shared serial Eastmoney limiter; some networks may see connection-level blocking.",
        ),
    ]
    conn.executemany(
        """
        INSERT INTO data_sources(id, name, priority, url, usage, risk_note, updated_at)
        VALUES(?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            name=excluded.name,
            priority=excluded.priority,
            url=excluded.url,
            usage=excluded.usage,
            risk_note=excluded.risk_note,
            updated_at=excluded.updated_at
        """,
        [(*row, updated_at) for row in rows],
    )

def metrics_price(metrics_json: str) -> float | None:
    try:
        metrics = json.loads(metrics_json)
    except json.JSONDecodeError:
        return None
    return as_float(metrics.get("price"))


def sync_watchlist_from_recommendations(conn: sqlite3.Connection) -> int:
    """Backfill the long-term watchlist from earlier recommendation runs."""
    rows = conn.execute(
        """
        SELECT
            rc.code,
            rc.name,
            rr.run_date,
            rc.run_id,
            rc.rank,
            rc.score,
            rc.reasons,
            rc.risk_flags,
            rc.metrics_json,
            rr.created_at
        FROM recommendation_candidates rc
        JOIN recommendation_runs rr ON rr.id = rc.run_id
        ORDER BY rr.run_date ASC, rc.run_id ASC, rc.rank ASC
        """
    ).fetchall()
    inserted = 0
    seen: set[str] = set()
    now = now_cn().isoformat(timespec="seconds")
    for row in rows:
        code = row["code"]
        if code in seen:
            continue
        seen.add(code)
        thesis = f"脚本初筛理由：{row['reasons'] or '待补充产业命题验证'}"
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO stock_watchlist(
                code, name, status, first_recommended_date, first_run_id, first_rank,
                first_score, first_price, first_metrics_json, thesis, risk_flags,
                created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                code,
                row["name"],
                "active",
                row["run_date"],
                row["run_id"],
                row["rank"],
                row["score"],
                metrics_price(row["metrics_json"]),
                row["metrics_json"],
                thesis,
                row["risk_flags"],
                row["created_at"] or now,
                now,
            ),
        )
        inserted += cur.rowcount
    return inserted


def seed_xinwei_reviews(conn: sqlite3.Connection) -> int:
    """Create pending six-factor research checklist rows for watchlist stocks."""
    codes = [row["code"] for row in conn.execute("SELECT code FROM stock_watchlist").fetchall()]
    if not codes:
        return 0
    now = now_cn().isoformat(timespec="seconds")
    rows = [
        (code, dimension_id, dimension_name, "pending", now, now)
        for code in codes
        for dimension_id, dimension_name in XINWEI_DIMENSIONS
    ]
    cur = conn.executemany(
        """
        INSERT OR IGNORE INTO stock_xinwei_reviews(
            code, dimension_id, dimension_name, status, created_at, updated_at
        ) VALUES(?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    return cur.rowcount


def upsert_watchlist_entries(
    conn: sqlite3.Connection,
    run_id: int,
    trade_date: str,
    created_at: str,
    candidates: list["Candidate"],
) -> None:
    rows = []
    for idx, item in enumerate(candidates, start=1):
        metrics_json = json.dumps(item.metrics, ensure_ascii=False, separators=(",", ":"))
        reasons = "；".join(item.reasons) or "待补充产业命题验证"
        risk_text = "；".join(item.risk_flags) if item.risk_flags else "无明显规则风险"
        rows.append(
            (
                item.code,
                item.name,
                "active",
                trade_date,
                run_id,
                idx,
                item.score,
                item.metrics.get("price"),
                metrics_json,
                f"脚本初筛理由：{reasons}",
                risk_text,
                created_at,
                created_at,
            )
        )
    conn.executemany(
        """
        INSERT INTO stock_watchlist(
            code, name, status, first_recommended_date, first_run_id, first_rank,
            first_score, first_price, first_metrics_json, thesis, risk_flags,
            created_at, updated_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(code) DO UPDATE SET
            name=excluded.name,
            risk_flags=excluded.risk_flags,
            updated_at=excluded.updated_at
        """,
        rows,
    )
    seed_xinwei_reviews(conn)


def days_between(start_date: str | None, end_date: str | None) -> int | None:
    if not start_date or not end_date:
        return None
    try:
        return (datetime.fromisoformat(end_date).date() - datetime.fromisoformat(start_date).date()).days
    except ValueError:
        return None


def refresh_watchlist_metrics(conn: sqlite3.Connection) -> int:
    rows = conn.execute(
        """
        SELECT
            w.code,
            w.name,
            w.status,
            w.first_recommended_date,
            w.first_price,
            ms.trade_date,
            ms.price,
            ms.change_pct,
            ms.amount,
            ms.turnover_pct,
            ms.main_net_inflow,
            ms.pe_ttm,
            ms.pb,
            ms.industry,
            (
                SELECT rc.score
                FROM recommendation_candidates rc
                JOIN recommendation_runs rr ON rr.id = rc.run_id
                WHERE rc.code = w.code
                  AND rr.run_date <= ms.trade_date
                ORDER BY rr.run_date DESC, rc.run_id DESC, rc.rank ASC
                LIMIT 1
            ) AS latest_score
        FROM stock_watchlist w
        JOIN market_snapshot ms
          ON ms.code = w.code
         AND ms.trade_date >= w.first_recommended_date
        WHERE w.first_price IS NOT NULL
        ORDER BY w.code, ms.trade_date
        """
    ).fetchall()
    if not rows:
        return 0

    created_at = now_cn().isoformat(timespec="seconds")
    payload: list[tuple[Any, ...]] = []
    peaks: dict[str, float] = {}
    for row in rows:
        code = row["code"]
        price = row["price"]
        first_price = row["first_price"]
        if price is None:
            continue
        price = float(price)
        first_price = float(first_price) if first_price else 0.0
        previous_peak = peaks.get(code)
        peak = price if previous_peak is None else max(previous_peak, price)
        peaks[code] = peak
        return_pct = round((price / first_price - 1) * 100, 2) if first_price else None
        drawdown_pct = round((price / peak - 1) * 100, 2) if peak else None
        payload.append(
            (
                row["trade_date"],
                code,
                row["name"],
                row["status"],
                row["first_recommended_date"],
                first_price or None,
                price,
                return_pct,
                drawdown_pct,
                days_between(row["first_recommended_date"], row["trade_date"]),
                row["change_pct"],
                row["amount"],
                row["turnover_pct"],
                row["main_net_inflow"],
                row["pe_ttm"],
                row["pb"],
                row["latest_score"],
                row["industry"],
                created_at,
            )
        )

    conn.executemany(
        """
        INSERT INTO watchlist_daily_metrics(
            trade_date, code, name, status, first_recommended_date, first_price,
            price, return_pct, max_drawdown_pct, days_since_first, change_pct,
            amount, turnover_pct, main_net_inflow, pe_ttm, pb, latest_score,
            industry, created_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(trade_date, code) DO UPDATE SET
            name=excluded.name,
            status=excluded.status,
            first_recommended_date=excluded.first_recommended_date,
            first_price=excluded.first_price,
            price=excluded.price,
            return_pct=excluded.return_pct,
            max_drawdown_pct=excluded.max_drawdown_pct,
            days_since_first=excluded.days_since_first,
            change_pct=excluded.change_pct,
            amount=excluded.amount,
            turnover_pct=excluded.turnover_pct,
            main_net_inflow=excluded.main_net_inflow,
            pe_ttm=excluded.pe_ttm,
            pb=excluded.pb,
            latest_score=excluded.latest_score,
            industry=excluded.industry,
            created_at=excluded.created_at
        """,
        payload,
    )
    return len(payload)


def market_name(market_id: Any, code: str) -> str:
    if str(market_id) == "1":
        return "SH"
    if code.startswith("8") or code.startswith("4") or code.startswith("9"):
        return "BJ"
    return "SZ"


def normalize_snapshot_row(row: dict[str, Any], trade_date: str, created_at: str) -> dict[str, Any]:
    code = str(row.get("f12", "")).zfill(6)
    return {
        "trade_date": trade_date,
        "code": code,
        "name": str(row.get("f14", "")),
        "market": market_name(row.get("f13"), code),
        "industry": row.get("f100"),
        "price": as_float(row.get("f2")),
        "change_pct": as_float(row.get("f3")),
        "change_amount": as_float(row.get("f4")),
        "volume_lot": as_float(row.get("f5")),
        "amount": as_float(row.get("f6")),
        "amplitude_pct": as_float(row.get("f7")),
        "turnover_pct": as_float(row.get("f8")),
        "pe_dynamic": as_float(row.get("f9")),
        "pe_ttm": as_float(row.get("f115")),
        "pb": as_float(row.get("f23")),
        "volume_ratio": as_float(row.get("f10")),
        "high": as_float(row.get("f15")),
        "low": as_float(row.get("f16")),
        "open": as_float(row.get("f17")),
        "last_close": as_float(row.get("f18")),
        "total_mcap": as_float(row.get("f20")),
        "float_mcap": as_float(row.get("f21")),
        "pct_60d": as_float(row.get("f24")),
        "pct_ytd": as_float(row.get("f25")),
        "main_net_inflow": as_float(row.get("f62")),
        "raw_json": json.dumps(row, ensure_ascii=False, separators=(",", ":")),
        "source": "eastmoney-clist",
        "created_at": created_at,
    }


def fetch_eastmoney_snapshot(progress: bool = True) -> list[dict[str, Any]]:
    page_size = 200
    params = {
        "pn": "1",
        "pz": str(page_size),
        "po": "1",
        "np": "1",
        "fltt": "2",
        "invt": "2",
        "fid": "f3",
        "fs": A_SHARE_FS,
        "fields": SNAPSHOT_FIELDS,
    }
    first = eastmoney_get_json(params)
    data = first.get("data") or {}
    total = int(data.get("total") or 0)
    rows = list(data.get("diff") or [])
    total_pages = max(1, math.ceil(total / page_size))
    if progress:
        print(f"Eastmoney snapshot: {total} stocks, {total_pages} pages")
    for page in range(2, total_pages + 1):
        params["pn"] = str(page)
        try:
            payload = eastmoney_get_json(params)
        except Exception as exc:
            if progress:
                print(f"  retrying page {page}/{total_pages} after source disconnect")
            time.sleep(8 + random.uniform(0.5, 2.0))
            try:
                payload = eastmoney_get_json(params, retries=12)
            except Exception as retry_exc:
                raise RuntimeError(
                    f"failed to fetch Eastmoney snapshot page {page}/{total_pages}"
                ) from retry_exc
        page_rows = (payload.get("data") or {}).get("diff") or []
        rows.extend(page_rows)
        if progress and (page == total_pages or page % 10 == 0):
            print(f"  fetched page {page}/{total_pages}, rows={len(rows)}")
    return rows


def insert_snapshot(conn: sqlite3.Connection, rows: list[dict[str, Any]], trade_date: str) -> int:
    created_at = now_cn().isoformat(timespec="seconds")
    normalized = [normalize_snapshot_row(row, trade_date, created_at) for row in rows]
    normalized = [row for row in normalized if row["code"] and row["name"]]
    deduped = {(row["trade_date"], row["code"]): row for row in normalized}
    normalized = list(deduped.values())
    columns = list(normalized[0].keys()) if normalized else []
    if not columns:
        return 0
    placeholders = ",".join(["?"] * len(columns))
    update_cols = [col for col in columns if col not in {"trade_date", "code"}]
    updates = ",".join(f"{col}=excluded.{col}" for col in update_cols)
    sql = (
        f"INSERT INTO market_snapshot({','.join(columns)}) "
        f"VALUES({placeholders}) "
        f"ON CONFLICT(trade_date, code) DO UPDATE SET {updates}"
    )
    conn.executemany(sql, [[row[col] for col in columns] for row in normalized])
    conn.commit()
    return len(normalized)


@dataclass
class Candidate:
    code: str
    name: str
    score: float
    reasons: list[str]
    risk_flags: list[str]
    metrics: dict[str, Any]


def risk_flags(row: sqlite3.Row) -> list[str]:
    flags: list[str] = []
    name = row["name"] or ""
    if "ST" in name.upper() or "退" in name:
        flags.append("特殊处理/退市风险名称")
    if (row["amount"] or 0) < 300_000_000:
        flags.append("成交额不足3亿")
    if row["pe_ttm"] is None or row["pe_ttm"] <= 0:
        flags.append("PE为负或缺失")
    if row["pb"] is not None and row["pb"] > 10:
        flags.append("PB偏高")
    if (row["change_pct"] or 0) >= 8.8:
        flags.append("当日涨幅接近涨停，追高风险")
    if (row["turnover_pct"] or 0) > 18:
        flags.append("换手过高")
    if (row["float_mcap"] or 0) < 2_000_000_000:
        flags.append("流通市值偏小")
    if (row["main_net_inflow"] or 0) <= 0:
        flags.append("主力净流入不占优")
    return flags


def score_row(row: sqlite3.Row) -> Candidate | None:
    name = row["name"] or ""
    change_pct = row["change_pct"] or 0
    amount = row["amount"] or 0
    turnover = row["turnover_pct"] or 0
    volume_ratio = row["volume_ratio"] or 0
    pe_ttm = row["pe_ttm"]
    pb = row["pb"]
    float_mcap = row["float_mcap"] or 0
    main_net = row["main_net_inflow"] or 0

    if "ST" in name.upper() or "退" in name:
        return None
    if not row["price"] or row["price"] <= 0:
        return None
    if amount < 300_000_000:
        return None
    if float_mcap < 2_000_000_000:
        return None
    if not (0.8 <= change_pct <= 8.8):
        return None
    if main_net <= 0:
        return None
    if pe_ttm is not None and pe_ttm <= 0:
        return None

    score = 0.0
    reasons: list[str] = []

    liquidity_score = 15 * clamp(amount / 1_500_000_000)
    score += liquidity_score
    if amount >= 800_000_000:
        reasons.append("成交额充足")

    momentum_score = 18 * clamp(change_pct / 6.0)
    if change_pct > 6:
        momentum_score -= 4 * clamp((change_pct - 6) / 2.8)
    score += momentum_score
    reasons.append(f"日内趋势为正({change_pct:.2f}%)")

    flow_ratio = main_net / amount if amount else 0
    flow_score = 26 * clamp(flow_ratio / 0.12)
    score += flow_score
    if flow_ratio >= 0.04:
        reasons.append(f"主力净流入占成交额{flow_ratio * 100:.1f}%")

    if 1.5 <= turnover <= 10:
        turnover_score = 12
        reasons.append("换手处于可跟踪区间")
    elif 10 < turnover <= 15:
        turnover_score = 7
    else:
        turnover_score = 4 * clamp(turnover / 1.5)
    score += turnover_score

    volume_score = 10 * clamp(volume_ratio / 2.5)
    if volume_ratio > 4.0:
        volume_score -= 3
    score += max(0, volume_score)
    if volume_ratio >= 1.2:
        reasons.append("量比放大")

    valuation_score = 0.0
    if pe_ttm is not None:
        if 0 < pe_ttm <= 25:
            valuation_score += 10
            reasons.append("PE(TTM)低于25")
        elif pe_ttm <= 50:
            valuation_score += 7
        elif pe_ttm <= 80:
            valuation_score += 4
    if pb is not None:
        if 0 < pb <= 3:
            valuation_score += 6
            reasons.append("PB低于3")
        elif pb <= 6:
            valuation_score += 4
        elif pb <= 10:
            valuation_score += 1
    score += valuation_score

    size_score = 0.0
    if 5_000_000_000 <= float_mcap <= 120_000_000_000:
        size_score = 7
        reasons.append("流通市值适中")
    elif float_mcap > 120_000_000_000:
        size_score = 4
    score += size_score

    flags = risk_flags(row)
    penalty = 0.0
    for flag in flags:
        if "追高" in flag:
            penalty += 8
        elif "PB" in flag:
            penalty += 4
        elif "换手过高" in flag:
            penalty += 6
        else:
            penalty += 3
    score -= penalty

    factor_scores = {
        "liquidity": round(liquidity_score, 2),
        "momentum": round(momentum_score, 2),
        "capital_flow": round(flow_score, 2),
        "turnover": round(turnover_score, 2),
        "volume_ratio": round(max(0, volume_score), 2),
        "valuation": round(valuation_score, 2),
        "float_mcap_fit": round(size_score, 2),
        "risk_penalty": round(penalty, 2),
        "raw_score_before_penalty": round(score + penalty, 2),
        "final_score": round(score, 2),
    }
    metrics = {
        "industry": row["industry"],
        "price": row["price"],
        "change_pct": change_pct,
        "amount_yi": round(amount / 100_000_000, 2),
        "turnover_pct": turnover,
        "volume_ratio": volume_ratio,
        "pe_ttm": pe_ttm,
        "pb": pb,
        "float_mcap_yi": round(float_mcap / 100_000_000, 2),
        "main_net_inflow_wan": round(main_net / 10_000, 2),
        "main_net_inflow_to_amount_pct": round(flow_ratio * 100, 2),
        "factor_scores": factor_scores,
        "strategy_version": STRATEGY_VERSION,
    }
    return Candidate(row["code"], row["name"], round(score, 2), reasons, flags, metrics)


def load_snapshot_rows(conn: sqlite3.Connection, trade_date: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT * FROM market_snapshot
            WHERE trade_date = ?
            ORDER BY amount DESC
            """,
            (trade_date,),
        )
    )


def build_candidates(conn: sqlite3.Connection, trade_date: str, top: int) -> tuple[int, list[Candidate]]:
    rows = load_snapshot_rows(conn, trade_date)
    scored = [candidate for row in rows if (candidate := score_row(row)) is not None]
    scored.sort(key=lambda item: item.score, reverse=True)
    return len(rows), scored[:top]


def save_recommendations(
    conn: sqlite3.Connection,
    trade_date: str,
    universe_count: int,
    candidates: list[Candidate],
) -> int:
    created_at = now_cn().isoformat(timespec="seconds")
    notes = (
        "Rule-based daily candidate screen. It is a research watchlist, not a promise "
        "of return or personalized financial advice."
    )
    cur = conn.execute(
        """
        INSERT INTO recommendation_runs(
            run_date, created_at, strategy_version, universe_count, candidate_count, notes
        ) VALUES(?, ?, ?, ?, ?, ?)
        """,
        (trade_date, created_at, STRATEGY_VERSION, universe_count, len(candidates), notes),
    )
    run_id = int(cur.lastrowid)
    conn.executemany(
        """
        INSERT INTO recommendation_candidates(
            run_id, rank, code, name, score, action_label, reasons, risk_flags, metrics_json
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                run_id,
                idx,
                item.code,
                item.name,
                item.score,
                "买入候选/观察",
                "；".join(item.reasons),
                "；".join(item.risk_flags) if item.risk_flags else "无明显规则风险",
                json.dumps(item.metrics, ensure_ascii=False, separators=(",", ":")),
            )
            for idx, item in enumerate(candidates, start=1)
        ],
    )
    upsert_watchlist_entries(conn, run_id, trade_date, created_at, candidates)
    refresh_watchlist_metrics(conn)
    conn.commit()
    return run_id


def print_candidates(candidates: list[Candidate]) -> None:
    if not candidates:
        print("No candidates matched the current strategy.")
        return
    print("rank code   name       score industry        chg% amount(亿) main净流入(万) PE  PB")
    for idx, item in enumerate(candidates, start=1):
        m = item.metrics
        print(
            f"{idx:>4} {item.code:<6} {item.name[:6]:<8} {item.score:>5.1f} "
            f"{str(m.get('industry') or '')[:8]:<10} "
            f"{m.get('change_pct') or 0:>5.2f} {m.get('amount_yi') or 0:>9.2f} "
            f"{m.get('main_net_inflow_wan') or 0:>13.0f} "
            f"{m.get('pe_ttm') if m.get('pe_ttm') is not None else '-':>5} "
            f"{m.get('pb') if m.get('pb') is not None else '-':>5}"
        )
        print(f"     reasons: {'；'.join(item.reasons)}")
        if item.risk_flags:
            print(f"     risks: {'；'.join(item.risk_flags)}")


def command_init(args: argparse.Namespace) -> None:
    with connect(args.db) as conn:
        init_db(conn)
        from a_stock_factors import refresh_stock_factors

        row = conn.execute("SELECT MAX(trade_date) AS trade_date FROM market_snapshot").fetchone()
        factor_date = row["trade_date"] if row and row["trade_date"] else None
        factor_count = len(refresh_stock_factors(conn, factor_date))
        count = refresh_watchlist_metrics(conn)
        conn.commit()
    print(f"Initialized database: {args.db}")
    if factor_count:
        print(f"Refreshed {factor_count} stock factor rows")
    if count:
        print(f"Refreshed {count} watchlist daily metric rows")


def command_update(args: argparse.Namespace) -> bool:
    with connect(args.db) as conn:
        init_db(conn)
        if not is_a_share_trading_day(args.date):
            started_at = now_cn().isoformat(timespec="seconds")
            message = f"A-share market closed on {args.date}; skipped full-market snapshot."
            conn.execute(
                """
                INSERT INTO ingestion_log(source, started_at, finished_at, status, rows, message)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                ("market-calendar", started_at, started_at, "skipped", 0, message),
            )
            conn.commit()
            print(message)
            return False
        started_at = now_cn().isoformat(timespec="seconds")
        cur = conn.execute(
            "INSERT INTO ingestion_log(source, started_at, status) VALUES(?, ?, ?)",
            ("eastmoney-clist", started_at, "running"),
        )
        log_id = int(cur.lastrowid)
        conn.commit()
        try:
            rows = fetch_eastmoney_snapshot(progress=not args.quiet)
            count = insert_snapshot(conn, rows, args.date)
            from a_stock_factors import refresh_stock_factors

            factor_count = len(refresh_stock_factors(conn, args.date))
            metric_count = refresh_watchlist_metrics(conn)
            conn.execute(
                """
                UPDATE ingestion_log
                SET finished_at=?, status=?, rows=?, message=?
                WHERE id=?
                """,
                (
                    now_cn().isoformat(timespec="seconds"),
                    "ok",
                    count,
                    f"snapshot saved; factors refreshed={factor_count}; watchlist metrics refreshed={metric_count}",
                    log_id,
                ),
            )
            conn.commit()
            print(f"Saved {count} rows for {args.date} into {args.db}")
            print(f"Refreshed {factor_count} stock factor rows")
            print(f"Refreshed {metric_count} watchlist daily metric rows")
            return True
        except Exception as exc:
            conn.execute(
                """
                UPDATE ingestion_log
                SET finished_at=?, status=?, message=?
                WHERE id=?
                """,
                (now_cn().isoformat(timespec="seconds"), "failed", str(exc), log_id),
            )
            conn.commit()
            raise
    return False


def command_recommend(args: argparse.Namespace) -> None:
    with connect(args.db) as conn:
        init_db(conn)
        universe_count, candidates = build_candidates(conn, args.date, args.top)
        run_id = save_recommendations(conn, args.date, universe_count, candidates)
    print(f"Recommendation run #{run_id}: universe={universe_count}, candidates={len(candidates)}")
    print_candidates(candidates)


def command_run_daily(args: argparse.Namespace) -> None:
    updated = command_update(args)
    if updated is False:
        print("Recommendation run skipped because this is not an A-share trading day.")
        return
    command_recommend(args)


def command_show_latest(args: argparse.Namespace) -> None:
    with connect(args.db) as conn:
        run = conn.execute(
            """
            SELECT * FROM recommendation_runs
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        if not run:
            print("No recommendation runs found.")
            return
        rows = conn.execute(
            """
            SELECT * FROM recommendation_candidates
            WHERE run_id=?
            ORDER BY rank
            LIMIT ?
            """,
            (run["id"], args.top),
        ).fetchall()
    print(
        f"Latest run #{run['id']} date={run['run_date']} "
        f"strategy={run['strategy_version']} candidates={run['candidate_count']}"
    )
    for row in rows:
        metrics = json.loads(row["metrics_json"])
        print(
            f"{row['rank']:>2}. {row['code']} {row['name']} score={row['score']} "
            f"chg={metrics.get('change_pct')}% amount={metrics.get('amount_yi')}亿 "
            f"main={metrics.get('main_net_inflow_wan')}万"
        )
        print(f"    {row['reasons']}")
        print(f"    risk: {row['risk_flags']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local A-share database and daily candidate screen")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite database path")
    parser.add_argument("--date", default=today_cn(), help="Trade/snapshot date, YYYY-MM-DD")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="Create database tables")

    update = sub.add_parser("update", help="Fetch and store full-market snapshot")
    update.add_argument("--quiet", action="store_true", help="Suppress page progress")

    recommend = sub.add_parser("recommend", help="Score latest stored snapshot")
    recommend.add_argument("--top", type=int, default=20, help="Number of candidates to store/show")

    daily = sub.add_parser("run-daily", help="Fetch snapshot then score candidates")
    daily.add_argument("--top", type=int, default=20, help="Number of candidates to store/show")
    daily.add_argument("--quiet", action="store_true", help="Suppress page progress")

    latest = sub.add_parser("show-latest", help="Print most recent recommendation run")
    latest.add_argument("--top", type=int, default=20, help="Number of candidates to show")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        {
            "init": command_init,
            "update": command_update,
            "recommend": command_recommend,
            "run-daily": command_run_daily,
            "show-latest": command_show_latest,
        }[args.command](args)
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
