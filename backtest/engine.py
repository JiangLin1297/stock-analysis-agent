#!/usr/bin/env python3
"""
历史回测引擎 — 按交易日逐日推进，模拟三线交易决策执行。
支持随机时段测试、完整回测、指标计算和格式化输出。

用法:
    py backtest_engine.py 600744 --start 2024-01-01 --end 2024-12-31
    py backtest_engine.py 600744 --time_frame mid --days 120
"""

import sys
import os
import json
import random
import math
from datetime import datetime, date, timedelta
from collections import defaultdict

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── 交易成本 ──
COMMISSION_RATE = 0.0003      # 佣金 0.03%
STAMP_TAX_RATE = 0.0005       # 印花税 0.05% (仅卖出)
SLIPPAGE_RATE = 0.001         # 滑点 0.1%

# ── 时间维度映射 ──
TIMEFRAME_DAYS = {
    "short": (1, 30),
    "mid": (30, 180),
    "long": (180, 500),
}
TIMEFRAME_TARGET = {
    "short_term": 10,
    "mid_term": 30,
    "long_term": 100,
}


def _count_consecutive_losses(trade_log: list) -> int:
    """Count consecutive losing closed trades from the end of the log."""
    count = 0
    for t in reversed(trade_log):
        if t.get("action") in ("CLOSE", "CLOSE_FINAL", "TRIM"):
            if t.get("pnl", 0) <= 0:
                count += 1
            else:
                break
        else:
            continue
    return count


def random_period_test(symbol: str, time_frame: str = "mid",
                       days: int = 60, seed: int = None) -> dict:
    """
    随机选取一段历史区间用于回测。

    Args:
        symbol: 股票代码
        time_frame: "short"(1-30天) / "mid"(30-180天) / "long"(180-500天)
        days: 回测区间长度（交易日数）
        seed: 随机种子，用于结果复现

    Returns:
        {"symbol", "start_date", "end_date", "days", "time_frame", "seed"}
    """
    from data.pipeline import download_full_history

    if seed is not None:
        random.seed(seed)

    cache_path = download_full_history(symbol, ndays=800)
    df = pd.read_csv(cache_path)
    df['date'] = pd.to_datetime(df['date']).dt.date

    all_dates = sorted(df['date'].unique())
    if len(all_dates) < days + 30:
        raise ValueError(f"历史数据不足: 需要{days+30}天，实际{len(all_dates)}天")

    # 排除最近30天（避免数据不完整），随机选起始位置
    max_start_idx = len(all_dates) - days - 30
    start_idx = random.randint(30, max_start_idx)  # 前30天用于预热指标计算
    start_date = all_dates[start_idx]
    end_idx = min(start_idx + days, len(all_dates) - 1)
    end_date = all_dates[end_idx]

    return {
        "symbol": symbol,
        "start_date": str(start_date),
        "end_date": str(end_date),
        "days": days,
        "time_frame": time_frame,
        "seed": seed,
    }


