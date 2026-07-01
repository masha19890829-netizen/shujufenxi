#!/usr/bin/env python
"""Local A-share watchlist dashboard backed by the SQLite research database."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import secrets
import sqlite3
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

from a_stock_daily import (
    DEFAULT_DB,
    connect,
    init_db,
    is_a_share_trading_day,
    today_cn,
    refresh_watchlist_metrics,
    sync_watchlist_from_recommendations,
)
from a_stock_evidence_gate import refresh_evidence_gate
from a_stock_model import refresh_model_scores
from a_stock_opportunity import (
    ensure_opportunity_schema,
    latest_opportunity_radar,
    opportunity_for_code,
)
from a_stock_provider_health import refresh_provider_health
from a_stock_qa import init_qa_schema, latest_qa_payload
from a_stock_weekly_review import latest_weekly_review_payload
from a_stock_replay import refresh_replay_results
from a_stock_tool_registry import seed_capability_roadmap, seed_tool_registry


ROOT = Path(__file__).resolve().parents[1]

DIMENSION_LABELS = {
    "industry_inflection": "产业拐点",
    "scarcity_position": "稀缺卡位",
    "leader_customer_binding": "双龙头客户绑定",
    "capacity_order_expansion": "产能/订单扩张",
    "earnings_inflection": "业绩拐点确认",
    "expectation_gap": "巨大预期差",
}

GATE_STATUS_LABELS = {
    "formula_supported": "六项闭环",
    "needs_manual_review": "有线索待核验",
    "missing_evidence": "缺S/A证据",
    "rejected": "证据冲突/失败",
    "stale": "证据过期",
}

ACCOUNT_PLANS = {
    "free": {
        "name": "普通账号",
        "price": "免费",
        "pitch": "适合第一次了解体系的用户，只看今日结论和少量观察样本。",
        "permissions": ["每日总判断", "3只示例观察股", "基础风险提示", "延迟查看部分证据"],
    },
    "research": {
        "name": "研究会员",
        "price": "¥99/月",
        "pitch": "适合新手系统学习产业趋势投资，重点看证据缺口和研究队列。",
        "permissions": ["完整今日股票池", "六项信维公式状态", "S/A证据摘要", "未来3个月催化清单", "历史纸面复盘"],
    },
    "pro": {
        "name": "高级会员",
        "price": "¥299/月",
        "pitch": "适合长期跟踪用户，获得更完整的风险监控和自选股提醒。",
        "permissions": ["全部研究会员权益", "自选股证据变化提醒", "仓位/半凯利区间", "排除清单与止损触发", "专题产业链报告"],
    },
    "team": {
        "name": "团队版",
        "price": "定制",
        "pitch": "适合小型投研团队，把证据库、任务流和复盘体系标准化。",
        "permissions": ["多人账号", "研究任务分配", "私有股票池", "导出报告", "API/本地部署支持"],
    },
}


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>A股信维公式观察台</title>
  <link rel="icon" href="data:,">
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f7fb;
      --surface: #ffffff;
      --surface-2: #eef2f7;
      --text: #17202d;
      --muted: #667085;
      --line: #d9e0ea;
      --green: #16735b;
      --red: #b94747;
      --blue: #245ec4;
      --amber: #ad6b14;
      --ink-soft: #27384f;
      --shadow: 0 12px 34px rgba(28, 39, 58, .08);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Microsoft YaHei", "PingFang SC", system-ui, -apple-system, Segoe UI, sans-serif;
      background: var(--bg);
      color: var(--text);
      letter-spacing: 0;
    }
    header {
      background: #17202d;
      color: #fff;
      border-bottom: 4px solid #e2b14c;
    }
    .wrap {
      width: min(1440px, calc(100vw - 32px));
      margin: 0 auto;
    }
    .topbar {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 18px;
      padding: 18px 0;
    }
    h1 {
      margin: 0;
      font-size: clamp(22px, 3vw, 34px);
      line-height: 1.15;
      font-weight: 760;
    }
    .subtitle {
      color: rgba(255,255,255,.72);
      margin-top: 6px;
      font-size: 13px;
    }
    .status-pill {
      white-space: nowrap;
      border: 1px solid rgba(255,255,255,.28);
      padding: 8px 12px;
      border-radius: 6px;
      color: rgba(255,255,255,.86);
      background: rgba(255,255,255,.08);
      font-size: 13px;
    }
    main {
      padding: 22px 0 34px;
    }
    .kpi-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
      gap: 12px;
      margin-bottom: 16px;
    }
    .kpi {
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      box-shadow: var(--shadow);
      min-height: 100px;
    }
    .kpi .label {
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 8px;
    }
    .kpi .value {
      font-size: clamp(20px, 2vw, 30px);
      font-weight: 760;
      line-height: 1.15;
      word-break: break-word;
    }
    .kpi .hint {
      margin-top: 8px;
      font-size: 12px;
      color: var(--muted);
      min-height: 16px;
    }
    .morning-section {
      margin-bottom: 16px;
    }
    .report-layout {
      display: grid;
      grid-template-columns: minmax(0, 1.12fr) minmax(360px, .88fr);
      gap: 14px;
      padding: 16px;
    }
    .report-stack {
      display: grid;
      gap: 12px;
      min-width: 0;
    }
    .report-note {
      border: 1px solid var(--line);
      border-left: 4px solid #e2b14c;
      border-radius: 8px;
      background: #fcfdff;
      padding: 12px;
      color: #27384f;
      line-height: 1.55;
      font-size: 13px;
    }
    .report-note strong {
      display: block;
      margin-bottom: 6px;
      font-size: 15px;
      color: var(--text);
    }
    .report-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
    }
    .report-card {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fcfdff;
      padding: 11px;
      min-height: 82px;
    }
    .report-card .label {
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 6px;
    }
    .report-card .value {
      font-size: 19px;
      font-weight: 760;
      line-height: 1.15;
    }
    .report-card .hint {
      margin-top: 7px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
    }
    .report-block {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fcfdff;
      padding: 12px;
      min-width: 0;
    }
    .report-block h3 {
      margin: 0 0 10px;
      font-size: 14px;
      line-height: 1.2;
    }
    .morning-list {
      display: grid;
      gap: 8px;
      max-height: 330px;
      overflow: auto;
    }
    .morning-item {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      padding: 10px;
      line-height: 1.45;
      font-size: 13px;
      cursor: pointer;
    }
    .morning-item:hover {
      background: #f7faff;
    }
    .morning-title {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 5px;
      font-weight: 750;
    }
    .morning-meta {
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
      color: var(--muted);
      font-size: 12px;
    }
    .gap-line {
      margin-top: 6px;
      color: #405169;
      font-size: 12px;
    }
    .grid {
      display: grid;
      grid-template-columns: minmax(0, 1.55fr) minmax(360px, .9fr);
      gap: 16px;
      align-items: start;
    }
    section {
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
      overflow: hidden;
    }
    .section-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      border-bottom: 1px solid var(--line);
      padding: 14px 16px;
      background: #fbfcfe;
    }
    h2 {
      margin: 0;
      font-size: 16px;
      line-height: 1.2;
    }
    .controls {
      display: flex;
      gap: 8px;
      align-items: center;
      flex-wrap: wrap;
      justify-content: flex-end;
    }
    select, input {
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      color: var(--text);
      height: 34px;
      padding: 0 10px;
      font: inherit;
      font-size: 13px;
      min-width: 120px;
    }
    input { min-width: 190px; }
    .table-wrap {
      overflow: auto;
      max-height: 620px;
    }
    table {
      border-collapse: collapse;
      width: 100%;
      min-width: 980px;
      font-size: 13px;
    }
    th, td {
      border-bottom: 1px solid var(--line);
      padding: 10px 10px;
      text-align: right;
      vertical-align: middle;
      white-space: nowrap;
    }
    th {
      position: sticky;
      top: 0;
      z-index: 1;
      background: #f2f5f9;
      color: #465466;
      font-size: 12px;
      font-weight: 700;
    }
    td:first-child, th:first-child,
    td:nth-child(2), th:nth-child(2),
    td:nth-child(11), th:nth-child(11),
    td:nth-child(16), th:nth-child(16) {
      text-align: left;
    }
    tbody tr {
      cursor: pointer;
    }
    tbody tr:hover {
      background: #f7faff;
    }
    tbody tr.active-row {
      background: #eef5ff;
      outline: 2px solid rgba(36,94,196,.22);
      outline-offset: -2px;
    }
    .code {
      color: var(--blue);
      font-weight: 750;
    }
    .name {
      font-weight: 700;
      color: var(--ink-soft);
    }
    .pos { color: var(--red); font-weight: 720; }
    .neg { color: var(--green); font-weight: 720; }
    .flat { color: var(--muted); font-weight: 720; }
    .tag {
      display: inline-flex;
      align-items: center;
      height: 24px;
      padding: 0 8px;
      border-radius: 6px;
      background: var(--surface-2);
      color: #405169;
      font-size: 12px;
    }
    .right-col {
      display: grid;
      gap: 16px;
    }
    .panel-body {
      padding: 16px;
    }
    .chart-shell {
      height: 300px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: linear-gradient(180deg, #ffffff 0%, #f8fafc 100%);
      padding: 10px;
    }
    #trendCanvas {
      width: 100%;
      height: 100%;
      display: block;
    }
    .stock-summary {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
      margin-top: 12px;
    }
    .metric-box {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px;
      background: #fcfdff;
      min-height: 70px;
    }
    .metric-box .label { color: var(--muted); font-size: 12px; }
    .metric-box .value { margin-top: 5px; font-weight: 760; font-size: 17px; }
    .mini-list {
      margin: 12px 0 0;
      padding: 0;
      list-style: none;
      display: grid;
      gap: 8px;
      color: #35445a;
      font-size: 13px;
    }
    .mini-list li {
      border-left: 3px solid #e2b14c;
      padding-left: 9px;
      line-height: 1.5;
    }
    .run-list {
      display: grid;
      gap: 10px;
      padding: 16px;
    }
    .gate-section {
      margin-bottom: 16px;
    }
    .gate-layout {
      display: grid;
      grid-template-columns: minmax(0, 1.25fr) minmax(320px, .75fr);
      gap: 14px;
      padding: 16px;
    }
    .gate-matrix {
      display: grid;
      gap: 8px;
      max-height: 380px;
      overflow: auto;
    }
    .gate-row {
      display: grid;
      grid-template-columns: 128px repeat(6, minmax(72px, 1fr)) 96px;
      gap: 6px;
      align-items: center;
      font-size: 12px;
    }
    .gate-cell {
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 7px 6px;
      background: #fcfdff;
      min-height: 34px;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .gate-cell.head {
      background: #f2f5f9;
      color: #465466;
      font-weight: 750;
    }
    .gate-status-verified { border-color: rgba(22,115,91,.38); color: var(--green); }
    .gate-status-needs_review { border-color: rgba(173,107,20,.45); color: var(--amber); }
    .gate-status-pending, .gate-status-stale { border-color: rgba(102,112,133,.35); color: var(--muted); }
    .gate-status-failed { border-color: rgba(185,71,71,.45); color: var(--red); }
    .task-list {
      display: grid;
      gap: 8px;
      max-height: 380px;
      overflow: auto;
    }
    .task-item {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px;
      background: #fcfdff;
      font-size: 13px;
      line-height: 1.45;
    }
    .task-title {
      font-weight: 750;
      margin-bottom: 4px;
    }
    .run-item {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      background: #fcfdff;
    }
    .run-title {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      font-weight: 750;
      margin-bottom: 8px;
    }
    .candidate-strip {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
    }
    .tool-section {
      margin-top: 16px;
    }
    .tool-layout {
      display: grid;
      grid-template-columns: minmax(0, 1.05fr) minmax(340px, .95fr);
      gap: 14px;
      padding: 16px;
    }
    .tool-list, .roadmap-list {
      display: grid;
      gap: 10px;
    }
    .tool-item {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      background: #fcfdff;
      min-height: 96px;
    }
    .tool-title {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 10px;
      margin-bottom: 8px;
      font-weight: 750;
      line-height: 1.35;
    }
    .tool-meta {
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
      margin-bottom: 8px;
    }
    .tool-note {
      color: #405169;
      font-size: 13px;
      line-height: 1.5;
    }
    .source-band {
      margin-top: 16px;
      background: #17202d;
      color: rgba(255,255,255,.82);
      border-radius: 8px;
      padding: 16px;
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 14px;
      font-size: 13px;
      line-height: 1.6;
    }
    .source-band strong { color: #fff; }
    .empty {
      padding: 24px;
      color: var(--muted);
      text-align: center;
    }
    @media (max-width: 1100px) {
      .kpi-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
      .grid { grid-template-columns: 1fr; }
      .right-col { grid-template-columns: 1fr; }
    }
    @media (max-width: 720px) {
      .wrap { width: min(100vw - 20px, 1440px); }
      .topbar { align-items: flex-start; flex-direction: column; }
      .kpi-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .section-head { align-items: flex-start; flex-direction: column; }
      .controls { justify-content: flex-start; width: 100%; }
      select, input { width: 100%; min-width: 0; }
      .stock-summary { grid-template-columns: 1fr; }
      .report-layout { grid-template-columns: 1fr; }
      .report-grid { grid-template-columns: 1fr; }
      .gate-layout { grid-template-columns: 1fr; }
      .gate-row { grid-template-columns: 110px repeat(6, minmax(68px, 1fr)) 84px; min-width: 720px; }
      .tool-layout { grid-template-columns: 1fr; }
      .source-band { grid-template-columns: 1fr; }
    }
    @import url("https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@500;600;700&family=Noto+Sans+SC:wght@400;500;700;900&family=Noto+Serif+SC:wght@600;700;900&family=Space+Grotesk:wght@500;600;700&display=swap");

    /* Wall Street terminal refresh: visual-only override layer. */
    :root {
      --ws-bg: #07111c;
      --ws-bg-2: #0b1826;
      --ws-panel: rgba(10, 24, 38, 0.86);
      --ws-panel-2: rgba(13, 32, 50, 0.92);
      --ws-border: rgba(151, 177, 202, 0.18);
      --ws-border-strong: rgba(218, 181, 96, 0.34);
      --ws-text: #edf4fb;
      --ws-muted: #8fa2b5;
      --ws-subtle: #52677a;
      --ws-gold: #d7b56d;
      --ws-gold-2: #f0d99a;
      --ws-cyan: #54d6ff;
      --ws-green: #3ce19a;
      --ws-red: #ff6b6b;
      --ws-shadow: 0 22px 80px rgba(0, 0, 0, 0.42);
      --ws-radius-lg: 26px;
      --ws-radius-md: 18px;
      --ws-radius-sm: 12px;
      --ws-font-display: "Noto Serif SC", "Source Han Serif SC", "思源宋体", "Songti SC", "STSong", serif;
      --ws-font-ui: "Noto Sans SC", "Microsoft YaHei UI", "PingFang SC", "Hiragino Sans GB", sans-serif;
      --ws-font-latin: "Space Grotesk", "DIN Alternate", "Bahnschrift", sans-serif;
      --ws-font-data: "JetBrains Mono", "Cascadia Mono", "SFMono-Regular", monospace;
    }

    html {
      background: var(--ws-bg);
    }

    body {
      min-height: 100vh;
      color: var(--ws-text);
      background:
        radial-gradient(circle at 12% 8%, rgba(84, 214, 255, 0.18), transparent 30rem),
        radial-gradient(circle at 88% 0%, rgba(215, 181, 109, 0.18), transparent 32rem),
        linear-gradient(135deg, #050b12 0%, var(--ws-bg) 42%, #0d1722 100%) !important;
      font-family: var(--ws-font-ui) !important;
      letter-spacing: 0.01em;
    }

    body::before {
      content: "";
      position: fixed;
      inset: 0;
      pointer-events: none;
      z-index: -1;
      background:
        linear-gradient(rgba(255,255,255,0.035) 1px, transparent 1px),
        linear-gradient(90deg, rgba(255,255,255,0.028) 1px, transparent 1px);
      background-size: 72px 72px;
      mask-image: radial-gradient(circle at 50% 18%, black, transparent 78%);
    }

    body::after {
      content: "";
      position: fixed;
      inset: 0;
      pointer-events: none;
      z-index: -1;
      background: linear-gradient(180deg, rgba(255,255,255,0.06), transparent 18%, rgba(0,0,0,0.22));
      mix-blend-mode: screen;
      opacity: 0.58;
    }

    a {
      color: var(--ws-cyan);
    }

    header,
    .hero,
    .topbar,
    .toolbar,
    .summary,
    .panel,
    .card,
    .report-card,
    .report-block,
    .report-note,
    section,
    article {
      border-color: var(--ws-border) !important;
    }

    header,
    .hero,
    .topbar {
      position: relative;
      overflow: hidden;
      background:
        linear-gradient(110deg, rgba(12, 30, 47, 0.96), rgba(8, 16, 27, 0.82)),
        radial-gradient(circle at 18% 0%, rgba(84, 214, 255, 0.16), transparent 24rem) !important;
      border: 1px solid var(--ws-border-strong) !important;
      border-radius: var(--ws-radius-lg) !important;
      box-shadow: var(--ws-shadow) !important;
    }

    header::before,
    .hero::before,
    .topbar::before {
      content: "XINWEI A-SHARE INTELLIGENCE";
      position: absolute;
      top: 18px;
      right: 22px;
      color: rgba(240, 217, 154, 0.22);
      font-family: var(--ws-font-data);
      font-size: 11px;
      letter-spacing: 0.22em;
      text-transform: uppercase;
    }

    h1,
    h2,
    h3 {
      color: var(--ws-text) !important;
      letter-spacing: -0.035em;
    }

    h1 {
      font-family: var(--ws-font-display) !important;
      font-size: clamp(42px, 5.8vw, 86px) !important;
      line-height: 0.98 !important;
      font-weight: 900 !important;
      text-wrap: balance;
      background: linear-gradient(92deg, #ffffff 0%, #dceaff 42%, var(--ws-gold-2) 100%);
      -webkit-background-clip: text;
      background-clip: text;
      color: transparent !important;
      text-shadow: 0 16px 48px rgba(84, 214, 255, 0.08);
    }

    h2 {
      font-family: var(--ws-font-display) !important;
      font-size: clamp(24px, 2.9vw, 42px) !important;
      line-height: 1.12 !important;
      font-weight: 800 !important;
    }

    h3 {
      font-family: var(--ws-font-latin), var(--ws-font-ui) !important;
      font-size: 15px !important;
      text-transform: uppercase;
      letter-spacing: 0.14em;
    }

    p,
    .muted,
    .hint,
    .meta,
    .report-card .hint,
    .report-card .label,
    small {
      color: var(--ws-muted) !important;
    }

    p,
    li,
    td,
    .report-note,
    .logic-node {
      font-size: 15.5px;
      line-height: 1.75;
      font-weight: 400;
    }

    .muted,
    .hint,
    .meta,
    small {
      font-size: 12.5px !important;
      line-height: 1.65 !important;
      letter-spacing: 0.02em;
    }

    .report-layout,
    .grid,
    .cards,
    .dashboard-grid {
      gap: 18px !important;
    }

    .panel,
    .card,
    .report-card,
    .report-block,
    .report-note,
    .metric-card,
    .queue-card,
    .stock-card {
      position: relative;
      overflow: hidden;
      background:
        linear-gradient(180deg, rgba(18, 38, 59, 0.92), rgba(8, 18, 30, 0.88)) !important;
      border: 1px solid var(--ws-border) !important;
      border-radius: var(--ws-radius-md) !important;
      box-shadow: 0 16px 46px rgba(0, 0, 0, 0.28), inset 0 1px 0 rgba(255, 255, 255, 0.04) !important;
      backdrop-filter: blur(18px);
    }

    .panel::before,
    .card::before,
    .report-card::before,
    .report-block::before,
    .metric-card::before,
    .queue-card::before,
    .stock-card::before {
      content: "";
      position: absolute;
      inset: 0 0 auto 0;
      height: 1px;
      background: linear-gradient(90deg, transparent, rgba(84, 214, 255, 0.42), rgba(240, 217, 154, 0.36), transparent);
      opacity: 0.78;
    }

    .report-note {
      background:
        linear-gradient(135deg, rgba(215, 181, 109, 0.17), rgba(84, 214, 255, 0.08)),
        rgba(9, 20, 33, 0.88) !important;
      border-color: var(--ws-border-strong) !important;
    }

    .report-note strong,
    .value,
    .report-card .value,
    .metric-value,
    .score,
    [class*="score"] {
      color: var(--ws-gold-2) !important;
      font-family: var(--ws-font-data);
      font-variant-numeric: tabular-nums;
      letter-spacing: -0.045em;
    }

    button,
    .button,
    .btn,
    select,
    input {
      color: var(--ws-text) !important;
      background: linear-gradient(180deg, rgba(20, 45, 68, 0.92), rgba(8, 20, 33, 0.96)) !important;
      border: 1px solid rgba(84, 214, 255, 0.28) !important;
      border-radius: 999px !important;
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.06), 0 10px 28px rgba(0,0,0,0.22);
    }

    button:hover,
    .button:hover,
    .btn:hover {
      border-color: rgba(240, 217, 154, 0.62) !important;
      box-shadow: 0 0 0 4px rgba(215, 181, 109, 0.08), 0 14px 38px rgba(0,0,0,0.32);
      transform: translateY(-1px);
    }

    table {
      width: 100%;
      border-collapse: separate !important;
      border-spacing: 0 8px !important;
      color: var(--ws-text) !important;
      font-variant-numeric: tabular-nums;
    }

    thead th,
    th {
      color: var(--ws-muted) !important;
      font-family: var(--ws-font-data);
      font-size: 10.5px !important;
      letter-spacing: 0.09em;
      text-transform: uppercase;
      border-bottom: 1px solid rgba(151, 177, 202, 0.16) !important;
    }

    tbody tr,
    tr {
      background: rgba(9, 22, 36, 0.58) !important;
    }

    td {
      border-top: 1px solid rgba(151, 177, 202, 0.08) !important;
      border-bottom: 1px solid rgba(151, 177, 202, 0.08) !important;
      color: rgba(237, 244, 251, 0.92) !important;
    }

    td:first-child {
      border-left: 1px solid rgba(151, 177, 202, 0.08) !important;
      border-radius: 12px 0 0 12px;
    }

    td:last-child {
      border-right: 1px solid rgba(151, 177, 202, 0.08) !important;
      border-radius: 0 12px 12px 0;
    }

    tbody tr:hover,
    tr:hover {
      background: rgba(22, 48, 73, 0.8) !important;
      box-shadow: 0 0 0 1px rgba(84, 214, 255, 0.18);
    }

    .tag,
    .pill,
    .badge,
    [class*="badge"],
    [class*="pill"] {
      color: #06111a !important;
      background: linear-gradient(135deg, var(--ws-gold-2), var(--ws-cyan)) !important;
      border: 0 !important;
      border-radius: 999px !important;
      font-family: var(--ws-font-data);
      font-size: 10.5px !important;
      font-weight: 800 !important;
      letter-spacing: 0.04em;
      box-shadow: 0 8px 22px rgba(84, 214, 255, 0.16);
    }

    .positive,
    .up,
    .gain,
    [class*="positive"],
    [class*="up"] {
      color: var(--ws-green) !important;
    }

    .negative,
    .down,
    .loss,
    [class*="negative"],
    [class*="down"] {
      color: var(--ws-red) !important;
    }

    svg,
    canvas {
      filter: drop-shadow(0 18px 34px rgba(0, 0, 0, 0.24));
    }

    ::selection {
      color: #07111c;
      background: var(--ws-gold-2);
    }

    .cinematic-hero {
      position: relative;
      display: grid;
      grid-template-columns: minmax(0, 1.05fr) minmax(360px, 0.95fr);
      gap: 28px;
      margin: 18px auto 22px;
      padding: clamp(22px, 4vw, 54px);
      max-width: 1500px;
      min-height: 620px;
      overflow: hidden;
      border: 1px solid rgba(215, 181, 109, 0.28);
      border-radius: 34px;
      background:
        radial-gradient(circle at 72% 24%, rgba(84, 214, 255, 0.22), transparent 28rem),
        radial-gradient(circle at 12% 12%, rgba(240, 217, 154, 0.16), transparent 22rem),
        linear-gradient(135deg, rgba(7, 16, 28, 0.98), rgba(8, 25, 42, 0.94));
      box-shadow: 0 34px 120px rgba(0, 0, 0, 0.48), inset 0 1px 0 rgba(255,255,255,0.08);
      isolation: isolate;
    }

    .cinematic-hero::before {
      content: "";
      position: absolute;
      inset: -30%;
      z-index: -1;
      background:
        conic-gradient(from 120deg, transparent, rgba(84,214,255,0.12), transparent, rgba(215,181,109,0.12), transparent);
      animation: heroSweep 18s linear infinite;
      opacity: 0.72;
    }

    .cinematic-hero::after {
      content: "";
      position: absolute;
      inset: 0;
      pointer-events: none;
      background:
        linear-gradient(90deg, rgba(255,255,255,0.04) 1px, transparent 1px),
        linear-gradient(rgba(255,255,255,0.035) 1px, transparent 1px);
      background-size: 46px 46px;
      mask-image: linear-gradient(120deg, black, transparent 68%);
    }

    .cinematic-copy {
      position: relative;
      z-index: 2;
      align-self: center;
      max-width: 720px;
    }

    .cinematic-kicker {
      display: inline-flex;
      align-items: center;
      gap: 10px;
      margin-bottom: 22px;
      padding: 9px 14px;
      color: var(--ws-gold-2);
      border: 1px solid rgba(240, 217, 154, 0.24);
      border-radius: 999px;
      background: rgba(240, 217, 154, 0.08);
      font-family: var(--ws-font-data);
      font-family: var(--ws-font-latin), var(--ws-font-ui);
      font-size: 11px;
      letter-spacing: 0.16em;
      text-transform: uppercase;
    }

    .cinematic-kicker::before {
      content: "";
      width: 9px;
      height: 9px;
      border-radius: 999px;
      background: var(--ws-green);
      box-shadow: 0 0 18px var(--ws-green);
      animation: livePulse 1.6s ease-in-out infinite;
    }

    .cinematic-title {
      margin: 0;
      font-family: var(--ws-font-display) !important;
      font-size: clamp(50px, 8vw, 118px) !important;
      line-height: 0.93 !important;
      letter-spacing: -0.075em;
      font-weight: 900 !important;
    }

    .cinematic-title span {
      display: block;
      padding-bottom: 0.08em;
      background:
        linear-gradient(92deg, #ffffff 0%, #e7f6ff 38%, #f6dda0 75%, #c59a42 100%);
      -webkit-background-clip: text;
      background-clip: text;
      color: transparent;
      text-shadow: 0 20px 72px rgba(240, 217, 154, 0.08);
    }

    .cinematic-lede {
      max-width: 650px;
      margin: 24px 0 0;
      color: rgba(237,244,251,0.72) !important;
      font-size: clamp(17px, 1.75vw, 22px);
      line-height: 1.95;
      font-weight: 400;
      letter-spacing: 0.015em;
    }

    .cinematic-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      margin-top: 30px;
    }

    .cinematic-action {
      display: inline-flex;
      align-items: center;
      gap: 10px;
      padding: 13px 18px;
      color: #06111a !important;
      text-decoration: none;
      border-radius: 999px;
      background: linear-gradient(135deg, var(--ws-gold-2), var(--ws-cyan));
      font-weight: 800;
      box-shadow: 0 16px 40px rgba(84, 214, 255, 0.16);
    }

    .cinematic-action.secondary {
      color: var(--ws-text) !important;
      background: rgba(255,255,255,0.08);
      border: 1px solid rgba(151,177,202,0.18);
    }

    .signal-strip {
      display: grid;
      grid-template-columns: repeat(4, minmax(118px, 1fr));
      gap: 12px;
      margin-top: 34px;
    }

    .signal-chip {
      padding: 14px 15px;
      border: 1px solid rgba(151,177,202,0.16);
      border-radius: 18px;
      background: rgba(5, 15, 25, 0.58);
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.06);
    }

    .signal-chip b {
      display: block;
      color: var(--ws-gold-2);
      font-family: var(--ws-font-data);
      font-size: 24px;
      letter-spacing: -0.05em;
    }

    .signal-chip span {
      display: block;
      margin-top: 5px;
      color: var(--ws-muted);
      font-size: 12px;
    }

    .market-3d {
      position: relative;
      z-index: 2;
      display: grid;
      place-items: center;
      min-height: 520px;
      perspective: 1100px;
    }

    .orbit-system {
      position: relative;
      width: min(46vw, 560px);
      height: min(46vw, 560px);
      min-width: 360px;
      min-height: 360px;
      transform-style: preserve-3d;
      transform: rotateX(62deg) rotateZ(-18deg);
      animation: orbitTilt 9s ease-in-out infinite alternate;
    }

    .orbit-ring {
      position: absolute;
      inset: 12%;
      border: 1px solid rgba(84, 214, 255, 0.22);
      border-radius: 50%;
      box-shadow: 0 0 32px rgba(84, 214, 255, 0.12), inset 0 0 28px rgba(215, 181, 109, 0.06);
      animation: orbitSpin 18s linear infinite;
      transform-style: preserve-3d;
    }

    .orbit-ring.r2 {
      inset: 24%;
      border-color: rgba(240,217,154,0.28);
      animation-duration: 12s;
      animation-direction: reverse;
    }

    .orbit-ring.r3 {
      inset: 4%;
      border-style: dashed;
      animation-duration: 24s;
    }

    .orbit-core {
      position: absolute;
      inset: 34%;
      display: grid;
      place-items: center;
      border-radius: 50%;
      color: var(--ws-gold-2);
      background:
        radial-gradient(circle at 35% 30%, rgba(255,255,255,0.34), transparent 16%),
        radial-gradient(circle, rgba(84,214,255,0.34), rgba(215,181,109,0.24) 46%, rgba(8,20,33,0.86) 70%);
      box-shadow: 0 0 80px rgba(84,214,255,0.26), inset 0 0 30px rgba(255,255,255,0.08);
      transform: translateZ(78px) rotateX(-62deg) rotateZ(18deg);
    }

    .orbit-core strong {
      font-family: var(--ws-font-data);
      font-size: 36px;
      letter-spacing: -0.08em;
    }

    .sector-node {
      position: absolute;
      left: 50%;
      top: 50%;
      width: 96px;
      padding: 8px 10px;
      color: var(--ws-text);
      border: 1px solid rgba(84,214,255,0.26);
      border-radius: 999px;
      background: rgba(7, 20, 34, 0.84);
      font-family: var(--ws-font-data);
      font-size: 10.5px;
      text-align: center;
      box-shadow: 0 10px 30px rgba(0,0,0,0.28);
      transform: rotate(var(--angle)) translateX(220px) rotate(calc(-1 * var(--angle))) rotateX(-62deg) rotateZ(18deg);
      animation: nodeGlow 3.8s ease-in-out infinite;
      animation-delay: var(--delay);
    }

    .intelligence-stage {
      display: grid;
      grid-template-columns: 1fr 1fr 1fr;
      gap: 18px;
      max-width: 1500px;
      margin: 0 auto 28px;
    }

    .stage-card {
      min-height: 330px;
      padding: 22px;
      border: 1px solid rgba(151,177,202,0.16);
      border-radius: 26px;
      background: linear-gradient(180deg, rgba(14,31,49,0.9), rgba(7,18,31,0.92));
      box-shadow: 0 20px 70px rgba(0,0,0,0.32);
      overflow: hidden;
    }

    .stage-card h2 {
      margin: 0;
      font-size: 26px !important;
      line-height: 1.16 !important;
      letter-spacing: -0.03em;
    }

    .stage-card p {
      margin: 8px 0 18px;
      color: var(--ws-muted) !important;
      line-height: 1.7;
    }

    .mindmap {
      position: relative;
      height: 230px;
    }

    .mindmap .hub,
    .mindmap .leaf {
      position: absolute;
      display: grid;
      place-items: center;
      border-radius: 999px;
      font-family: var(--ws-font-data);
      font-size: 12px;
      text-align: center;
      box-shadow: 0 12px 34px rgba(0,0,0,0.28);
    }

    .mindmap .hub {
      left: 50%;
      top: 50%;
      width: 112px;
      height: 112px;
      color: #06111a;
      background: linear-gradient(135deg, var(--ws-gold-2), var(--ws-cyan));
      transform: translate(-50%, -50%);
      animation: hubBreath 2.8s ease-in-out infinite;
    }

    .mindmap .leaf {
      width: 92px;
      height: 42px;
      color: var(--ws-text);
      border: 1px solid rgba(151,177,202,0.18);
      background: rgba(9,24,39,0.9);
      animation: floatLeaf 4.6s ease-in-out infinite;
      animation-delay: var(--delay);
    }

    .mindmap .leaf::before {
      content: "";
      position: absolute;
      left: 50%;
      top: 50%;
      width: var(--line);
      height: 1px;
      background: linear-gradient(90deg, rgba(84,214,255,0.54), transparent);
      transform-origin: 0 0;
      transform: rotate(var(--line-angle));
      z-index: -1;
      animation: dataFlow 1.8s linear infinite;
    }

    .logic-flow {
      display: grid;
      gap: 10px;
      margin-top: 16px;
    }

    .logic-node {
      position: relative;
      padding: 13px 14px 13px 44px;
      border: 1px solid rgba(151,177,202,0.14);
      border-radius: 16px;
      background: rgba(6,18,30,0.68);
      color: rgba(237,244,251,0.9);
      font-size: 14.5px;
      animation: logicReveal 6s ease-in-out infinite;
      animation-delay: var(--delay);
    }

    .logic-node::before {
      content: attr(data-step);
      position: absolute;
      left: 12px;
      top: 50%;
      width: 22px;
      height: 22px;
      display: grid;
      place-items: center;
      border-radius: 50%;
      color: #06111a;
      background: var(--ws-gold-2);
      font-family: var(--ws-font-data);
      font-size: 10.5px;
      transform: translateY(-50%);
    }

    .db-console {
      position: relative;
      display: grid;
      gap: 10px;
      margin-top: 16px;
      padding: 14px;
      border: 1px solid rgba(84,214,255,0.16);
      border-radius: 18px;
      background: rgba(1,8,14,0.58);
    }

    .db-row {
      display: grid;
      grid-template-columns: 80px 1fr 52px;
      align-items: center;
      gap: 10px;
      font-family: var(--ws-font-data);
      font-size: 10.5px;
      color: var(--ws-muted);
    }

    .db-bar {
      height: 8px;
      overflow: hidden;
      border-radius: 999px;
      background: rgba(151,177,202,0.12);
    }

    .db-bar span {
      display: block;
      width: var(--w);
      height: 100%;
      border-radius: inherit;
      background: linear-gradient(90deg, var(--ws-cyan), var(--ws-gold-2));
      animation: dbPulse 2.4s ease-in-out infinite;
      animation-delay: var(--delay);
    }

    .db-ticker {
      display: flex;
      gap: 10px;
      margin-top: 12px;
      color: var(--ws-green);
      font-family: var(--ws-font-data);
      font-size: 12px;
      white-space: nowrap;
      animation: tickerMove 10s linear infinite;
    }

    @keyframes heroSweep { to { transform: rotate(360deg); } }
    @keyframes livePulse { 50% { transform: scale(1.5); opacity: 0.45; } }
    @keyframes orbitTilt { to { transform: rotateX(55deg) rotateZ(-5deg) translateY(-10px); } }
    @keyframes orbitSpin { to { transform: rotateZ(360deg); } }
    @keyframes nodeGlow { 50% { border-color: rgba(240,217,154,0.72); box-shadow: 0 0 28px rgba(240,217,154,0.18); } }
    @keyframes hubBreath { 50% { transform: translate(-50%, -50%) scale(1.06); box-shadow: 0 0 42px rgba(84,214,255,0.28); } }
    @keyframes floatLeaf { 50% { transform: translateY(-8px); } }
    @keyframes dataFlow { 50% { opacity: 0.36; } }
    @keyframes logicReveal { 0%, 100% { border-color: rgba(151,177,202,0.14); } 50% { border-color: rgba(84,214,255,0.42); background: rgba(14,35,54,0.82); } }
    @keyframes dbPulse { 50% { filter: brightness(1.45); transform: scaleX(0.94); } }
    @keyframes tickerMove { to { transform: translateX(-42%); } }

    @media (max-width: 1080px) {
      .cinematic-hero,
      .intelligence-stage {
        grid-template-columns: 1fr;
      }

      .market-3d {
        min-height: 420px;
      }
    }

    @media (max-width: 760px) {
      body {
        background:
          radial-gradient(circle at 16% 4%, rgba(84, 214, 255, 0.16), transparent 22rem),
          linear-gradient(160deg, #050b12, #0c1723) !important;
      }

      header,
      .hero,
      .topbar,
      .panel,
      .card,
      .report-card,
      .report-block,
      .report-note {
        border-radius: 18px !important;
      }

      header::before,
      .hero::before,
      .topbar::before {
        display: none;
      }

      table {
        border-spacing: 0 6px !important;
      }

      h1 {
        font-size: clamp(38px, 13vw, 64px) !important;
        line-height: 1.02 !important;
      }

      h2 {
        font-size: clamp(24px, 8vw, 34px) !important;
      }

      p,
      li,
      td,
      .report-note,
      .logic-node {
        font-size: 14.5px;
        line-height: 1.72;
      }

      .cinematic-hero {
        min-height: auto;
        margin-top: 8px;
        padding: 22px;
      }

      .signal-strip {
        grid-template-columns: 1fr 1fr;
      }

      .cinematic-title {
        font-size: clamp(44px, 15vw, 68px) !important;
        line-height: 0.98 !important;
      }

      .cinematic-lede {
        font-size: 15.5px;
        line-height: 1.85;
      }

      .orbit-system {
        width: 330px;
        height: 330px;
        min-width: 300px;
        min-height: 300px;
      }

      .sector-node {
        transform: rotate(var(--angle)) translateX(140px) rotate(calc(-1 * var(--angle))) rotateX(-62deg) rotateZ(18deg);
      }
    }
    .cinematic-hero,
    .intelligence-stage,
    body > header {
      display: none !important;
    }

    .product-home {
      min-height: 100vh;
      padding: 24px min(5vw, 56px) 56px;
      background:
        linear-gradient(120deg, rgba(246, 249, 246, .96), rgba(235, 241, 245, .96)),
        linear-gradient(90deg, rgba(24, 92, 72, .08), rgba(194, 145, 63, .08));
      color: #15231e;
      font-family: "Noto Sans SC", "Microsoft YaHei", "PingFang SC", sans-serif;
    }

    .product-home * {
      box-sizing: border-box;
    }

    .product-topline {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 18px;
      max-width: 1220px;
      margin: 0 auto 22px;
      padding: 12px 0;
      border-bottom: 1px solid rgba(31, 65, 52, .14);
    }

    .product-brand {
      font-size: 18px;
      font-weight: 900;
      letter-spacing: 0;
      color: #13241f;
    }

    .product-subtitle {
      margin-top: 4px;
      font-size: 13px;
      color: #5b6b64;
    }

    .account-switcher {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 6px;
      border: 1px solid rgba(31, 65, 52, .16);
      border-radius: 8px;
      background: rgba(255,255,255,.76);
      box-shadow: 0 10px 28px rgba(20, 45, 38, .08);
    }

    .account-switcher span {
      padding: 0 8px;
      font-size: 13px;
      font-weight: 800;
      color: #24473b;
      white-space: nowrap;
    }

    .account-switcher select {
      width: 132px;
      min-width: 0;
      height: 34px;
      border-radius: 6px;
      border: 1px solid rgba(31, 65, 52, .16);
      background: #fff;
      color: #15231e !important;
      font-size: 13px;
    }

    .decision-hero {
      display: grid;
      grid-template-columns: minmax(0, 1.35fr) minmax(320px, .65fr);
      gap: 22px;
      max-width: 1220px;
      margin: 0 auto;
      align-items: stretch;
    }

    .decision-copy,
    .account-panel,
    .decision-tile,
    .pick-card,
    .plan-card {
      border: 1px solid rgba(31, 65, 52, .14);
      border-radius: 8px;
      background: rgba(255,255,255,.82);
      box-shadow: 0 18px 46px rgba(18, 45, 37, .10);
    }

    .decision-copy {
      padding: clamp(26px, 4vw, 48px);
      background:
        linear-gradient(135deg, rgba(255,255,255,.90), rgba(239, 246, 241, .86)),
        linear-gradient(90deg, rgba(24, 92, 72, .10), transparent);
    }

    .decision-date {
      display: inline-flex;
      align-items: center;
      min-height: 30px;
      padding: 0 10px;
      border-radius: 6px;
      background: #183f34;
      color: #f3f8f5;
      font-size: 13px;
      font-weight: 800;
      letter-spacing: 0;
    }

    .decision-copy h1 {
      margin: 22px 0 14px;
      color: #14231e !important;
      font-family: "Noto Serif SC", "Songti SC", serif !important;
      font-size: clamp(34px, 5vw, 64px) !important;
      line-height: 1.04 !important;
      letter-spacing: 0 !important;
      font-weight: 900 !important;
      background: none !important;
      -webkit-text-fill-color: currentColor;
    }

    .decision-copy p,
    .account-panel p,
    .decision-tile p,
    .pick-card p,
    .plan-card p {
      margin: 0;
      color: #53665d !important;
      font-size: 15px;
      line-height: 1.7;
      letter-spacing: 0;
    }

    .decision-actions {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      margin-top: 26px;
    }

    .decision-actions a {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 42px;
      padding: 0 16px;
      border-radius: 7px;
      background: #183f34;
      color: #fff;
      text-decoration: none;
      font-weight: 850;
      font-size: 14px;
    }

    .decision-actions a.secondary {
      background: #efe4c6;
      color: #473716;
    }

    .account-panel {
      padding: 24px;
    }

    .panel-eyebrow,
    .section-copy span,
    .decision-tile span,
    .pick-card .pick-label,
    .plan-card .plan-label {
      display: block;
      color: #6a755f;
      font-size: 12px;
      font-weight: 900;
      letter-spacing: .08em;
      text-transform: uppercase;
    }

    .account-panel h2,
    .section-copy h2,
    .plan-card h3,
    .pick-card h3 {
      margin: 8px 0 10px;
      color: #14231e !important;
      font-family: "Noto Serif SC", "Songti SC", serif !important;
      letter-spacing: 0 !important;
      text-transform: none;
    }

    .account-panel ul {
      list-style: none;
      margin: 18px 0 0;
      padding: 0;
      display: grid;
      gap: 9px;
    }

    .account-panel li {
      padding: 9px 10px;
      border-radius: 7px;
      background: #f4f7f4;
      color: #263b33;
      font-size: 13px;
      line-height: 1.45;
    }

    .decision-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 14px;
      max-width: 1220px;
      margin: 18px auto 0;
    }

    .decision-tile {
      padding: 20px;
      min-height: 164px;
      display: grid;
      align-content: start;
      gap: 10px;
    }

    .decision-tile strong {
      color: #14231e !important;
      font-size: clamp(24px, 3vw, 34px);
      line-height: 1.08;
      font-family: "Noto Sans SC", "Microsoft YaHei", sans-serif;
      letter-spacing: 0;
    }

    .decision-tile.buy {
      border-top: 5px solid #1b6b4f;
    }

    .decision-tile.watch {
      border-top: 5px solid #c7973e;
    }

    .decision-tile.avoid {
      border-top: 5px solid #b54b4b;
    }

    .beginner-section,
    .radar-section,
    .membership-section {
      max-width: 1220px;
      margin: 30px auto 0;
    }

    .section-copy {
      display: flex;
      align-items: end;
      justify-content: space-between;
      gap: 18px;
      margin-bottom: 14px;
    }

    .section-copy h2 {
      font-size: clamp(24px, 3vw, 38px) !important;
      line-height: 1.12 !important;
    }

    .beginner-picks,
    .plan-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 14px;
    }

    .radar-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 14px;
    }

    .pick-card,
    .radar-card,
    .plan-card {
      padding: 20px;
      min-height: 230px;
    }

    .pick-card h3,
    .radar-card h3,
    .plan-card h3 {
      font-size: 22px !important;
      line-height: 1.18 !important;
    }

    .radar-card {
      border: 1px solid rgba(31, 65, 52, .14);
      border-radius: 8px;
      background: linear-gradient(180deg, rgba(255,255,255,.88), rgba(242,247,244,.90));
      box-shadow: 0 18px 46px rgba(18, 45, 37, .10);
      min-height: 260px;
      display: grid;
      align-content: start;
      gap: 10px;
    }

    .radar-card.buy {
      border-top: 5px solid #1b6b4f;
    }

    .radar-card.wait {
      border-top: 5px solid #c7973e;
    }

    .radar-card.watch {
      border-top: 5px solid #607d8b;
    }

    .radar-card.exclude {
      border-top: 5px solid #b54b4b;
    }

    .radar-label {
      display: inline-flex;
      width: fit-content;
      min-height: 26px;
      align-items: center;
      padding: 0 8px;
      border-radius: 6px;
      background: #183f34;
      color: #fff;
      font-size: 12px;
      font-weight: 900;
    }

    .radar-meta,
    .radar-evidence {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
    }

    .radar-meta span,
    .radar-evidence span,
    .locked-pill {
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      padding: 0 8px;
      border-radius: 6px;
      background: #eef3ef;
      color: #38584b;
      font-size: 12px;
      font-weight: 800;
    }

    .locked-pill {
      background: #efe4c6;
      color: #473716;
    }

    .pick-meta,
    .plan-meta {
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
      margin: 14px 0;
    }

    .product-home .tag,
    .pick-meta span,
    .plan-meta span {
      display: inline-flex;
      align-items: center;
      min-height: 26px;
      padding: 0 8px;
      border-radius: 6px;
      background: #eef3ef;
      color: #38584b;
      font-size: 12px;
      font-weight: 800;
    }

    .pick-card button,
    .plan-card button {
      margin-top: 16px;
      width: 100%;
      height: 40px;
      border: 0;
      border-radius: 7px;
      background: #183f34;
      color: #fff;
      font-weight: 850;
      cursor: pointer;
    }

    .plan-card.featured {
      border-color: rgba(199, 151, 62, .46);
      background: linear-gradient(180deg, #fffaf0, #ffffff);
    }

    .auth-box {
      margin-top: 18px;
      padding-top: 16px;
      border-top: 1px solid rgba(31, 65, 52, .12);
    }

    .auth-status {
      min-height: 28px;
      padding: 7px 9px;
      border-radius: 7px;
      background: #eef3ef;
      color: #24473b;
      font-size: 13px;
      font-weight: 850;
    }

    .auth-fields {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
      margin-top: 10px;
    }

    .auth-fields input {
      width: 100%;
      min-width: 0;
      height: 38px;
      border-radius: 7px;
      border: 1px solid rgba(31, 65, 52, .16);
      background: #fff;
      color: #15231e !important;
      font-size: 13px;
    }

    .auth-actions {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 8px;
      margin-top: 10px;
    }

    .auth-actions button {
      height: 38px;
      border: 0;
      border-radius: 7px;
      background: #183f34;
      color: #fff;
      font-size: 13px;
      font-weight: 850;
      cursor: pointer;
    }

    .auth-actions button:nth-child(2) {
      background: #efe4c6;
      color: #473716;
    }

    .auth-actions button:nth-child(3) {
      background: #e9eeee;
      color: #2f453c;
    }

    .auth-hint {
      margin-top: 8px;
      color: #66756e;
      font-size: 12px;
      line-height: 1.45;
    }

    .product-home + .cinematic-hero + .intelligence-stage + script + header + main.wrap {
      margin-top: 0;
      background: #f7faf8;
      color: #15231e;
    }

    body.tier-free main.wrap {
      display: none !important;
    }

    body.tier-research main.wrap .tool-section,
    body.tier-research main.wrap .source-band {
      display: none !important;
    }

    body.tier-free .product-home::after {
      content: "普通账号权限：可看每日结论、3只样本和基础风险提示。登录研究会员后解锁完整股票池、六项证据矩阵和历史复盘。";
      display: block;
      max-width: 1220px;
      margin: 22px auto 0;
      padding: 14px 16px;
      border: 1px solid rgba(199, 151, 62, .36);
      border-radius: 8px;
      background: #fff7e2;
      color: #5a4214;
      font-size: 14px;
      line-height: 1.6;
      font-weight: 750;
    }

    @media (max-width: 900px) {
      .product-topline,
      .decision-hero,
      .section-copy {
        grid-template-columns: 1fr;
        flex-direction: column;
        align-items: stretch;
      }

      .decision-grid,
      .beginner-picks,
      .plan-grid {
        grid-template-columns: 1fr;
      }

      .auth-fields,
      .auth-actions {
        grid-template-columns: 1fr;
      }

      .product-home {
        padding: 16px 14px 36px;
      }
    }

    /* Usability repair: keep the dashboard visible and make the landing layer compact. */
    .product-home {
      min-height: auto !important;
      padding: 18px min(4vw, 48px) 22px !important;
    }

    .product-topline {
      margin-bottom: 14px !important;
      padding-bottom: 10px !important;
    }

    .decision-hero {
      grid-template-columns: minmax(0, 1.2fr) minmax(280px, .8fr) !important;
      gap: 16px !important;
    }

    .decision-copy {
      padding: clamp(22px, 3vw, 34px) !important;
    }

    .decision-copy h1 {
      margin: 14px 0 10px !important;
      font-size: clamp(30px, 3.8vw, 48px) !important;
      line-height: 1.08 !important;
    }

    .decision-actions {
      margin-top: 18px !important;
    }

    .account-panel {
      padding: 18px !important;
    }

    .account-panel ul {
      margin-top: 12px !important;
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }

    .account-panel .auth-box {
      display: none;
    }

    .decision-grid {
      margin-top: 12px !important;
    }

    .decision-tile {
      min-height: 112px !important;
      padding: 16px !important;
    }

    .decision-tile strong {
      font-size: clamp(22px, 2.3vw, 30px) !important;
    }

    .beginner-section,
    .radar-section,
    .membership-section {
      display: none !important;
    }

    body.tier-free .product-home::after {
      display: none !important;
    }

    body.tier-free main.wrap,
    body.tier-research main.wrap,
    body.tier-pro main.wrap,
    body.tier-team main.wrap {
      display: block !important;
    }

    main.wrap {
      margin-top: 0 !important;
      padding-top: 18px !important;
      color: #15231e !important;
    }

    main.wrap .kpi-grid {
      grid-template-columns: repeat(auto-fit, minmax(132px, 1fr)) !important;
      gap: 8px !important;
      margin-bottom: 16px !important;
    }

    main.wrap .kpi {
      min-height: 82px !important;
      padding: 11px !important;
    }

    main.wrap .kpi .value {
      font-size: clamp(18px, 1.7vw, 24px) !important;
    }

    main.wrap section,
    main.wrap .kpi,
    main.wrap .report-card,
    main.wrap .report-block,
    main.wrap .report-note,
    main.wrap .metric-box,
    main.wrap .run-item,
    main.wrap .tool-item,
    main.wrap .task-item,
    main.wrap .gate-cell {
      background: #ffffff !important;
      border-color: rgba(31, 65, 52, .14) !important;
      border-radius: 8px !important;
      box-shadow: 0 12px 32px rgba(18, 45, 37, .08) !important;
      color: #15231e !important;
      backdrop-filter: none !important;
    }

    main.wrap .section-head,
    main.wrap th {
      background: #eef4f0 !important;
      color: #35483f !important;
    }

    main.wrap h2 {
      color: #14231e !important;
      font-family: "Noto Sans SC", "Microsoft YaHei", "PingFang SC", sans-serif !important;
      font-size: 16px !important;
      line-height: 1.25 !important;
      letter-spacing: 0 !important;
      text-transform: none !important;
    }

    main.wrap h3 {
      color: #14231e !important;
      font-family: "Noto Sans SC", "Microsoft YaHei", "PingFang SC", sans-serif !important;
      font-size: 14px !important;
      line-height: 1.25 !important;
      letter-spacing: 0 !important;
      text-transform: none !important;
    }

    main.wrap p,
    main.wrap li,
    main.wrap td,
    main.wrap .report-note {
      color: #34463d !important;
      font-size: 13px !important;
      line-height: 1.55 !important;
    }

    main.wrap .value,
    main.wrap .report-card .value,
    main.wrap .metric-box .value,
    main.wrap [class*="score"] {
      color: #183f34 !important;
      font-family: "Noto Sans SC", "Microsoft YaHei", sans-serif !important;
      letter-spacing: 0 !important;
    }

    main.wrap .tag,
    main.wrap .pill,
    main.wrap .badge,
    main.wrap [class*="badge"],
    main.wrap [class*="pill"] {
      min-height: 24px;
      color: #24473b !important;
      background: #eef3ef !important;
      border: 1px solid rgba(31, 65, 52, .12) !important;
      border-radius: 6px !important;
      box-shadow: none !important;
      font-family: "Noto Sans SC", "Microsoft YaHei", sans-serif !important;
      font-size: 12px !important;
      font-weight: 800 !important;
      letter-spacing: 0 !important;
      text-transform: none !important;
    }

    main.wrap select,
    main.wrap input,
    main.wrap button {
      color: #15231e !important;
      background: #ffffff !important;
      border: 1px solid rgba(31, 65, 52, .16) !important;
      border-radius: 6px !important;
      box-shadow: none !important;
    }

    main.wrap table {
      border-spacing: 0 !important;
      color: #15231e !important;
    }

    main.wrap .gate-layout,
    main.wrap .tool-layout,
    main.wrap .gate-layout > div,
    main.wrap .tool-layout > div,
    main.wrap .tool-list,
    main.wrap .roadmap-list,
    main.wrap .tool-item,
    main.wrap .tool-title,
    main.wrap .tool-note {
      min-width: 0;
      overflow-wrap: anywhere;
      word-break: break-word;
    }

    main.wrap tbody tr,
    main.wrap tr {
      background: #ffffff !important;
    }

    main.wrap td {
      border-color: rgba(31, 65, 52, .10) !important;
      color: #23372f !important;
    }

    main.wrap tbody tr:hover {
      background: #f4f8f5 !important;
      box-shadow: none !important;
    }

    .data-alerts {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
      gap: 10px;
      margin: 0 0 16px;
    }

    .data-alert {
      border: 1px solid rgba(31, 65, 52, .14);
      border-left-width: 5px;
      border-radius: 8px;
      padding: 12px 13px;
      background: #ffffff;
      box-shadow: 0 12px 32px rgba(18, 45, 37, .08);
    }

    .data-alert strong {
      display: block;
      margin-bottom: 5px;
      color: #14231e;
      font-size: 14px;
    }

    .data-alert span {
      color: #53665d;
      font-size: 12px;
      line-height: 1.45;
    }

    .data-alert.warning {
      border-left-color: #c7973e;
    }

    .data-alert.danger {
      border-left-color: #b54b4b;
    }

    .data-alert.info {
      border-left-color: #1b6b4f;
    }

    @media (max-width: 900px) {
      .product-home {
        padding: 12px 12px 14px !important;
      }

      .product-topline {
        gap: 8px !important;
        margin-bottom: 10px !important;
      }

      .product-subtitle {
        display: none;
      }

      .decision-hero {
        grid-template-columns: 1fr !important;
        gap: 10px !important;
      }

      .decision-copy {
        padding: 18px !important;
      }

      .decision-copy h1 {
        margin: 10px 0 8px !important;
        font-size: clamp(28px, 9vw, 38px) !important;
      }

      .decision-copy p {
        font-size: 14px !important;
        line-height: 1.55 !important;
      }

      .decision-actions {
        margin-top: 14px !important;
      }

      .account-panel {
        display: none;
      }

      .decision-grid {
        display: none !important;
      }

      .decision-tile {
        min-height: 76px !important;
        padding: 11px 12px !important;
        gap: 4px !important;
      }

      .decision-tile strong {
        font-size: 22px !important;
      }

      .decision-tile p {
        display: none;
      }

      .beginner-section,
      .radar-section {
        display: none;
      }

      main.wrap {
        padding-top: 12px !important;
      }
    }
  </style>
</head>
<body class="tier-free">
  <section class="product-home" id="decisionHome" aria-label="小白投资者决策首页">
    <div class="product-topline">
      <div>
        <div class="product-brand">信维公式 · 产业趋势投研</div>
        <div class="product-subtitle">每天先回答三个问题：今天能不能买、重点研究谁、哪些先避开。</div>
      </div>
      <div class="account-switcher">
        <span id="accountBadge">普通账号</span>
        <select id="accountTier" aria-label="账号权限">
          <option value="free">普通账号</option>
          <option value="research">研究会员</option>
          <option value="pro">高级会员</option>
          <option value="team">团队版</option>
        </select>
      </div>
    </div>

    <div class="decision-hero">
      <div class="decision-copy">
        <div class="decision-date" id="decisionDate">读取今日数据...</div>
        <h1>今天先看结论，再看证据。</h1>
        <p id="plainConclusion">正在读取本地投研数据库。页面会把模型分、证据缺口和仓位纪律翻译成普通投资者能理解的行动建议。</p>
        <div class="decision-actions">
          <a href="#morningReport">看晨报</a>
          <a href="#gateWorkbench" class="secondary">看闸门证据</a>
        </div>
      </div>
      <aside class="account-panel">
        <div class="panel-eyebrow">当前可见权限</div>
        <h2 id="planName">普通账号</h2>
        <p id="planPitch">适合第一次了解体系的用户，只看今日大方向和少量观察样本。</p>
        <ul id="permissionList"></ul>
        <div class="auth-box">
          <div class="auth-status" id="authStatus">未登录 · 当前按普通账号预览</div>
          <div class="auth-fields">
            <input id="authEmail" type="email" placeholder="邮箱" autocomplete="email">
            <input id="authPassword" type="password" placeholder="密码至少8位" autocomplete="current-password">
          </div>
          <div class="auth-actions">
            <button type="button" id="loginButton">登录</button>
            <button type="button" id="registerButton">注册普通账号</button>
            <button type="button" id="logoutButton">退出</button>
          </div>
          <div class="auth-hint">演示账号：free@xinwei.local / free123456；pro@xinwei.local / pro123456</div>
        </div>
      </aside>
    </div>

    <div class="decision-grid" aria-label="今日操作分区">
      <article class="decision-tile buy">
        <span>今天买什么</span>
        <strong id="buySignal">读取中</strong>
        <p id="buySignalHint">只有六项信维公式全部通过，才进入买入资格池。</p>
      </article>
      <article class="decision-tile watch">
        <span>重点研究</span>
        <strong id="watchSignal">读取中</strong>
        <p id="watchSignalHint">有 S/A 线索但没闭环时，只能进入研究清单。</p>
      </article>
      <article class="decision-tile avoid">
        <span>先别碰</span>
        <strong id="avoidSignal">读取中</strong>
        <p id="avoidSignalHint">高波动、高估值或证据缺失的标的先做风险观察。</p>
      </article>
    </div>

    <div class="beginner-section" id="beginnerPicks">
      <div class="section-copy">
        <span>今日给新手看的 3 个篮子</span>
        <h2>先分清“可以买”和“值得研究”。</h2>
      </div>
      <div class="beginner-picks" id="beginnerPicksList">
        <div class="pick-card">读取股票清单...</div>
      </div>
    </div>

    <div class="radar-section" id="opportunityRadar">
      <div class="section-copy">
        <span>下一个信维通信雷达</span>
        <h2>最像“0到1产业拐点”的候选，先看证据缺口。</h2>
      </div>
      <div class="radar-grid" id="opportunityRadarList">
        <article class="radar-card">读取机会分...</article>
      </div>
    </div>

    <div class="membership-section" id="membershipPlans">
      <div class="section-copy">
        <span>会员产品规划</span>
        <h2>付费不是买答案，而是买证据、纪律和时间。</h2>
      </div>
      <div class="plan-grid" id="planGrid"></div>
    </div>
  </section>
  <section class="cinematic-hero" aria-label="信维公式金融智能首页">
    <div class="cinematic-copy">
      <div class="cinematic-kicker">Live Research Terminal · 产业趋势投资</div>
      <h1 class="cinematic-title">
        <span>信维公式</span>
        <span>市场智能指挥舱</span>
      </h1>
      <p class="cinematic-lede">
        把产业命题、S/A/B/C 证据、六维公式闸门、模型优先级、周度复盘和数据质量巡检合成一个可浏览的在线研究系统。
        它不是短线赌博面板，而是面向产业趋势投资的证据雷达。
      </p>
      <div class="cinematic-actions">
        <a class="cinematic-action" href="#morningReport">查看今日研究队列</a>
        <a class="cinematic-action secondary" href="#qaReport">数据质量与周复盘</a>
      </div>
      <div class="signal-strip" id="cinematicSignals">
        <div class="signal-chip"><b id="heroDataDate">--</b><span>本地数据日</span></div>
        <div class="signal-chip"><b id="heroModelCount">--</b><span>模型样本</span></div>
        <div class="signal-chip"><b id="heroWeeklyCount">--</b><span>周复盘候选</span></div>
        <div class="signal-chip"><b id="heroQaStatus">SYNC</b><span>评测组状态</span></div>
      </div>
    </div>
    <div class="market-3d" aria-label="3D 市场视图动画">
      <div class="orbit-system">
        <div class="orbit-ring r3"></div>
        <div class="orbit-ring"></div>
        <div class="orbit-ring r2"></div>
        <div class="orbit-core"><strong>6D</strong></div>
        <div class="sector-node" style="--angle:0deg;--delay:0s">半导体</div>
        <div class="sector-node" style="--angle:58deg;--delay:.2s">AI算力</div>
        <div class="sector-node" style="--angle:118deg;--delay:.4s">电网设备</div>
        <div class="sector-node" style="--angle:180deg;--delay:.6s">创新药</div>
        <div class="sector-node" style="--angle:238deg;--delay:.8s">机器人</div>
        <div class="sector-node" style="--angle:300deg;--delay:1s">低空经济</div>
      </div>
    </div>
  </section>
  <section class="intelligence-stage" aria-label="信维公式逻辑动画">
    <article class="stage-card">
      <h2>产业脑图</h2>
      <p>从产业趋势出发，先找大逻辑，再查证据，不用热度替代判断。</p>
      <div class="mindmap">
        <div class="hub">产业命题</div>
        <div class="leaf" style="left:4%;top:12%;--line:150px;--line-angle:24deg;--delay:0s">政策催化</div>
        <div class="leaf" style="right:2%;top:16%;--line:138px;--line-angle:154deg;--delay:.4s">订单验证</div>
        <div class="leaf" style="left:8%;bottom:10%;--line:136px;--line-angle:-30deg;--delay:.8s">财务质量</div>
        <div class="leaf" style="right:8%;bottom:12%;--line:132px;--line-angle:-150deg;--delay:1.2s">估值风险</div>
      </div>
    </article>
    <article class="stage-card">
      <h2>信维公式链路</h2>
      <p>S/A 级证据必须闭环，needs_review 不算通过，缺证据只能观察。</p>
      <div class="logic-flow">
        <div class="logic-node" data-step="1" style="--delay:0s">产业趋势进入候选池</div>
        <div class="logic-node" data-step="2" style="--delay:.45s">六项维度逐项校验</div>
        <div class="logic-node" data-step="3" style="--delay:.9s">S/A/B/C 证据打分</div>
        <div class="logic-node" data-step="4" style="--delay:1.35s">量化 EV / 胜率 / 半凯利仅作参考</div>
        <div class="logic-node" data-step="5" style="--delay:1.8s">输出深度研究、等证据或风险排除</div>
      </div>
    </article>
    <article class="stage-card">
      <h2>数据库实时脉冲</h2>
      <p>在线版展示最新快照；本地系统继续负责刷新、QA 巡检和周度复盘。</p>
      <div class="db-console">
        <div class="db-row"><span>model</span><div class="db-bar"><span style="--w:84%;--delay:0s"></span></div><span>84%</span></div>
        <div class="db-row"><span>evidence</span><div class="db-bar"><span style="--w:62%;--delay:.35s"></span></div><span>62%</span></div>
        <div class="db-row"><span>review</span><div class="db-bar"><span style="--w:73%;--delay:.7s"></span></div><span>73%</span></div>
        <div class="db-row"><span>qa</span><div class="db-bar"><span style="--w:91%;--delay:1.05s"></span></div><span>91%</span></div>
      </div>
      <div class="db-ticker">
        <span>stock_model_scores · syncing</span>
        <span>stock_evidence_items · checking</span>
        <span>watchlist_daily_metrics · streaming</span>
        <span>weekly_review_runs · learning</span>
      </div>
    </article>
  </section>
  <script>
    window.addEventListener('DOMContentLoaded', () => {
      fetch('/api/summary', { headers: { 'Accept': 'application/json' } })
        .then((response) => response.ok ? response.json() : null)
        .then((summary) => {
          if (!summary) return;
          const setText = (id, value) => {
            const node = document.getElementById(id);
            if (node) node.textContent = value == null || value === '' ? '--' : value;
          };
          setText('heroDataDate', summary.latest_trade_date);
          setText('heroModelCount', summary.model_score_count);
          setText('heroWeeklyCount', summary.weekly_review_candidate_count);
          setText('heroQaStatus', String(summary.qa_status || 'unknown').toUpperCase());
        })
        .catch(() => {});
    });
  </script>
  <header>
    <div class="wrap topbar">
      <div>
        <h1>A股信维公式观察台</h1>
        <div class="subtitle">本地 SQLite 数据库 · 每日推荐入池 · 后续走势复盘</div>
      </div>
      <div class="status-pill" id="freshness">读取本地数据中</div>
    </div>
  </header>

  <main class="wrap" id="terminalWorkbench">
    <div class="data-alerts" id="dataAlerts"></div>

    <section class="morning-section" id="morningReport">
      <div class="section-head">
        <h2>信维晨报</h2>
        <span class="tag" id="morningMeta">本地网页推送</span>
      </div>
      <div class="report-layout">
        <div class="report-stack">
          <div class="report-note" id="morningHeadline">读取本地晨报中</div>
          <div class="report-grid" id="morningCards"></div>
          <div class="report-block">
            <h3>最新候选Top</h3>
            <div class="morning-list" id="morningTopCandidates"></div>
          </div>
        </div>
        <div class="report-stack">
          <div class="report-block">
            <h3>研究队列</h3>
            <div class="morning-list" id="morningQueues"></div>
          </div>
          <div class="report-block">
            <h3>S/A证据缺口</h3>
            <div class="morning-list" id="morningEvidenceGaps"></div>
          </div>
        </div>
      </div>
    </section>

    <div class="kpi-grid" id="kpis"></div>

    <section class="gate-section" id="gateWorkbench">
      <div class="section-head">
        <h2>公式闸门工作台</h2>
        <span class="tag">模型分只排研究优先级；买入资格只看六项S/A闭环</span>
      </div>
      <div class="gate-layout">
        <div>
          <h2>六项闸门矩阵</h2>
          <div class="gate-matrix" id="gateMatrix"></div>
        </div>
        <div>
          <h2>待核验证据任务</h2>
          <div class="task-list" id="researchTasks"></div>
        </div>
      </div>
    </section>

    <div class="grid">
      <section>
        <div class="section-head">
          <h2>观察池跟踪</h2>
          <div class="controls">
            <select id="statusFilter" aria-label="状态筛选">
              <option value="all">全部状态</option>
              <option value="active" selected>只看 active</option>
            </select>
            <input id="searchBox" type="search" placeholder="搜索代码 / 名称 / 行业" aria-label="搜索观察池">
          </div>
        </div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>优先级</th>
                <th>代码</th>
                <th>名称</th>
                <th>研究队列</th>
                <th>状态</th>
                <th>入池日</th>
                <th>入池价</th>
                <th>最新价</th>
                <th>收益</th>
                <th>回撤</th>
                <th>模型分</th>
                <th>因子分</th>
                <th>质量</th>
                <th>证据分</th>
                <th>风险分</th>
                <th>市场分</th>
                <th>成交额</th>
                <th>行业</th>
                <th>PE</th>
                <th>PB</th>
              </tr>
            </thead>
            <tbody id="watchRows">
              <tr><td colspan="20" class="empty">读取中</td></tr>
            </tbody>
          </table>
        </div>
      </section>

      <div class="right-col">
        <section>
          <div class="section-head">
            <h2>单票走势</h2>
            <div class="controls">
              <select id="stockSelect" aria-label="选择股票"></select>
            </div>
          </div>
          <div class="panel-body">
            <div class="chart-shell"><canvas id="trendCanvas"></canvas></div>
            <div class="stock-summary" id="stockSummary"></div>
            <ul class="mini-list" id="stockNotes"></ul>
          </div>
        </section>

        <section>
          <div class="section-head">
            <h2>最新日报候选</h2>
          </div>
          <div class="run-list" id="runList"></div>
        </section>
      </div>
    </div>

    <section class="tool-section">
      <div class="section-head">
        <h2>GitHub 能力库</h2>
        <span class="tag">工具只增强研究，不自动交易</span>
      </div>
      <div class="tool-layout">
        <div>
          <h2>可复用工具</h2>
          <div class="tool-list" id="toolList"></div>
        </div>
        <div>
          <h2>能力路线图</h2>
          <div class="roadmap-list" id="roadmapList"></div>
        </div>
      </div>
    </section>

    <div class="source-band">
      <div><strong>数据底座</strong><br>行情来自本地 `market_snapshot`，因子来自 `stock_factor_daily`，推荐来自 `recommendation_runs` 与 `recommendation_candidates`，观察池来自 `stock_watchlist`。</div>
      <div><strong>使用口径</strong><br>脚本候选只作为第一层市场筛选；买入前仍必须按信维公式做产业命题、客户绑定、订单产能、扣非利润和预期差验证。</div>
    </div>
  </main>

  <script>
    const state = { summary: null, morningReport: null, watchlist: [], runs: [], tools: [], roadmap: [], gateMatrix: [], researchTasks: [], opportunityRadar: null, account: null, selectedCode: null, stock: null };
    const money = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2 });
    const pct = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2, minimumFractionDigits: 2 });

    function escapeHtml(value) {
      return String(value ?? '').replace(/[&<>"']/g, s => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[s]));
    }
    function fmt(value, digits = 2) {
      if (value === null || value === undefined || Number.isNaN(Number(value))) return '-';
      return Number(value).toLocaleString('zh-CN', { maximumFractionDigits: digits });
    }
    function fmtPct(value) {
      if (value === null || value === undefined || Number.isNaN(Number(value))) return '-';
      const cls = value > 0 ? 'pos' : value < 0 ? 'neg' : 'flat';
      const sign = value > 0 ? '+' : '';
      return `<span class="${cls}">${sign}${pct.format(Number(value))}%</span>`;
    }
    function returnClass(value) {
      if (value > 0) return 'pos';
      if (value < 0) return 'neg';
      return 'flat';
    }
    function bucketText(value) {
      return ({
        formula_supported: '公式已验证',
        deep_research: '深度研究',
        blocked_by_evidence: '证据阻塞',
        track: '继续跟踪',
        wait_evidence: '等证据',
        risk_watch: '风险观察',
        archive_watch: '低优先'
      })[value] || value || '-';
    }
    function riskReasonText(value) {
      return ({
        'scarcity/customer binding still pending': '稀缺卡位/客户绑定仍待验证',
        'most Xinwei dimensions still pending': '多数信维维度仍待验证',
        'several Xinwei dimensions still pending': '多项信维维度仍待验证',
        'no S/A evidence collected': '尚未采集S/A级证据',
        'post-entry return below -10%': '入池后收益低于-10%',
        'post-entry return below 0': '入池后收益为负',
        'drawdown worse than -15%': '回撤超过-15%',
        'drawdown worse than -8%': '回撤超过-8%',
        'PE valuation needs caution': 'PE估值需谨慎',
        'PB above 15': 'PB高于15',
        'PB above 10': 'PB高于10',
        'latest market score below 45': '最新市场分低于45',
        'ST risk flag': '存在ST风险标签'
      })[value] || value;
    }
    function decisionTone(value) {
      return ({
        '可买': 'buy',
        '等证据': 'wait',
        '只观察': 'watch',
        '排除': 'exclude'
      })[value] || 'watch';
    }
    async function api(path) {
      const response = await fetch(path, { headers: { 'Accept': 'application/json' } });
      if (!response.ok) throw new Error(`${path} ${response.status}`);
      return response.json();
    }
    function renderKpis() {
      const s = state.summary || {};
      const cards = [
        ['最新数据日', s.latest_trade_date || '-', s.snapshot_rows ? `${fmt(s.snapshot_rows, 0)} 只A股` : '等待快照'],
        ['观察池', fmt(s.watchlist_count || 0, 0), '历史推荐自动沉淀'],
        ['能力库', fmt(s.tool_registry_count || 0, 0), `${fmt(s.capability_roadmap_count || 0, 0)} 项路线图`],
        ['历史K线库', fmt(s.kline_row_count || 0, 0), `${fmt(s.kline_stock_count || 0, 0)} 只，至 ${s.kline_max_date || '-'}`],
        ['源校验', `${fmt(s.provider_ok_count || 0, 0)}/${fmt(s.provider_health_count || 0, 0)}`, s.provider_health_date ? `${s.provider_health_date}，缺K线 ${fmt(s.provider_comparison_missing_count || 0, 0)}` : '等待校验'],
        ['纸面回放', `${fmt(s.replay_active_count || 0, 0)}/${fmt(s.replay_total_count || 0, 0)}`, s.replay_latest_date ? `最新${s.replay_latest_date}，均值 ${fmt(s.replay_avg_latest_return)}%` : '等待回放'],
        ['因子层', fmt(s.factor_row_count || 0, 0), s.factor_trade_date ? `${s.factor_version || '-'}，技术均值 ${fmt(s.avg_technical_score)}` : '等待因子'],
        ['公式已验证', fmt(s.formula_supported_count || 0, 0), '六项S/A闭环后才有买入资格'],
        ['深度研究', fmt(s.deep_research_count || 0, 0), `模型日 ${s.model_trade_date || '-'}`],
        ['证据阻塞', fmt(s.blocked_by_evidence_count || 0, 0), '关键维度未闭环，仓位0%'],
        ['等证据', fmt(s.wait_evidence_count || 0, 0), '市场强但信维证据薄'],
        ['买入资格', fmt(s.formula_gate_eligible_count || 0, 0), `待核${fmt(s.formula_gate_needs_review_count || 0, 0)} · 缺证${fmt(s.formula_gate_missing_count || 0, 0)}`],
        ['最新候选', fmt(s.latest_candidate_count || 0, 0), s.latest_run_id ? `Run #${s.latest_run_id}` : '暂无日报'],
        ['模型Top', fmt(s.top_model_score), escapeHtml(s.top_model_stock || '')],
        ['平均收益', fmtPct(s.avg_return_pct), '按首次入池价计算'],
        ['最佳收益', fmtPct(s.best_return_pct), escapeHtml(s.best_stock || '')],
        ['最大回撤', fmtPct(s.worst_drawdown_pct), '入池后价格序列'],
      ];
      document.getElementById('kpis').innerHTML = cards.map(([label, value, hint]) => `
        <div class="kpi">
          <div class="label">${label}</div>
          <div class="value">${value}</div>
          <div class="hint">${hint}</div>
        </div>
      `).join('');
      document.getElementById('freshness').textContent = `本地库刷新：${s.latest_trade_date || '-'} · ${s.generated_at || '-'}`;
      renderDataAlerts();
    }
    function renderDataAlerts() {
      const node = document.getElementById('dataAlerts');
      if (!node) return;
      const s = state.summary || {};
      const alerts = [];
      const eligible = Number(s.formula_gate_eligible_count || 0);
      const providerTotal = Number(s.provider_health_count || 0);
      const providerOk = Number(s.provider_ok_count || 0);
      const qaStatus = String(s.qa_status || '').toLowerCase();
      if (eligible <= 0) {
        alerts.push({
          tone: 'danger',
          title: '买入池为空，实际仓位 0%',
          body: '六项信维公式没有形成 S/A 证据闭环，模型分只能决定研究顺序。'
        });
      }
      if (s.provider_health_date && s.latest_trade_date && s.provider_health_date !== s.latest_trade_date) {
        alerts.push({
          tone: 'warning',
          title: '数据源校验滞后',
          body: `行情数据日 ${s.latest_trade_date}，源校验仍停在 ${s.provider_health_date}。`
        });
      }
      if (providerTotal > 0 && providerOk < providerTotal) {
        alerts.push({
          tone: 'warning',
          title: `源校验通过 ${fmt(providerOk, 0)}/${fmt(providerTotal, 0)}`,
          body: `比对缺失 ${fmt(s.provider_comparison_missing_count || 0, 0)}，主源缺失 ${fmt(s.provider_primary_missing_count || 0, 0)}，日报结论需保守。`
        });
      }
      if (qaStatus === 'fail') {
        alerts.push({
          tone: 'danger',
          title: 'QA 巡检失败',
          body: `最近 QA run #${s.qa_run_id || '-'} 状态为 fail，告警 ${fmt(s.qa_alert_count || 0, 0)} 条。`
        });
      }
      if (Number(s.replay_missing_count || 0) > 0) {
        alerts.push({
          tone: 'info',
          title: '纸面回放存在缺口',
          body: `当前缺少 ${fmt(s.replay_missing_count || 0, 0)} 条入场回放，不用短期回放替代产业证据。`
        });
      }
      node.innerHTML = alerts.length
        ? alerts.map(item => `
          <div class="data-alert ${item.tone}">
            <strong>${escapeHtml(item.title)}</strong>
            <span>${escapeHtml(item.body)}</span>
          </div>
        `).join('')
        : '<div class="data-alert info"><strong>暂无硬性数据告警</strong><span>仍需按信维公式逐项验证，不把市场分当作买入理由。</span></div>';
    }
    function renderMorningCandidates(rows, emptyText) {
      if (!rows || !rows.length) return `<div class="empty">${emptyText}</div>`;
      return rows.map(row => {
        const gaps = (row.sa_evidence_gaps || row.blocking_dimension_labels || []).slice(0, 4);
        const gapText = gaps.length ? gaps.join('、') : '无结构化缺口';
        return `
          <div class="morning-item morning-stock" data-code="${escapeHtml(row.code)}">
            <div class="morning-title">
              <span><span class="code">${escapeHtml(row.code)}</span> ${escapeHtml(row.name)}</span>
              <span class="tag">${escapeHtml(bucketText(row.action_bucket))}</span>
            </div>
            <div class="morning-meta">
              <span>优先级 #${escapeHtml(row.priority_rank || '-')}</span>
              <span>总分 ${fmt(row.total_score)}</span>
              <span>证据 ${fmt(row.evidence_availability_score ?? row.evidence_score)}</span>
              <span>闸门 ${fmt(row.formula_verification_score)}</span>
            </div>
            <div class="gap-line">S/A缺口：${escapeHtml(gapText)}</div>
          </div>`;
      }).join('');
    }
    function renderMorningReport() {
      const report = state.morningReport || {};
      const health = report.data_source_health || {};
      const replay = report.paper_replay || {};
      const position = report.position_boundary || {};
      const gate = report.gate_summary || {};
      const queues = report.queues || {};
      document.getElementById('morningMeta').textContent = `${report.report_date || '-'} · 数据 ${report.model_trade_date || report.latest_trade_date || '-'}`;
      document.getElementById('morningHeadline').innerHTML = `
        <strong>${escapeHtml(report.headline || '暂无晨报')}</strong>
        <span>${escapeHtml(report.conclusion || '等待本地数据刷新')}</span>
      `;
      const cards = [
        ['数据源健康', `${fmt(health.ok_count || 0, 0)}/${fmt(health.total_count || 0, 0)}`, health.check_date ? `${health.check_date} · 异常${fmt(health.issue_count || 0, 0)}` : '等待校验'],
        ['纸面回放', `${fmt(replay.active_count || 0, 0)}/${fmt(replay.total_count || 0, 0)}`, replay.latest_date ? `均值${fmt(replay.avg_latest_return_pct)}% · T+1 ${fmt(replay.avg_1d_return_pct)}%` : '等待回放'],
        ['买入池', fmt(position.buy_pool_count || 0, 0), position.today_position_boundary || '0%'],
        ['闸门快照', fmt(gate.total || 0, 0), `闭环${fmt(gate.formula_supported || 0, 0)} · 缺证${fmt(gate.missing_evidence || 0, 0)}`],
        ['证据阻塞', fmt((queues.blocked_by_evidence || []).length, 0), '模型高分但关键维度未闭环'],
        ['等待证据', fmt((queues.wait_evidence || []).length, 0), '市场/因子只决定研究顺序'],
      ];
      document.getElementById('morningCards').innerHTML = cards.map(([label, value, hint]) => `
        <div class="report-card">
          <div class="label">${label}</div>
          <div class="value">${value}</div>
          <div class="hint">${escapeHtml(hint)}</div>
        </div>
      `).join('');
      document.getElementById('morningTopCandidates').innerHTML = renderMorningCandidates(report.top_candidates || [], '暂无候选队列');
      const queueRows = [
        ['blocked_by_evidence', '证据阻塞', queues.blocked_by_evidence || []],
        ['deep_research', '深挖', queues.deep_research || []],
        ['wait_evidence', '等待证据', queues.wait_evidence || []],
      ];
      document.getElementById('morningQueues').innerHTML = queueRows.map(([key, label, rows]) => `
        <div class="morning-item">
          <div class="morning-title"><span>${label}</span><span class="tag">${fmt(rows.length, 0)}只</span></div>
          <div class="candidate-strip">
            ${rows.slice(0, 8).map(row => `<span class="tag morning-stock" data-code="${escapeHtml(row.code)}">${escapeHtml(row.code)} ${escapeHtml(row.name)}</span>`).join('') || '<span class="tag">暂无</span>'}
          </div>
        </div>
      `).join('');
      const tasks = report.evidence_gap_tasks || [];
      document.getElementById('morningEvidenceGaps').innerHTML = tasks.length ? tasks.slice(0, 12).map(task => `
        <div class="morning-item morning-stock" data-code="${escapeHtml(task.code)}">
          <div class="morning-title">
            <span><span class="code">${escapeHtml(task.code)}</span> ${escapeHtml(task.name)}</span>
            <span class="tag">P${escapeHtml(task.priority)}</span>
          </div>
          <div class="morning-meta"><span>${escapeHtml(task.dimension_name)}</span><span>${escapeHtml(task.task_type)}</span></div>
          <div class="gap-line">${escapeHtml(task.title)}</div>
        </div>
      `).join('') : '<div class="empty">暂无待核验证据任务</div>';
      document.querySelectorAll('.morning-stock[data-code]').forEach(node => {
        node.addEventListener('click', () => selectStock(node.dataset.code));
      });
    }
    function gateStatusText(value) {
      return ({
        verified: '已验证',
        needs_review: '有线索',
        pending: '缺证',
        failed: '冲突',
        stale: '过期'
      })[value] || value || '-';
    }
    function dimensionShortName(value) {
      return ({
        industry_inflection: '产业',
        scarcity_position: '卡位',
        leader_customer_binding: '客户',
        capacity_order_expansion: '产能',
        earnings_inflection: '业绩',
        expectation_gap: '预期差'
      })[value] || value || '-';
    }
    function renderGateMatrix() {
      const box = document.getElementById('gateMatrix');
      const rows = state.gateMatrix || [];
      const dims = ['industry_inflection', 'scarcity_position', 'leader_customer_binding', 'capacity_order_expansion', 'earnings_inflection', 'expectation_gap'];
      if (!rows.length) {
        box.innerHTML = '<div class="empty">暂无公式闸门快照</div>';
        return;
      }
      const head = `
        <div class="gate-row">
          <div class="gate-cell head">股票</div>
          ${dims.map(d => `<div class="gate-cell head">${dimensionShortName(d)}</div>`).join('')}
          <div class="gate-cell head">资格</div>
        </div>`;
      const body = rows.slice(0, 30).map(row => {
        const byDim = {};
        (row.dimensions || []).forEach(d => { byDim[d.dimension_id] = d; });
        return `
          <div class="gate-row" data-code="${escapeHtml(row.code)}">
            <div class="gate-cell"><span class="code">${escapeHtml(row.code)}</span> ${escapeHtml(row.name)}</div>
            ${dims.map(d => {
              const status = byDim[d] ? byDim[d].status : 'pending';
              return `<div class="gate-cell gate-status-${escapeHtml(status)}" title="${escapeHtml((byDim[d] && byDim[d].dimension_name) || d)}">${gateStatusText(status)}</div>`;
            }).join('')}
            <div class="gate-cell">${row.eligible_for_buy ? '可进入' : '0%'}</div>
          </div>`;
      }).join('');
      box.innerHTML = head + body;
      box.querySelectorAll('.gate-row[data-code]').forEach(row => {
        row.addEventListener('click', () => selectStock(row.dataset.code));
      });
    }
    function renderResearchTasks() {
      const box = document.getElementById('researchTasks');
      const rows = state.researchTasks || [];
      if (!rows.length) {
        box.innerHTML = '<div class="empty">暂无待核验任务</div>';
        return;
      }
      box.innerHTML = rows.slice(0, 30).map(task => `
        <div class="task-item">
          <div class="task-title">P${escapeHtml(task.priority)} ${escapeHtml(task.code)} ${escapeHtml(task.name)} · ${escapeHtml(task.dimension_name)}</div>
          <div>${escapeHtml(task.title)}</div>
          <div style="color: var(--muted); margin-top: 4px;">${escapeHtml(task.detail)}</div>
        </div>
      `).join('');
    }
    function filteredWatchlist() {
      const status = document.getElementById('statusFilter').value;
      const q = document.getElementById('searchBox').value.trim().toLowerCase();
      return state.watchlist.filter(row => {
        const statusOk = status === 'all' || row.status === status;
        const hay = `${row.code} ${row.name} ${row.industry || ''} ${row.model_action_bucket || ''}`.toLowerCase();
        return statusOk && (!q || hay.includes(q));
      });
    }
    function renderWatchlist() {
      const rows = filteredWatchlist();
      const tbody = document.getElementById('watchRows');
      if (!rows.length) {
        tbody.innerHTML = '<tr><td colspan="20" class="empty">没有匹配的观察池股票</td></tr>';
        return;
      }
      tbody.innerHTML = rows.map(row => `
        <tr data-code="${escapeHtml(row.code)}" class="${row.code === state.selectedCode ? 'active-row' : ''}">
          <td>${row.model_priority_rank ? `#${escapeHtml(row.model_priority_rank)}` : '-'}</td>
          <td><span class="code">${escapeHtml(row.code)}</span></td>
          <td><span class="name">${escapeHtml(row.name)}</span></td>
          <td><span class="tag">${escapeHtml(bucketText(row.model_action_bucket))}</span></td>
          <td><span class="tag">${escapeHtml(row.status)}</span></td>
          <td>${escapeHtml(row.first_recommended_date)}</td>
          <td>${fmt(row.first_price)}</td>
          <td>${fmt(row.latest_price)}</td>
          <td>${fmtPct(row.return_pct)}</td>
          <td>${fmtPct(row.max_drawdown_pct)}</td>
          <td>${fmt(row.model_total_score)}</td>
          <td>${fmt(row.model_factor_score)}</td>
          <td>${fmt(row.model_factor_quality_score)}</td>
          <td>${fmt(row.model_evidence_score)}</td>
          <td>${fmt(row.model_risk_score)}</td>
          <td>${fmt(row.model_market_score)}</td>
          <td>${fmt(row.amount_yi)}亿</td>
          <td>${escapeHtml(row.industry || '-')}</td>
          <td>${fmt(row.pe_ttm)}</td>
          <td>${fmt(row.pb)}</td>
        </tr>
      `).join('');
      tbody.querySelectorAll('tr[data-code]').forEach(tr => {
        tr.addEventListener('click', () => selectStock(tr.dataset.code));
      });
    }
    function renderSelect() {
      const select = document.getElementById('stockSelect');
      select.innerHTML = state.watchlist.map(row => `
        <option value="${escapeHtml(row.code)}">${escapeHtml(row.code)} ${escapeHtml(row.name)}</option>
      `).join('');
      if (state.selectedCode) select.value = state.selectedCode;
      select.addEventListener('change', () => selectStock(select.value));
    }
    function renderRuns() {
      const list = document.getElementById('runList');
      if (!state.runs.length) {
        list.innerHTML = '<div class="empty">暂无推荐日报</div>';
        return;
      }
      list.innerHTML = state.runs.slice(0, 5).map(run => `
        <div class="run-item">
          <div class="run-title">
            <span>#${run.id} · ${escapeHtml(run.run_date)}</span>
            <span>${fmt(run.candidate_count, 0)} 只</span>
          </div>
          <div class="candidate-strip">
            ${(run.candidates || []).slice(0, 8).map(c => {
              const replay = c.replay || {};
              const replayText = replay.status === 'open' || replay.status === 'complete_20d'
                ? ` T+1 ${fmt(replay.return_1d_pct)}%`
                : '';
              return `<span class="tag">${escapeHtml(c.rank)} ${escapeHtml(c.name)} ${fmt(c.score, 1)}${escapeHtml(replayText)}</span>`;
            }).join('')}
          </div>
        </div>
      `).join('');
    }
    function renderToolRegistry() {
      const toolList = document.getElementById('toolList');
      const roadmapList = document.getElementById('roadmapList');
      if (!state.tools.length) {
        toolList.innerHTML = '<div class="empty">暂无工具登记</div>';
      } else {
        toolList.innerHTML = state.tools.map(row => `
          <div class="tool-item">
            <div class="tool-title">
              <span>${escapeHtml(row.repo_full_name)}</span>
              <span class="tag">${escapeHtml(row.reuse_decision)}</span>
            </div>
            <div class="tool-meta">
              <span class="tag">${escapeHtml(row.category)}</span>
              <span class="tag">${escapeHtml(row.capability_layer)}</span>
            </div>
            <div class="tool-note">${escapeHtml(row.integration_plan)}</div>
          </div>
        `).join('');
      }
      if (!state.roadmap.length) {
        roadmapList.innerHTML = '<div class="empty">暂无路线图</div>';
      } else {
        roadmapList.innerHTML = state.roadmap.slice(0, 9).map(row => `
          <div class="tool-item">
            <div class="tool-title">
              <span>${escapeHtml(row.capability_name)}</span>
              <span class="tag">${escapeHtml(row.status)}</span>
            </div>
            <div class="tool-meta">
              <span class="tag">P${escapeHtml(row.priority)}</span>
              <span class="tag">${escapeHtml(row.capability_id)}</span>
            </div>
            <div class="tool-note">${escapeHtml(row.next_step)}</div>
          </div>
        `).join('');
      }
    }
    function drawTrend(stock) {
      const canvas = document.getElementById('trendCanvas');
      const box = canvas.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      canvas.width = Math.max(320, Math.floor(box.width * dpr));
      canvas.height = Math.max(220, Math.floor(box.height * dpr));
      const ctx = canvas.getContext('2d');
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      const w = box.width;
      const h = box.height;
      ctx.clearRect(0, 0, w, h);
      const history = (stock && stock.history || []).filter(d => d.price !== null && d.price !== undefined);
      const pad = { left: 48, right: 18, top: 22, bottom: 34 };
      ctx.strokeStyle = '#d9e0ea';
      ctx.lineWidth = 1;
      for (let i = 0; i < 4; i++) {
        const y = pad.top + (h - pad.top - pad.bottom) * i / 3;
        ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(w - pad.right, y); ctx.stroke();
      }
      if (!history.length) {
        ctx.fillStyle = '#667085';
        ctx.font = '13px Microsoft YaHei, sans-serif';
        ctx.fillText('暂无价格序列', pad.left, h / 2);
        return;
      }
      const prices = history.map(d => Number(d.price));
      const min = Math.min(...prices);
      const max = Math.max(...prices);
      const span = Math.max(.01, max - min);
      const x = i => history.length === 1 ? (pad.left + w - pad.right) / 2 : pad.left + (w - pad.left - pad.right) * i / (history.length - 1);
      const y = p => pad.top + (max - p) / span * (h - pad.top - pad.bottom);
      ctx.fillStyle = '#667085';
      ctx.font = '12px Microsoft YaHei, sans-serif';
      ctx.fillText(fmt(max), 8, pad.top + 4);
      ctx.fillText(fmt(min), 8, h - pad.bottom + 4);
      const first = Number(stock.watch.first_price || prices[0]);
      if (first) {
        const yFirst = y(first);
        ctx.setLineDash([4, 4]);
        ctx.strokeStyle = '#ad6b14';
        ctx.beginPath(); ctx.moveTo(pad.left, yFirst); ctx.lineTo(w - pad.right, yFirst); ctx.stroke();
        ctx.setLineDash([]);
      }
      const lastPrice = prices[prices.length - 1];
      ctx.strokeStyle = lastPrice >= first ? '#b94747' : '#16735b';
      ctx.lineWidth = 2.5;
      ctx.beginPath();
      history.forEach((d, i) => {
        const px = x(i), py = y(Number(d.price));
        if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
      });
      ctx.stroke();
      history.forEach((d, i) => {
        const px = x(i), py = y(Number(d.price));
        ctx.fillStyle = i === history.length - 1 ? '#17202d' : '#245ec4';
        ctx.beginPath(); ctx.arc(px, py, i === history.length - 1 ? 4 : 2.6, 0, Math.PI * 2); ctx.fill();
      });
      ctx.fillStyle = '#667085';
      ctx.fillText(history[0].trade_date, pad.left, h - 10);
      ctx.textAlign = 'right';
      ctx.fillText(history[history.length - 1].trade_date, w - pad.right, h - 10);
      ctx.textAlign = 'left';
    }
    function renderStock(stock) {
      state.stock = stock;
      drawTrend(stock);
      const w = stock.watch || {};
      const m = stock.model_score || {};
      const opp = stock.opportunity || {};
      document.getElementById('stockSummary').innerHTML = [
        ['机会结论', escapeHtml(opp.decision_label || '-')],
        ['机会分', fmt(opp.opportunity_score)],
        ['EV', opp.ev_pct === null || opp.ev_pct === undefined ? '高级会员解锁' : `${fmt(opp.ev_pct)}%`],
        ['仓位上限', opp.position_cap_pct === null || opp.position_cap_pct === undefined ? '高级会员解锁' : `${fmt(opp.position_cap_pct)}%`],
        ['入池', `${escapeHtml(w.first_recommended_date || '-')} · #${escapeHtml(w.first_rank || '-')}`],
        ['入池价', fmt(w.first_price)],
        ['最新价', fmt(w.latest_price)],
        ['收益', fmtPct(w.return_pct)],
        ['最大回撤', fmtPct(w.max_drawdown_pct)],
        ['研究队列', escapeHtml(bucketText(m.action_bucket || w.model_action_bucket))],
        ['模型分', fmt(m.total_score || w.model_total_score)],
        ['因子分', fmt(m.factor_score || w.model_factor_score)],
        ['因子质量', fmt(m.factor_quality_score || w.model_factor_quality_score)],
        ['证据分', fmt(m.evidence_score || w.model_evidence_score)],
      ].map(([label, value]) => `<div class="metric-box"><div class="label">${label}</div><div class="value">${value}</div></div>`).join('');
      const thesis = w.thesis || '待补充产业命题验证';
      const risks = w.risk_flags || '无明显规则风险';
      const candidates = (stock.candidates || []).slice(0, 4).map(c => `${c.run_date} #${c.rank} 得分${fmt(c.score, 1)}`).join('；');
      const reviews = stock.xinwei_reviews || [];
      const reviewStatus = reviews.length
        ? reviews.map(r => `${r.dimension_name}:${r.status}`).join('；')
        : '待建立验证清单';
      const evidence = stock.evidence_items || [];
      const sCount = m.s_evidence_count ?? evidence.filter(e => e.evidence_grade === 'S').length;
      const aCount = m.a_evidence_count ?? evidence.filter(e => e.evidence_grade === 'A').length;
      const latestEvidence = evidence[0]
        ? `${evidence[0].evidence_date || '-'} ${evidence[0].evidence_grade}级 ${evidence[0].title}`
        : '暂无';
      const gateSnapshot = stock.xinwei_gate_snapshot || {};
      const evidenceLinks = stock.evidence_links || [];
      const blockingDims = gateSnapshot.blocking_dimensions || [];
      const linkLine = evidenceLinks.length
        ? `结构化证据链${evidenceLinks.length}条；阻塞维度：${blockingDims.join('、') || '无'}`
        : `结构化证据链暂无；阻塞维度：${blockingDims.join('、') || '待生成'}`;
      const coverage = stock.research_coverage
        ? `近180天研报${stock.research_coverage.report_count_180d}篇，机构${stock.research_coverage.org_count}家，最新评级${stock.research_coverage.latest_rating || '-'}`
        : '暂无机构覆盖数据';
      const modelDetail = m.score_detail || {};
      const formulaGate = modelDetail.formula_gate || {};
      const factorDetail = modelDetail.factor_detail || {};
      const factorLine = factorDetail.available
        ? `因子层：综合${fmt(m.factor_score)}，质量${fmt(m.factor_quality_score)}，样本${escapeHtml(factorDetail.sample_days || '-')}日，趋势${fmt(factorDetail.trend_score)} / 资金${fmt(factorDetail.flow_score)} / 流动性${fmt(factorDetail.liquidity_score)} / 技术${fmt(factorDetail.technical_score)}，RSI${fmt(factorDetail.rsi14)}，ATR${fmt(factorDetail.atr14_pct)}`
        : '因子层：暂无因子记录';
      const modelLine = m.total_score !== undefined
        ? `研究优先级：#${m.priority_rank || '-'} ${bucketText(m.action_bucket)}，总分${fmt(m.total_score)}，市场${fmt(m.market_score)} / 证据${fmt(m.evidence_score)} / 因子${fmt(m.factor_score)} / 行为${fmt(m.behavior_score)} / 风险${fmt(m.risk_score)}`
        : '研究优先级：暂无模型评分';
      const gateLine = formulaGate.status
        ? `公式闸门：${escapeHtml(formulaGate.label || formulaGate.status)}；买入资格=${formulaGate.eligible_for_buy ? '是' : '否'}；已验证${fmt(formulaGate.supported_count || 0, 0)}/${fmt(formulaGate.required_count || 6, 0)}；待核验：${escapeHtml((formulaGate.needs_review_dimensions || []).join('、') || '无')}；缺证据：${escapeHtml((formulaGate.pending_dimensions || []).join('、') || '无')}`
        : '公式闸门：暂无';
      const riskReasons = modelDetail.risk_reasons && modelDetail.risk_reasons.length
        ? modelDetail.risk_reasons.map(riskReasonText).join('；')
        : '暂无模型风险扣分说明';
      const oppEvidence = opp.evidence_validation || {};
      const oppGate = opp.formula_gate || {};
      const oppEvents = opp.thesis_events || [];
      const oppLocks = opp.locked_fields || [];
      const opportunityLine = opp.decision_label
        ? `机会结论：${escapeHtml(opp.decision_label)}；机会分${fmt(opp.opportunity_score)}；关键缺口：${escapeHtml(opp.key_gap || '无')}`
        : '机会结论：等待机会分刷新';
      const opportunityEvidenceLine = Object.keys(oppEvidence).length
        ? `S/A/B/C验证：S${fmt(oppEvidence.S || 0, 0)} / A${fmt(oppEvidence.A || 0, 0)} / B${fmt(oppEvidence.B || 0, 0)} / C${fmt(oppEvidence.C || 0, 0)}；公式${fmt(oppGate.supported_count || 0, 0)}/${fmt(oppGate.required_count || 6, 0)}`
        : `S/A/B/C验证：${escapeHtml(oppLocks[1] || '会员解锁完整证据链')}`;
      const opportunityEvLine = opp.ev_pct === null || opp.ev_pct === undefined
        ? `EV/仓位：${escapeHtml(oppLocks[0] || '高级会员解锁')}`
        : `EV/仓位：EV ${fmt(opp.ev_pct)}%，胜率${fmt((opp.win_rate || 0) * 100)}%，赔率${fmt(opp.odds)}，半凯利${fmt((opp.half_kelly || 0) * 100)}%，仓位≤${fmt(opp.position_cap_pct)}%`;
      const opportunityCatalystLine = oppEvents.length
        ? `未来90天催化：${escapeHtml(oppEvents.slice(0, 3).map(e => `${e.catalyst_date || e.event_date} ${e.title}`).join('；'))}`
        : `未来90天催化：${escapeHtml(opp.catalyst_summary || '等待催化')}`;
      document.getElementById('stockNotes').innerHTML = [
        opportunityLine,
        opportunityEvidenceLine,
        opportunityEvLine,
        opportunityCatalystLine,
        modelLine,
        gateLine,
        factorLine,
        `初筛命题：${escapeHtml(thesis)}`,
        `信维公式：${escapeHtml(reviewStatus)}`,
        `证据链：${escapeHtml(linkLine)}`,
        `证据库：S级${sCount}条，A级${aCount}条；最新：${escapeHtml(latestEvidence)}`,
        `机构覆盖：${escapeHtml(coverage)}`,
        `模型风险：${escapeHtml(riskReasons)}`,
        `风险标签：${escapeHtml(risks)}`,
        `候选记录：${escapeHtml(candidates || '暂无')}`,
      ].map(text => `<li>${text}</li>`).join('');
      renderWatchlist();
    }
    async function selectStock(code) {
      if (!code) return;
      state.selectedCode = code;
      document.getElementById('stockSelect').value = code;
      const stock = await api(`/api/stock?code=${encodeURIComponent(code)}`);
      renderStock(stock);
    }
    const accountPlans = {
      free: {
        name: '普通账号',
        price: '免费',
        pitch: '适合刚进来的用户，只看今日结论和少量观察样本。',
        permissions: ['每日总判断', '3只示例观察股', '基础风险提示', '延迟查看部分证据']
      },
      research: {
        name: '研究会员',
        price: '¥99/月',
        pitch: '适合新手系统学习产业趋势投资，重点看证据缺口和研究队列。',
        permissions: ['完整今日股票池', '六项信维公式状态', 'S/A证据摘要', '未来3个月催化清单', '历史纸面复盘']
      },
      pro: {
        name: '高级会员',
        price: '¥299/月',
        pitch: '适合愿意长期跟踪的用户，获得更完整的风险监控和自选股提醒。',
        permissions: ['全部研究会员权益', '自选股证据变化提醒', '仓位/半凯利区间', '排除清单与止损触发', '专题产业链报告']
      },
      team: {
        name: '团队版',
        price: '定制',
        pitch: '适合小型投研团队，把证据库、任务流和复盘体系标准化。',
        permissions: ['多人账号', '研究任务分配', '私有股票池', '导出报告', 'API/本地部署支持']
      }
    };
    function renderAccountPlan() {
      const plans = (state.account && state.account.plans) || accountPlans;
      const user = state.account && state.account.user;
      const tier = document.getElementById('accountTier').value || (user && user.tier) || 'free';
      const plan = plans[tier] || plans.free || accountPlans.free;
      document.getElementById('accountBadge').textContent = plan.name;
      document.getElementById('planName').textContent = `${plan.name} · ${plan.price}`;
      document.getElementById('planPitch').textContent = plan.pitch;
      document.getElementById('permissionList').innerHTML = plan.permissions.map(item => `<li>${escapeHtml(item)}</li>`).join('');
      document.getElementById('authStatus').textContent = user
        ? `已登录：${user.display_name || user.email} · 当前账号 ${((plans[user.tier] || {}).name || user.tier)}`
        : '未登录 · 当前按普通账号预览';
    }
    function applyTierClass() {
      const user = state.account && state.account.user;
      const actualTier = user && user.tier ? user.tier : 'free';
      document.body.classList.remove('tier-free', 'tier-research', 'tier-pro', 'tier-team');
      document.body.classList.add(`tier-${actualTier}`);
    }
    function renderPlanGrid() {
      const plans = (state.account && state.account.plans) || accountPlans;
      document.getElementById('planGrid').innerHTML = Object.entries(plans).map(([key, plan]) => `
        <article class="plan-card ${key === 'pro' ? 'featured' : ''}">
          <span class="plan-label">${key === 'pro' ? '推荐' : '会员等级'}</span>
          <h3>${escapeHtml(plan.name)}</h3>
          <p>${escapeHtml(plan.pitch)}</p>
          <div class="plan-meta">
            <span>${escapeHtml(plan.price)}</span>
            <span>${escapeHtml(plan.permissions.length)}项权益</span>
          </div>
          <p>${escapeHtml(plan.permissions.slice(0, 3).join(' / '))}</p>
          <button type="button" data-tier="${escapeHtml(key)}">查看该权限</button>
        </article>
      `).join('');
      document.querySelectorAll('.plan-card button[data-tier]').forEach(btn => {
        btn.addEventListener('click', () => {
          document.getElementById('accountTier').value = btn.dataset.tier;
          renderAccountPlan();
          document.getElementById('decisionHome').scrollIntoView({ behavior: 'smooth', block: 'start' });
        });
      });
    }
    async function refreshOpportunityRadar() {
      try {
        state.opportunityRadar = await api('/api/opportunity-radar?limit=12');
        renderDecisionHome();
      } catch (err) {
        state.opportunityRadar = { rows: [] };
      }
    }
    async function authRequest(path, payload) {
      const response = await fetch(path, {
        method: 'POST',
        headers: { 'Accept': 'application/json', 'Content-Type': 'application/json' },
        body: JSON.stringify(payload || {})
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || `${path} ${response.status}`);
      return data;
    }
    async function refreshAccount() {
      state.account = await api('/api/auth/me');
      const user = state.account && state.account.user;
      if (user && user.tier) {
        document.getElementById('accountTier').value = user.tier;
      }
      applyTierClass();
      renderAccountPlan();
      renderPlanGrid();
      await refreshOpportunityRadar();
    }
    async function submitAuth(mode) {
      const email = document.getElementById('authEmail').value.trim();
      const password = document.getElementById('authPassword').value;
      try {
        state.account = await authRequest(mode === 'register' ? '/api/auth/register' : '/api/auth/login', { email, password });
        if (state.account.user && state.account.user.tier) {
          document.getElementById('accountTier').value = state.account.user.tier;
        }
        applyTierClass();
        renderAccountPlan();
        renderPlanGrid();
        await refreshOpportunityRadar();
      } catch (err) {
        document.getElementById('authStatus').textContent = err.message || '账号操作失败';
      }
    }
    async function logoutAccount() {
      try {
        state.account = await authRequest('/api/auth/logout', {});
      } catch (err) {
        state.account = { authenticated: false, user: null, plans: accountPlans };
      }
      document.getElementById('accountTier').value = 'free';
      applyTierClass();
      renderAccountPlan();
      renderPlanGrid();
      await refreshOpportunityRadar();
    }
    function simpleStockMeta(row) {
      const score = row.total_score ?? row.model_total_score ?? row.latest_score;
      const evidence = row.evidence_availability_score ?? row.model_evidence_score ?? row.evidence_score;
      const gate = row.formula_verification_score ?? row.model_formula_verification_score;
      return [
        row.priority_rank ? `#${row.priority_rank}` : null,
        row.action_bucket ? bucketText(row.action_bucket) : row.model_action_bucket ? bucketText(row.model_action_bucket) : null,
        score !== undefined ? `分数 ${fmt(score)}` : null,
        evidence !== undefined ? `证据 ${fmt(evidence)}` : null,
        gate !== undefined ? `闸门 ${fmt(gate)}` : null
      ].filter(Boolean);
    }
    function renderPickCard(label, tone, row, emptyText, explainer) {
      if (!row) {
        return `
          <article class="pick-card ${tone}">
            <span class="pick-label">${escapeHtml(label)}</span>
            <h3>${escapeHtml(emptyText)}</h3>
            <p>${escapeHtml(explainer)}</p>
            <div class="pick-meta"><span>仓位 0%</span><span>等待证据</span></div>
          </article>`;
      }
      const gaps = (row.sa_evidence_gaps || row.blocking_dimension_labels || []).slice(0, 3);
      const meta = simpleStockMeta(row);
      return `
        <article class="pick-card ${tone}">
          <span class="pick-label">${escapeHtml(label)}</span>
          <h3>${escapeHtml(row.code)} ${escapeHtml(row.name)}</h3>
          <p>${escapeHtml(explainer)}</p>
          <div class="pick-meta">
            ${meta.map(x => `<span>${escapeHtml(x)}</span>`).join('')}
          </div>
          <p>${gaps.length ? `还缺：${escapeHtml(gaps.join('、'))}` : '当前缺口以个股详情页为准。'}</p>
          <button type="button" data-code="${escapeHtml(row.code)}">查看证据</button>
        </article>`;
    }
    function renderOpportunityRadar() {
      const box = document.getElementById('opportunityRadarList');
      if (!box) return;
      const radar = state.opportunityRadar || {};
      const rows = radar.rows || [];
      if (!rows.length) {
        box.innerHTML = '<article class="radar-card"><span class="radar-label">暂无机会分</span><h3>等待模型刷新</h3><p>运行模型刷新后，会在这里显示最像0到1产业拐点的候选。</p></article>';
        return;
      }
      box.innerHTML = rows.slice(0, 5).map(row => {
        const gate = row.formula_gate || {};
        const evidence = row.evidence_validation || {};
        const locked = row.locked_fields || [];
        const canSeePosition = row.position_cap_pct !== null && row.position_cap_pct !== undefined;
        return `
          <article class="radar-card ${decisionTone(row.decision_label)}" data-code="${escapeHtml(row.code)}">
            <span class="radar-label">${escapeHtml(row.decision_label || '只观察')}</span>
            <h3>${escapeHtml(row.code)} ${escapeHtml(row.name)}</h3>
            <p>${escapeHtml(row.thesis || '产业命题待补全。')}</p>
            <div class="radar-meta">
              <span>机会分 ${fmt(row.opportunity_score)}</span>
              <span>公式 ${fmt(gate.supported_count || 0, 0)}/${fmt(gate.required_count || 6, 0)}</span>
              <span>证据 ${fmt(row.evidence_score)}</span>
              <span>催化 ${fmt(row.catalyst_score)}</span>
            </div>
            <div class="radar-evidence">
              ${Object.keys(evidence).length ? `
                <span>S ${fmt(evidence.S || 0, 0)}</span>
                <span>A ${fmt(evidence.A || 0, 0)}</span>
                <span>B ${fmt(evidence.B || 0, 0)}</span>
                <span>C ${fmt(evidence.C || 0, 0)}</span>
              ` : '<span>证据链会员解锁</span>'}
              ${canSeePosition ? `<span>EV ${fmt(row.ev_pct)}%</span><span>仓位≤${fmt(row.position_cap_pct)}%</span>` : `<span class="locked-pill">${escapeHtml(locked[0] || 'EV/仓位高级会员解锁')}</span>`}
            </div>
            <p><strong>关键缺口：</strong>${escapeHtml(row.key_gap || '等待证据。')}</p>
            <p><strong>90天看点：</strong>${escapeHtml(row.catalyst_summary || '等待催化。')}</p>
          </article>`;
      }).join('');
      box.querySelectorAll('.radar-card[data-code]').forEach(card => {
        card.addEventListener('click', () => selectStock(card.dataset.code));
      });
    }
    function renderDecisionHome() {
      const report = state.morningReport || {};
      const queues = report.queues || {};
      const gate = report.gate_summary || {};
      const position = report.position_boundary || {};
      const buyRows = (report.top_candidates || []).filter(row => row.eligible_for_buy || row.action_bucket === 'formula_supported');
      const researchRows = [
        ...(queues.blocked_by_evidence || []),
        ...(queues.deep_research || []),
        ...(report.top_candidates || [])
      ].filter((row, index, arr) => row && arr.findIndex(x => x.code === row.code) === index);
      const avoidRows = state.watchlist.filter(row => ['risk_watch', 'archive_watch'].includes(row.model_action_bucket));
      const buyCount = Number(position.buy_pool_count || gate.eligible || buyRows.length || 0);
      const watchCount = researchRows.length;
      const avoidCount = avoidRows.length;
      document.getElementById('decisionDate').textContent = `数据日 ${report.model_trade_date || report.latest_trade_date || '-'}`;
      document.getElementById('plainConclusion').textContent = buyCount > 0
        ? `今天有 ${buyCount} 只进入买入资格池。普通用户先看第一只，会员继续看证据链、仓位和触发条件。`
        : `今天没有股票通过完整买入闸门。小白用户先看重点研究股，付费会员看证据缺口和触发条件，避免把市场热度误当成买入理由。`;
      document.getElementById('buySignal').textContent = buyCount > 0 ? `${buyCount} 只可进买入池` : '今天不买';
      document.getElementById('buySignalHint').textContent = buyCount > 0
        ? `仍按单票不超过15%执行，先看证据链。`
        : `买入池为0，仓位纪律是0%，先等S/A证据闭环。`;
      document.getElementById('watchSignal').textContent = `${watchCount} 只重点跟踪`;
      document.getElementById('watchSignalHint').textContent = '有线索但没闭环的股票，用来研究，不直接下单。';
      document.getElementById('avoidSignal').textContent = `${avoidCount} 只先避开`;
      document.getElementById('avoidSignalHint').textContent = '高风险、低优先级或证据不足标的，先放进风险观察。';
      document.getElementById('beginnerPicksList').innerHTML = [
        renderPickCard('买入资格', 'buy', buyRows[0], '今日暂无可买', '六项公式没有全部闭环时，普通投资者不应该硬买。'),
        renderPickCard('重点研究', 'watch', researchRows[0], '暂无重点研究', '这里是今天最值得花时间看证据的股票，不等于买入。'),
        renderPickCard('风险观察', 'avoid', avoidRows[0], '暂无风险样本', '先避开估值、回撤或证据缺口明显的股票。')
      ].join('');
      renderOpportunityRadar();
      document.querySelectorAll('.pick-card button[data-code]').forEach(btn => {
        btn.addEventListener('click', () => selectStock(btn.dataset.code));
      });
    }
    async function init() {
      try {
        const [summary, morningReport, watchlist, runs, tools, gateMatrix, researchTasks, opportunityRadar, account] = await Promise.all([
          api('/api/summary'),
          api('/api/morning-report'),
          api('/api/watchlist'),
          api('/api/runs'),
          api('/api/tool-registry'),
          api('/api/gate-matrix'),
          api('/api/research-tasks'),
          api('/api/opportunity-radar?limit=12'),
          api('/api/auth/me')
        ]);
        state.summary = summary;
        state.morningReport = morningReport;
        state.watchlist = watchlist.rows || [];
        state.runs = runs.rows || [];
        state.tools = tools.rows || [];
        state.roadmap = tools.roadmap || [];
        state.gateMatrix = gateMatrix.rows || [];
        state.researchTasks = researchTasks.rows || [];
        state.opportunityRadar = opportunityRadar;
        state.account = account;
        state.selectedCode = state.watchlist[0] ? state.watchlist[0].code : null;
        if (state.account && state.account.user && state.account.user.tier) {
          document.getElementById('accountTier').value = state.account.user.tier;
        }
        applyTierClass();
        renderAccountPlan();
        renderPlanGrid();
        renderDecisionHome();
        renderKpis();
        renderMorningReport();
        renderGateMatrix();
        renderResearchTasks();
        renderWatchlist();
        renderSelect();
        renderRuns();
        renderToolRegistry();
        if (state.selectedCode) await selectStock(state.selectedCode);
      } catch (err) {
        document.getElementById('watchRows').innerHTML = `<tr><td colspan="20" class="empty">读取失败：${escapeHtml(err.message)}</td></tr>`;
      }
    }
    document.getElementById('statusFilter').addEventListener('change', renderWatchlist);
    document.getElementById('searchBox').addEventListener('input', renderWatchlist);
    document.getElementById('accountTier').addEventListener('change', renderAccountPlan);
    document.getElementById('loginButton').addEventListener('click', () => submitAuth('login'));
    document.getElementById('registerButton').addEventListener('click', () => submitAuth('register'));
    document.getElementById('logoutButton').addEventListener('click', logoutAccount);
    window.addEventListener('resize', () => state.stock && drawTrend(state.stock));
    init();
  </script>
</body>
</html>
"""


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def parse_json(value: str | None) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def labels_from_dimensions(value: str | list[Any] | None) -> list[str]:
    raw = parse_json(value) if isinstance(value, str) else value
    if not raw:
        return []
    labels: list[str] = []
    for item in raw:
        key = str(item)
        labels.append(DIMENSION_LABELS.get(key, key))
    return labels


