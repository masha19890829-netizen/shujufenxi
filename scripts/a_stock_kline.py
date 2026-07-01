#!/usr/bin/env python
"""Refresh daily K-line history for the local A-share research database."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))

from a_stock_daily import (
    DEFAULT_DB,
    UA,
    as_float,
    connect,
    eastmoney_get_url_json,
    init_db,
    market_name,
    now_cn,
)


EASTMONEY_KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
TENCENT_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"


@dataclass
class KlineRow:
    trade_date: str
    code: str
    name: str
    market: str
    open: float | None
    close: float | None
    high: float | None
    low: float | None
    volume_lot: float | None
    amount: float | None
    amplitude_pct: float | None
    change_pct: float | None
    change_amount: float | None
    turnover_pct: float | None
    source: str
    raw: dict[str, Any]


def market_id_for_code(code: str) -> int:
    if code.startswith(("4", "8")) or code.startswith("92"):
        return 0
    return 1 if code.startswith(("6", "9")) else 0


def tencent_symbol(code: str) -> str:
    if code.startswith(("4", "8")) or code.startswith("92"):
        return "bj" + code
    return ("sh" if market_id_for_code(code) == 1 else "sz") + code


def normalize_code(code: str) -> str:
    digits = "".join(ch for ch in code if ch.isdigit())
    if len(digits) >= 6:
        return digits[-6:]
    return digits.zfill(6)


def parse_eastmoney_kline_line(code: str, name: str, line: str) -> KlineRow | None:
    parts = line.split(",")
    if len(parts) < 11:
        return None
    market_id = market_id_for_code(code)
    raw = {
        "line": line,
        "fields": [
            "date",
            "open",
            "close",
            "high",
            "low",
            "volume_lot",
            "amount",
            "amplitude_pct",
            "change_pct",
            "change_amount",
            "turnover_pct",
        ],
    }
    return KlineRow(
        trade_date=parts[0],
        code=code,
        name=name,
        market=market_name(market_id, code),
        open=as_float(parts[1]),
        close=as_float(parts[2]),
        high=as_float(parts[3]),
        low=as_float(parts[4]),
        volume_lot=as_float(parts[5]),
        amount=as_float(parts[6]),
        amplitude_pct=as_float(parts[7]),
        change_pct=as_float(parts[8]),
        change_amount=as_float(parts[9]),
        turnover_pct=as_float(parts[10]),
        source="eastmoney-kline",
        raw=raw,
    )


def fetch_eastmoney_kline(code: str, days: int = 120) -> list[KlineRow]:
    code = normalize_code(code)
    market_id = market_id_for_code(code)
    params = {
        "secid": f"{market_id}.{code}",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": "101",
        "fqt": "1",
        "end": "20500101",
        "lmt": str(days),
    }
    payload = eastmoney_get_url_json(
        EASTMONEY_KLINE_URL,
        params=params,
        timeout=15,
        retries=4,
        referer="https://quote.eastmoney.com/",
        origin="https://quote.eastmoney.com",
    )
    data = payload.get("data") or {}
    name = data.get("name") or code
    rows = []
    for line in data.get("klines") or []:
        parsed = parse_eastmoney_kline_line(code, name, line)
        if parsed:
            rows.append(parsed)
    return rows


def parse_tencent_kline_row(code: str, name: str, row: list[Any]) -> KlineRow | None:
    if len(row) < 6:
        return None
    open_price = as_float(row[1])
    close_price = as_float(row[2])
    high_price = as_float(row[3])
    low_price = as_float(row[4])
    change_pct = as_float(row[7]) if len(row) >= 8 else None
    amplitude_pct = None
    if high_price is not None and low_price is not None and open_price:
        amplitude_pct = ((high_price - low_price) / open_price) * 100
    raw = {
        "row": row,
        "fields": [
            "date",
            "open",
            "close",
            "high",
            "low",
            "volume",
            "amount_or_turnover_optional",
            "change_pct_optional",
        ],
    }
    return KlineRow(
        trade_date=str(row[0])[:10],
        code=code,
        name=name or code,
        market=market_name(market_id_for_code(code), code),
        open=open_price,
        close=close_price,
        high=high_price,
        low=low_price,
        volume_lot=as_float(row[5]),
        amount=None,
        amplitude_pct=amplitude_pct,
        change_pct=change_pct,
        change_amount=None,
        turnover_pct=None,
        source="tencent-kline",
        raw=raw,
    )


def fetch_tencent_kline(code: str, days: int = 120) -> list[KlineRow]:
    code = normalize_code(code)
    symbol = tencent_symbol(code)
    params = {"param": f"{symbol},day,,,{days},qfq"}
    url = TENCENT_KLINE_URL + "?" + urlencode(params)
    headers = {
        "User-Agent": UA,
        "Referer": "https://gu.qq.com/",
        "Accept": "application/json,text/plain,*/*",
        "Connection": "close",
    }
    req = Request(url, headers=headers)
    with urlopen(req, timeout=15) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if payload.get("code") != 0:
        return []
    stock_data = (payload.get("data") or {}).get(symbol) or {}
    rows = stock_data.get("qfqday") or stock_data.get("day") or []
    qt = stock_data.get("qt")
    name = code
    if isinstance(qt, dict) and isinstance(qt.get(symbol), list) and len(qt[symbol]) > 1:
        name = qt[symbol][1] or code
    parsed_rows = []
    for row in rows:
        parsed = parse_tencent_kline_row(code, name, row)
        if parsed:
            parsed_rows.append(parsed)
    parsed_rows.sort(key=lambda item: item.trade_date)
    previous_close: float | None = None
    for row in parsed_rows:
        if previous_close:
            if row.close is not None:
                row.change_amount = row.close - previous_close
                if row.change_pct is None:
                    row.change_pct = (row.change_amount / previous_close) * 100
            if row.high is not None and row.low is not None:
                row.amplitude_pct = ((row.high - row.low) / previous_close) * 100
        previous_close = row.close if row.close is not None else previous_close
    return parsed_rows


def fetch_mootdx_kline(code: str, days: int = 120) -> list[KlineRow]:
    """Try mootdx if installed; return an empty list when unavailable or failing."""
    try:
        from mootdx.quotes import Quotes  # type: ignore
    except Exception:
        return []
    try:
        code = normalize_code(code)
        client = Quotes.factory(market="std")
        frame = client.bars(symbol=code, category=4, offset=days)
    except Exception:
        return []
    if frame is None or getattr(frame, "empty", False):
        return []
    rows: list[KlineRow] = []
    for _, item in frame.iterrows():
        date_value = item.get("datetime")
        trade_date = str(date_value)[:10]
        rows.append(
            KlineRow(
                trade_date=trade_date,
                code=code,
                name=code,
                market=market_name(market_id_for_code(code), code),
                open=as_float(item.get("open")),
                close=as_float(item.get("close")),
                high=as_float(item.get("high")),
                low=as_float(item.get("low")),
                volume_lot=as_float(item.get("vol")),
                amount=as_float(item.get("amount")),
                amplitude_pct=None,
                change_pct=None,
                change_amount=None,
                turnover_pct=None,
                source="mootdx-kline",
                raw={key: str(value) for key, value in dict(item).items()},
            )
        )
    return rows


def fetch_kline(code: str, days: int, provider: str = "auto") -> list[KlineRow]:
    if provider in {"auto", "mootdx"}:
        rows = fetch_mootdx_kline(code, days)
        if rows or provider == "mootdx":
            return rows
    if provider in {"auto", "tencent"}:
        rows = fetch_tencent_kline(code, days)
        if rows or provider == "tencent":
            return rows
    return fetch_eastmoney_kline(code, days)


def save_kline_rows(conn: sqlite3.Connection, rows: list[KlineRow]) -> int:
    if not rows:
        return 0
    created_at = now_cn().isoformat(timespec="seconds")
    payload = [
        (
            row.trade_date,
            row.code,
            row.name,
            row.market,
            row.open,
            row.close,
            row.high,
            row.low,
            row.volume_lot,
            row.amount,
            row.amplitude_pct,
            row.change_pct,
            row.change_amount,
            row.turnover_pct,
            row.source,
            json.dumps(row.raw, ensure_ascii=False, separators=(",", ":")),
            created_at,
        )
        for row in rows
    ]
    conn.executemany(
        """
        INSERT INTO stock_kline_daily(
            trade_date, code, name, market, open, close, high, low,
            volume_lot, amount, amplitude_pct, change_pct, change_amount,
            turnover_pct, source, raw_json, created_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(trade_date, code) DO UPDATE SET
            name=excluded.name,
            market=excluded.market,
            open=excluded.open,
            close=excluded.close,
            high=excluded.high,
            low=excluded.low,
            volume_lot=excluded.volume_lot,
            amount=excluded.amount,
            amplitude_pct=excluded.amplitude_pct,
            change_pct=excluded.change_pct,
            change_amount=excluded.change_amount,
            turnover_pct=excluded.turnover_pct,
            source=excluded.source,
            raw_json=excluded.raw_json,
            created_at=excluded.created_at
        """,
        payload,
    )
    return len(payload)


def model_scope_codes(conn: sqlite3.Connection, limit: int | None) -> list[tuple[str, str]]:
    date_row = conn.execute("SELECT MAX(trade_date) AS trade_date FROM stock_model_scores").fetchone()
    model_date = date_row["trade_date"] if date_row and date_row["trade_date"] else None
    if not model_date:
        return active_watchlist_codes(conn, limit)
    params: list[Any] = [model_date]
    limit_clause = ""
    if limit:
        limit_clause = "LIMIT ?"
        params.append(limit)
    rows = conn.execute(
        f"""
        SELECT sm.code, sm.name
        FROM stock_model_scores sm
        JOIN stock_watchlist w ON w.code = sm.code
        WHERE sm.trade_date = ?
          AND w.status = 'active'
        ORDER BY sm.priority_rank ASC
        {limit_clause}
        """,
        params,
    ).fetchall()
    return [(row["code"], row["name"]) for row in rows]


def active_watchlist_codes(conn: sqlite3.Connection, limit: int | None) -> list[tuple[str, str]]:
    params: list[Any] = []
    limit_clause = ""
    if limit:
        limit_clause = "LIMIT ?"
        params.append(limit)
    rows = conn.execute(
        f"""
        SELECT code, name
        FROM stock_watchlist
        WHERE status = 'active'
        ORDER BY first_recommended_date DESC, first_rank ASC
        {limit_clause}
        """,
        params,
    ).fetchall()
    return [(row["code"], row["name"]) for row in rows]


def all_watchlist_codes(conn: sqlite3.Connection, limit: int | None) -> list[tuple[str, str]]:
    params: list[Any] = []
    limit_clause = ""
    if limit:
        limit_clause = "LIMIT ?"
        params.append(limit)
    rows = conn.execute(
        f"""
        SELECT code, name
        FROM stock_watchlist
        ORDER BY status ASC, first_recommended_date DESC, first_rank ASC
        {limit_clause}
        """,
        params,
    ).fetchall()
    return [(row["code"], row["name"]) for row in rows]


def replay_missing_codes(conn: sqlite3.Connection, limit: int | None) -> list[tuple[str, str]]:
    """Prioritize stocks whose recommendation replay lacks post-run K-line rows."""
    params: list[Any] = []
    limit_clause = ""
    if limit:
        limit_clause = "LIMIT ?"
        params.append(limit)
    rows = conn.execute(
        f"""
        SELECT
            code,
            name,
            COUNT(*) AS missing_rows,
            MAX(run_date) AS latest_missing_run
        FROM paper_replay_results
        WHERE status IN ('no_entry_kline', 'stale_entry_gap')
        GROUP BY code, name
        ORDER BY missing_rows DESC, latest_missing_run DESC, code ASC
        {limit_clause}
        """,
        params,
    ).fetchall()
    return [(row["code"], row["name"]) for row in rows]


def select_codes(conn: sqlite3.Connection, scope: str, limit: int | None) -> list[tuple[str, str]]:
    if scope == "model":
        return model_scope_codes(conn, limit)
    if scope == "all":
        return all_watchlist_codes(conn, limit)
    return active_watchlist_codes(conn, limit)


def refresh_code(conn: sqlite3.Connection, code: str, days: int, provider: str = "auto") -> int:
    rows = fetch_kline(code, days, provider)
    return save_kline_rows(conn, rows)


def command_refresh_code(args: argparse.Namespace) -> None:
    with connect(args.db) as conn:
        init_db(conn)
        count = refresh_code(conn, args.code, args.days, args.provider)
        conn.commit()
    print(f"Refreshed {count} K-line rows for {normalize_code(args.code)}")


def command_refresh_watchlist(args: argparse.Namespace) -> None:
    limit = None if args.limit == 0 else args.limit
    with connect(args.db) as conn:
        init_db(conn)
        codes = select_codes(conn, args.scope, limit)
        total_rows = 0
        failures: list[str] = []
        for idx, (code, name) in enumerate(codes, start=1):
            try:
                count = refresh_code(conn, code, args.days, args.provider)
                total_rows += count
                conn.commit()
                if not args.quiet:
                    print(f"{idx:>3}/{len(codes)} {code} {name} rows={count}")
            except Exception as exc:
                conn.rollback()
                failures.append(f"{code} {name}: {exc}")
                if not args.quiet:
                    print(f"{idx:>3}/{len(codes)} {code} {name} failed: {exc}")
                if args.stop_on_error:
                    break
    print(f"Refreshed {total_rows} K-line rows for {len(codes) - len(failures)} stocks")
    if failures:
        print("Failures:")
        for item in failures[:20]:
            print(f"  {item}")


def command_refresh_replay_missing(args: argparse.Namespace) -> None:
    limit = None if args.limit == 0 else args.limit
    with connect(args.db) as conn:
        init_db(conn)
        codes = replay_missing_codes(conn, limit)
        total_rows = 0
        failures: list[str] = []
        for idx, (code, name) in enumerate(codes, start=1):
            try:
                count = refresh_code(conn, code, args.days, args.provider)
                total_rows += count
                conn.commit()
                if not args.quiet:
                    print(f"{idx:>3}/{len(codes)} {code} {name} rows={count}")
            except Exception as exc:
                conn.rollback()
                failures.append(f"{code} {name}: {exc}")
                if not args.quiet:
                    print(f"{idx:>3}/{len(codes)} {code} {name} failed: {exc}")
                if args.stop_on_error:
                    break
        replay_count = 0
        if not args.no_replay_refresh:
            from a_stock_replay import refresh_replay_results

            replay_count = len(refresh_replay_results(conn))
            conn.commit()
    print(f"Refreshed {total_rows} K-line rows for {len(codes) - len(failures)} replay-missing stocks")
    if not args.no_replay_refresh:
        print(f"Refreshed {replay_count} paper replay rows")
    if failures:
        print("Failures:")
        for item in failures[:20]:
            print(f"  {item}")


def command_show(args: argparse.Namespace) -> None:
    code = normalize_code(args.code)
    with connect(args.db) as conn:
        init_db(conn)
        rows = conn.execute(
            """
            SELECT trade_date, code, name, open, close, high, low, amount,
                   change_pct, turnover_pct, source
            FROM stock_kline_daily
            WHERE code = ?
            ORDER BY trade_date DESC
            LIMIT ?
            """,
            (code, args.top),
        ).fetchall()
    if not rows:
        print(f"No K-line rows for {code}.")
        return
    print(f"K-line rows for {code}:")
    for row in rows:
        print(
            f"{row['trade_date']} {row['name']} close={row['close']} "
            f"chg={row['change_pct']}% amount={row['amount']} source={row['source']}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Refresh local daily K-line history")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite database path")
    sub = parser.add_subparsers(dest="command", required=True)

    one = sub.add_parser("refresh-code", help="Refresh one stock's K-line history")
    one.add_argument("code", help="Six-digit A-share code")
    one.add_argument("--days", type=int, default=120, help="Number of daily bars to request")
    one.add_argument("--provider", choices=["auto", "mootdx", "tencent", "eastmoney"], default="auto")

    watch = sub.add_parser("refresh-watchlist", help="Refresh watchlist K-line history")
    watch.add_argument("--scope", choices=["model", "active", "all"], default="model")
    watch.add_argument("--limit", type=int, default=50, help="0 means no limit")
    watch.add_argument("--days", type=int, default=120, help="Number of daily bars to request")
    watch.add_argument("--provider", choices=["auto", "mootdx", "tencent", "eastmoney"], default="auto")
    watch.add_argument("--quiet", action="store_true", help="Suppress per-stock progress")
    watch.add_argument("--stop-on-error", action="store_true", help="Stop after first failed stock")

    missing = sub.add_parser("refresh-replay-missing", help="Refresh K-lines for candidates missing paper replay data")
    missing.add_argument("--limit", type=int, default=60, help="0 means no limit")
    missing.add_argument("--days", type=int, default=180, help="Number of daily bars to request")
    missing.add_argument("--provider", choices=["auto", "mootdx", "tencent", "eastmoney"], default="auto")
    missing.add_argument("--quiet", action="store_true", help="Suppress per-stock progress")
    missing.add_argument("--stop-on-error", action="store_true", help="Stop after first failed stock")
    missing.add_argument("--no-replay-refresh", action="store_true", help="Skip paper replay refresh after K-line fetch")

    show = sub.add_parser("show", help="Show stored K-line rows for one stock")
    show.add_argument("code", help="Six-digit A-share code")
    show.add_argument("--top", type=int, default=10)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "refresh-code":
            command_refresh_code(args)
        elif args.command == "refresh-watchlist":
            command_refresh_watchlist(args)
        elif args.command == "refresh-replay-missing":
            command_refresh_replay_missing(args)
        elif args.command == "show":
            command_show(args)
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
