#!/usr/bin/env python3
"""
自动校准因子阈值 — 基于历史回测数据优化 factor_weights.json 的 threshold 参数。

用法:
    py calibrate_thresholds.py                    # 默认 600744，5年
    py calibrate_thresholds.py --symbol 000001    # 指定股票
    py calibrate_thresholds.py --years 3          # 指定年数

流程:
    1. 加载 600744 过去5年的缓存K线数据
    2. 对每个交易日计算因子评分（不调API，纯本地计算）
    3. 遍历 short/mid/long 三线各自的阈值组合 (45-70, 步长1)
    4. 模拟交易：评分 >= 阈值 → 买入，持有N天后卖出
    5. 选出胜率最高且收益为正的阈值组合（约束：胜率≥45%，交易次数≥5）
    6. 更新 factor_weights.json
    7. 打印新旧阈值对比
"""

import sys
import os
import json
import argparse
from datetime import datetime, timedelta
from itertools import product

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

WEIGHTS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "analysis", "factor_weights.json")
ROOT_WEIGHTS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "factor_weights.json")

# 持仓天数映射（模拟回测用）
HOLD_DAYS = {"short": 10, "mid": 40, "long": 120}


def load_weights():
    with open(WEIGHTS_PATH, 'r', encoding='utf-8') as f:
        return json.load(f)