def calculate_drawdown(history: list[dict[str, Any]]) -> float | None:
    peak: float | None = None
    worst = 0.0
    for row in history:
        price = row.get("price")
        if price is None:
            continue
        price = float(price)
        peak = price if peak is None else max(peak, price)
        if peak:
            worst = min(worst, (price / peak - 1) * 100)
    return round(worst, 2) if peak is not None else None


def latest_trade_date(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT MAX(trade_date) AS trade_date FROM market_snapshot").fetchone()
    return row["trade_date"] if row and row["trade_date"] else None


def stock_history(conn: sqlite3.Connection, code: str, since: str | None = None) -> list[dict[str, Any]]:
    params: list[Any] = [code]
    clause = ""
    if since:
        clause = "AND trade_date >= ?"
        params.append(since)
    rows = conn.execute(
        f"""
        SELECT trade_date, price, change_pct, amount, turnover_pct, main_net_inflow, pe_ttm, pb
        FROM market_snapshot
        WHERE code = ? {clause}
        ORDER BY trade_date
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def watchlist_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    trade_date = latest_trade_date(conn)
    rows = conn.execute(
        """
        WITH latest_candidate AS (
            SELECT *
            FROM (
                SELECT
                    rc.*,
                    ROW_NUMBER() OVER (PARTITION BY rc.code ORDER BY rc.run_id DESC, rc.rank ASC) AS rn
                FROM recommendation_candidates rc
            )
            WHERE rn = 1
        ),
        latest_metric AS (
            SELECT *
            FROM watchlist_daily_metrics
            WHERE trade_date = (
                SELECT MAX(trade_date)
                FROM watchlist_daily_metrics
            )
        ),
        latest_model AS (
            SELECT *
            FROM stock_model_scores
            WHERE trade_date = (
                SELECT MAX(trade_date)
                FROM stock_model_scores
            )
        ),
        latest_gate AS (
            SELECT *
            FROM xinwei_gate_snapshots
            WHERE snapshot_date = (
                SELECT MAX(snapshot_date)
                FROM xinwei_gate_snapshots
            )
        )
        SELECT
            w.code,
            w.name,
            w.status,
            w.first_recommended_date,
            w.first_run_id,
            w.first_rank,
            w.first_score,
            w.first_price,
            w.thesis,
            w.risk_flags,
            COALESCE(wm.trade_date, ms.trade_date) AS latest_trade_date,
            COALESCE(wm.industry, ms.industry) AS industry,
            COALESCE(wm.price, ms.price) AS latest_price,
            COALESCE(wm.change_pct, ms.change_pct) AS change_pct,
            COALESCE(wm.amount, ms.amount) AS amount,
            COALESCE(wm.turnover_pct, ms.turnover_pct) AS turnover_pct,
            COALESCE(wm.main_net_inflow, ms.main_net_inflow) AS main_net_inflow,
            COALESCE(wm.pe_ttm, ms.pe_ttm) AS pe_ttm,
            COALESCE(wm.pb, ms.pb) AS pb,
            COALESCE(wm.latest_score, lc.score) AS latest_score,
            sm.priority_rank AS model_priority_rank,
            sm.action_bucket AS model_action_bucket,
            sm.total_score AS model_total_score,
            sm.market_score AS model_market_score,
            sm.evidence_score AS model_evidence_score,
            sm.evidence_availability_score AS model_evidence_availability_score,
            sm.formula_verification_score AS model_formula_verification_score,
            sm.behavior_score AS model_behavior_score,
            sm.factor_score AS model_factor_score,
            sm.factor_quality_score AS model_factor_quality_score,
            sm.risk_score AS model_risk_score,
            xg.gate_status AS gate_status,
            xg.eligible_for_buy AS gate_eligible_for_buy,
            xg.supported_count AS gate_supported_count,
            xg.needs_review_count AS gate_needs_review_count,
            xg.pending_count AS gate_pending_count,
            xg.failed_count AS gate_failed_count,
            xg.stale_count AS gate_stale_count,
            xg.required_count AS gate_required_count,
            xg.blocking_dimensions AS gate_blocking_dimensions,
            wm.return_pct AS stored_return_pct,
            wm.max_drawdown_pct AS stored_max_drawdown_pct,
            wm.days_since_first
        FROM stock_watchlist w
        LEFT JOIN market_snapshot ms
            ON ms.code = w.code
            AND ms.trade_date = ?
        LEFT JOIN latest_metric wm
            ON wm.code = w.code
        LEFT JOIN latest_candidate lc
            ON lc.code = w.code
        LEFT JOIN latest_model sm
            ON sm.code = w.code
        LEFT JOIN latest_gate xg
            ON xg.code = w.code
        ORDER BY
            CASE WHEN w.status = 'active' THEN 0 ELSE 1 END,
            CASE WHEN sm.priority_rank IS NULL THEN 1 ELSE 0 END,
            sm.priority_rank ASC,
            w.first_recommended_date DESC,
            w.first_rank ASC
        """,
        (trade_date,),
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        latest_price = item.get("latest_price")
        first_price = item.get("first_price")
        item["return_pct"] = item.get("stored_return_pct")
        if item["return_pct"] is None:
            item["return_pct"] = (
                round((latest_price / first_price - 1) * 100, 2)
                if latest_price is not None and first_price not in (None, 0)
                else None
            )
        item["amount_yi"] = round(item["amount"] / 100_000_000, 2) if item.get("amount") else None
        item["max_drawdown_pct"] = item.get("stored_max_drawdown_pct")
        if item["max_drawdown_pct"] is None:
            history = stock_history(conn, item["code"], item.get("first_recommended_date"))
            item["max_drawdown_pct"] = calculate_drawdown(history)
        result.append(item)
    return result


def summary_payload(conn: sqlite3.Connection) -> dict[str, Any]:
    latest_run = conn.execute(
        "SELECT * FROM recommendation_runs WHERE candidate_count > 0 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if latest_run is None:
        latest_run = conn.execute(
            "SELECT * FROM recommendation_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    latest_date = latest_trade_date(conn)
    latest_metric_date = conn.execute(
        "SELECT MAX(trade_date) AS trade_date FROM watchlist_daily_metrics"
    ).fetchone()["trade_date"]
    watchlist_count = conn.execute("SELECT COUNT(*) AS n FROM stock_watchlist").fetchone()["n"]
    performance_summary = (
        conn.execute(
            """
            SELECT
                COUNT(*) AS metric_row_count,
                AVG(return_pct) AS avg_return_pct,
                MAX(return_pct) AS best_return_pct,
                MIN(max_drawdown_pct) AS worst_drawdown_pct
            FROM watchlist_daily_metrics
            WHERE trade_date = ?
            """,
            (latest_metric_date,),
        ).fetchone()
        if latest_metric_date
        else None
    )
    best = (
        conn.execute(
            """
            SELECT code, name, return_pct
            FROM watchlist_daily_metrics
            WHERE trade_date = ?
              AND return_pct IS NOT NULL
            ORDER BY return_pct DESC
            LIMIT 1
            """,
            (latest_metric_date,),
        ).fetchone()
        if latest_metric_date
        else None
    )
    model_date = conn.execute("SELECT MAX(trade_date) AS trade_date FROM stock_model_scores").fetchone()["trade_date"]
    factor_date = conn.execute("SELECT MAX(trade_date) AS trade_date FROM stock_factor_daily").fetchone()["trade_date"]
    snapshot_rows = (
        conn.execute("SELECT COUNT(*) AS n FROM market_snapshot WHERE trade_date=?", (latest_date,)).fetchone()["n"]
        if latest_date
        else 0
    )
    kline_summary = conn.execute(
        """
        SELECT
            COUNT(*) AS kline_row_count,
            COUNT(DISTINCT code) AS kline_stock_count,
            MIN(trade_date) AS kline_min_date,
            MAX(trade_date) AS kline_max_date
        FROM stock_kline_daily
        """
    ).fetchone()
    provider_health_date = conn.execute(
        "SELECT MAX(check_date) AS check_date FROM provider_health_checks"
    ).fetchone()["check_date"]
    provider_health_summary = (
        conn.execute(
            """
            SELECT
                COUNT(*) AS provider_health_count,
                SUM(CASE WHEN status IN ('pass', 'range_pass') THEN 1 ELSE 0 END) AS provider_ok_count,
                SUM(CASE WHEN status = 'warn' THEN 1 ELSE 0 END) AS provider_warn_count,
                SUM(CASE WHEN status = 'fail' THEN 1 ELSE 0 END) AS provider_fail_count,
                SUM(CASE WHEN status = 'comparison_missing' THEN 1 ELSE 0 END) AS provider_comparison_missing_count,
                SUM(CASE WHEN status = 'primary_missing' THEN 1 ELSE 0 END) AS provider_primary_missing_count
            FROM provider_health_checks
            WHERE check_date = ?
            """,
            (provider_health_date,),
        ).fetchone()
        if provider_health_date
        else None
    )
    tool_registry_count = conn.execute("SELECT COUNT(*) AS n FROM external_tool_registry").fetchone()["n"]
    capability_roadmap_count = conn.execute("SELECT COUNT(*) AS n FROM capability_roadmap").fetchone()["n"]
    factor_summary = (
        conn.execute(
            """
            SELECT
                COUNT(*) AS factor_row_count,
                AVG(quality_score) AS avg_factor_quality,
                AVG(composite_score) AS avg_factor_score,
                AVG(technical_score) AS avg_technical_score
            FROM stock_factor_daily
            WHERE trade_date = ?
            """,
            (factor_date,),
        ).fetchone()
        if factor_date
        else None
    )
    factor_json_row = (
        conn.execute(
            """
            SELECT factor_json
            FROM stock_factor_daily
            WHERE trade_date = ? AND factor_json IS NOT NULL
            LIMIT 1
            """,
            (factor_date,),
        ).fetchone()
        if factor_date
        else None
    )
    factor_meta = parse_json(factor_json_row["factor_json"]) if factor_json_row else None
    model_summary = (
        conn.execute(
            """
            SELECT
                COUNT(*) AS model_score_count,
                SUM(CASE WHEN action_bucket = 'formula_supported' THEN 1 ELSE 0 END) AS formula_supported_count,
                SUM(CASE WHEN action_bucket = 'deep_research' THEN 1 ELSE 0 END) AS deep_research_count,
                SUM(CASE WHEN action_bucket = 'blocked_by_evidence' THEN 1 ELSE 0 END) AS blocked_by_evidence_count,
                SUM(CASE WHEN action_bucket = 'wait_evidence' THEN 1 ELSE 0 END) AS wait_evidence_count,
                SUM(CASE WHEN action_bucket = 'risk_watch' THEN 1 ELSE 0 END) AS risk_watch_count
            FROM stock_model_scores
            WHERE trade_date = ?
            """,
            (model_date,),
        ).fetchone()
        if model_date
        else None
    )
    replay_summary = conn.execute(
        """
        SELECT
            COUNT(*) AS replay_total_count,
            SUM(CASE WHEN status IN ('open', 'complete_20d') THEN 1 ELSE 0 END) AS replay_active_count,
            SUM(CASE WHEN status = 'no_entry_kline' THEN 1 ELSE 0 END) AS replay_missing_count,
            SUM(CASE WHEN latest_return_pct > 0 THEN 1 ELSE 0 END) AS replay_positive_count,
            AVG(latest_return_pct) AS replay_avg_latest_return,
            AVG(return_1d_pct) AS replay_avg_1d_return,
            SUM(take_profit_5_hit) AS replay_take_profit_5_hits,
            SUM(stop_loss_5_hit) AS replay_stop_loss_5_hits,
            MAX(latest_date) AS replay_latest_date
        FROM paper_replay_results
        """
    ).fetchone()
    top_model = (
        conn.execute(
            """
            SELECT code, name, total_score, action_bucket
            FROM stock_model_scores
            WHERE trade_date = ?
            ORDER BY priority_rank ASC
            LIMIT 1
            """,
            (model_date,),
        ).fetchone()
        if model_date
        else None
    )
    ensure_opportunity_schema(conn)
    opportunity_date = conn.execute(
        "SELECT MAX(trade_date) AS trade_date FROM stock_opportunity_scores"
    ).fetchone()["trade_date"]
    opportunity_summary = (
        conn.execute(
            """
            SELECT
                COUNT(*) AS opportunity_count,
                SUM(CASE WHEN decision_label = '可买' THEN 1 ELSE 0 END) AS opportunity_buy_count,
                SUM(CASE WHEN decision_label = '等证据' THEN 1 ELSE 0 END) AS opportunity_wait_count,
                SUM(CASE WHEN decision_label = '只观察' THEN 1 ELSE 0 END) AS opportunity_watch_count,
                SUM(CASE WHEN decision_label = '排除' THEN 1 ELSE 0 END) AS opportunity_exclude_count
            FROM stock_opportunity_scores
            WHERE trade_date = ?
            """,
            (opportunity_date,),
        ).fetchone()
        if opportunity_date
        else None
    )
    top_opportunity = (
        conn.execute(
            """
            SELECT code, name, opportunity_score, decision_label
            FROM stock_opportunity_scores
            WHERE trade_date = ?
            ORDER BY opportunity_rank ASC
            LIMIT 1
            """,
            (opportunity_date,),
        ).fetchone()
        if opportunity_date
        else None
    )
    gate_summary = (
        conn.execute(
            """
            SELECT
                COUNT(*) AS gate_count,
                SUM(CASE WHEN eligible_for_buy = 1 THEN 1 ELSE 0 END) AS eligible_count,
                SUM(CASE WHEN gate_status = 'needs_manual_review' THEN 1 ELSE 0 END) AS needs_review_count,
                SUM(CASE WHEN gate_status = 'missing_evidence' THEN 1 ELSE 0 END) AS missing_count,
                SUM(CASE WHEN gate_status = 'rejected' THEN 1 ELSE 0 END) AS rejected_count,
                SUM(CASE WHEN gate_status = 'formula_supported' THEN 1 ELSE 0 END) AS supported_count
            FROM xinwei_gate_snapshots
            WHERE snapshot_date = (
                SELECT MAX(snapshot_date)
                FROM xinwei_gate_snapshots
            )
            """,
        ).fetchone()
        if model_date
        else []
    )
    qa_run = conn.execute(
        """
        SELECT id, run_date, mode, status, failure_rate, missing_field_rate, api_p95_ms, alert_count
        FROM qa_runs
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    weekly_review = conn.execute(
        """
        SELECT
            id,
            generated_at,
            week_start,
            week_end,
            status,
            candidate_count,
            candidate_event_count,
            replay_coverage_rate,
            avg_latest_return_pct,
            positive_rate,
            alert_count
        FROM weekly_review_runs
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    return {
        "generated_at": conn.execute("SELECT datetime('now', 'localtime') AS ts").fetchone()["ts"],
        "latest_trade_date": latest_date,
        "latest_run_id": latest_run["id"] if latest_run else None,
        "latest_run_date": latest_run["run_date"] if latest_run else None,
        "latest_candidate_count": latest_run["candidate_count"] if latest_run else 0,
        "watchlist_count": watchlist_count,
        "snapshot_rows": snapshot_rows,
        "kline_row_count": kline_summary["kline_row_count"] if kline_summary else 0,
        "kline_stock_count": kline_summary["kline_stock_count"] if kline_summary else 0,
        "kline_min_date": kline_summary["kline_min_date"] if kline_summary else None,
        "kline_max_date": kline_summary["kline_max_date"] if kline_summary else None,
        "provider_health_date": provider_health_date,
        "provider_health_count": provider_health_summary["provider_health_count"] if provider_health_summary else 0,
        "provider_ok_count": provider_health_summary["provider_ok_count"] if provider_health_summary else 0,
        "provider_warn_count": provider_health_summary["provider_warn_count"] if provider_health_summary else 0,
        "provider_fail_count": provider_health_summary["provider_fail_count"] if provider_health_summary else 0,
        "provider_comparison_missing_count": provider_health_summary["provider_comparison_missing_count"] if provider_health_summary else 0,
        "provider_primary_missing_count": provider_health_summary["provider_primary_missing_count"] if provider_health_summary else 0,
        "replay_total_count": replay_summary["replay_total_count"] if replay_summary else 0,
        "replay_active_count": replay_summary["replay_active_count"] if replay_summary else 0,
        "replay_missing_count": replay_summary["replay_missing_count"] if replay_summary else 0,
        "replay_positive_count": replay_summary["replay_positive_count"] if replay_summary else 0,
        "replay_avg_latest_return": round(replay_summary["replay_avg_latest_return"], 2) if replay_summary and replay_summary["replay_avg_latest_return"] is not None else None,
        "replay_avg_1d_return": round(replay_summary["replay_avg_1d_return"], 2) if replay_summary and replay_summary["replay_avg_1d_return"] is not None else None,
        "replay_take_profit_5_hits": replay_summary["replay_take_profit_5_hits"] if replay_summary else 0,
        "replay_stop_loss_5_hits": replay_summary["replay_stop_loss_5_hits"] if replay_summary else 0,
        "replay_latest_date": replay_summary["replay_latest_date"] if replay_summary else None,
        "tool_registry_count": tool_registry_count,
        "capability_roadmap_count": capability_roadmap_count,
        "factor_trade_date": factor_date,
        "factor_version": factor_meta.get("factor_version") if factor_meta else None,
        "factor_row_count": factor_summary["factor_row_count"] if factor_summary else 0,
        "avg_factor_quality": round(factor_summary["avg_factor_quality"], 2) if factor_summary and factor_summary["avg_factor_quality"] is not None else None,
        "avg_factor_score": round(factor_summary["avg_factor_score"], 2) if factor_summary and factor_summary["avg_factor_score"] is not None else None,
        "avg_technical_score": round(factor_summary["avg_technical_score"], 2) if factor_summary and factor_summary["avg_technical_score"] is not None else None,
        "recommendation_run_count": conn.execute("SELECT COUNT(*) AS n FROM recommendation_runs").fetchone()["n"],
        "model_trade_date": model_date,
        "model_score_count": model_summary["model_score_count"] if model_summary else 0,
        "formula_supported_count": model_summary["formula_supported_count"] if model_summary else 0,
        "deep_research_count": model_summary["deep_research_count"] if model_summary else 0,
        "blocked_by_evidence_count": model_summary["blocked_by_evidence_count"] if model_summary else 0,
        "wait_evidence_count": model_summary["wait_evidence_count"] if model_summary else 0,
        "risk_watch_count": model_summary["risk_watch_count"] if model_summary else 0,
        "top_model_stock": f"{top_model['code']} {top_model['name']}" if top_model else None,
        "top_model_score": top_model["total_score"] if top_model else None,
        "top_model_bucket": top_model["action_bucket"] if top_model else None,
        "opportunity_trade_date": opportunity_date,
        "opportunity_count": opportunity_summary["opportunity_count"] if opportunity_summary else 0,
        "opportunity_buy_count": opportunity_summary["opportunity_buy_count"] if opportunity_summary else 0,
        "opportunity_wait_count": opportunity_summary["opportunity_wait_count"] if opportunity_summary else 0,
        "opportunity_watch_count": opportunity_summary["opportunity_watch_count"] if opportunity_summary else 0,
        "opportunity_exclude_count": opportunity_summary["opportunity_exclude_count"] if opportunity_summary else 0,
        "top_opportunity_stock": f"{top_opportunity['code']} {top_opportunity['name']}" if top_opportunity else None,
        "top_opportunity_score": top_opportunity["opportunity_score"] if top_opportunity else None,
        "top_opportunity_label": top_opportunity["decision_label"] if top_opportunity else None,
        "formula_gate_count": gate_summary["gate_count"] if gate_summary else 0,
        "formula_gate_eligible_count": gate_summary["eligible_count"] if gate_summary else 0,
        "formula_gate_needs_review_count": gate_summary["needs_review_count"] if gate_summary else 0,
        "formula_gate_missing_count": gate_summary["missing_count"] if gate_summary else 0,
        "formula_gate_rejected_count": gate_summary["rejected_count"] if gate_summary else 0,
        "formula_gate_supported_count": gate_summary["supported_count"] if gate_summary else 0,
        "qa_run_id": qa_run["id"] if qa_run else None,
        "qa_run_date": qa_run["run_date"] if qa_run else None,
        "qa_mode": qa_run["mode"] if qa_run else None,
        "qa_status": qa_run["status"] if qa_run else None,
        "qa_failure_rate": qa_run["failure_rate"] if qa_run else None,
        "qa_missing_field_rate": qa_run["missing_field_rate"] if qa_run else None,
        "qa_api_p95_ms": qa_run["api_p95_ms"] if qa_run else None,
        "qa_alert_count": qa_run["alert_count"] if qa_run else 0,
        "weekly_review_id": weekly_review["id"] if weekly_review else None,
        "weekly_review_run_date": weekly_review["generated_at"] if weekly_review else None,
        "weekly_review_generated_at": weekly_review["generated_at"] if weekly_review else None,
        "weekly_review_week_start": weekly_review["week_start"] if weekly_review else None,
        "weekly_review_week_end": weekly_review["week_end"] if weekly_review else None,
        "weekly_review_status": weekly_review["status"] if weekly_review else None,
        "weekly_review_candidate_count": weekly_review["candidate_count"] if weekly_review else 0,
        "weekly_review_candidate_event_count": weekly_review["candidate_event_count"] if weekly_review else 0,
        "weekly_review_replay_coverage_rate": weekly_review["replay_coverage_rate"] if weekly_review else None,
        "weekly_review_avg_latest_return_pct": round(weekly_review["avg_latest_return_pct"], 2)
        if weekly_review and weekly_review["avg_latest_return_pct"] is not None
        else None,
        "weekly_review_positive_rate": round(weekly_review["positive_rate"], 4)
        if weekly_review and weekly_review["positive_rate"] is not None
        else None,
        "weekly_review_alert_count": weekly_review["alert_count"] if weekly_review else 0,
        "avg_return_pct": round(performance_summary["avg_return_pct"], 2)
        if performance_summary and performance_summary["avg_return_pct"] is not None
        else None,
        "best_return_pct": performance_summary["best_return_pct"]
        if performance_summary and performance_summary["best_return_pct"] is not None
        else None,
        "best_stock": f"{best['code']} {best['name']}" if best and best["return_pct"] is not None else None,
        "worst_drawdown_pct": performance_summary["worst_drawdown_pct"]
        if performance_summary and performance_summary["worst_drawdown_pct"] is not None
        else None,
    }


def model_candidate_rows(
    conn: sqlite3.Connection,
    trade_date: str | None,
    gate_date: str | None,
    action_bucket: str | None = None,
    limit: int = 12,
) -> list[dict[str, Any]]:
    if not trade_date:
        return []
    params: list[Any] = [gate_date or trade_date, trade_date]
    bucket_clause = ""
    if action_bucket:
        bucket_clause = "AND sm.action_bucket = ?"
        params.append(action_bucket)
    params.append(max(1, min(limit, 50)))
    rows = conn.execute(
        f"""
        SELECT
            sm.priority_rank,
            sm.code,
            sm.name,
            sm.action_bucket,
            sm.total_score,
            sm.market_score,
            sm.evidence_score,
            sm.evidence_availability_score,
            sm.formula_verification_score,
            sm.factor_score,
            sm.factor_quality_score,
            sm.risk_score,
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
          {bucket_clause}
        ORDER BY sm.priority_rank ASC
        LIMIT ?
        """,
        params,
    ).fetchall()
    payload: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        blocking_labels = labels_from_dimensions(item.pop("blocking_dimensions", None))
        item["blocking_dimension_labels"] = blocking_labels
        item["sa_evidence_gaps"] = blocking_labels
        item["gate_status_label"] = GATE_STATUS_LABELS.get(
            item.get("gate_status") or "",
            item.get("gate_status") or "未生成",
        )
        item["eligible_for_buy"] = bool(item.get("eligible_for_buy"))
        payload.append(item)
    return payload


def morning_report_payload(conn: sqlite3.Connection) -> dict[str, Any]:
    summary = summary_payload(conn)
    report_date = today_cn()
    trade_day = is_a_share_trading_day(report_date)
    latest_date = summary.get("latest_trade_date")
    model_date = summary.get("model_trade_date")
    gate_date = conn.execute(
        "SELECT MAX(snapshot_date) AS snapshot_date FROM xinwei_gate_snapshots"
    ).fetchone()["snapshot_date"]
    task_date = conn.execute(
        "SELECT MAX(task_date) AS task_date FROM research_tasks"
    ).fetchone()["task_date"]
    health_date = summary.get("provider_health_date")
    health_counts = (
        {
            row["status"]: row["n"]
            for row in conn.execute(
                """
                SELECT status, COUNT(*) AS n
                FROM provider_health_checks
                WHERE check_date = ?
                GROUP BY status
                """,
                (health_date,),
            ).fetchall()
        }
        if health_date
        else {}
    )
    health_issues = (
        [
            dict(row)
            for row in conn.execute(
                """
                SELECT code, name, status, note, price_diff_pct
                FROM provider_health_checks
                WHERE check_date = ?
                  AND status NOT IN ('pass', 'range_pass')
                ORDER BY
                    CASE status
                        WHEN 'fail' THEN 1
                        WHEN 'primary_missing' THEN 2
                        WHEN 'comparison_missing' THEN 3
                        WHEN 'warn' THEN 4
                        ELSE 9
                    END,
                    code
                LIMIT 10
                """,
                (health_date,),
            ).fetchall()
        ]
        if health_date
        else []
    )
    gate_counts = (
        {
            row["gate_status"]: row["n"]
            for row in conn.execute(
                """
                SELECT gate_status, COUNT(*) AS n
                FROM xinwei_gate_snapshots
                WHERE snapshot_date = ?
                GROUP BY gate_status
                """,
                (gate_date,),
            ).fetchall()
        }
        if gate_date
        else {}
    )
    top_candidates = model_candidate_rows(conn, model_date, gate_date, limit=12)
    queues = {
        "blocked_by_evidence": model_candidate_rows(conn, model_date, gate_date, "blocked_by_evidence", 10),
        "deep_research": model_candidate_rows(conn, model_date, gate_date, "deep_research", 10),
        "wait_evidence": model_candidate_rows(conn, model_date, gate_date, "wait_evidence", 15),
    }
    evidence_gap_tasks = (
        [
            dict(row)
            for row in conn.execute(
                """
                SELECT
                    task_date,
                    code,
                    name,
                    dimension_id,
                    dimension_name,
                    task_type,
                    priority,
                    title,
                    detail,
                    source
                FROM research_tasks
                WHERE task_date = ?
                  AND status = 'open'
                ORDER BY priority ASC, code ASC, dimension_id ASC
                LIMIT 30
                """,
                (task_date,),
            ).fetchall()
        ]
        if task_date
        else []
    )
    eligible_count = int(summary.get("formula_gate_eligible_count") or 0)
    if eligible_count <= 0:
        conclusion = "买入池为空：没有股票完成六项S/A证据闭环，今日实际仓位边界为0%。"
        today_position_boundary = "0%"
    else:
        conclusion = "存在六项闭环候选：仅进入人工交易计划复核，不自动下单。"
        today_position_boundary = "单票≤15%，仍需人工复核"
    if trade_day:
        headline = f"{report_date} 为A股交易日，网页使用本地最新数据日 {model_date or latest_date or '-'}。"
    else:
        headline = f"{report_date} 非A股交易日/休市，网页使用最近交易日 {model_date or latest_date or '-'} 的本地数据。"
    risk_triggers = [
        "核心客户订单被公告或权威渠道证伪，清仓复核。",
        "核心客户砍单或正式采购降级为送样/验证，清仓复核。",
        "连续两个季度收入/订单斜率走平，减仓复核。",
        "应收账款增速持续显著高于收入增速，列入风险观察。",
        "六项闸门任一维度由verified转为failed/stale，仓位回到0%。",
    ]
    return {
        "report_date": report_date,
        "is_trading_day": trade_day,
        "latest_trade_date": latest_date,
        "model_trade_date": model_date,
        "gate_snapshot_date": gate_date,
        "task_date": task_date,
        "headline": headline,
        "conclusion": conclusion,
        "data_source_health": {
            "check_date": health_date,
            "total_count": summary.get("provider_health_count") or 0,
            "ok_count": summary.get("provider_ok_count") or 0,
            "warn_count": summary.get("provider_warn_count") or 0,
            "fail_count": summary.get("provider_fail_count") or 0,
            "comparison_missing_count": summary.get("provider_comparison_missing_count") or 0,
            "primary_missing_count": summary.get("provider_primary_missing_count") or 0,
            "issue_count": len(health_issues),
            "status_counts": health_counts,
            "issues": health_issues,
        },
        "paper_replay": {
            "latest_date": summary.get("replay_latest_date"),
            "total_count": summary.get("replay_total_count") or 0,
            "active_count": summary.get("replay_active_count") or 0,
            "positive_count": summary.get("replay_positive_count") or 0,
            "missing_count": summary.get("replay_missing_count") or 0,
            "avg_latest_return_pct": summary.get("replay_avg_latest_return"),
            "avg_1d_return_pct": summary.get("replay_avg_1d_return"),
            "take_profit_5_hits": summary.get("replay_take_profit_5_hits") or 0,
            "stop_loss_5_hits": summary.get("replay_stop_loss_5_hits") or 0,
        },
        "gate_summary": {
            "date": gate_date,
            "total": summary.get("formula_gate_count") or 0,
            "eligible": eligible_count,
            "formula_supported": summary.get("formula_gate_supported_count") or 0,
            "needs_manual_review": summary.get("formula_gate_needs_review_count") or 0,
            "missing_evidence": summary.get("formula_gate_missing_count") or 0,
            "rejected": summary.get("formula_gate_rejected_count") or 0,
            "status_counts": gate_counts,
        },
        "position_boundary": {
            "buy_pool_count": eligible_count,
            "today_position_boundary": today_position_boundary,
            "single_stock_max_after_verification": "15%",
            "rule": "未形成六项S/A证据闭环前，实际仓位固定为0%。",
        },
        "top_candidates": top_candidates,
        "queues": queues,
        "evidence_gap_tasks": evidence_gap_tasks,
        "risk_triggers": risk_triggers,
    }


def runs_payload(conn: sqlite3.Connection, limit: int = 10) -> list[dict[str, Any]]:
    runs = conn.execute(
        """
        SELECT *
        FROM recommendation_runs
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    payload: list[dict[str, Any]] = []
    for run in runs:
        item = dict(run)
        candidates = conn.execute(
            """
            SELECT
                rc.rank,
                rc.code,
                rc.name,
                rc.score,
                rc.reasons,
                rc.risk_flags,
                rc.metrics_json,
                prr.status AS replay_status,
                prr.entry_date AS replay_entry_date,
                prr.latest_date AS replay_latest_date,
                prr.return_1d_pct AS replay_return_1d_pct,
                prr.latest_return_pct AS replay_latest_return_pct,
                prr.trading_days_observed AS replay_trading_days_observed
            FROM recommendation_candidates rc
            LEFT JOIN paper_replay_results prr
                ON prr.run_id = rc.run_id
                AND prr.rank = rc.rank
            WHERE rc.run_id = ?
            ORDER BY rc.rank
            LIMIT 12
            """,
            (run["id"],),
        ).fetchall()
        run_candidates: list[dict[str, Any]] = []
        for row in candidates:
            candidate = dict(row)
            candidate["metrics"] = parse_json(candidate.pop("metrics_json"))
            candidate["replay"] = {
                "status": candidate.pop("replay_status"),
                "entry_date": candidate.pop("replay_entry_date"),
                "latest_date": candidate.pop("replay_latest_date"),
                "return_1d_pct": candidate.pop("replay_return_1d_pct"),
                "latest_return_pct": candidate.pop("replay_latest_return_pct"),
                "trading_days_observed": candidate.pop("replay_trading_days_observed"),
            }
            run_candidates.append(candidate)
        item["candidates"] = run_candidates
        payload.append(item)
    return payload


def tool_registry_payload(conn: sqlite3.Connection) -> dict[str, Any]:
    tools = conn.execute(
        """
        SELECT
            repo_full_name,
            name,
            url,
            category,
            capability_layer,
            reuse_decision,
            priority,
            license,
            source_level,
            risk_note,
            integration_plan,
            source_url,
            last_reviewed_at
        FROM external_tool_registry
        ORDER BY priority ASC, repo_full_name ASC
        LIMIT 40
        """
    ).fetchall()
    roadmap = conn.execute(
        """
        SELECT
            capability_id,
            capability_name,
            status,
            priority,
            source_repos,
            next_step,
            target_files,
            acceptance_criteria
        FROM capability_roadmap
        ORDER BY priority ASC, capability_id ASC
        LIMIT 20
        """
    ).fetchall()
    return {
        "rows": [dict(row) for row in tools],
        "roadmap": [dict(row) for row in roadmap],
    }


def account_plans_payload() -> dict[str, Any]:
    return {
        "default_tier": "free",
        "plans": ACCOUNT_PLANS,
        "compliance_note": "会员权益提供研究工具、证据库和风险监控，不承诺收益，不把未闭环标的作为买入结论。",
    }


SESSION_COOKIE = "xinwei_session"
PASSWORD_ITERATIONS = 120_000


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        PASSWORD_ITERATIONS,
    ).hex()
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt}${digest}"


def verify_password(password: str, stored_hash: str | None) -> bool:
    if not stored_hash:
        return False
    try:
        algorithm, iterations, salt, digest = stored_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt.encode("utf-8"),
            int(iterations),
        ).hex()
        return hmac.compare_digest(actual, digest)
    except (ValueError, TypeError):
        return False


