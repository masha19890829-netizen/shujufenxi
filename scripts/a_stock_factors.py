#!/usr/bin/env python
"""Build persisted daily factor features from local A-share snapshots."""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics
import sys
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from a_stock_daily import DEFAULT_DB, as_float, clamp, connect, init_db, now_cn


FACTOR_VERSION = "stock-factor-v0.3-indicators"


@dataclass
class FactorRow:
    trade_date: str
    code: str
    name: str
    source: str
    sample_days: int
    quality_score: float
    price: float | None
    prev_price: float | None
    return_1d_pct: float | None
    momentum_5d_pct: float | None
    momentum_20d_pct: float | None
    ma5: float | None
    ma20: float | None
    ma60: float | None
    ma5_sample_days: int
    ma20_sample_days: int
    ma60_sample_days: int
    volatility_5d_pct: float | None
    volatility_20d_pct: float | None
    high_20d: float | None
    low_20d: float | None
    drawdown_20d_pct: float | None
    rsi14: float | None
    macd_dif: float | None
    macd_dea: float | None
    macd_hist: float | None
    boll_mid: float | None
    boll_upper: float | None
    boll_lower: float | None
    boll_position_pct: float | None
    atr14_pct: float | None
    amount_ma5: float | None
    amount_ma20: float | None
    turnover_ma5: float | None
    main_net_inflow_ma5: float | None
    main_net_inflow_to_amount_pct: float | None
    liquidity_score: float
    trend_score: float
    flow_score: float
    volatility_score: float
    valuation_score: float
    technical_score: float
    composite_score: float
    detail: dict[str, Any]


def mean_or_none(values: list[float | None]) -> float | None:
    clean = [float(value) for value in values if value is not None]
    if not clean:
        return None
    return round(sum(clean) / len(clean), 4)


def std_pct(values: list[float | None]) -> float | None:
    clean = [float(value) for value in values if value is not None]
    if len(clean) < 2:
        return None
    return round(statistics.stdev(clean), 4)


def pct_change(current: float | None, base: float | None) -> float | None:
    if current is None or base in (None, 0):
        return None
    return round((float(current) / float(base) - 1.0) * 100.0, 4)


def ema_update(previous: float | None, value: float | None, period: int) -> float | None:
    if value is None:
        return previous
    if previous is None:
        return value
    alpha = 2.0 / (period + 1.0)
    return previous + alpha * (value - previous)


def rsi_from_returns(return_values: list[float | None]) -> float | None:
    clean = [float(value) for value in return_values if value is not None]
    if len(clean) < 14:
        return None
    window = clean[-14:]
    gains = [max(value, 0.0) for value in window]
    losses = [abs(min(value, 0.0)) for value in window]
    avg_gain = sum(gains) / len(window)
    avg_loss = sum(losses) / len(window)
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return round(100.0 - 100.0 / (1.0 + rs), 4)


def score_technical(
    rsi14: float | None,
    macd_hist: float | None,
    price: float | None,
    boll_position_pct: float | None,
    atr14_pct: float | None,
) -> float:
    score = 50.0
    if rsi14 is not None:
        if 45 <= rsi14 <= 65:
            score += 14.0
        elif 35 <= rsi14 <= 75:
            score += 7.0
        elif rsi14 > 82:
            score -= 14.0
        elif rsi14 < 25:
            score -= 10.0

    if macd_hist is not None and price:
        hist_pct = (macd_hist / price) * 100.0
        score += max(-14.0, min(14.0, hist_pct * 45.0))

    if boll_position_pct is not None:
        if 35 <= boll_position_pct <= 75:
            score += 12.0
        elif 20 <= boll_position_pct <= 90:
            score += 5.0
        elif boll_position_pct > 96:
            score -= 12.0
        elif boll_position_pct < 8:
            score -= 8.0

    if atr14_pct is not None:
        if atr14_pct <= 4.0:
            score += 8.0
        elif atr14_pct <= 8.0:
            score += 2.0
        else:
            score -= min(18.0, (atr14_pct - 8.0) * 2.0)

    return round(clamp(score, 0.0, 100.0), 2)


