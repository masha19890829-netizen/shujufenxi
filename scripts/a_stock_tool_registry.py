#!/usr/bin/env python
"""Seed the local GitHub tooling registry for the A-share research platform."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from a_stock_daily import DEFAULT_DB, connect, init_db, now_cn


TOOL_ROWS: list[dict[str, Any]] = [
    {
        "repo_full_name": "simonlin1212/a-stock-data",
        "name": "a-stock-data",
        "url": "https://github.com/simonlin1212/a-stock-data",
        "category": "data",
        "capability_layer": "a_share_source_map",
        "reuse_decision": "core",
        "priority": 0,
        "license": "Repo license",
        "source_level": "local_skill_and_github_repo",
        "risk_note": "It is a data access skill, not a stock-picking model.",
        "integration_plan": "Keep as the primary A-share source discipline and endpoint map.",
        "source_url": "https://github.com/simonlin1212/a-stock-data",
    },
    {
        "repo_full_name": "mootdx/mootdx",
        "name": "mootdx",
        "url": "https://github.com/mootdx/mootdx",
        "category": "data",
        "capability_layer": "tdx_quote_kline_finance",
        "reuse_decision": "core",
        "priority": 1,
        "license": "MIT; README states learning-only/non-commercial use",
        "source_level": "official_repo_readme",
        "risk_note": "TDX server stability and non-commercial notice must be respected.",
        "integration_plan": "Use as preferred provider for K-line, TDX quotes, offline reader and finance snapshots.",
        "source_url": "https://github.com/mootdx/mootdx/blob/master/README.md",
    },
    {
        "repo_full_name": "akfamily/akshare",
        "name": "AKShare",
        "url": "https://github.com/akfamily/akshare",
        "category": "data",
        "capability_layer": "broad_financial_data_adapter",
        "reuse_decision": "optional_adapter",
        "priority": 2,
        "license": "MIT",
        "source_level": "official_repo_readme",
        "risk_note": "Convenient wrapper, but endpoint drift can create hidden failures.",
        "integration_plan": "Use only as fallback/cross-check for unique data gaps, not as the core source.",
        "source_url": "https://github.com/akfamily/akshare/blob/main/README.md",
    },
    {
        "repo_full_name": "waditu/tushare",
        "name": "Tushare",
        "url": "https://github.com/waditu/tushare",
        "category": "data",
        "capability_layer": "historical_fundamental_calendar",
        "reuse_decision": "optional_adapter",
        "priority": 3,
        "license": "Project license; Pro token may be required",
        "source_level": "official_repo_readme",
        "risk_note": "Tushare Pro access and token limits are external dependencies.",
        "integration_plan": "Add token-gated provider for trade calendar, point-in-time fundamentals and long history.",
        "source_url": "https://github.com/waditu/tushare/blob/master/README.md",
    },
    {
        "repo_full_name": "zvtvz/zvt",
        "name": "ZVT",
        "url": "https://github.com/zvtvz/zvt",
        "category": "data_architecture",
        "capability_layer": "provider_schema_factor_tag",
        "reuse_decision": "architecture_only",
        "priority": 4,
        "license": "MIT",
        "source_level": "official_repo_readme",
        "risk_note": "Full framework is larger than the current SQLite-first scope.",
        "integration_plan": "Borrow the provider registry, schema layering and dynamic tag ideas.",
        "source_url": "https://github.com/zvtvz/zvt/blob/master/README.md",
    },
    {
        "repo_full_name": "shidenggui/easyquotation",
        "name": "easyquotation",
        "url": "https://github.com/shidenggui/easyquotation",
        "category": "data",
        "capability_layer": "realtime_quote_fallback",
        "reuse_decision": "optional_adapter",
        "priority": 5,
        "license": "MIT",
        "source_level": "official_repo_readme",
        "risk_note": "Free quote endpoints can change fields or throttle without notice.",
        "integration_plan": "Use as lightweight Sina/Tencent quote fallback and quote-crosscheck candidate.",
        "source_url": "https://github.com/shidenggui/easyquotation/blob/master/README.md",
    },
    {
        "repo_full_name": "mpquant/Ashare",
        "name": "Ashare",
        "url": "https://github.com/mpquant/Ashare",
        "category": "data",
        "capability_layer": "minimal_realtime_kline_api",
        "reuse_decision": "optional_adapter",
        "priority": 6,
        "license": "Open source repo",
        "source_level": "official_repo_readme",
        "risk_note": "Small one-file implementation is easy to audit, but not a complete data warehouse.",
        "integration_plan": "Use as a minimal fallback reference for Sina/Tencent dual-provider switching.",
        "source_url": "https://github.com/mpquant/Ashare/blob/main/README.md",
    },
    {
        "repo_full_name": "mpquant/MyTT",
        "name": "MyTT",
        "url": "https://github.com/mpquant/MyTT",
        "category": "factor",
        "capability_layer": "technical_indicator_library",
        "reuse_decision": "optional_adapter",
        "priority": 7,
        "license": "GPL badge in README",
        "source_level": "official_repo_readme",
        "risk_note": "GPL licensing means direct code reuse needs care; formulas can be reimplemented.",
        "integration_plan": "Use formulas as a reference for local indicators after license review.",
        "source_url": "https://github.com/mpquant/MyTT/blob/main/README.md",
    },
    {
        "repo_full_name": "microsoft/qlib",
        "name": "Qlib",
        "url": "https://github.com/microsoft/qlib",
        "category": "factor_ml",
        "capability_layer": "ml_factor_pipeline_backtest",
        "reuse_decision": "architecture_only",
        "priority": 8,
        "license": "MIT",
        "source_level": "official_repo_readme",
        "risk_note": "CN official dataset availability is constrained; full framework is heavy.",
        "integration_plan": "Export local SQLite panels into Qlib-like factor datasets for offline experiments.",
        "source_url": "https://github.com/microsoft/qlib/blob/main/README.md",
    },
    {
        "repo_full_name": "ricequant/rqalpha",
        "name": "RQAlpha",
        "url": "https://github.com/ricequant/rqalpha",
        "category": "backtest",
        "capability_layer": "backtest_simulation_risk_mods",
        "reuse_decision": "architecture_only",
        "priority": 9,
        "license": "Non-commercial use per README",
        "source_level": "official_repo_readme",
        "risk_note": "Non-commercial limit and external RQData coupling need caution.",
        "integration_plan": "Borrow A-share simulation rules, analyser/risk/scheduler module boundaries.",
        "source_url": "https://github.com/ricequant/rqalpha/blob/master/README.rst",
    },
    {
        "repo_full_name": "vnpy/vnpy",
        "name": "VeighNa",
        "url": "https://github.com/vnpy/vnpy",
        "category": "execution_backtest",
        "capability_layer": "event_engine_paper_risk_gateway",
        "reuse_decision": "architecture_only",
        "priority": 10,
        "license": "MIT",
        "source_level": "official_repo_readme",
        "risk_note": "Live broker gateways are out of scope until manual risk approvals exist.",
        "integration_plan": "Borrow event engine, paper account, risk manager and strategy lab patterns.",
        "source_url": "https://github.com/vnpy/vnpy/blob/master/README.md",
    },
    {
        "repo_full_name": "yutiansut/QUANTAXIS",
        "name": "QUANTAXIS",
        "url": "https://github.com/yutiansut/QUANTAXIS",
        "category": "backtest",
        "capability_layer": "account_protocol_factor_engine",
        "reuse_decision": "architecture_only",
        "priority": 11,
        "license": "MIT",
        "source_level": "official_repo_readme",
        "risk_note": "Large stack with Mongo/ClickHouse style assumptions; avoid adding dependency now.",
        "integration_plan": "Borrow account model, backtest audit and factor layer ideas.",
        "source_url": "https://github.com/yutiansut/QUANTAXIS/blob/master/README.md",
    },
    {
        "repo_full_name": "AI4Finance-Foundation/FinRL-Trading",
        "name": "FinRL-X",
        "url": "https://github.com/AI4Finance-Foundation/FinRL-Trading",
        "category": "ml_trading_architecture",
        "capability_layer": "weight_contract_backtest_risk",
        "reuse_decision": "architecture_only",
        "priority": 12,
        "license": "Apache-2.0",
        "source_level": "official_repo_readme",
        "risk_note": "Designed mainly around global/US execution examples; adapt cautiously for A-share rules.",
        "integration_plan": "Borrow weight-centric interface, reproducibility discipline and risk overlay structure.",
        "source_url": "https://github.com/AI4Finance-Foundation/FinRL-Trading/blob/master/README.md",
    },
    {
        "repo_full_name": "AI4Finance-Foundation/FinRL",
        "name": "FinRL",
        "url": "https://github.com/AI4Finance-Foundation/FinRL",
        "category": "ml_trading_architecture",
        "capability_layer": "drl_research_pipeline",
        "reuse_decision": "architecture_only",
        "priority": 13,
        "license": "MIT",
        "source_level": "official_repo_readme",
        "risk_note": "Original repo recommends newer FinRL-X for production-oriented work.",
        "integration_plan": "Use as learning reference for train-test-trade pipeline and environment design.",
        "source_url": "https://github.com/AI4Finance-Foundation/FinRL/blob/master/README.md",
    },
    {
        "repo_full_name": "TauricResearch/TradingAgents",
        "name": "TradingAgents",
        "url": "https://github.com/TauricResearch/TradingAgents",
        "category": "agent",
        "capability_layer": "multi_agent_research_debate_memory",
        "reuse_decision": "architecture_only",
        "priority": 14,
        "license": "Apache-2.0",
        "source_level": "official_repo_readme",
        "risk_note": "LLM outputs are non-deterministic; use for research debate, not automatic orders.",
        "integration_plan": "Borrow analyst/researcher/risk/portfolio roles and persistent decision log.",
        "source_url": "https://github.com/TauricResearch/TradingAgents/blob/main/README.md",
    },
    {
        "repo_full_name": "hsliuping/TradingAgents-CN",
        "name": "TradingAgents-CN",
        "url": "https://github.com/hsliuping/TradingAgents-CN",
        "category": "agent",
        "capability_layer": "china_localized_multi_agent_platform",
        "reuse_decision": "manual_review",
        "priority": 15,
        "license": "Hybrid; proprietary app/frontend parts per README",
        "source_level": "official_repo_readme",
        "risk_note": "Hybrid license and proprietary folders mean direct reuse is restricted.",
        "integration_plan": "Review docs for A-share provider fallback and report-export ideas only.",
        "source_url": "https://github.com/hsliuping/TradingAgents-CN/blob/main/README.md",
    },
    {
        "repo_full_name": "WindRiders/a-stock-agent",
        "name": "a-stock-agent",
        "url": "https://github.com/WindRiders/a-stock-agent",
        "category": "agent",
        "capability_layer": "cli_scan_backtest_risk_report",
        "reuse_decision": "manual_review",
        "priority": 16,
        "license": "MIT",
        "source_level": "official_repo_readme",
        "risk_note": "Scoring is technical/fundamental weighted, not the Xinwei formula.",
        "integration_plan": "Borrow A-share T+1, limit-up/down, transaction-cost and risk-metric checklists.",
        "source_url": "https://github.com/WindRiders/a-stock-agent/blob/main/README.md",
    },
    {
        "repo_full_name": "shidenggui/easytrader",
        "name": "easytrader",
        "url": "https://github.com/shidenggui/easytrader",
        "category": "execution",
        "capability_layer": "broker_client_automation",
        "reuse_decision": "sandbox_only",
        "priority": 17,
        "license": "Repo license",
        "source_level": "official_repo_readme",
        "risk_note": "Real execution automation is blocked until explicit user approval and risk controls.",
        "integration_plan": "Do not connect live trading; keep as future paper/sandbox execution reference.",
        "source_url": "https://github.com/shidenggui/easytrader/blob/master/README.md",
    },
    {
        "repo_full_name": "shidenggui/easyquant",
        "name": "easyquant",
        "url": "https://github.com/shidenggui/easyquant",
        "category": "event_engine",
        "capability_layer": "event_engine_strategy_template",
        "reuse_decision": "architecture_only",
        "priority": 18,
        "license": "MIT",
        "source_level": "official_repo_readme",
        "risk_note": "Python 3.5-era stack; treat as design reference only.",
        "integration_plan": "Borrow multi-source quote engine and clock-event concepts.",
        "source_url": "https://github.com/shidenggui/easyquant/blob/master/README.md",
    },
    {
        "repo_full_name": "waditu/czsc",
        "name": "CZSC",
        "url": "https://github.com/waditu/czsc",
        "category": "factor_signal",
        "capability_layer": "chanlun_signal_event_position",
        "reuse_decision": "architecture_only",
        "priority": 19,
        "license": "Repo license; Python 3.10+ and Rust/PyO3 architecture",
        "source_level": "official_repo_readme",
        "risk_note": "Chan theory signals are timing aids, not evidence for industry inflection.",
        "integration_plan": "Borrow signal-event-position layering and HTML visualization ideas for optional timing review.",
        "source_url": "https://github.com/waditu/czsc/blob/master/README.md",
    },
    {
        "repo_full_name": "jealous/stockstats",
        "name": "stockstats",
        "url": "https://github.com/jealous/stockstats",
        "category": "factor",
        "capability_layer": "pandas_indicator_wrapper",
        "reuse_decision": "optional_adapter",
        "priority": 20,
        "license": "BSD-3-Clause",
        "source_level": "official_repo_readme",
        "risk_note": "Indicator formulas must be treated as descriptive factors, not standalone buy signals.",
        "integration_plan": "Use as a reference for RSI, MACD, KDJ, Bollinger, ATR and cross-up/down formula coverage.",
        "source_url": "https://github.com/jealous/stockstats/blob/master/README.md",
    },
    {
        "repo_full_name": "bukosabino/ta",
        "name": "ta",
        "url": "https://github.com/bukosabino/ta",
        "category": "factor",
        "capability_layer": "technical_analysis_feature_engineering",
        "reuse_decision": "optional_adapter",
        "priority": 21,
        "license": "MIT",
        "source_level": "official_repo_readme",
        "risk_note": "Generic OHLCV features need A-share limit-up/down and suspension handling before backtests.",
        "integration_plan": "Use as a compact reference for volume, volatility, trend and momentum feature groups.",
        "source_url": "https://github.com/bukosabino/ta/blob/master/README.md",
    },
    {
        "repo_full_name": "mementum/backtrader",
        "name": "backtrader",
        "url": "https://github.com/mementum/backtrader",
        "category": "backtest",
        "capability_layer": "event_backtest_engine",
        "reuse_decision": "architecture_only",
        "priority": 22,
        "license": "Repo license",
        "source_level": "official_repo_readme",
        "risk_note": "Live trading integrations are out of scope; use only for offline replay design.",
        "integration_plan": "Borrow strategy/feed/analyser concepts for the future paper backtest module.",
        "source_url": "https://github.com/mementum/backtrader/blob/master/README.rst",
    },
    {
        "repo_full_name": "kernc/backtesting.py",
        "name": "backtesting.py",
        "url": "https://github.com/kernc/backtesting.py",
        "category": "backtest",
        "capability_layer": "simple_strategy_backtest_stats",
        "reuse_decision": "architecture_only",
        "priority": 23,
        "license": "Repo license",
        "source_level": "official_repo_readme",
        "risk_note": "Single-strategy examples must be adapted to A-share T+1 and limit rules.",
        "integration_plan": "Borrow concise strategy API and result-stat presentation for paper replay reports.",
        "source_url": "https://github.com/kernc/backtesting.py/blob/master/README.md",
    },
    {
        "repo_full_name": "polakowo/vectorbt",
        "name": "vectorbt",
        "url": "https://github.com/polakowo/vectorbt",
        "category": "backtest_factor",
        "capability_layer": "vectorized_parameter_sweep",
        "reuse_decision": "architecture_only",
        "priority": 24,
        "license": "Fair Code badge in README",
        "source_level": "official_repo_readme",
        "risk_note": "Fair-code licensing and large dependency surface require review before direct reuse.",
        "integration_plan": "Borrow vectorized experiment, walk-forward and drawdown analytics patterns.",
        "source_url": "https://github.com/polakowo/vectorbt/blob/master/README.md",
    },
    {
        "repo_full_name": "zsrl/qka",
        "name": "QKA",
        "url": "https://github.com/zsrl/qka",
        "category": "backtest",
        "capability_layer": "a_share_backtest_framework",
        "reuse_decision": "architecture_only",
        "priority": 25,
        "license": "MIT",
        "source_level": "official_repo_readme",
        "risk_note": "Backtest framework outputs must be treated as research validation, not buy signals.",
        "integration_plan": "Borrow A-share data/strategy/backtest/report boundaries for the local paper replay module.",
        "source_url": "https://github.com/zsrl/qka/blob/main/README.md",
    },
    {
        "repo_full_name": "1nchaos/adata",
        "name": "adata",
        "url": "https://github.com/1nchaos/adata",
        "category": "data",
        "capability_layer": "a_share_multi_source_database",
        "reuse_decision": "manual_review",
        "priority": 26,
        "license": "Repo license",
        "source_level": "github_topic_and_repo_readme",
        "risk_note": "Large data wrapper; endpoint provenance and license need review before dependency use.",
        "integration_plan": "Review its multi-source fallback and local storage ideas for provider health checks.",
        "source_url": "https://github.com/1nchaos/adata",
    },
    {
        "repo_full_name": "myhhub/stock",
        "name": "stock",
        "url": "https://github.com/myhhub/stock",
        "category": "analysis_platform",
        "capability_layer": "chips_patterns_backtest_mobile_ui",
        "reuse_decision": "manual_review",
        "priority": 27,
        "license": "Repo license",
        "source_level": "github_topic_and_repo_readme",
        "risk_note": "Combines data, indicators, backtest and auto-trading; keep separated from evidence scoring.",
        "integration_plan": "Review chip distribution, pattern recognition and UI ideas without importing trading automation.",
        "source_url": "https://github.com/myhhub/stock",
    },
    {
        "repo_full_name": "HuggingAGI/mcp-baostock-server",
        "name": "MCP BaoStock Server",
        "url": "https://github.com/HuggingAGI/mcp-baostock-server",
        "category": "data_mcp",
        "capability_layer": "baostock_history_finance_mcp",
        "reuse_decision": "optional_adapter",
        "priority": 28,
        "license": "Repo license",
        "source_level": "official_repo_readme",
        "risk_note": "BaoStock/pandas dependency adds setup cost; MCP output still needs source-level validation.",
        "integration_plan": "Use as a candidate MCP-style historical K-line and quarterly finance fallback.",
        "source_url": "https://github.com/HuggingAGI/mcp-baostock-server",
    },
    {
        "repo_full_name": "openstockdata/stock-data-mcp",
        "name": "stock-data-mcp",
        "url": "https://github.com/openstockdata/stock-data-mcp",
        "category": "data_mcp",
        "capability_layer": "multi_market_mcp_tool_registry",
        "reuse_decision": "manual_review",
        "priority": 29,
        "license": "Repo license",
        "source_level": "official_repo_readme",
        "risk_note": "Requires external tokens for some providers; upstream wrapper provenance must be visible.",
        "integration_plan": "Borrow MCP tool registry and multi-source failover design for future connector mode.",
        "source_url": "https://github.com/openstockdata/stock-data-mcp",
    },
    {
        "repo_full_name": "huweihua123/stock-mcp",
        "name": "stock-mcp",
        "url": "https://github.com/huweihua123/stock-mcp",
        "category": "data_mcp",
        "capability_layer": "mcp_market_data_technical_research",
        "reuse_decision": "manual_review",
        "priority": 30,
        "license": "Repo license",
        "source_level": "official_repo_readme",
        "risk_note": "Broad MCP server mixes markets and analytics; keep calculations auditable in SQLite.",
        "integration_plan": "Review artifact/export and data-normalization ideas for agent-friendly local data access.",
        "source_url": "https://github.com/huweihua123/stock-mcp",
    },
    {
        "repo_full_name": "HKUDS/Vibe-Trading",
        "name": "Vibe-Trading",
        "url": "https://github.com/HKUDS/Vibe-Trading",
        "category": "agent",
        "capability_layer": "research_goal_agent_backtest_workflow",
        "reuse_decision": "architecture_only",
        "priority": 31,
        "license": "MIT",
        "source_level": "official_repo_readme",
        "risk_note": "Project states it is research software and not investment advice; live connectors stay disabled.",
        "integration_plan": "Borrow research-goal, MCP, vectorized backtest and factor-review workflow ideas.",
        "source_url": "https://github.com/HKUDS/Vibe-Trading/blob/main/README_zh.md",
    },
    {
        "repo_full_name": "UFund-Me/Qbot",
        "name": "Qbot",
        "url": "https://github.com/UFund-Me/Qbot",
        "category": "analysis_platform",
        "capability_layer": "research_backtest_simulation_closed_loop",
        "reuse_decision": "architecture_only",
        "priority": 32,
        "license": "Repo license",
        "source_level": "official_repo_readme",
        "risk_note": "Full closed-loop trading stack is out of current scope; use simulation patterns only.",
        "integration_plan": "Borrow paper simulation, reporting and workflow orchestration ideas while blocking live trading.",
        "source_url": "https://github.com/UFund-Me/Qbot",
    },
    {
        "repo_full_name": "hugo2046/QuantsPlaybook",
        "name": "QuantsPlaybook",
        "url": "https://github.com/hugo2046/QuantsPlaybook",
        "category": "research_reference",
        "capability_layer": "broker_factor_strategy_reproduction",
        "reuse_decision": "manual_review",
        "priority": 33,
        "license": "Repo license",
        "source_level": "official_repo_readme",
        "risk_note": "Research reproductions may not be production-ready or point-in-time safe.",
        "integration_plan": "Use as a reading list for factor validation, leakage checks and report templates.",
        "source_url": "https://github.com/hugo2046/QuantsPlaybook",
    },
    {
        "repo_full_name": "lotey/lite-qmt-executor",
        "name": "lite-qmt-executor",
        "url": "https://github.com/lotey/lite-qmt-executor",
        "category": "execution",
        "capability_layer": "qmt_execution_engine_wal_risk",
        "reuse_decision": "sandbox_only",
        "priority": 34,
        "license": "Repo license",
        "source_level": "official_repo_readme",
        "risk_note": "Execution engine explicitly does not provide stock selection; live trading is disabled here.",
        "integration_plan": "Borrow WAL, order-state, anti-duplicate and emergency-stop ideas for future paper execution.",
        "source_url": "https://github.com/lotey/lite-qmt-executor",
    },
    {
        "repo_full_name": "guangxiangdebizi/QMT-MCP",
        "name": "QMT-MCP",
        "url": "https://github.com/guangxiangdebizi/QMT-MCP",
        "category": "execution_mcp",
        "capability_layer": "xtquant_mcp_strategy_backtest_risk",
        "reuse_decision": "sandbox_only",
        "priority": 35,
        "license": "Repo license",
        "source_level": "official_repo_readme",
        "risk_note": "Provides strategy generation and execution hooks; do not connect to real account without explicit approval.",
        "integration_plan": "Review MCP boundary and risk-control layering for a paper-only execution connector.",
        "source_url": "https://github.com/guangxiangdebizi/QMT-MCP",
    },
    {
        "repo_full_name": "jm12138/qmt-mcp-server",
        "name": "qmt-mcp-server",
        "url": "https://github.com/jm12138/qmt-mcp-server",
        "category": "data_mcp",
        "capability_layer": "qmt_market_data_download_mcp",
        "reuse_decision": "sandbox_only",
        "priority": 36,
        "license": "Apache-2.0",
        "source_level": "official_repo_readme",
        "risk_note": "Requires QMT environment; use data download/query tools only in sandbox.",
        "integration_plan": "Use as reference for future QMT market-data bridge, not order execution.",
        "source_url": "https://github.com/jm12138/qmt-mcp-server",
    },
    {
        "repo_full_name": "rainx/pytdx",
        "name": "pytdx",
        "url": "https://github.com/rainx/pytdx",
        "category": "data",
        "capability_layer": "legacy_tdx_api",
        "reuse_decision": "exclude",
        "priority": 99,
        "license": "Archived project",
        "source_level": "official_repo_readme",
        "risk_note": "README says archived/unmaintained and not for commercial use.",
        "integration_plan": "Exclude from dependencies; prefer mootdx for TDX functionality.",
        "source_url": "https://github.com/rainx/pytdx/blob/archive/README.md",
    },
]


ROADMAP_ROWS: list[dict[str, Any]] = [
    {
        "capability_id": "license_guardrail",
        "capability_name": "License and execution guardrail",
        "status": "active",
        "priority": 0,
        "source_repos": "mootdx/mootdx; ricequant/rqalpha; hsliuping/TradingAgents-CN; mpquant/MyTT",
        "next_step": "Keep registry license notes visible before any dependency or code reuse.",
        "target_files": "scripts/a_stock_tool_registry.py; docs/github_a_stock_tool_survey.md",
        "acceptance_criteria": "Every external tool has a reuse_decision and risk_note before integration.",
    },
    {
        "capability_id": "provider_redundancy",
        "capability_name": "Quote provider redundancy and health checks",
        "status": "started",
        "priority": 1,
        "source_repos": "mootdx/mootdx; shidenggui/easyquotation; mpquant/Ashare",
        "next_step": "Extend provider_health_checks from snapshot-vs-Kline range checks to live Tencent quote and future mootdx/Tushare cross-checks.",
        "target_files": "scripts/a_stock_daily.py; scripts/a_stock_provider_health.py; data/a_stock.db",
        "acceptance_criteria": "Daily report flags missing providers, intraday-vs-close mode and price drift instead of silently trusting one provider.",
    },
    {
        "capability_id": "technical_factor_library",
        "capability_name": "Local factor and indicator layer",
        "status": "started",
        "priority": 2,
        "source_repos": "mpquant/MyTT; microsoft/qlib; jealous/stockstats; bukosabino/ta; waditu/czsc; HKUDS/Vibe-Trading",
        "next_step": "Use stock-factor-v0.3 indicators to add regime labels, leakage checks and sector-relative ranks.",
        "target_files": "scripts/a_stock_factors.py; scripts/a_stock_daily.py; scripts/a_stock_model.py",
        "acceptance_criteria": "Model reads persisted RSI/MACD/Bollinger/ATR features, keeps quality adjustment, and exposes factor details.",
    },
    {
        "capability_id": "backtest_risk_engine",
        "capability_name": "A-share paper backtest and risk ledger",
        "status": "started",
        "priority": 3,
        "source_repos": "zsrl/qka; ricequant/rqalpha; vnpy/vnpy; yutiansut/QUANTAXIS; WindRiders/a-stock-agent; UFund-Me/Qbot",
        "next_step": "Extend paper_replay_results into a portfolio ledger with T+1, limit-up/down, slippage, fees and 15% single-name cap.",
        "target_files": "scripts/a_stock_replay.py; scripts/a_stock_daily.py; scripts/a_stock_web.py; future scripts/a_stock_backtest.py",
        "acceptance_criteria": "Every recommendation can be replayed from next-day open with horizon returns, hit/stop flags and drawdown statistics before any portfolio simulation.",
    },
    {
        "capability_id": "agent_debate_review",
        "capability_name": "Bull/bear/risk/evidence review workflow",
        "status": "planned",
        "priority": 4,
        "source_repos": "TauricResearch/TradingAgents; hsliuping/TradingAgents-CN; HKUDS/Vibe-Trading",
        "next_step": "Add a report template where bullish thesis, bearish thesis and risk manager cite S/A evidence separately.",
        "target_files": "system_prompt.md; future scripts/a_stock_agent_review.py",
        "acceptance_criteria": "No agent output can upgrade a Xinwei dimension without S/A source fields.",
    },
    {
        "capability_id": "schema_provider_registry",
        "capability_name": "Provider/schema registry",
        "status": "started",
        "priority": 5,
        "source_repos": "zvtvz/zvt; AI4Finance-Foundation/FinRL-Trading",
        "next_step": "Normalize source metadata for tables and make refresh jobs report provider and field provenance.",
        "target_files": "scripts/a_stock_daily.py; scripts/a_stock_tool_registry.py",
        "acceptance_criteria": "Each persisted dataset can answer source, refresh time, provider and failure mode.",
    },
    {
        "capability_id": "qlib_export",
        "capability_name": "Offline ML/factor research export",
        "status": "planned",
        "priority": 6,
        "source_repos": "microsoft/qlib; AI4Finance-Foundation/FinRL-Trading",
        "next_step": "Export local SQLite panels to parquet/csv for notebooks and walk-forward experiments.",
        "target_files": "future scripts/a_stock_factor_export.py; notebooks",
        "acceptance_criteria": "Research experiments use frozen local snapshots and avoid look-ahead leakage.",
    },
    {
        "capability_id": "mcp_data_bridge",
        "capability_name": "MCP-style market data bridge",
        "status": "planned",
        "priority": 7,
        "source_repos": "HuggingAGI/mcp-baostock-server; openstockdata/stock-data-mcp; huweihua123/stock-mcp; jm12138/qmt-mcp-server",
        "next_step": "Evaluate MCP servers as read-only adapters and mirror outputs into SQLite with source provenance.",
        "target_files": "future scripts/a_stock_mcp_bridge.py; scripts/a_stock_daily.py",
        "acceptance_criteria": "No MCP tool can bypass local source grading, provider metadata, or point-in-time storage.",
    },
    {
        "capability_id": "execution_sandbox",
        "capability_name": "Execution sandbox and order-state guardrails",
        "status": "blocked_for_live_trading",
        "priority": 8,
        "source_repos": "lotey/lite-qmt-executor; guangxiangdebizi/QMT-MCP; shidenggui/easytrader; vnpy/vnpy",
        "next_step": "Borrow order ledger, WAL, duplicate prevention and emergency-stop concepts only for paper trading.",
        "target_files": "future scripts/a_stock_paper_execution.py",
        "acceptance_criteria": "Live account connection remains impossible without explicit user approval and separate risk checklist.",
    },
]


def seed_tool_registry(conn: sqlite3.Connection) -> None:
    now = now_cn().isoformat(timespec="seconds")
    rows = []
    for tool in TOOL_ROWS:
        raw = json.dumps(tool, ensure_ascii=False, sort_keys=True)
        rows.append(
            (
                tool["repo_full_name"],
                tool["name"],
                tool["url"],
                tool["category"],
                tool["capability_layer"],
                tool["reuse_decision"],
                tool["priority"],
                tool.get("license"),
                tool["source_level"],
                tool["risk_note"],
                tool["integration_plan"],
                tool["source_url"],
                now,
                raw,
                now,
                now,
            )
        )
    conn.executemany(
        """
        INSERT INTO external_tool_registry(
            repo_full_name, name, url, category, capability_layer, reuse_decision,
            priority, license, source_level, risk_note, integration_plan,
            source_url, last_reviewed_at, raw_json, created_at, updated_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(repo_full_name) DO UPDATE SET
            name=excluded.name,
            url=excluded.url,
            category=excluded.category,
            capability_layer=excluded.capability_layer,
            reuse_decision=excluded.reuse_decision,
            priority=excluded.priority,
            license=excluded.license,
            source_level=excluded.source_level,
            risk_note=excluded.risk_note,
            integration_plan=excluded.integration_plan,
            source_url=excluded.source_url,
            last_reviewed_at=excluded.last_reviewed_at,
            raw_json=excluded.raw_json,
            updated_at=excluded.updated_at
        """,
        rows,
    )


def seed_capability_roadmap(conn: sqlite3.Connection) -> None:
    now = now_cn().isoformat(timespec="seconds")
    rows = []
    for item in ROADMAP_ROWS:
        raw = json.dumps(item, ensure_ascii=False, sort_keys=True)
        rows.append(
            (
                item["capability_id"],
                item["capability_name"],
                item["status"],
                item["priority"],
                item["source_repos"],
                item["next_step"],
                item["target_files"],
                item["acceptance_criteria"],
                raw,
                now,
                now,
            )
        )
    conn.executemany(
        """
        INSERT INTO capability_roadmap(
            capability_id, capability_name, status, priority, source_repos,
            next_step, target_files, acceptance_criteria, raw_json, created_at, updated_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(capability_id) DO UPDATE SET
            capability_name=excluded.capability_name,
            status=excluded.status,
            priority=excluded.priority,
            source_repos=excluded.source_repos,
            next_step=excluded.next_step,
            target_files=excluded.target_files,
            acceptance_criteria=excluded.acceptance_criteria,
            raw_json=excluded.raw_json,
            updated_at=excluded.updated_at
        """,
        rows,
    )


def refresh_registry(db_path: Path = DEFAULT_DB) -> None:
    with connect(db_path) as conn:
        init_db(conn)
        seed_tool_registry(conn)
        seed_capability_roadmap(conn)
        conn.commit()


def show_registry(db_path: Path = DEFAULT_DB, limit: int = 20) -> str:
    with connect(db_path) as conn:
        init_db(conn)
        rows = conn.execute(
            """
            SELECT priority, repo_full_name, category, reuse_decision, capability_layer
            FROM external_tool_registry
            ORDER BY priority ASC, repo_full_name ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        roadmap = conn.execute(
            """
            SELECT priority, capability_id, status, next_step
            FROM capability_roadmap
            ORDER BY priority ASC, capability_id ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    lines = ["External tool registry:"]
    for row in rows:
        lines.append(
            f"{row['priority']:>2}  {row['repo_full_name']:<38} "
            f"{row['category']:<22} {row['reuse_decision']:<18} {row['capability_layer']}"
        )
    lines.append("")
    lines.append("Capability roadmap:")
    for row in roadmap:
        lines.append(f"{row['priority']:>2}  {row['capability_id']:<28} {row['status']:<8} {row['next_step']}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Maintain the local A-share GitHub tooling registry")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite database path")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("refresh", help="Create tables and seed the curated registry")
    show = sub.add_parser("show", help="Show the curated registry and roadmap")
    show.add_argument("--limit", type=int, default=20)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "refresh":
        refresh_registry(args.db)
        print(f"Seeded GitHub tooling registry in {args.db}")
        return 0
    if args.command == "show":
        print(show_registry(args.db, max(1, args.limit)))
        return 0
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