def init_account_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            display_name TEXT NOT NULL,
            tier TEXT NOT NULL DEFAULT 'free',
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            last_login_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app_sessions (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            expires_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES app_users(id)
        )
        """
    )
    seed_demo_users = [
        ("free@xinwei.local", "free123456", "普通演示账号", "free"),
        ("research@xinwei.local", "research123456", "研究会员演示", "research"),
        ("pro@xinwei.local", "pro123456", "高级会员演示", "pro"),
    ]
    for email, password, display_name, tier in seed_demo_users:
        exists = conn.execute("SELECT 1 FROM app_users WHERE email = ?", (email,)).fetchone()
        if not exists:
            conn.execute(
                """
                INSERT INTO app_users (email, password_hash, display_name, tier)
                VALUES (?, ?, ?, ?)
                """,
                (email, hash_password(password), display_name, tier),
            )


def user_payload(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    tier = row["tier"] if row["tier"] in ACCOUNT_PLANS else "free"
    return {
        "id": row["id"],
        "email": row["email"],
        "display_name": row["display_name"],
        "tier": tier,
        "status": row["status"],
        "created_at": row["created_at"],
        "last_login_at": row["last_login_at"],
        "plan": ACCOUNT_PLANS[tier],
    }


def get_session_token(cookie_header: str | None) -> str | None:
    if not cookie_header:
        return None
    for part in cookie_header.split(";"):
        key, _, value = part.strip().partition("=")
        if key == SESSION_COOKIE and value:
            return value
    return None


def current_user_payload(conn: sqlite3.Connection, token: str | None) -> dict[str, Any]:
    if not token:
        return {"authenticated": False, "user": None, "plans": ACCOUNT_PLANS}
    row = conn.execute(
        """
        SELECT u.*
        FROM app_sessions s
        JOIN app_users u ON u.id = s.user_id
        WHERE s.token = ?
          AND s.expires_at > datetime('now')
          AND u.status = 'active'
        """,
        (token,),
    ).fetchone()
    return {"authenticated": row is not None, "user": user_payload(row), "plans": ACCOUNT_PLANS}


def tier_from_cookie(conn: sqlite3.Connection, cookie_header: str | None) -> str:
    payload = current_user_payload(conn, get_session_token(cookie_header))
    user = payload.get("user") if payload else None
    tier = user.get("tier") if user else "free"
    return tier if tier in ACCOUNT_PLANS else "free"


def create_session(conn: sqlite3.Connection, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    conn.execute(
        """
        INSERT INTO app_sessions (token, user_id, expires_at)
        VALUES (?, ?, datetime('now', '+30 days'))
        """,
        (token, user_id),
    )
    conn.execute("UPDATE app_users SET last_login_at = datetime('now'), updated_at = datetime('now') WHERE id = ?", (user_id,))
    return token


def auth_cookie_header(token: str) -> str:
    return f"{SESSION_COOKIE}={token}; Path=/; Max-Age=2592000; SameSite=Lax; HttpOnly"


def clear_auth_cookie_header() -> str:
    return f"{SESSION_COOKIE}=; Path=/; Max-Age=0; SameSite=Lax; HttpOnly"


def gate_matrix_payload(conn: sqlite3.Connection, limit: int = 40) -> dict[str, Any]:
    snapshot_date = conn.execute(
        "SELECT MAX(snapshot_date) AS snapshot_date FROM xinwei_gate_snapshots"
    ).fetchone()["snapshot_date"]
    if not snapshot_date:
        return {"snapshot_date": None, "rows": []}
    rows = conn.execute(
        """
        SELECT
            xgs.snapshot_date,
            xgs.code,
            xgs.name,
            xgs.gate_status,
            xgs.eligible_for_buy,
            xgs.supported_count,
            xgs.needs_review_count,
            xgs.pending_count,
            xgs.failed_count,
            xgs.stale_count,
            xgs.required_count,
            xgs.blocking_dimensions,
            xgs.dimension_status_json,
            sm.priority_rank,
            sm.action_bucket,
            sm.total_score,
            sm.evidence_availability_score,
            sm.formula_verification_score
        FROM xinwei_gate_snapshots xgs
        LEFT JOIN stock_model_scores sm
          ON sm.code = xgs.code
         AND sm.trade_date = xgs.snapshot_date
        WHERE xgs.snapshot_date = ?
        ORDER BY COALESCE(sm.priority_rank, 9999), xgs.code
        LIMIT ?
        """,
        (snapshot_date, max(1, min(limit, 120))),
    ).fetchall()
    payload_rows: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["blocking_dimensions"] = parse_json(item.get("blocking_dimensions")) or []
        item["dimensions"] = parse_json(item.pop("dimension_status_json")) or []
        payload_rows.append(item)
    return {"snapshot_date": snapshot_date, "rows": payload_rows}


def research_tasks_payload(conn: sqlite3.Connection, limit: int = 80) -> dict[str, Any]:
    task_date = conn.execute("SELECT MAX(task_date) AS task_date FROM research_tasks").fetchone()["task_date"]
    if not task_date:
        return {"task_date": None, "rows": []}
    rows = conn.execute(
        """
        SELECT
            task_date,
            code,
            name,
            dimension_id,
            dimension_name,
            task_type,
            priority,
            status,
            title,
            detail,
            source,
            updated_at
        FROM research_tasks
        WHERE task_date = ?
          AND status = 'open'
        ORDER BY priority ASC, code ASC, dimension_id ASC
        LIMIT ?
        """,
        (task_date, max(1, min(limit, 200))),
    ).fetchall()
    return {"task_date": task_date, "rows": [dict(row) for row in rows]}


def stock_payload(conn: sqlite3.Connection, code: str, tier: str = "free") -> dict[str, Any]:
    rows = watchlist_rows(conn)
    watch = next((row for row in rows if row["code"] == code), None)
    if not watch:
        raise KeyError(code)
    history = stock_history(conn, code, watch.get("first_recommended_date"))
    candidates = conn.execute(
        """
        SELECT
            rr.run_date,
            rc.run_id,
            rc.rank,
            rc.score,
            rc.reasons,
            rc.risk_flags,
            rc.metrics_json
        FROM recommendation_candidates rc
        JOIN recommendation_runs rr ON rr.id = rc.run_id
        WHERE rc.code = ?
        ORDER BY rr.run_date DESC, rc.run_id DESC
        LIMIT 20
        """,
        (code,),
    ).fetchall()
    notes = conn.execute(
        """
        SELECT id, note_date, category, evidence_grade, title, body, source_url
        FROM stock_research_notes
        WHERE code = ?
        ORDER BY note_date DESC, id DESC
        LIMIT 30
        """,
        (code,),
    ).fetchall()
    xinwei_reviews = conn.execute(
        """
        SELECT
            dimension_id,
            dimension_name,
            status,
            evidence_grade,
            score,
            summary,
            source_url,
            last_checked_at
        FROM stock_xinwei_reviews
        WHERE code = ?
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
        (code,),
    ).fetchall()
    evidence_items = conn.execute(
        """
        SELECT
            evidence_date,
            source,
            evidence_type,
            evidence_grade,
            title,
            summary,
            source_url
        FROM stock_evidence_items
        WHERE code = ?
        ORDER BY evidence_date DESC, id DESC
        LIMIT 20
        """,
        (code,),
    ).fetchall()
    coverage = conn.execute(
        """
        SELECT
            as_of_date,
            report_count_total,
            report_count_180d,
            org_count,
            latest_report_date,
            latest_rating
        FROM stock_research_coverage
        WHERE code = ?
        ORDER BY as_of_date DESC
        LIMIT 1
        """,
        (code,),
    ).fetchone()
    model_score = conn.execute(
        """
        SELECT
            trade_date,
            model_version,
            priority_rank,
            action_bucket,
            total_score,
            market_score,
            evidence_score,
            evidence_availability_score,
            formula_verification_score,
            behavior_score,
            factor_score,
            factor_quality_score,
            risk_score,
            latest_candidate_score,
            latest_return_pct,
            latest_drawdown_pct,
            evidence_item_count,
            s_evidence_count,
            a_evidence_count,
            review_supported_count,
            review_needs_review_count,
            review_pending_count,
            review_failed_count,
            research_org_count,
            research_report_count_180d,
            score_json
        FROM stock_model_scores
        WHERE code = ?
        ORDER BY trade_date DESC
        LIMIT 1
        """,
        (code,),
    ).fetchone()
    model_payload = dict(model_score) if model_score else None
    if model_payload:
        model_payload["score_detail"] = parse_json(model_payload.pop("score_json"))
    gate_snapshot = conn.execute(
        """
        SELECT *
        FROM xinwei_gate_snapshots
        WHERE code = ?
        ORDER BY snapshot_date DESC
        LIMIT 1
        """,
        (code,),
    ).fetchone()
    gate_payload = dict(gate_snapshot) if gate_snapshot else None
    if gate_payload:
        gate_payload["blocking_dimensions"] = parse_json(gate_payload.get("blocking_dimensions")) or []
        gate_payload["dimensions"] = parse_json(gate_payload.pop("dimension_status_json")) or []
        gate_payload["evidence_chain"] = parse_json(gate_payload.pop("evidence_chain_json")) or {}
    evidence_links = conn.execute(
        """
        SELECT
            dimension_id,
            dimension_name,
            evidence_item_id,
            evidence_grade,
            evidence_status,
            match_reason,
            match_keywords,
            source,
            evidence_type,
            title,
            evidence_date,
            source_url,
            is_manual_confirmed
        FROM xinwei_evidence_links
        WHERE code = ?
        ORDER BY
            CASE dimension_id
                WHEN 'industry_inflection' THEN 1
                WHEN 'scarcity_position' THEN 2
                WHEN 'leader_customer_binding' THEN 3
                WHEN 'capacity_order_expansion' THEN 4
                WHEN 'earnings_inflection' THEN 5
                WHEN 'expectation_gap' THEN 6
                ELSE 99
            END,
            CASE evidence_grade WHEN 'S' THEN 0 WHEN 'A' THEN 1 ELSE 9 END,
            evidence_date DESC
        LIMIT 80
        """,
        (code,),
    ).fetchall()
    return {
        "watch": watch,
        "history": history,
        "candidates": [dict(row) | {"metrics": parse_json(row["metrics_json"])} for row in candidates],
        "notes": [dict(row) for row in notes],
        "xinwei_reviews": [dict(row) for row in xinwei_reviews],
        "evidence_items": [dict(row) for row in evidence_items],
        "evidence_links": [
            dict(row) | {"match_keywords": parse_json(row["match_keywords"]) or []}
            for row in evidence_links
        ],
        "research_coverage": dict(coverage) if coverage else None,
        "model_score": model_payload,
        "xinwei_gate_snapshot": gate_payload,
        "opportunity": opportunity_for_code(conn, code, tier),
    }