def score_liquidity(amount: float | None, amount_ma5: float | None, turnover_ma5: float | None) -> float:
    base_amount = amount_ma5 if amount_ma5 is not None else amount
    score = 0.0
    if base_amount is not None:
        score += 70.0 * clamp(base_amount / 1_500_000_000)
    if turnover_ma5 is not None:
        if 1.0 <= turnover_ma5 <= 10.0:
            score += 30.0
        elif turnover_ma5 < 1.0:
            score += 30.0 * clamp(turnover_ma5 / 1.0)
        else:
            score += max(0.0, 30.0 - min(30.0, (turnover_ma5 - 10.0) * 2.0))
    return round(clamp(score, 0.0, 100.0), 2)


def score_trend(return_1d: float | None, momentum_5d: float | None, price: float | None, ma5: float | None, ma20: float | None) -> float:
    score = 50.0
    if return_1d is not None:
        if return_1d >= 0:
            score += min(18.0, return_1d * 2.2)
        else:
            score -= min(22.0, abs(return_1d) * 2.4)
    if momentum_5d is not None:
        if momentum_5d >= 0:
            score += min(20.0, momentum_5d * 1.4)
        else:
            score -= min(24.0, abs(momentum_5d) * 1.6)
    if price is not None and ma5 is not None:
        score += 8.0 if price >= ma5 else -6.0
    if price is not None and ma20 is not None and ma20 > 0:
        distance = (price / ma20 - 1.0) * 100.0
        if 0 <= distance <= 12:
            score += 10.0
        elif distance > 12:
            score += max(-8.0, 10.0 - (distance - 12.0) * 1.2)
        else:
            score -= min(10.0, abs(distance) * 0.9)
    return round(clamp(score, 0.0, 100.0), 2)


def score_flow(flow_ratio_pct: float | None, flow_ma5: float | None) -> float:
    score = 50.0
    if flow_ratio_pct is not None:
        score += max(-35.0, min(35.0, flow_ratio_pct * 5.0))
    if flow_ma5 is not None:
        score += max(-15.0, min(15.0, flow_ma5 / 50_000_000.0))
    return round(clamp(score, 0.0, 100.0), 2)


def score_volatility(vol_5d: float | None, drawdown_20d: float | None) -> float:
    score = 78.0
    if vol_5d is not None:
        if vol_5d <= 2.5:
            score += 10.0
        elif vol_5d <= 5.0:
            score += 4.0
        else:
            score -= min(32.0, (vol_5d - 5.0) * 4.0)
    if drawdown_20d is not None:
        if drawdown_20d >= -3.0:
            score += 8.0
        else:
            score -= min(35.0, abs(drawdown_20d) * 1.4)
    return round(clamp(score, 0.0, 100.0), 2)


def score_valuation(pe_ttm: float | None, pb: float | None) -> float:
    score = 50.0
    if pe_ttm is None:
        score -= 5.0
    elif pe_ttm <= 0:
        score -= 25.0
    elif pe_ttm <= 25:
        score += 22.0
    elif pe_ttm <= 50:
        score += 14.0
    elif pe_ttm <= 80:
        score += 6.0
    elif pe_ttm <= 150:
        score -= 4.0
    else:
        score -= 16.0

    if pb is None:
        score -= 3.0
    elif pb <= 0:
        score -= 15.0
    elif pb <= 3:
        score += 18.0
    elif pb <= 6:
        score += 10.0
    elif pb <= 10:
        score += 2.0
    else:
        score -= min(22.0, (pb - 10.0) * 2.0)
    return round(clamp(score, 0.0, 100.0), 2)


def quality_score(sample_days: int) -> float:
    if sample_days >= 60:
        return 100.0
    if sample_days >= 20:
        return 80.0 + (sample_days - 20) / 40.0 * 20.0
    if sample_days >= 5:
        return 45.0 + (sample_days - 5) / 15.0 * 35.0
    return round(15.0 + sample_days / 5.0 * 30.0, 2)