def save_weights(cfg):
    with open(WEIGHTS_PATH, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    with open(ROOT_WEIGHTS_PATH, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def compute_factors_for_day(df_up_to_day: pd.Series, close_series: pd.Series) -> dict:
    """基于当日及之前的数据计算因子原始值（简化版，不调用外部API）。"""
    factors = {}
    n = len(close_series)

    # momentum_20d: 过去20天收益率
    if n >= 20:
        factors["momentum_20d"] = (close_series.iloc[-1] / close_series.iloc[-20] - 1) * 100

    # momentum_60d
    if n >= 60:
        factors["momentum_60d"] = (close_series.iloc[-1] / close_series.iloc[-60] - 1) * 100

    # momentum_120d
    if n >= 120:
        factors["momentum_120d"] = (close_series.iloc[-1] / close_series.iloc[-120] - 1) * 100

    # volatility_20d: 过去20天日收益率标准差
    if n >= 20:
        returns = close_series.pct_change().dropna().tail(20)
        factors["volatility_20d"] = float(returns.std() * np.sqrt(252) * 100)

    # breakout_20d_high: 当前价是否突破20日高点
    if n >= 20:
        high_20d = close_series.tail(20).max()
        factors["breakout_20d_high"] = 1.0 if close_series.iloc[-1] >= high_20d else 0.0

    # volume_ratio (用价格波动率近似，因为历史快照可能无成交量)
    if n >= 10:
        recent_vol = close_series.pct_change().dropna().tail(5).std()
        past_vol = close_series.pct_change().dropna().tail(20).std()
        if past_vol and past_vol > 0:
            factors["volume_ratio"] = recent_vol / past_vol

    # ma_bull_alignment: MA5 > MA10 > MA20 > MA60
    if n >= 60:
        ma5 = close_series.tail(5).mean()
        ma10 = close_series.tail(10).mean()
        ma20 = close_series.tail(20).mean()
        ma60 = close_series.tail(60).mean()
        aligned = 1.0 if (ma5 > ma10 > ma20 > ma60) else 0.0
        factors["ma_bull_alignment"] = aligned

    return factors


def composite_score_simple(factors: dict, tf_weights: dict) -> float:
    """简化版综合评分（与 alpha.composite_score 逻辑一致）。"""
    threshold = tf_weights.get("threshold", 55)
    total_score = 50.0
    used_weights = 0

    for fname, weight in tf_weights.items():
        if fname == "threshold":
            continue
        raw = factors.get(fname)
        if raw is None:
            continue
        # 简化版 z-score: 假设均值0，标准差1（因子已标准化到合理范围）
        # 实际上直接用原始值的加权和
        contrib = raw * weight * 0.1  # 缩放因子
        total_score += contrib
        used_weights += abs(weight)

    # 归一化
    if used_weights > 0:
        total_score = 50 + (total_score - 50) / max(used_weights, 0.5)
    return max(0.0, min(100.0, total_score))


def simulate_trades(df: pd.DataFrame, tf: str, threshold: float,
                    weights: dict, hold_days: int) -> dict:
    """模拟交易：遍历每个交易日，评分>=阈值则买入，持有hold_days天后卖出。"""
    close = df['close'].values
    dates = df['date'].values
    n = len(close)

    # 构建因子权重（替换阈值）
    tf_weights = dict(weights[tf])
    tf_weights["threshold"] = threshold

    trades = []
    i = 120  # 跳过前120天预热期（需要120天数据算 momentum_120d）
    while i < n - hold_days:
        # 计算当日因子
        close_series = pd.Series(close[max(0, i-120):i+1])
        factors = compute_factors_for_day(df.iloc[i], close_series)
        score = composite_score_simple(factors, tf_weights)

        if score >= threshold:
            entry_price = close[i]
            exit_idx = min(i + hold_days, n - 1)
            exit_price = close[exit_idx]
            pnl_pct = (exit_price / entry_price - 1) * 100
            trades.append({
                "entry_date": str(dates[i])[:10],
                "exit_date": str(dates[exit_idx])[:10],
                "entry_price": entry_price,
                "exit_price": exit_price,
                "pnl_pct": round(pnl_pct, 2),
                "score": round(score, 1),
            })
            i = exit_idx + 1  # 卖出后才开下一笔
        else:
            i += 1

    # 统计
    if not trades:
        return {"trades": 0, "win_rate": 0, "avg_return": 0, "total_return": 0}

    wins = sum(1 for t in trades if t["pnl_pct"] > 0)
    total_pnl = sum(t["pnl_pct"] for t in trades)
    return {
        "trades": len(trades),
        "win_rate": round(wins / len(trades) * 100, 1),
        "avg_return": round(total_pnl / len(trades), 2),
        "total_return": round(total_pnl, 2),
    }


def run_calibration(symbol: str, years: int):
    """主校准流程。"""
    from data.pipeline import download_full_history

    print(f"\n{'='*70}")
    print(f"  因子阈值自动校准")
    print(f"  股票: {symbol} | 回测年数: {years}")
    print(f"  阈值搜索范围: 45-70 (步长1)")
    print(f"{'='*70}")

    # 1. 加载数据
    print(f"\n[1/4] 加载历史数据...")
    cache_path = download_full_history(symbol, ndays=years * 365 + 200)
    df = pd.read_csv(cache_path)
    df['date'] = pd.to_datetime(df['date'])
    df = df.sort_values('date').reset_index(drop=True)

    # 截取最近N年
    cutoff = df['date'].max() - pd.Timedelta(days=years * 365)
    df = df[df['date'] >= cutoff].reset_index(drop=True)
    print(f"  数据范围: {df['date'].iloc[0].date()} → {df['date'].iloc[-1].date()} ({len(df)} 交易日)")

    # 2. 加载当前权重
    print(f"\n[2/4] 加载当前因子权重...")
    weights = load_weights()
    old_thresholds = {tf: weights[tf].get("threshold", 55) for tf in ["short", "mid", "long"]}
    print(f"  当前阈值: short={old_thresholds['short']}, mid={old_thresholds['mid']}, long={old_thresholds['long']}")

    # 3. 遍历阈值组合
    print(f"\n[3/4] 搜索最优阈值...")
    threshold_range = range(45, 71)  # 45-70
    results = {}

    for tf in ["short", "mid", "long"]:
        print(f"\n  ── {tf.upper()} 线 ──")
        best = {"threshold": old_thresholds[tf], "win_rate": 0, "avg_return": -999, "trades": 0}
        hold = HOLD_DAYS[tf]

        for thr in threshold_range:
            result = simulate_trades(df, tf, float(thr), weights, hold)

            # 筛选：胜率≥45%，交易次数≥5，平均收益>0
            if (result["win_rate"] >= 45 and result["trades"] >= 5
                    and result["avg_return"] > 0):
                # 优先选胜率最高，其次平均收益最高
                if (result["win_rate"] > best["win_rate"]
                        or (result["win_rate"] == best["win_rate"]
                            and result["avg_return"] > best["avg_return"])):
                    best = {"threshold": thr, **result}

            results[(tf, thr)] = result

        print(f"  最优阈值: {best['threshold']} | "
              f"胜率={best['win_rate']}% | "
              f"平均收益={best['avg_return']}% | "
              f"交易次数={best['trades']}")

        # 更新权重
        weights[tf]["threshold"] = float(best["threshold"])

    # 4. 保存结果
    print(f"\n[4/4] 保存校准结果...")
    save_weights(weights)

    # 打印对比
    print(f"\n{'='*70}")
    print(f"  新旧阈值对比")
    print(f"{'='*70}")
    print(f"  {'时间框架':<10} {'旧阈值':>8} {'新阈值':>8} {'变化':>8}")
    print(f"  {'-'*36}")
    for tf, label in [("short", "短线"), ("mid", "中线"), ("long", "长线")]:
        old_val = old_thresholds[tf]
        new_val = weights[tf]["threshold"]
        delta = new_val - old_val
        arrow = "↑" if delta > 0 else ("↓" if delta < 0 else "─")
        print(f"  {label:<10} {old_val:>8.0f} {new_val:>8.0f} {arrow}{abs(delta):>7.0f}")

    print(f"\n  已更新: {WEIGHTS_PATH}")
    print(f"  已更新: {ROOT_WEIGHTS_PATH}")
    print(f"{'='*70}")

    return weights


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="自动校准因子阈值")
    parser.add_argument("--symbol", default="600744", help="股票代码")
    parser.add_argument("--years", type=int, default=5, help="回测年数")
    args = parser.parse_args()

    run_calibration(args.symbol, args.years)