def run_backtest(symbol: str, start_date, end_date,
                 initial_capital: float = 100000.0,
                 use_factor_model: bool = True) -> dict:
    """
    核心回测函数：按交易日逐日推进，模拟三线交易决策。

    流程:
      1. 加载历史数据缓存
      2. 逐日获取历史快照 → 运行分析 → 执行交易信号
      3. 三线各自独立持仓管理（short/mid/long）
      4. 每日调用 holding_evaluator 评估现有持仓
      5. 记录每笔交易日志

    use_factor_model=True: 使用多因子统计模型做决策（默认）
    use_factor_model=False: 使用 LLM Agent 管线做决策（旧版兼容）

    Returns:
        {"symbol", "start_date", "end_date", "initial_capital", "final_equity",
         "trade_log": [...], "equity_curve": [...], "metrics": {...}}
    """
    from data.pipeline import get_historical_snapshot, download_full_history
    from agents.decision import generate_3d_factor_signals
    from analysis.holding import evaluate_holding

    if not use_factor_model:
        from agents.decision import make_decision
        from agents.runner import run_all_agents
        from agents.time_frame import run_time_frame_agents
        from agents.debate import run_debate

    # 确保数据已缓存（download_full_history 返回正确的缓存路径）
    cache_path = download_full_history(symbol, ndays=800)
    df_full = pd.read_csv(cache_path)
    df_full['date'] = pd.to_datetime(df_full['date']).dt.date

    if isinstance(start_date, str):
        start_date = datetime.strptime(start_date, "%Y-%m-%d").date()
    if isinstance(end_date, str):
        end_date = datetime.strptime(end_date, "%Y-%m-%d").date()

    # 获取回测区间的所有交易日
    mask = (df_full['date'] >= start_date) & (df_full['date'] <= end_date)
    trading_days = sorted(df_full[mask]['date'].unique())
    if len(trading_days) < 10:
        raise ValueError(f"回测区间交易日不足: {len(trading_days)}天")

    # ── 初始化 ──
    cash = initial_capital
    # 三线独立持仓: {timeframe: {"entry_price": float, "quantity": int, "peak_price": float}}
    positions = {tf: None for tf in ["short_term", "mid_term", "long_term"]}
    trade_log = []
    equity_curve = []
    total_equity = initial_capital
    max_dd_so_far = 0.0
    peak_equity = initial_capital

    print(f"\n{'='*70}")
    print(f"  回测开始: {symbol} {start_date} → {end_date} ({len(trading_days)} 交易日)")
    print(f"  初始资金: ¥{initial_capital:,.0f}")
    print(f"{'='*70}")

    # ── 逐日推进 ──
    for day_idx, trade_date in enumerate(trading_days):
        if day_idx < 20:  # 前20天用于预热指标
            price_row = df_full[df_full['date'] == trade_date]
            if not price_row.empty:
                current_price = float(price_row['close'].iloc[0])
                equity_curve.append({
                    "date": str(trade_date),
                    "equity": total_equity,
                    "cash": cash,
                    "price": current_price,
                })
            continue

        try:
            # 1. 获取当日历史快照
            data = get_historical_snapshot(symbol, trade_date)
            current_price = data["quote"]["price"]
        except Exception as e:
            # 数据不足时跳过
            equity_curve.append({
                "date": str(trade_date),
                "equity": total_equity,
                "cash": cash,
                "price": total_equity / initial_capital * 100 if initial_capital > 0 else 0,
            })
            continue

        try:
            # 2. 运行分析
            if use_factor_model:
                portfolio_ctx = {
                    "total_equity": total_equity,
                    "cash": cash,
                    "initial_capital": initial_capital,
                    "drawdown_pct": max_dd_so_far,
                    "consecutive_losses": _count_consecutive_losses(trade_log),
                    "positions": {tf: p for tf, p in positions.items() if p},
                }
                decision = generate_3d_factor_signals(symbol, data, portfolio_ctx)
            else:
                reports = run_all_agents(data, use_mock=True)
                time_opinions = run_time_frame_agents(data, reports, use_mock=True)
                debate = run_debate(reports, use_mock=True, time_frame_opinions=time_opinions)
                decision = make_decision(data, reports, debate, use_mock=True,
                                         time_frame_opinions=time_opinions)
        except Exception:
            # 分析失败，仅更新持仓市值
            total_equity = cash + sum(
                positions[tf].get("quantity", 0) * current_price if positions[tf] else 0
                for tf in positions
            )
            equity_curve.append({
                "date": str(trade_date),
                "equity": total_equity,
                "cash": cash,
                "price": current_price,
            })
            continue

        # 3. 三线各自交易决策
        for tf_key in ["short_term", "mid_term", "long_term"]:
            dim = decision.get(tf_key, {})
            action = dim.get("action", "HOLD")
            pos_pct = dim.get("position_pct", 0)
            entry_price_sig = dim.get("entry_price", current_price)
            stop_loss = dim.get("stop_loss_price")
            pos = positions[tf_key]

            # 3a. 评估现有持仓
            if pos is not None:
                pos_data_for_eval = dict(data)
                hold_eval = evaluate_holding(
                    symbol, pos["entry_price"], current_price,
                    pos["quantity"], tf_key, pos_data_for_eval,
                    peak_price=pos.get("peak_price", current_price)
                )

                # 执行持仓管理
                if hold_eval["action"] == "CLOSE":
                    sell_qty = pos["quantity"]
                    sell_value = sell_qty * current_price
                    commission = sell_value * COMMISSION_RATE
                    stamp = sell_value * STAMP_TAX_RATE
                    slippage = sell_value * SLIPPAGE_RATE
                    net_proceeds = sell_value - commission - stamp - slippage
                    cash += net_proceeds

                    cost = pos["entry_price"] * pos["quantity"]
                    pnl = net_proceeds - cost
                    pnl_pct = (current_price / pos["entry_price"] - 1) * 100

                    trade_log.append({
                        "date": str(trade_date),
                        "timeframe": tf_key,
                        "action": "CLOSE",
                        "price": current_price,
                        "quantity": sell_qty,
                        "amount": round(sell_value, 2),
                        "pnl": round(pnl, 2),
                        "pnl_pct": round(pnl_pct, 2),
                        "reason": "; ".join(hold_eval["reasons"][:2]),
                    })
                    positions[tf_key] = None

                elif hold_eval["action"] == "TRIM":
                    ratio = hold_eval["ratio"] / 100
                    sell_qty = int(pos["quantity"] * ratio / 100) * 100
                    if sell_qty > 0:
                        sell_value = sell_qty * current_price
                        commission = sell_value * COMMISSION_RATE
                        stamp = sell_value * STAMP_TAX_RATE
                        slippage = sell_value * SLIPPAGE_RATE
                        net_proceeds = sell_value - commission - stamp - slippage
                        cash += net_proceeds

                        cost_part = pos["entry_price"] * sell_qty
                        pnl = net_proceeds - cost_part

                        trade_log.append({
                            "date": str(trade_date),
                            "timeframe": tf_key,
                            "action": "TRIM",
                            "price": current_price,
                            "quantity": sell_qty,
                            "amount": round(sell_value, 2),
                            "pnl": round(pnl, 2),
                            "pnl_pct": round((current_price / pos["entry_price"] - 1) * 100, 2),
                            "reason": "; ".join(hold_eval["reasons"][:2]),
                        })
                        pos["quantity"] -= sell_qty
                        if pos["quantity"] <= 0:
                            positions[tf_key] = None

                elif hold_eval["action"] == "ADD":
                    ratio = hold_eval["ratio"] / 100
                    add_qty = int(pos["quantity"] * ratio / 100) * 100
                    if add_qty > 0:
                        buy_value = add_qty * current_price
                        commission = buy_value * COMMISSION_RATE
                        slippage = buy_value * SLIPPAGE_RATE
                        total_cost = buy_value + commission + slippage
                        if total_cost <= cash:
                            cash -= total_cost
                            old_qty = pos["quantity"]
                            old_cost = pos["entry_price"] * old_qty
                            new_total_qty = old_qty + add_qty
                            pos["entry_price"] = round((old_cost + total_cost) / new_total_qty, 4)
                            pos["quantity"] = new_total_qty

                            trade_log.append({
                                "date": str(trade_date),
                                "timeframe": tf_key,
                                "action": "ADD",
                                "price": current_price,
                                "quantity": add_qty,
                                "amount": round(buy_value, 2),
                                "pnl": 0,
                                "pnl_pct": 0,
                                "reason": "; ".join(hold_eval["reasons"][:2]),
                            })

                # 更新峰值价格
                if pos is not None:
                    if current_price > pos.get("peak_price", current_price):
                        pos["peak_price"] = current_price

            # 3b. 开新仓
            elif action in ("BUY", "CAUTIOUS_BUY") and pos is None:
                # 中长线周线多时间框架共振过滤
                if tf_key in ("mid_term", "long_term"):
                    trend_state = data.get("trend_state", {}).get("trend_state", "SIDEWAYS")
                    weekly_data = data.get("weekly_trend", {})
                    weekly_state = weekly_data.get("weekly_trend", "UNKNOWN") if weekly_data else "UNKNOWN"

                    if trend_state == "BEAR":
                        continue  # BEAR趋势不开中长线新仓
                    if weekly_state != "UP":
                        continue  # 周线不共振不开中长线新仓

                    # 长线额外：基本面评分检查（历史回测模式数据不可用时跳过）
                    if tf_key == "long_term":
                        fin = data.get("financial", {})
                        has_fundamental = any(
                            fin.get(k) is not None
                            for k in ("pe", "pb", "roe", "net_profit_growth", "revenue_growth", "debt_ratio")
                        )
                        if has_fundamental:
                            fundamental_score = 0
                            for r in reports:
                                if r.get("agent") == "fundamental_analyst":
                                    fundamental_score = r.get("score", 0)
                                    break
                            if fundamental_score < 4:
                                continue

                # 计算仓位
                max_buy_value = initial_capital * min(pos_pct, 18) / 100
                if tf_key == "mid_term":
                    max_buy_value = initial_capital * min(pos_pct, 30) / 100
                elif tf_key == "long_term":
                    max_buy_value = initial_capital * min(pos_pct, 45) / 100

                max_buy_value = min(max_buy_value, cash * 0.95)  # 留5%现金
                if max_buy_value >= current_price * 100:  # 至少1手
                    buy_qty = int(max_buy_value / current_price / 100) * 100
                    if buy_qty > 0:
                        buy_value = buy_qty * current_price
                        commission = buy_value * COMMISSION_RATE
                        slippage = buy_value * SLIPPAGE_RATE
                        total_cost = buy_value + commission + slippage
                        if total_cost <= cash:
                            cash -= total_cost
                            positions[tf_key] = {
                                "entry_price": round(total_cost / buy_qty, 4),
                                "quantity": buy_qty,
                                "peak_price": current_price,
                                "open_date": str(trade_date),
                                "stop_loss": stop_loss,
                            }
                            trade_log.append({
                                "date": str(trade_date),
                                "timeframe": tf_key,
                                "action": "BUY",
                                "price": current_price,
                                "quantity": buy_qty,
                                "amount": round(buy_value, 2),
                                "pnl": 0,
                                "pnl_pct": 0,
                                "reason": dim.get("rationale", "")[:80],
                            })

        # 4. 更新总权益
        position_value = 0
        for tf_key in positions:
            if positions[tf_key]:
                position_value += positions[tf_key]["quantity"] * current_price
        total_equity = cash + position_value

        if total_equity > peak_equity:
            peak_equity = total_equity
        dd = (peak_equity - total_equity) / peak_equity * 100 if peak_equity > 0 else 0
        max_dd_so_far = max(max_dd_so_far, dd)

        equity_curve.append({
            "date": str(trade_date),
            "equity": round(total_equity, 2),
            "cash": round(cash, 2),
            "price": current_price,
        })

        # 进度显示
        if (day_idx + 1) % 50 == 0 or day_idx == len(trading_days) - 1:
            ret = (total_equity / initial_capital - 1) * 100
            print(f"  [{day_idx+1}/{len(trading_days)}] {trade_date} "
                  f"Equity=¥{total_equity:,.0f} ({ret:+.1f}%) "
                  f"Pos={sum(1 for p in positions.values() if p)}/3")

    # ── 最终清算：平掉所有持仓 ──
    final_date = trading_days[-1]
    for tf_key in ["short_term", "mid_term", "long_term"]:
        pos = positions[tf_key]
        if pos is not None:
            final_price = float(df_full[df_full['date'] == final_date]['close'].iloc[0]) if len(df_full[df_full['date'] == final_date]) > 0 else current_price
            sell_value = pos["quantity"] * final_price
            commission = sell_value * COMMISSION_RATE
            stamp = sell_value * STAMP_TAX_RATE
            slippage = sell_value * SLIPPAGE_RATE
            net_proceeds = sell_value - commission - stamp - slippage
            cash += net_proceeds

            cost = pos["entry_price"] * pos["quantity"]
            pnl = net_proceeds - cost

            trade_log.append({
                "date": str(final_date),
                "timeframe": tf_key,
                "action": "CLOSE_FINAL",
                "price": final_price,
                "quantity": pos["quantity"],
                "amount": round(sell_value, 2),
                "pnl": round(pnl, 2),
                "pnl_pct": round((final_price / pos["entry_price"] - 1) * 100, 2),
                "reason": "回测结束强制平仓",
            })
            positions[tf_key] = None

    total_equity = cash

    # ── 计算指标 ──
    metrics = calc_metrics(trade_log, equity_curve, initial_capital)

    print(f"\n  ── 回测完成 ──")
    print(f"  最终权益: ¥{total_equity:,.0f}")
    print(f"  总收益率: {metrics['total_return_pct']:+.2f}%")
    print(f"  最大回撤: {metrics['max_drawdown_pct']:.2f}%")
    print(f"  夏普比率: {metrics['sharpe_ratio']:.2f}")
    print(f"  胜率:     {metrics['win_rate_pct']:.1f}%")
    print(f"  三线达成率: 短{metrics['achievement_short']:.0f}% 中{metrics['achievement_mid']:.0f}% 长{metrics['achievement_long']:.0f}%")

    return {
        "symbol": symbol,
        "start_date": str(start_date),
        "end_date": str(end_date),
        "initial_capital": initial_capital,
        "final_equity": round(total_equity, 2),
        "trade_log": trade_log,
        "equity_curve": equity_curve,
        "metrics": metrics,
    }