def load_factor_input_rows(conn: sqlite3.Connection, through_date: str | None = None) -> list[sqlite3.Row]:
    params: list[Any] = []
    kline_clause = ""
    snapshot_clause = ""
    if through_date:
        kline_clause = "WHERE k.trade_date <= ?"
        snapshot_clause = "AND ms.trade_date <= ?"
        params.extend([through_date, through_date])
    return conn.execute(
        f"""
        WITH kline_rows AS (
            SELECT
                k.trade_date,
                k.code,
                COALESCE(NULLIF(k.name, ''), ms.name, k.code) AS name,
                k.close AS price,
                k.amount,
                k.turnover_pct,
                ms.main_net_inflow,
                ms.pe_ttm,
                ms.pb,
                k.high,
                k.low,
                k.change_pct,
                k.source AS source
            FROM stock_kline_daily k
            LEFT JOIN market_snapshot ms
              ON ms.code = k.code
             AND ms.trade_date = k.trade_date
            {kline_clause}
        ),
        snapshot_rows AS (
            SELECT
                ms.trade_date,
                ms.code,
                ms.name,
                ms.price,
                ms.amount,
                ms.turnover_pct,
                ms.main_net_inflow,
                ms.pe_ttm,
                ms.pb,
                ms.high,
                ms.low,
                ms.change_pct,
                ms.source AS source
            FROM market_snapshot ms
            WHERE NOT EXISTS (
                SELECT 1
                FROM stock_kline_daily k
                WHERE k.code = ms.code
                  AND k.trade_date = ms.trade_date
            )
            {snapshot_clause}
        )
        SELECT * FROM kline_rows
        UNION ALL
        SELECT * FROM snapshot_rows
        ORDER BY code ASC, trade_date ASC
        """,
        params,
    ).fetchall()