def ensure_database(db_path: Path) -> None:
    with connect(db_path) as conn:
        init_db(conn)
        init_account_schema(conn)
        init_qa_schema(conn)
        ensure_opportunity_schema(conn)
        seed_tool_registry(conn)
        seed_capability_roadmap(conn)
        sync_watchlist_from_recommendations(conn)
        refresh_watchlist_metrics(conn)
        refresh_model_scores(conn)
        refresh_provider_health(conn, scope="model", limit=50)
        refresh_replay_results(conn)
        conn.commit()


class DashboardHandler(BaseHTTPRequestHandler):
    db_path = DEFAULT_DB

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def send_bytes(
        self,
        content: bytes,
        content_type: str,
        status: HTTPStatus = HTTPStatus.OK,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(content)

    def send_json(
        self,
        payload: Any,
        status: HTTPStatus = HTTPStatus.OK,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_bytes(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            "application/json; charset=utf-8",
            status,
            extra_headers,
        )

    def read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        try:
            if path in {"/", "/index.html"}:
                self.send_bytes(INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
                return
            with connect(self.db_path) as conn:
                init_db(conn)
                init_account_schema(conn)
                if path == "/api/summary":
                    self.send_json(summary_payload(conn))
                elif path == "/api/morning-report":
                    self.send_json(morning_report_payload(conn))
                elif path == "/api/watchlist":
                    self.send_json({"rows": watchlist_rows(conn)})
                elif path == "/api/runs":
                    query = parse_qs(parsed.query)
                    limit = int(query.get("limit", ["10"])[0])
                    self.send_json({"rows": runs_payload(conn, max(1, min(limit, 50)))})
                elif path == "/api/tool-registry":
                    self.send_json(tool_registry_payload(conn))
                elif path == "/api/account-plans":
                    self.send_json(account_plans_payload())
                elif path == "/api/auth/me":
                    token = get_session_token(self.headers.get("Cookie"))
                    self.send_json(current_user_payload(conn, token))
                elif path == "/api/gate-matrix":
                    query = parse_qs(parsed.query)
                    limit = int(query.get("limit", ["40"])[0])
                    self.send_json(gate_matrix_payload(conn, limit))
                elif path == "/api/research-tasks":
                    query = parse_qs(parsed.query)
                    limit = int(query.get("limit", ["80"])[0])
                    self.send_json(research_tasks_payload(conn, limit))
                elif path == "/api/opportunity-radar":
                    query = parse_qs(parsed.query)
                    limit = int(query.get("limit", ["20"])[0])
                    tier = tier_from_cookie(conn, self.headers.get("Cookie"))
                    self.send_json(latest_opportunity_radar(conn, max(1, min(limit, 80)), tier))
                elif path == "/api/qa-report":
                    self.send_json(latest_qa_payload(conn))
                elif path == "/api/weekly-review":
                    self.send_json(latest_weekly_review_payload(conn))
                elif path == "/api/stock":
                    query = parse_qs(parsed.query)
                    code = (query.get("code") or [""])[0].strip()
                    if not code:
                        self.send_json({"error": "code is required"}, HTTPStatus.BAD_REQUEST)
                    else:
                        tier = tier_from_cookie(conn, self.headers.get("Cookie"))
                        self.send_json(stock_payload(conn, code, tier))
                else:
                    self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except KeyError:
            self.send_json({"error": "stock not found"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        try:
            body = self.read_json_body()
            with connect(self.db_path) as conn:
                init_db(conn)
                init_account_schema(conn)
                if path == "/api/auth/register":
                    email = str(body.get("email") or "").strip().lower()
                    password = str(body.get("password") or "")
                    display_name = str(body.get("display_name") or email.split("@")[0] or "新用户").strip()
                    if "@" not in email or len(email) > 160:
                        self.send_json({"error": "请输入有效邮箱"}, HTTPStatus.BAD_REQUEST)
                        return
                    if len(password) < 8:
                        self.send_json({"error": "密码至少8位"}, HTTPStatus.BAD_REQUEST)
                        return
                    try:
                        cur = conn.execute(
                            """
                            INSERT INTO app_users (email, password_hash, display_name, tier)
                            VALUES (?, ?, ?, 'free')
                            """,
                            (email, hash_password(password), display_name[:40] or "新用户"),
                        )
                    except sqlite3.IntegrityError:
                        self.send_json({"error": "该邮箱已注册"}, HTTPStatus.CONFLICT)
                        return
                    token = create_session(conn, int(cur.lastrowid))
                    conn.commit()
                    user = conn.execute("SELECT * FROM app_users WHERE id = ?", (int(cur.lastrowid),)).fetchone()
                    self.send_json(
                        {"authenticated": True, "user": user_payload(user), "plans": ACCOUNT_PLANS},
                        extra_headers={"Set-Cookie": auth_cookie_header(token)},
                    )
                elif path == "/api/auth/login":
                    email = str(body.get("email") or "").strip().lower()
                    password = str(body.get("password") or "")
                    user = conn.execute("SELECT * FROM app_users WHERE email = ?", (email,)).fetchone()
                    if not user or not verify_password(password, user["password_hash"]):
                        self.send_json({"error": "邮箱或密码错误"}, HTTPStatus.UNAUTHORIZED)
                        return
                    if user["status"] != "active":
                        self.send_json({"error": "账号不可用"}, HTTPStatus.FORBIDDEN)
                        return
                    token = create_session(conn, int(user["id"]))
                    conn.commit()
                    fresh_user = conn.execute("SELECT * FROM app_users WHERE id = ?", (int(user["id"]),)).fetchone()
                    self.send_json(
                        {"authenticated": True, "user": user_payload(fresh_user), "plans": ACCOUNT_PLANS},
                        extra_headers={"Set-Cookie": auth_cookie_header(token)},
                    )
                elif path == "/api/auth/logout":
                    token = get_session_token(self.headers.get("Cookie"))
                    if token:
                        conn.execute("DELETE FROM app_sessions WHERE token = ?", (token,))
                        conn.commit()
                    self.send_json(
                        {"authenticated": False, "user": None, "plans": ACCOUNT_PLANS},
                        extra_headers={"Set-Cookie": clear_auth_cookie_header()},
                    )
                else:
                    self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except json.JSONDecodeError:
            self.send_json({"error": "invalid json"}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the local A-share watchlist dashboard")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite database path")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host")
    parser.add_argument("--port", type=int, default=8765, help="Bind port")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    ensure_database(args.db)
    DashboardHandler.db_path = args.db
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    url = f"http://{args.host}:{args.port}"
    print(f"A-share watchlist dashboard running at {url}")
    print(f"Database: {args.db}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Shutting down.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

