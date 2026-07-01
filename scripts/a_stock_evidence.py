#!/usr/bin/env python
"""Collect source-backed evidence for the Xinwei Formula research checklist."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

try:
    import requests
except ImportError as exc:  # pragma: no cover
    raise SystemExit("requests is required: python -m pip install requests") from exc

sys.path.insert(0, str(Path(__file__).resolve().parent))

from a_stock_daily import DEFAULT_DB, XINWEI_DIMENSIONS, connect, init_db, now_cn, today_cn


UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
SESSION = requests.Session()
SESSION.trust_env = False
SESSION.headers.update({"User-Agent": UA})
CNINFO_STOCK_JSON = "https://www.cninfo.com.cn/new/data/szse_stock.json"
CNINFO_QUERY_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
EASTMONEY_REPORT_URL = "https://reportapi.eastmoney.com/report/list"
EM_MIN_INTERVAL = 1.6
_last_em_call = 0.0


@dataclass
class EvidenceItem:
    code: str
    name: str | None
    evidence_date: str | None
    source: str
    evidence_type: str
    evidence_grade: str
    title: str
    summary: str | None
    source_url: str | None
    raw: dict[str, Any]


def clean_text(value: Any) -> str:
    text = "" if value is None else str(value)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", text).strip()


def as_date(value: Any) -> str | None:
    if value in (None, "", "--", "-"):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000).date().isoformat()
        except (OSError, OverflowError, ValueError):
            return None
    text = str(value)
    match = re.search(r"20\d{2}[-/]\d{1,2}[-/]\d{1,2}", text)
    if match:
        return match.group(0).replace("/", "-")
    match = re.search(r"20\d{6}", text)
    if match:
        raw = match.group(0)
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return text[:10] if len(text) >= 10 else text


def market_prefix(code: str) -> str:
    if code.startswith(("6", "9")):
        return "sh"
    if code.startswith(("8", "4")):
        return "bj"
    return "sz"


def stock_name(conn, code: str) -> str | None:
    row = conn.execute(
        """
        SELECT name FROM stock_watchlist WHERE code=?
        UNION ALL
        SELECT name FROM market_snapshot WHERE code=? ORDER BY name LIMIT 1
        """,
        (code, code),
    ).fetchone()
    return row["name"] if row else None


def cninfo_orgid_map() -> dict[str, str]:
    try:
        resp = SESSION.get(CNINFO_STOCK_JSON, timeout=20, headers={"Referer": "https://www.cninfo.com.cn/"})
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return {}
    return {item["code"]: item["orgId"] for item in data.get("stockList", []) if item.get("code") and item.get("orgId")}


def fallback_orgid(code: str) -> str:
    if code.startswith("6"):
        return f"gssh0{code}"
    if code.startswith(("8", "4")):
        return f"gsbj0{code}"
    return f"gssz0{code}"


def fetch_cninfo_announcements(code: str, name: str | None, page_size: int = 30) -> list[EvidenceItem]:
    org_id = cninfo_orgid_map().get(code) or fallback_orgid(code)
    payload = {
        "stock": f"{code},{org_id}",
        "tabName": "fulltext",
        "pageSize": str(page_size),
        "pageNum": "1",
        "column": "",
        "category": "",
        "plate": "",
        "seDate": "",
        "searchkey": "",
        "secid": "",
        "sortName": "",
        "sortType": "",
        "isHLtitle": "true",
    }
    headers = {
        "User-Agent": UA,
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": "https://www.cninfo.com.cn/new/disclosure",
        "Origin": "https://www.cninfo.com.cn",
    }
    resp = SESSION.post(CNINFO_QUERY_URL, data=payload, headers=headers, timeout=25)
    resp.raise_for_status()
    data = resp.json()
    items: list[EvidenceItem] = []
    for row in data.get("announcements", []) or []:
        title = clean_text(row.get("announcementTitle"))
        anno_id = row.get("announcementId") or row.get("announcementIdString")
        items.append(
            EvidenceItem(
                code=code,
                name=name or clean_text(row.get("secName")),
                evidence_date=as_date(row.get("announcementTime")),
                source="cninfo",
                evidence_type="announcement",
                evidence_grade="S",
                title=title,
                summary=clean_text(row.get("announcementTypeName")),
                source_url=(
                    f"https://www.cninfo.com.cn/new/disclosure/detail?annoId={anno_id}"
                    if anno_id
                    else None
                ),
                raw=row,
            )
        )
    return items


def em_get_json(url: str, params: dict[str, Any]) -> dict[str, Any]:
    global _last_em_call
    wait = EM_MIN_INTERVAL - (time.time() - _last_em_call)
    if wait > 0:
        time.sleep(wait)
    headers = {"User-Agent": UA, "Referer": "https://data.eastmoney.com/"}
    try:
        resp = SESSION.get(url, params=params, headers=headers, timeout=30)
        resp.raise_for_status()
        text = resp.text.strip()
        if text.startswith(("jQuery", "datatable")) and "(" in text and text.endswith(")"):
            text = text[text.index("(") + 1 : -1]
        return json.loads(text)
    finally:
        _last_em_call = time.time()


def fetch_eastmoney_reports(
    code: str,
    name: str | None,
    max_pages: int = 2,
    begin_date: str | None = None,
) -> tuple[list[EvidenceItem], dict[str, Any]]:
    begin_date = begin_date or (date.today() - timedelta(days=720)).isoformat()
    end_date = date.today().isoformat()
    records: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        params = {
            "industryCode": "*",
            "pageSize": "100",
            "industry": "*",
            "rating": "*",
            "ratingChange": "*",
            "beginTime": begin_date,
            "endTime": end_date,
            "pageNo": str(page),
            "fields": "",
            "qType": "0",
            "orgCode": "",
            "code": code,
            "rcode": "",
            "p": str(page),
            "pageNum": str(page),
            "pageNumber": str(page),
        }
        data = em_get_json(EASTMONEY_REPORT_URL, params)
        page_rows = data.get("data") or []
        if not page_rows:
            break
        records.extend(page_rows)
        total_pages = int(data.get("TotalPage") or data.get("totalPage") or 1)
        if page >= total_pages:
            break
    items: list[EvidenceItem] = []
    for row in records:
        title = clean_text(row.get("title"))
        info_code = row.get("infoCode")
        summary_parts = [
            clean_text(row.get("orgSName")),
            clean_text(row.get("emRatingName")),
            clean_text(row.get("indvInduName")),
        ]
        summary = " | ".join(part for part in summary_parts if part)
        items.append(
            EvidenceItem(
                code=code,
                name=name,
                evidence_date=as_date(row.get("publishDate")),
                source="eastmoney-reportapi",
                evidence_type="research_report",
                evidence_grade="A",
                title=title,
                summary=summary,
                source_url=f"https://data.eastmoney.com/report/info/{info_code}.html" if info_code else None,
                raw=row,
            )
        )
    orgs = {clean_text(row.get("orgSName")) for row in records if clean_text(row.get("orgSName"))}
    latest_date = max((as_date(row.get("publishDate")) for row in records if as_date(row.get("publishDate"))), default=None)
    cutoff = date.today() - timedelta(days=180)
    recent_count = 0
    for row in records:
        report_date = as_date(row.get("publishDate"))
        if not report_date:
            continue
        try:
            if datetime.fromisoformat(report_date).date() >= cutoff:
                recent_count += 1
        except ValueError:
            pass
    latest_rating = None
    if records:
        latest_rating = clean_text(records[0].get("emRatingName")) or None
    coverage = {
        "report_count_total": len(records),
        "report_count_180d": recent_count,
        "org_count": len(orgs),
        "latest_report_date": latest_date,
        "latest_rating": latest_rating,
        "records_preview": records[:20],
    }
    return items, coverage


def save_evidence_items(conn, items: list[EvidenceItem]) -> int:
    if not items:
        return 0
    collected_at = now_cn().isoformat(timespec="seconds")
    rows = [
        (
            item.code,
            item.name,
            item.evidence_date,
            item.source,
            item.evidence_type,
            item.evidence_grade,
            item.title,
            item.summary,
            item.source_url,
            json.dumps(item.raw, ensure_ascii=False, separators=(",", ":")),
            collected_at,
        )
        for item in items
        if item.title
    ]
    cur = conn.executemany(
        """
        INSERT OR IGNORE INTO stock_evidence_items(
            code, name, evidence_date, source, evidence_type, evidence_grade,
            title, summary, source_url, raw_json, collected_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    return cur.rowcount


def save_research_coverage(conn, code: str, coverage: dict[str, Any]) -> None:
    collected_at = now_cn().isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT INTO stock_research_coverage(
            code, as_of_date, report_count_total, report_count_180d, org_count,
            latest_report_date, latest_rating, raw_json, collected_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(code, as_of_date) DO UPDATE SET
            report_count_total=excluded.report_count_total,
            report_count_180d=excluded.report_count_180d,
            org_count=excluded.org_count,
            latest_report_date=excluded.latest_report_date,
            latest_rating=excluded.latest_rating,
            raw_json=excluded.raw_json,
            collected_at=excluded.collected_at
        """,
        (
            code,
            today_cn(),
            int(coverage.get("report_count_total") or 0),
            int(coverage.get("report_count_180d") or 0),
            int(coverage.get("org_count") or 0),
            coverage.get("latest_report_date"),
            coverage.get("latest_rating"),
            json.dumps(coverage, ensure_ascii=False, separators=(",", ":")),
            collected_at,
        ),
    )


DIMENSION_KEYWORDS = {
    "industry_inflection": ["AI", "人工智能", "折叠", "射频", "天线", "卫星", "机器人", "端侧", "消费电子"],
    "scarcity_position": ["专利", "认证", "独家", "首家", "技术", "壁垒", "龙头", "射频", "天线"],
    "leader_customer_binding": ["客户", "供应", "供应商", "苹果", "华为", "小米", "三星", "英伟达", "特斯拉"],
    "capacity_order_expansion": ["订单", "合同", "中标", "产能", "扩产", "募投", "定增", "项目", "投资"],
    "earnings_inflection": ["年度报告", "季度报告", "半年度报告", "业绩", "扣非", "利润", "营收", "收入"],
}


def evidence_text(row: Any) -> str:
    return f"{row['title']} {row['summary'] or ''}"


def grade_rank(grade: str | None) -> int:
    return {"S": 3, "A": 2, "B": 1, "C": 0}.get(grade or "", -1)


def summarize_dimension(conn, code: str, dimension_id: str) -> tuple[str, str | None, float | None, str | None, str | None]:
    rows = conn.execute(
        """
        SELECT evidence_date, evidence_type, evidence_grade, title, summary, source_url
        FROM stock_evidence_items
        WHERE code=?
        ORDER BY evidence_date DESC, id DESC
        LIMIT 80
        """,
        (code,),
    ).fetchall()
    matches = []
    keywords = DIMENSION_KEYWORDS.get(dimension_id, [])
    for row in rows:
        text = evidence_text(row)
        if any(keyword in text for keyword in keywords):
            matches.append(row)

    if dimension_id == "expectation_gap":
        cov = conn.execute(
            """
            SELECT * FROM stock_research_coverage
            WHERE code=?
            ORDER BY as_of_date DESC
            LIMIT 1
            """,
            (code,),
        ).fetchone()
        if not cov:
            return "pending", None, None, "暂未采集机构覆盖度，无法判断预期差。", None
        count_180 = cov["report_count_180d"]
        org_count = cov["org_count"]
        if count_180 <= 5:
            summary = f"近180天研报{count_180}篇、机构{org_count}家，可能存在覆盖不足；需结合市场认知和旧业务估值复核。"
            return "needs_review", "A", 0.35, summary, None
        summary = f"近180天研报{count_180}篇、机构{org_count}家，覆盖度不低；预期差需从目标价分歧或产业认知滞后继续验证。"
        return "needs_review", "A", 0.2, summary, None

    if not matches:
        return "pending", None, None, "尚未采集到匹配该维度的 S/A 级标题线索。", None

    best_grade = max((row["evidence_grade"] for row in matches), key=grade_rank)
    latest = matches[0]
    summary = (
        f"采集到{len(matches)}条相关线索，最高等级{best_grade}。"
        f"最新：{latest['evidence_date'] or '-'} {latest['title']}。需人工核验是否满足信维公式。"
    )
    score = 0.45 if best_grade == "S" else 0.35
    return "needs_review", best_grade, score, summary, latest["source_url"]


def refresh_xinwei_reviews_from_evidence(conn, code: str) -> None:
    now = now_cn().isoformat(timespec="seconds")
    for dimension_id, dimension_name in XINWEI_DIMENSIONS:
        status, grade, score, summary, source_url = summarize_dimension(conn, code, dimension_id)
        conn.execute(
            """
            INSERT INTO stock_xinwei_reviews(
                code, dimension_id, dimension_name, status, evidence_grade,
                score, summary, source_url, last_checked_at, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(code, dimension_id) DO UPDATE SET
                dimension_name=excluded.dimension_name,
                status=CASE
                    WHEN stock_xinwei_reviews.status IN ('supported', 'failed') THEN stock_xinwei_reviews.status
                    ELSE excluded.status
                END,
                evidence_grade=CASE
                    WHEN stock_xinwei_reviews.status IN ('supported', 'failed') THEN stock_xinwei_reviews.evidence_grade
                    ELSE excluded.evidence_grade
                END,
                score=CASE
                    WHEN stock_xinwei_reviews.status IN ('supported', 'failed') THEN stock_xinwei_reviews.score
                    ELSE excluded.score
                END,
                summary=CASE
                    WHEN stock_xinwei_reviews.status IN ('supported', 'failed') THEN stock_xinwei_reviews.summary
                    ELSE excluded.summary
                END,
                source_url=CASE
                    WHEN stock_xinwei_reviews.status IN ('supported', 'failed') THEN stock_xinwei_reviews.source_url
                    ELSE excluded.source_url
                END,
                last_checked_at=excluded.last_checked_at,
                updated_at=excluded.updated_at
            """,
            (
                code,
                dimension_id,
                dimension_name,
                status,
                grade,
                score,
                summary,
                source_url,
                now,
                now,
                now,
            ),
        )


def enrich_stock(
    conn,
    code: str,
    announcement_limit: int,
    report_pages: int,
) -> dict[str, Any]:
    name = stock_name(conn, code)
    inserted = 0
    errors: list[str] = []

    try:
        cninfo_items = fetch_cninfo_announcements(code, name, announcement_limit)
        inserted += save_evidence_items(conn, cninfo_items)
    except Exception as exc:
        errors.append(f"cninfo: {exc}")

    try:
        report_items, coverage = fetch_eastmoney_reports(code, name, report_pages)
        inserted += save_evidence_items(conn, report_items)
        save_research_coverage(conn, code, coverage)
    except Exception as exc:
        errors.append(f"eastmoney_reports: {exc}")

    refresh_xinwei_reviews_from_evidence(conn, code)
    conn.commit()
    return {"code": code, "name": name, "inserted": inserted, "errors": errors}


def latest_run_codes(conn, top: int) -> list[str]:
    run = conn.execute("SELECT id FROM recommendation_runs ORDER BY id DESC LIMIT 1").fetchone()
    if not run:
        return []
    return [
        row["code"]
        for row in conn.execute(
            """
            SELECT code
            FROM recommendation_candidates
            WHERE run_id=?
            ORDER BY rank
            LIMIT ?
            """,
            (run["id"], top),
        )
    ]


def command_enrich(args: argparse.Namespace) -> None:
    with connect(args.db) as conn:
        init_db(conn)
        codes = list(args.codes or [])
        if args.latest_run_top:
            codes.extend(latest_run_codes(conn, args.latest_run_top))
        codes = sorted(dict.fromkeys(code.zfill(6) for code in codes if code))
        if not codes:
            raise SystemExit("No stock codes provided.")
        for code in codes:
            result = enrich_stock(conn, code, args.announcements, args.report_pages)
            error_text = " | ".join(result["errors"]) if result["errors"] else "ok"
            print(f"{code} {result['name'] or ''}: inserted={result['inserted']} status={error_text}")


def command_show(args: argparse.Namespace) -> None:
    with connect(args.db) as conn:
        init_db(conn)
        for code in args.codes:
            print(f"\n{code}")
            rows = conn.execute(
                """
                SELECT dimension_name, status, evidence_grade, score, summary
                FROM stock_xinwei_reviews
                WHERE code=?
                ORDER BY
                    CASE dimension_id
                        WHEN 'industry_inflection' THEN 1
                        WHEN 'scarcity_position' THEN 2
                        WHEN 'leader_customer_binding' THEN 3
                        WHEN 'capacity_order_expansion' THEN 4
                        WHEN 'earnings_inflection' THEN 5
                        WHEN 'expectation_gap' THEN 6
                        ELSE 99
                    END
                """,
                (code.zfill(6),),
            ).fetchall()
            for row in rows:
                score = "-" if row["score"] is None else row["score"]
                print(
                    f"  {row['dimension_name']}: {row['status']} "
                    f"grade={row['evidence_grade'] or '-'} score={score} "
                    f"{row['summary'] or ''}"
                )
            count = conn.execute(
                "SELECT COUNT(*) FROM stock_evidence_items WHERE code=?",
                (code.zfill(6),),
            ).fetchone()[0]
            print(f"  evidence_items={count}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect S/A-grade evidence for A-share watchlist stocks")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite database path")
    sub = parser.add_subparsers(dest="command", required=True)

    enrich = sub.add_parser("enrich", help="Collect announcements, reports, and coverage for stocks")
    enrich.add_argument("codes", nargs="*", help="Six-digit A-share codes")
    enrich.add_argument("--latest-run-top", type=int, default=0, help="Also enrich top N stocks from latest run")
    enrich.add_argument("--announcements", type=int, default=30, help="CNINFO announcement page size")
    enrich.add_argument("--report-pages", type=int, default=2, help="Eastmoney report pages to fetch")

    show = sub.add_parser("show", help="Show Xinwei review status for stocks")
    show.add_argument("codes", nargs="+", help="Six-digit A-share codes")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "enrich":
        command_enrich(args)
    elif args.command == "show":
        command_show(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
