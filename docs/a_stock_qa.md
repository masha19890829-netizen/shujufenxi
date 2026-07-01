# Xinwei QA Evaluation Layer

This document describes the local QA layer for the A-share Xinwei dashboard and
research database.

## Purpose

The QA layer evaluates data accuracy, model stability, website availability, and
Xinwei formula gate integrity. Quantitative calculations are reference-only and
do not create buy eligibility.

## Commands

Initialize QA tables and task catalog:

```powershell
python D:\spacex\scripts\a_stock_qa.py init
```

Run daily QA with a temporary local website server:

```powershell
python D:\spacex\scripts\a_stock_qa.py run --mode daily --top 20 --start-web --report
```

Run QA against an already-running dashboard:

```powershell
python D:\spacex\scripts\a_stock_qa.py run --mode daily --top 20 --website-url http://127.0.0.1:8765
```

Show the latest QA run:

```powershell
python D:\spacex\scripts\a_stock_qa.py show --top 20
```

## Tables

- `qa_runs`: one row per QA run, with status, failure rate, missing-field rate,
  API P95, alert count, and markdown report.
- `qa_checks`: individual Data QA, Model QA, Website QA, and Formula QA checks.
- `qa_tasks`: stable evaluation task catalog.
- `qa_quant_reference`: P1-P7 probability distribution, EV, win rate, odds,
  half-Kelly, and capped position reference.
- `qa_alerts`: open alerts derived from failed or major warning checks.

## Website API

The dashboard exposes the latest QA result at:

```text
http://127.0.0.1:8765/api/qa-report
```

The normal summary API also includes the latest QA status fields:

```text
qa_status
qa_alert_count
qa_failure_rate
qa_missing_field_rate
qa_api_p95_ms
```

## Guardrails

- `needs_review` is never treated as verified evidence.
- `formula_verification_score < 100` keeps the quantitative row in
  `observe_only`.
- `observe_only` rows always have `position_cap_pct = 0`.
- The single-stock cap remains 15% even when a row becomes `reference_only`.
- QA writes only QA tables; it does not mutate recommendation or model outputs.

## Default Thresholds

- Failure rate: `> 1%`
- Critical field missing rate: `> 2%`
- API P95 latency: `> 2500ms`
- QA retention: `30 days`