def build_factor_rows(conn: sqlite3.Connection, through_date: str | None = None) -> list[FactorRow]:
    rows = load_factor_input_rows(conn, through_date)
    by_code: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        by_code[row["code"]].append(row)

    factors: list[FactorRow] = []
    for code_rows in by_code.values():
        prices: deque[float | None] = deque(maxlen=60)
        amounts: deque[float | None] = deque(maxlen=20)
        turnovers: deque[float | None] = deque(maxlen=20)
        flows: deque[float | None] = deque(maxlen=20)
        returns: deque[float | None] = deque(maxlen=20)
        highs: deque[float | None] = deque(maxlen=20)
        lows: deque[float | None] = deque(maxlen=20)
        true_ranges: deque[float | None] = deque(maxlen=14)
        prev_price: float | None = None
        ema12: float | None = None
        ema26: float | None = None
        macd_signal: float | None = None

        for row in code_rows:
            price = as_float(row["price"])
            amount = as_float(row["amount"])
            turnover = as_float(row["turnover_pct"])
            main_flow = as_float(row["main_net_inflow"])
            high = as_float(row["high"])
            low = as_float(row["low"])
            pe_ttm = as_float(row["pe_ttm"])
            pb = as_float(row["pb"])
            return_1d = pct_change(price, prev_price)
            if return_1d is None:
                return_1d = as_float(row["change_pct"])

            prices.append(price)
            amounts.append(amount)
            turnovers.append(turnover)
            flows.append(main_flow)
            returns.append(return_1d)
            highs.append(high if high is not None else price)
            lows.append(low if low is not None else price)

            price_list = list(prices)
            sample_days = len([value for value in price_list if value is not None])
            ma5_values = price_list[-5:]
            ma20_values = price_list[-20:]
            ma60_values = price_list[-60:]
            ma5 = mean_or_none(ma5_values)
            ma20 = mean_or_none(ma20_values)
            ma60 = mean_or_none(ma60_values)
            ma5_days = len([value for value in ma5_values if value is not None])
            ma20_days = len([value for value in ma20_values if value is not None])
            ma60_days = len([value for value in ma60_values if value is not None])

            first_5 = next((value for value in ma5_values if value is not None), None)
            first_20 = next((value for value in ma20_values if value is not None), None)
            momentum_5d = pct_change(price, first_5)
            momentum_20d = pct_change(price, first_20)
            vol_5d = std_pct(list(returns)[-5:])
            vol_20d = std_pct(list(returns)[-20:])
            clean_highs = [value for value in highs if value is not None]
            clean_lows = [value for value in lows if value is not None]
            high_20d = round(max(clean_highs), 4) if clean_highs else None
            low_20d = round(min(clean_lows), 4) if clean_lows else None
            drawdown_20d = pct_change(price, high_20d)
            rsi14 = rsi_from_returns(list(returns))
            ema12 = ema_update(ema12, price, 12)
            ema26 = ema_update(ema26, price, 26)
            macd_dif = round(ema12 - ema26, 4) if ema12 is not None and ema26 is not None else None
            macd_signal = ema_update(macd_signal, macd_dif, 9)
            macd_dea = round(macd_signal, 4) if macd_signal is not None else None
            macd_hist = round((macd_dif - macd_signal) * 2.0, 4) if macd_dif is not None and macd_signal is not None else None
            boll_mid = ma20
            boll_upper = None
            boll_lower = None
            boll_position = None
            clean_ma20_prices = [value for value in ma20_values if value is not None]
            if len(clean_ma20_prices) >= 20 and boll_mid is not None:
                boll_std = statistics.stdev(clean_ma20_prices)
                boll_upper = round(boll_mid + boll_std * 2.0, 4)
                boll_lower = round(boll_mid - boll_std * 2.0, 4)
                if price is not None and boll_upper != boll_lower:
                    boll_position = round((price - boll_lower) / (boll_upper - boll_lower) * 100.0, 4)
            true_range = None
            if high is not None and low is not None:
                ranges = [high - low]
                if prev_price is not None:
                    ranges.extend([abs(high - prev_price), abs(low - prev_price)])
                true_range = max(ranges)
            true_ranges.append(true_range)
            atr14 = mean_or_none(list(true_ranges))
            atr14_pct = round((atr14 / price) * 100.0, 4) if atr14 is not None and price not in (None, 0) else None
            amount_ma5 = mean_or_none(list(amounts)[-5:])
            amount_ma20 = mean_or_none(list(amounts)[-20:])
            turnover_ma5 = mean_or_none(list(turnovers)[-5:])
            flow_ma5 = mean_or_none(list(flows)[-5:])
            flow_ratio = (
                round((main_flow / amount) * 100.0, 4)
                if main_flow is not None and amount not in (None, 0)
                else None
            )

            liquidity = score_liquidity(amount, amount_ma5, turnover_ma5)
            trend = score_trend(return_1d, momentum_5d, price, ma5, ma20)
            flow_score = score_flow(flow_ratio, flow_ma5)
            vol_score = score_volatility(vol_5d, drawdown_20d)
            valuation = score_valuation(pe_ttm, pb)
            technical = score_technical(rsi14, macd_hist, price, boll_position, atr14_pct)
            quality = quality_score(sample_days)
            composite = round(
                0.25 * trend
                + 0.20 * liquidity
                + 0.15 * flow_score
                + 0.15 * vol_score
                + 0.10 * valuation
                + 0.15 * technical,
                2,
            )
            detail = {
                "factor_version": FACTOR_VERSION,
                "weights": {
                    "trend_score": 0.25,
                    "liquidity_score": 0.20,
                    "flow_score": 0.15,
                    "volatility_score": 0.15,
                    "valuation_score": 0.10,
                    "technical_score": 0.15,
                },
                "history_warning": (
                    "history shorter than 20 trading days; long-window factors are provisional"
                    if sample_days < 20
                    else None
                ),
                "source": row["source"],
                "raw_inputs": {
                    "amount": amount,
                    "turnover_pct": turnover,
                    "main_net_inflow": main_flow,
                    "pe_ttm": pe_ttm,
                    "pb": pb,
                },
                "technical_inputs": {
                    "rsi14": rsi14,
                    "macd_dif": macd_dif,
                    "macd_dea": macd_dea,
                    "macd_hist": macd_hist,
                    "boll_position_pct": boll_position,
                    "atr14_pct": atr14_pct,
                },
            }
            factors.append(
                FactorRow(
                    trade_date=row["trade_date"],
                    code=row["code"],
                    name=row["name"],
                    source=row["source"],
                    sample_days=sample_days,
                    quality_score=quality,
                    price=price,
                    prev_price=prev_price,
                    return_1d_pct=return_1d,
                    momentum_5d_pct=momentum_5d,
                    momentum_20d_pct=momentum_20d,
                    ma5=ma5,
                    ma20=ma20,
                    ma60=ma60,
                    ma5_sample_days=ma5_days,
                    ma20_sample_days=ma20_days,
                    ma60_sample_days=ma60_days,
                    volatility_5d_pct=vol_5d,
                    volatility_20d_pct=vol_20d,
                    high_20d=high_20d,
                    low_20d=low_20d,
                    drawdown_20d_pct=drawdown_20d,
                    rsi14=rsi14,
                    macd_dif=macd_dif,
                    macd_dea=macd_dea,
                    macd_hist=macd_hist,
                    boll_mid=boll_mid,
                    boll_upper=boll_upper,
                    boll_lower=boll_lower,
                    boll_position_pct=boll_position,
                    atr14_pct=atr14_pct,
                    amount_ma5=amount_ma5,
                    amount_ma20=amount_ma20,
                    turnover_ma5=turnover_ma5,
                    main_net_inflow_ma5=flow_ma5,
                    main_net_inflow_to_amount_pct=flow_ratio,
                    liquidity_score=liquidity,
                    trend_score=trend,
                    flow_score=flow_score,
                    volatility_score=vol_score,
                    valuation_score=valuation,
                    technical_score=technical,
                    composite_score=composite,
                    detail=detail,
                )
            )
            if price is not None:
                prev_price = price
    return factors