def calc_metrics(trade_log: list[dict], equity_curve: list[dict],
                 initial_capital: float = 100000.0) -> dict:
    """
    从交易日志和权益曲线计算回测指标。
    """
    if not equity_curve:
        return {"error": "无权益数据"}

    # 权益序列
    equities = [e["equity"] for e in equity_curve]
    final_equity = equities[-1]
    total_return_pct = (final_equity / initial_capital - 1) * 100

    # 最大回撤
    peak = equities[0]
    max_dd = 0.0
    for eq in equities:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak * 100
        if dd > max_dd:
            max_dd = dd

    # 日收益率序列
    daily_returns = []
    for i in range(1, len(equities)):
        if equities[i-1] > 0:
            daily_returns.append((equities[i] - equities[i-1]) / equities[i-1])

    # 夏普比率（年化，无风险利率2%）
    rf_daily = 0.02 / 252
    if daily_returns and np.std(daily_returns) > 0:
        excess = np.mean(daily_returns) - rf_daily
        sharpe = np.sqrt(252) * excess / np.std(daily_returns)
    else:
        sharpe = 0.0

    # 胜率
    closed_trades = [t for t in trade_log if t["action"] in ("CLOSE", "CLOSE_FINAL", "TRIM")]
    win_trades = [t for t in closed_trades if t["pnl"] > 0]
    win_rate = len(win_trades) / len(closed_trades) * 100 if closed_trades else 0

    # 盈亏比
    if win_trades:
        avg_win = np.mean([t["pnl"] for t in win_trades])
        loss_trades = [t for t in closed_trades if t["pnl"] <= 0]
        avg_loss = abs(np.mean([t["pnl"] for t in loss_trades])) if loss_trades else 1
        profit_factor = avg_win / avg_loss if avg_loss > 0 else 999
    else:
        profit_factor = 0

    # 三线达成率
    achievement = {}
    for tf_key in ["short_term", "mid_term", "long_term"]:
        tf_trades = [t for t in closed_trades if t["timeframe"] == tf_key and t["pnl"] != 0]
        target = TIMEFRAME_TARGET[tf_key]
        if tf_trades:
            achieved = [t for t in tf_trades if t["pnl_pct"] >= target]
            achievement[tf_key] = len(achieved) / len(tf_trades) * 100
        else:
            achievement[tf_key] = 0

    # 各线收益率
    tf_returns = {}
    for tf_key in ["short_term", "mid_term", "long_term"]:
        tf_trades = [t for t in closed_trades if t["timeframe"] == tf_key]
        if tf_trades:
            tf_pnl = sum(t["pnl"] for t in tf_trades)
            tf_turnover = sum(abs(t["amount"]) for t in tf_trades)
            tf_returns[tf_key] = round(tf_pnl / max(tf_turnover, 1) * 100, 2)
        else:
            tf_returns[tf_key] = 0

    return {
        "total_return_pct": round(total_return_pct, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "sharpe_ratio": round(sharpe, 2),
        "win_rate_pct": round(win_rate, 1),
        "profit_factor": round(profit_factor, 2),
        "total_trades": len(closed_trades),
        "win_trades": len(win_trades),
        "loss_trades": len(closed_trades) - len(win_trades),
        "achievement_short": round(achievement.get("short_term", 0), 1),
        "achievement_mid": round(achievement.get("mid_term", 0), 1),
        "achievement_long": round(achievement.get("long_term", 0), 1),
        "return_short": tf_returns.get("short_term", 0),
        "return_mid": tf_returns.get("mid_term", 0),
        "return_long": tf_returns.get("long_term", 0),
        "final_equity": round(final_equity, 2),
        "annualized_return": round(total_return_pct / max(len(equity_curve), 1) * 252, 2),
    }


def format_results(result: dict) -> str:
    """格式化回测结果为可读表格字符串。"""
    m = result.get("metrics", {})
    tf_log = result.get("trade_log", [])

    lines = []
    lines.append(f"\n{'='*70}")
    lines.append(f"  回测报告: {result['symbol']} | {result['start_date']} → {result['end_date']}")
    lines.append(f"{'='*70}")

    # 核心指标
    lines.append(f"\n  ┌─────────────────────────────────────────────┐")
    lines.append(f"  │  初始资金: ¥{result['initial_capital']:>12,.0f}                │")
    lines.append(f"  │  最终权益: ¥{result['final_equity']:>12,.0f}                │")
    lines.append(f"  │  总收益率: {m.get('total_return_pct', 0):>+12.2f}%                │")
    lines.append(f"  │  年化收益: {m.get('annualized_return', 0):>+12.2f}%                │")
    lines.append(f"  │  最大回撤: {m.get('max_drawdown_pct', 0):>12.2f}%                │")
    lines.append(f"  │  夏普比率: {m.get('sharpe_ratio', 0):>12.2f}                  │")
    lines.append(f"  └─────────────────────────────────────────────┘")

    # 交易统计
    lines.append(f"\n  ┌─────────────────────────────────────────────┐")
    lines.append(f"  │  总交易: {m.get('total_trades', 0):>4d}  胜: {m.get('win_trades', 0):>3d}  负: {m.get('loss_trades', 0):>3d}         │")
    lines.append(f"  │  胜率:   {m.get('win_rate_pct', 0):>6.1f}%  盈亏比: {m.get('profit_factor', 0):>6.2f}              │")
    lines.append(f"  └─────────────────────────────────────────────┘")

    # 三线达成率
    lines.append(f"\n  ┌─────────────────────────────────────────────┐")
    lines.append(f"  │  三线收益目标达成率                          │")
    lines.append(f"  │  短线 ≥10%:  {m.get('achievement_short', 0):>6.1f}%  (收益: {m.get('return_short', 0):>+.2f}%)  │")
    lines.append(f"  │  中线 ≥30%:  {m.get('achievement_mid', 0):>6.1f}%  (收益: {m.get('return_mid', 0):>+.2f}%)  │")
    lines.append(f"  │  长线 ≥100%: {m.get('achievement_long', 0):>6.1f}%  (收益: {m.get('return_long', 0):>+.2f}%)  │")
    lines.append(f"  └─────────────────────────────────────────────┘")

    # 交易明细（最近10条）
    lines.append(f"\n  ── 交易明细 (最近10条) ──")
    lines.append(f"  {'日期':<12} {'维度':<6} {'操作':<10} {'价格':>8} {'数量':>6} {'盈亏':>10} {'盈亏%':>8}")
    lines.append(f"  {'-'*60}")
    for t in tf_log[-10:]:
        lines.append(f"  {t['date']:<12} {t['timeframe']:<6} {t['action']:<10} "
                     f"{t['price']:>8.2f} {t['quantity']:>6d} "
                     f"{t['pnl']:>+10.2f} {t['pnl_pct']:>+7.2f}%")

    lines.append(f"\n{'='*70}\n")
    return "\n".join(lines)


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')

    import argparse
    parser = argparse.ArgumentParser(description='历史回测引擎')
    parser.add_argument('symbol', help='股票代码')
    parser.add_argument('--start', default=None, help='起始日期 YYYY-MM-DD')
    parser.add_argument('--end', default=None, help='结束日期 YYYY-MM-DD')
    parser.add_argument('--time_frame', default='mid', help='时间维度 short/mid/long')
    parser.add_argument('--days', type=int, default=120, help='随机回测天数')
    parser.add_argument('--seed', type=int, default=None, help='随机种子')
    parser.add_argument('--capital', type=float, default=100000, help='初始资金')
    parser.add_argument('--llm', action='store_true', help='使用LLM Agent管线(默认因子模型)')
    args = parser.parse_args()

    if args.start and args.end:
        result = run_backtest(args.symbol, args.start, args.end, args.capital,
                              use_factor_model=not args.llm)
    else:
        period = random_period_test(args.symbol, args.time_frame, args.days, args.seed)
        print(f"\n  随机时段: {period['start_date']} → {period['end_date']} "
              f"({period['days']}天, seed={period['seed']})")
        result = run_backtest(args.symbol, period['start_date'], period['end_date'],
                              args.capital, use_factor_model=not args.llm)

    print(format_results(result))