def save_factor_rows(conn: sqlite3.Connection, factors: list[FactorRow]) -> None:
    created_at = now_cn().isoformat(timespec="seconds")
    rows = []
    for item in factors:
        rows.append(
            (
                item.trade_date,
                item.code,
                item.name,
                item.source,
                item.sample_days,
                item.quality_score,
                item.price,
                item.prev_price,
                item.return_1d_pct,
                item.momentum_5d_pct,
                item.momentum_20d_pct,
                item.ma5,
                item.ma20,
                item.ma60,
                item.ma5_sample_days,
                item.ma20_sample_days,
                item.ma60_sample_days,
                item.volatility_5d_pct,
                item.volatility_20d_pct,
                item.high_20d,
                item.low_20d,
                item.drawdown_20d_pct,
                item.rsi14,
                item.macd_dif,
                item.macd_dea,
                item.macd_hist,
                item.boll_mid,
                item.boll_upper,
                item.boll_lower,
                item.boll_position_pct,
                item.atr14_pct,
                item.amount_ma5,
                item.amount_ma20,
                item.turnover_ma5,
                item.main_net_inflow_ma5,
                item.main_net_inflow_to_amount_pct,
                item.liquidity_score,
                item.trend_score,
                item.flow_score,
                item.volatility_score,
                item.valuation_score,
                item.technical_score,
                item.composite_score,
                json.dumps(item.detail, ensure_ascii=False, separators=(",", ":")),
                created_at,
            )
        )
    conn.executemany(
        """
        INSERT INTO stock_factor_daily(
            trade_date, code, name, source, sample_days, quality_score,
            price, prev_price, return_1d_pct, momentum_5d_pct, momentum_20d_pct,
            ma5, ma20, ma60, ma5_sample_days, ma20_sample_days, ma60_sample_days,
            volatility_5d_pct, volatility_20d_pct, high_20d, low_20d,
            drawdown_20d_pct, rsi14, macd_dif, macd_dea, macd_hist,
            boll_mid, boll_upper, boll_lower, boll_position_pct, atr14_pct,
            amount_ma5, amount_ma20, turnover_ma5,
            main_net_inflow_ma5, main_net_inflow_to_amount_pct,
            liquidity_score, trend_score, flow_score, volatility_score,
            valuation_score, technical_score, composite_score, factor_json, created_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(trade_date, code) DO UPDATE SET
            name=excluded.name,
            source=excluded.source,
            sample_days=excluded.sample_days,
            quality_score=excluded.quality_score,
            price=excluded.price,
            prev_price=excluded.prev_price,
            return_1d_pct=excluded.return_1d_pct,
            momentum_5d_pct=excluded.momentum_5d_pct,
            momentum_20d_pct=excluded.momentum_20d_pct,
            ma5=excluded.ma5,
            ma20=excluded.ma20,
            ma60=excluded.ma60,
            ma5_sample_days=excluded.ma5_sample_days,
            ma20_sample_days=excluded.ma20_sample_days,
            ma60_sample_days=excluded.ma60_sample_days,
            volatility_5d_pct=excluded.volatility_5d_pct,
            volatility_20d_pct=excluded.volatility_20d_pct,
            high_20d=excluded.high_20d,
            low_20d=excluded.low_20d,
            drawdown_20d_pct=excluded.drawdown_20d_pct,
            rsi14=excluded.rsi14,
            macd_dif=excluded.macd_dif,
            macd_dea=excluded.macd_dea,
            macd_hist=excluded.macd_hist,
            boll_mid=excluded.boll_mid,
            boll_upper=excluded.boll_upper,
            boll_lower=excluded.boll_lower,
            boll_position_pct=excluded.boll_position_pct,
            atr14_pct=excluded.atr14_pct,
            amount_ma5=excluded.amount_ma5,
            amount_ma20=excluded.amount_ma20,
            turnover_ma5=excluded.turnover_ma5,
            main_net_inflow_ma5=excluded.main_net_inflow_ma5,
            main_net_inflow_to_amount_pct=excluded.main_net_inflow_to_amount_pct,
            liquidity_score=excluded.liquidity_score,
            trend_score=excluded.trend_score,
            flow_score=excluded.flow_score,
            volatility_score=excluded.volatility_score,
            valuation_score=excluded.valuation_score,
            technical_score=excluded.technical_score,
            composite_score=excluded.composite_score,
            factor_json=excluded.factor_json,
            created_at=excluded.created_at
        """,
        rows,
    )


def refresh_stock_factors(conn: sqlite3.Connection, through_date: str | None = None) -> list[FactorRow]:
    init_db(conn)
    factors = build_factor_rows(conn, through_date)
    if factors:
        save_factor_rows(conn, factors)
    return factors


def command_refresh(args: argparse.Namespace) -> None:
    with connect(args.db) as conn:
        factors = refresh_stock_factors(conn, args.date)
        conn.commit()
    latest = max((row.trade_date for row in factors), default=args.date or "-")
    print(f"Refreshed {len(factors)} factor rows through {latest}. version={FACTOR_VERSION}")


def command_show(args: argparse.Namespace) -> None:
    with connect(args.db) as conn:
        init_db(conn)
        date = args.date
        if not date:
            row = conn.execute("SELECT MAX(trade_date) AS trade_date FROM stock_factor_daily").fetchone()
            date = row["trade_date"] if row and row["trade_date"] else None
        if not date:
            print("No factor rows found.")
            return
        rows = conn.execute(
            """
            SELECT
                code, name, composite_score, quality_score, sample_days,
                trend_score, liquidity_score, flow_score, volatility_score,
                valuation_score, technical_score, rsi14, macd_hist, atr14_pct,
                return_1d_pct, momentum_5d_pct, drawdown_20d_pct,
                50.0 + (composite_score - 50.0) * quality_score / 100.0 AS adjusted_score
            FROM stock_factor_daily
            WHERE trade_date = ?
            ORDER BY adjusted_score DESC, quality_score DESC
            LIMIT ?
            """,
            (date, args.top),
        ).fetchall()
    print(f"Factor rows date={date} version={FACTOR_VERSION}")
    for idx, row in enumerate(rows, start=1):
        print(
            f"{idx:>3}. {row['code']} {row['name']} factor={row['composite_score']:.2f} "
            f"adj={row['adjusted_score']:.2f} quality={row['quality_score']:.2f} days={row['sample_days']} "
            f"trend={row['trend_score']:.2f} flow={row['flow_score']:.2f} "
            f"tech={row['technical_score']:.2f} rsi={row['rsi14']} "
            f"ret1d={row['return_1d_pct']} mom5={row['momentum_5d_pct']} "
            f"dd20={row['drawdown_20d_pct']}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Refresh local A-share factor rows")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite database path")
    parser.add_argument("--date", help="Only use snapshots through this date")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("refresh", help="Refresh persisted factor rows")
    show = sub.add_parser("show", help="Show top factor rows")
    show.add_argument("--top", type=int, default=20)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "refresh":
        command_refresh(args)
    elif args.command == "show":
        command_show(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
