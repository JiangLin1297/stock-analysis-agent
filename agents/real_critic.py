"""
实盘 Critic 引擎 — 基于真实交易结果自动优化策略参数。
不依赖历史回测，直接从 real_trades.json 读取实盘数据。

用法:
    from agents.real_critic import trigger_real_critic
    result = trigger_real_critic()
"""

import os
import sys
import json
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Windows GBK 兼容：强制 stdout/stderr 使用 UTF-8
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

# ═══════════════════════════════════════════════════════════════
# 熔断规则（硬性约束，不可被 Critic 修改）
# ═══════════════════════════════════════════════════════════════
CIRCUIT_BREAKERS = {
    "single_stock_max_pct": 25,       # 单票仓位上限 25%
    "total_position_max_pct": 80,     # 总仓位上限 80%
    "consecutive_losses_pause": 3,    # 连续 3 笔亏损 → 暂停
    "pause_days": 3,                  # 暂停 3 天
    "max_drawdown_close_pct": 15,     # 回撤 > 15% → 清仓短线中线
}

# Critic 可修改的参数范围
CRITIC_MODIFIABLE = {
    "factor_weights": ["short.threshold", "mid.threshold", "long.threshold"],
    "decision.py": ["base_pos", "ATR_STOP_*", "COOLDOWN_DAYS"],
    "holding.py": ["ATR_STOP_*"],
}


def _project_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    log_path = os.path.join(_project_root(), "real_evolution_log.txt")
    try:
        with open(log_path, 'a', encoding='utf-8') as f:
            f.write(line + "\n")
    except Exception:
        pass


def _load_real_trades() -> list:
    trades_path = os.path.join(_project_root(), "portfolio", "real_trades.json")
    if not os.path.exists(trades_path):
        return []
    try:
        with open(trades_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, Exception):
        return []


def _load_analysis_history(days: int = 30) -> list:
    """从数据库加载最近 N 天的深度分析记录。"""
    try:
        from data.database import get_analysis_history
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00")
        records = get_analysis_history(type_="深度分析", limit=200)
        return [r for r in records if r.get("created_at", "") >= cutoff]
    except Exception:
        return []


def _evaluate_signal_quality(records: list) -> dict:
    """评估历史分析信号质量：对比 BUY 信号发出后 5日/20日 的实际涨跌幅。

    Returns:
        {
            "total_signals": int,
            "buy_signals": int,
            "hit_rate_5d": float,  # 5日正收益占比
            "hit_rate_20d": float, # 20日正收益占比
            "avg_return_5d": float,
            "avg_return_20d": float,
            "quality_score": float, # 0~10
            "details": list,
        }
    """
    if not records:
        return {"total_signals": 0, "buy_signals": 0, "quality_score": 5.0,
                "hit_rate_5d": 0, "hit_rate_20d": 0,
                "avg_return_5d": 0, "avg_return_20d": 0, "details": []}

    details = []
    buy_count = 0
    hit_5d = 0
    hit_20d = 0
    returns_5d = []
    returns_20d = []

    for rec in records:
        sym = rec.get("symbol", "")
        result = rec.get("result", {})
        created = rec.get("created_at", "")[:10]
        decision = result.get("final_decision", result)

        # 检查三线信号
        for tf_key in ["short_term", "mid_term", "long_term"]:
            dim = decision.get(tf_key, {})
            action = str(dim.get("action", "HOLD")).upper()
            if action not in ("BUY", "STRONG_BUY", "CAUTIOUS_BUY"):
                continue

            buy_count += 1
            score = dim.get("_factor_score", dim.get("score", 0))

            # 获取后续价格验证
            try:
                from data.database import load_kline
                import pandas as _pd
                klines = load_kline(sym, start_date=created)
                if klines is None or len(klines) < 3:
                    continue
                close_prices = klines["close"].values
                if len(close_prices) < 2:
                    continue

                base_price = close_prices[0]
                ret_5d = (close_prices[min(5, len(close_prices)-1)] / base_price - 1) * 100 if len(close_prices) > 1 else 0
                ret_20d = (close_prices[min(20, len(close_prices)-1)] / base_price - 1) * 100 if len(close_prices) > 1 else 0

                returns_5d.append(ret_5d)
                returns_20d.append(ret_20d)

                if ret_5d > 0:
                    hit_5d += 1
                if ret_20d > 0:
                    hit_20d += 1

                details.append({
                    "symbol": sym, "date": created, "timeframe": tf_key,
                    "score": score, "return_5d": round(ret_5d, 2),
                    "return_20d": round(ret_20d, 2),
                })
            except Exception:
                pass

    total_evaluated = len(returns_5d)
    hit_rate_5d = (hit_5d / total_evaluated * 100) if total_evaluated > 0 else 0
    hit_rate_20d = (hit_20d / total_evaluated * 100) if total_evaluated > 0 else 0
    avg_ret_5d = (sum(returns_5d) / total_evaluated) if total_evaluated > 0 else 0
    avg_ret_20d = (sum(returns_20d) / total_evaluated) if total_evaluated > 0 else 0

    # 质量评分: 0~10
    # 基于命中率和平均收益
    if total_evaluated == 0:
        quality_score = 5.0
    else:
        score_hit = hit_rate_5d / 10  # 0~10
        score_ret = max(0, min(10, 5 + avg_ret_5d))  # -5%~+5% → 0~10
        quality_score = (score_hit * 0.6 + score_ret * 0.4)
        quality_score = round(max(0, min(10, quality_score)), 1)

    return {
        "total_signals": len(details),
        "buy_signals": buy_count,
        "evaluated": total_evaluated,
        "hit_rate_5d": round(hit_rate_5d, 1),
        "hit_rate_20d": round(hit_rate_20d, 1),
        "avg_return_5d": round(avg_ret_5d, 2),
        "avg_return_20d": round(avg_ret_20d, 2),
        "quality_score": quality_score,
        "details": details[:20],  # 最多返回20条详情
    }


def _load_positions() -> dict:
    pos_path = os.path.join(_project_root(), "portfolio", "positions.json")
    if not os.path.exists(pos_path):
        return {"holdings": [], "total_cash": 100000.0}
    try:
        with open(pos_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, Exception):
        return {"holdings": [], "total_cash": 100000.0}


def _get_current_prices(symbols: list) -> dict:
    """获取当前市价。失败时返回空字典。"""
    prices = {}
    try:
        from data.pipeline import get_compressed_data
        for sym in symbols:
            try:
                data = get_compressed_data(sym)
                p = float(data.get("quote", {}).get("price", 0) or 0)
                if p == 0:
                    p = float(data.get("technical", {}).get("close", 0) or 0)
                if p > 0:
                    prices[sym] = p
            except Exception:
                pass
    except Exception:
        pass
    return prices


def _compute_real_metrics(trades: list) -> dict:
    """从实盘交易记录计算回测等价指标（含 mark-to-market）。"""
    if not trades:
        return {}

    sell_trades = [t for t in trades if t.get("action", "").upper() == "SELL"]
    buy_trades = [t for t in trades if t.get("action", "").upper() == "BUY"]
    closed_sells = [t for t in sell_trades if float(t.get("pnl", 0)) != 0]

    # 已平仓交易统计
    total_closed = len(closed_sells)
    win_trades = [t for t in closed_sells if float(t.get("pnl", 0)) > 0]
    loss_trades = [t for t in closed_sells if float(t.get("pnl", 0)) < 0]

    total_realized_pnl = sum(float(t.get("pnl", 0)) for t in closed_sells)
    # 胜率：无平仓记录时为 -1（表示 N/A）
    win_rate = len(win_trades) / total_closed * 100 if total_closed > 0 else -1

    avg_win = (sum(float(t.get("pnl", 0)) for t in win_trades) / len(win_trades)
               if win_trades else 0)
    avg_loss = (abs(sum(float(t.get("pnl", 0)) for t in loss_trades)) / len(loss_trades)
                if loss_trades else 0)
    profit_factor = avg_win / avg_loss if avg_loss > 0 else (999 if avg_win > 0 else 0)

    # Mark-to-market: 用当前市价估值持仓
    positions = _load_positions()
    total_cash = float(positions.get("total_cash", 100000))
    holdings = positions.get("holdings", [])
    symbols = list(set(h["symbol"] for h in holdings))
    current_prices = _get_current_prices(symbols)

    cost_basis = 0.0
    market_value = 0.0
    unrealized_pnl = 0.0
    for h in holdings:
        entry = float(h.get("entry_price", 0))
        qty = int(h.get("quantity", 0))
        sym = h["symbol"]
        current = current_prices.get(sym, entry)  # 无市价时用成本价
        cost_basis += entry * qty
        market_value += current * qty
        unrealized_pnl += (current - entry) * qty

    total_assets = total_cash + market_value

    # 总投入资本 = 持仓成本 + 当前现金（即用户实际投入的钱）
    invested_capital = cost_basis + total_cash
    total_return_pct = ((total_assets - invested_capital) / invested_capital * 100
                        if invested_capital > 0 else 0)

    # 总交易笔数（买入+卖出全部计入）
    total_trades = len(trades)

    # 最大回撤：基于实际投入资本和已实现盈亏构建 equity curve
    max_dd = 0.0
    peak = invested_capital
    running = invested_capital
    for t in sell_trades:
        running += float(t.get("pnl", 0))
        if running > peak:
            peak = running
        dd = (peak - running) / peak * 100 if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd
    # 加入当前总资产对回撤的影响
    if total_assets > peak:
        peak = total_assets
    dd_now = (peak - total_assets) / peak * 100 if peak > 0 else 0
    max_dd = max(max_dd, dd_now)

    # 三线达成率
    targets = {"short": 10, "mid": 30, "long": 100}
    achievement = {}
    for tf in ["short", "mid", "long"]:
        tf_trades = [t for t in closed_sells if t.get("time_frame") == tf]
        if tf_trades:
            target = targets[tf]
            achieved = [t for t in tf_trades
                        if (float(t.get("price", 0)) / max(float(t.get("entry_price", 1)), 0.01) - 1) * 100 >= target]
            achievement[f"achievement_{tf}"] = len(achieved) / len(tf_trades) * 100
        else:
            achievement[f"achievement_{tf}"] = 0

    return {
        "total_return_pct": round(total_return_pct, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "sharpe_ratio": 0.0,
        "win_rate_pct": round(win_rate, 1),
        "profit_factor": round(profit_factor, 2),
        "total_trades": total_trades,
        "win_trades": len(win_trades),
        "loss_trades": len(loss_trades),
        "total_realized_pnl": round(total_realized_pnl, 2),
        "unrealized_pnl": round(unrealized_pnl, 2),
        **achievement,
        "final_equity": round(total_assets, 2),
        "invested_capital": round(invested_capital, 2),
        "annualized_return": 0.0,
    }


def _check_circuit_breakers(metrics: dict, trades: list) -> list:
    """检查熔断规则，返回触发的熔断动作列表。"""
    actions = []

    positions = _load_positions()
    total_cash = float(positions.get("total_cash", 100000))
    holdings = positions.get("holdings", [])
    symbols = list(set(h["symbol"] for h in holdings))
    current_prices = _get_current_prices(symbols)
    total_assets = total_cash + sum(
        float(current_prices.get(h["symbol"], h.get("entry_price", 0))) * int(h.get("quantity", 0))
        for h in holdings
    )

    # 单票仓位检查
    for h in holdings:
        current = current_prices.get(h["symbol"], float(h.get("entry_price", 0)))
        value = current * int(h.get("quantity", 0))
        pct = value / total_assets * 100 if total_assets > 0 else 0
        if pct > CIRCUIT_BREAKERS["single_stock_max_pct"]:
            actions.append({
                "type": "single_stock_overweight",
                "symbol": h["symbol"],
                "pct": round(pct, 1),
                "limit": CIRCUIT_BREAKERS["single_stock_max_pct"],
                "action": f"单票{h['symbol']}仓位{pct:.1f}%超过{CIRCUIT_BREAKERS['single_stock_max_pct']}%上限",
            })

    # 总仓位检查
    market_value = sum(
        float(current_prices.get(h["symbol"], h.get("entry_price", 0))) * int(h.get("quantity", 0))
        for h in holdings
    )
    position_pct = market_value / total_assets * 100 if total_assets > 0 else 0
    if position_pct > CIRCUIT_BREAKERS["total_position_max_pct"]:
        actions.append({
            "type": "total_position_overweight",
            "pct": round(position_pct, 1),
            "limit": CIRCUIT_BREAKERS["total_position_max_pct"],
            "action": f"总仓位{position_pct:.1f}%超过{CIRCUIT_BREAKERS['total_position_max_pct']}%上限",
        })

    # 连续亏损检查
    sell_trades = sorted(
        [t for t in trades if t.get("action", "").upper() == "SELL"],
        key=lambda x: x.get("date", ""), reverse=True
    )
    consec_losses = 0
    for t in sell_trades:
        if float(t.get("pnl", 0)) <= 0:
            consec_losses += 1
        else:
            break
    if consec_losses >= CIRCUIT_BREAKERS["consecutive_losses_pause"]:
        actions.append({
            "type": "consecutive_losses",
            "count": consec_losses,
            "action": f"连续{consec_losses}笔亏损，暂停新开仓{CIRCUIT_BREAKERS['pause_days']}天",
        })

    # 总资产回撤检查
    max_dd = metrics.get("max_drawdown_pct", 0)
    if max_dd > CIRCUIT_BREAKERS["max_drawdown_close_pct"]:
        actions.append({
            "type": "max_drawdown_breach",
            "drawdown": round(max_dd, 1),
            "limit": CIRCUIT_BREAKERS["max_drawdown_close_pct"],
            "action": f"回撤{max_dd:.1f}%超过{CIRCUIT_BREAKERS['max_drawdown_close_pct']}%，应清仓短线和中线",
        })

    return actions


def _build_backtest_result(trades: list, metrics: dict) -> dict:
    """将实盘交易转换为 critique_backtest 兼容的格式。"""
    trade_log = []
    for t in trades:
        action = t.get("action", "").upper()
        if action == "BUY":
            trade_log.append({
                "date": t.get("date", ""),
                "timeframe": t.get("time_frame", "short") + "_term",
                "action": "BUY",
                "price": float(t.get("price", 0)),
                "quantity": int(t.get("quantity", 0)),
                "amount": float(t.get("price", 0)) * int(t.get("quantity", 0)),
                "pnl": 0,
                "pnl_pct": 0,
                "reason": t.get("reason", ""),
            })
        elif action == "SELL":
            entry = float(t.get("entry_price", t.get("price", 0)))
            price = float(t.get("price", 0))
            qty = int(t.get("quantity", 0))
            pnl = float(t.get("pnl", 0))
            pnl_pct = ((price / entry) - 1) * 100 if entry > 0 else 0
            trade_log.append({
                "date": t.get("date", ""),
                "timeframe": t.get("time_frame", "short") + "_term",
                "action": "CLOSE",
                "price": price,
                "quantity": qty,
                "amount": price * qty,
                "pnl": pnl,
                "pnl_pct": round(pnl_pct, 2),
                "reason": t.get("reason", ""),
            })

    return {
        "symbol": "实盘组合",
        "start_date": trades[0].get("date", "?") if trades else "?",
        "end_date": trades[-1].get("date", "?") if trades else "?",
        "initial_capital": metrics.get("invested_capital", 100000.0),
        "final_equity": metrics.get("final_equity", 100000),
        "trade_log": trade_log,
        "equity_curve": [],
        "metrics": metrics,
    }


def _cross_stock_gene_analysis() -> list:
    """跨股票基因分析：从 stock_genes + signal_quality 学习，优化因子权重。

    读取所有置信度>60的基因档案，结合信号表现数据，分析哪些基因特征与正收益相关，
    输出因子权重调整建议并自动修改 factor_weights.json。
    """
    try:
        from data.database import get_all_stock_genes, get_signal_performance, get_signal_accuracy
    except Exception as e:
        _log(f"基因分析跳过: {e}")
        return []

    genes = get_all_stock_genes(min_confidence=60)
    if len(genes) < 3:
        _log(f"基因样本不足({len(genes)}个，需≥3)，跳过基因分析")
        return []

    _log(f"加载 {len(genes)} 个高置信度基因档案")

    # 分析基因维度与信号收益的相关性
    signals = get_signal_performance(limit=500)
    if len(signals) < 10:
        _log(f"信号样本不足({len(signals)}个，需≥10)，跳过基因分析")
        return []

    # 按基因维度分组统计收益
    dimension_stats = {}
    for gene in genes:
        sym = gene.get("symbol", "")
        sym_signals = [s for s in signals if s.get("symbol") == sym and s.get("pnl_pct") is not None]
        if not sym_signals:
            continue

        avg_pnl = sum(s["pnl_pct"] for s in sym_signals) / len(sym_signals)
        win_rate = sum(1 for s in sym_signals if s["pnl_pct"] > 0) / len(sym_signals)

        for dim in ["avg_rally_strength", "avg_pullback_depth", "avg_atr_level",
                     "avg_ma60_alignment_days", "avg_false_breakout_prob"]:
            val = gene.get(dim, 0)
            if dim not in dimension_stats:
                dimension_stats[dim] = []
            dimension_stats[dim].append({
                "symbol": sym,
                "value": val,
                "avg_pnl": avg_pnl,
                "win_rate": win_rate,
                "count": len(sym_signals),
            })

    suggestions = []

    # 分析反弹强度与收益的关系
    if "avg_rally_strength" in dimension_stats:
        entries = dimension_stats["avg_rally_strength"]
        if len(entries) >= 3:
            high_rally = [e for e in entries if e["value"] > 0.5]
            low_rally = [e for e in entries if e["value"] <= 0.5]
            if high_rally and low_rally:
                high_pnl = sum(e["avg_pnl"] for e in high_rally) / len(high_rally)
                low_pnl = sum(e["avg_pnl"] for e in low_rally) / len(low_rally)
                if high_pnl > low_pnl + 2:
                    suggestions.append({
                        "dimension": "rally_strength",
                        "finding": f"高反弹({high_pnl:+.1f}%) vs 低反弹({low_pnl:+.1f}%)",
                        "action": "主力痕迹权重+5%",
                        "weight_delta": {"主力痕迹": 0.05},
                    })

    # 分析回调深度与收益的关系
    if "avg_pullback_depth" in dimension_stats:
        entries = dimension_stats["avg_pullback_depth"]
        if len(entries) >= 3:
            deep_pullback = [e for e in entries if e["value"] > 8]
            shallow_pullback = [e for e in entries if e["value"] <= 8]
            if deep_pullback and shallow_pullback:
                deep_wr = sum(e["win_rate"] for e in deep_pullback) / len(deep_pullback)
                shallow_wr = sum(e["win_rate"] for e in shallow_pullback) / len(shallow_pullback)
                if deep_wr < shallow_wr - 0.15:
                    suggestions.append({
                        "dimension": "pullback_depth",
                        "finding": f"深回调胜率({deep_wr:.0%})显著低于浅回调({shallow_wr:.0%})",
                        "action": "未来空间权重-5%，基本质量权重+5%",
                        "weight_delta": {"未来空间": -0.05, "基本质量": 0.05},
                    })

    # 分析ATR水平与收益的关系
    if "avg_atr_level" in dimension_stats:
        entries = dimension_stats["avg_atr_level"]
        if len(entries) >= 3:
            high_atr = [e for e in entries if e["value"] > 4]
            if high_atr:
                high_pnl = sum(e["avg_pnl"] for e in high_atr) / len(high_atr)
                if high_pnl < -2:
                    suggestions.append({
                        "dimension": "atr_level",
                        "finding": f"高波动股平均亏损{high_pnl:+.1f}%",
                        "action": "热点引擎权重-5%，基本质量权重+5%",
                        "weight_delta": {"热点引擎": -0.05, "基本质量": 0.05},
                    })

    # 分析假突破概率
    if "avg_false_breakout_prob" in dimension_stats:
        entries = dimension_stats["avg_false_breakout_prob"]
        if len(entries) >= 3:
            high_fb = [e for e in entries if e["value"] > 0.4]
            if high_fb:
                high_wr = sum(e["win_rate"] for e in high_fb) / len(high_fb)
                if high_wr < 0.3:
                    suggestions.append({
                        "dimension": "false_breakout",
                        "finding": f"高假突破({high_wr:.0%}胜率)需加强过滤",
                        "action": "主力痕迹权重+5%，热点引擎权重-5%",
                        "weight_delta": {"主力痕迹": 0.05, "热点引擎": -0.05},
                    })

    # 应用建议到 factor_weights.json
    if suggestions:
        _apply_gene_weight_adjustments(suggestions)

    return suggestions


def _apply_gene_weight_adjustments(suggestions: list):
    """将基因分析的权重调整建议应用到 factor_weights.json。"""
    weights_path = os.path.join(_project_root(), "analysis", "factor_weights.json")
    try:
        with open(weights_path, 'r', encoding='utf-8') as f:
            weights = json.load(f)
    except Exception as e:
        _log(f"读取 factor_weights.json 失败: {e}")
        return

    adjusted = False
    for sug in suggestions:
        delta = sug.get("weight_delta", {})
        for factor, change in delta.items():
            if factor in weights:
                old = weights[factor]
                new_val = max(0.05, min(0.5, old + change))
                weights[factor] = round(new_val, 2)
                adjusted = True
                _log(f"  因子权重调整: {factor} {old:.2f} → {weights[factor]:.2f} "
                     f"({sug['dimension']}: {sug['finding']})")

    # 归一化主因子权重
    if adjusted:
        main_keys = ["主力痕迹", "热点引擎", "未来空间", "基本质量"]
        total = sum(weights.get(k, 0) for k in main_keys)
        if total > 0:
            for k in main_keys:
                if k in weights:
                    weights[k] = round(weights[k] / total, 2)

        try:
            with open(weights_path, 'w', encoding='utf-8') as f:
                json.dump(weights, f, ensure_ascii=False, indent=2)
            _log(f"factor_weights.json 已更新 (基于{suggestions.__len__()}条基因分析)")
        except Exception as e:
            _log(f"写入 factor_weights.json 失败: {e}")


def trigger_real_critic(days: int = 30, use_mock: bool = True) -> dict:
    """
    实盘 Critic 入口：读取实盘交易 → 计算指标 → 检查熔断 → 分析 → 生成修改指令。

    Args:
        days: 分析最近 N 天的交易
        use_mock: True=本地启发式，False=调用 DeepSeek API

    Returns:
        {
            "metrics": {...},
            "circuit_breakers": [...],
            "critic_result": {...},
            "operations_applied": int,
            "skipped_threshold_changes": bool,
        }
    """
    _log("=" * 60)
    _log("实盘 Critic 引擎启动")

    # 1. 加载实盘交易
    trades = _load_real_trades()
    if not trades:
        _log("无实盘交易记录，跳过分析")
        return {"error": "无实盘交易记录", "metrics": {}, "circuit_breakers": [],
                "critic_result": {}, "operations_applied": 0}

    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    recent_trades = [t for t in trades if t.get("date", "") >= cutoff]
    if not recent_trades:
        _log(f"最近{days}天无交易记录")
        return {"error": f"最近{days}天无交易记录", "metrics": {},
                "circuit_breakers": [], "critic_result": {}, "operations_applied": 0}

    _log(f"加载 {len(recent_trades)} 笔最近交易 (共 {len(trades)} 笔)")

    # 2. 计算实盘指标
    metrics = _compute_real_metrics(recent_trades)
    _wr = metrics.get('win_rate_pct', -1)
    _wr_str = f"{_wr:.1f}%" if _wr >= 0 else "N/A(无平仓)"
    _log(f"总收益: {metrics.get('total_return_pct', 0):+.2f}% | "
         f"胜率: {_wr_str} | "
         f"最大回撤: {metrics.get('max_drawdown_pct', 0):.2f}% | "
         f"交易: {metrics.get('total_trades', 0)}笔")

    # 3. 检查熔断规则
    circuit_breakers = _check_circuit_breakers(metrics, trades)
    if circuit_breakers:
        _log(f"熔断触发: {len(circuit_breakers)} 条")
        for cb in circuit_breakers:
            _log(f"  - {cb['action']}")
    else:
        _log("熔断检查通过")

    # 4. 构建 critique_backtest 输入
    backtest_result = _build_backtest_result(recent_trades, metrics)

    # 4.5 评估历史分析信号质量
    _log("加载历史分析记录，评估信号质量...")
    analysis_records = _load_analysis_history(days=days)
    signal_quality = _evaluate_signal_quality(analysis_records)
    sq_score = signal_quality.get("quality_score", 5)
    sq_hit5 = signal_quality.get("hit_rate_5d", 0)
    sq_evaluated = signal_quality.get("evaluated", 0)
    if sq_evaluated > 0:
        _log(f"信号质量: 评分{sq_score}/10 | 5日命中率{sq_hit5:.0f}% "
             f"| 平均5日收益{signal_quality.get('avg_return_5d', 0):+.2f}% "
             f"| 共评估{sq_evaluated}个BUY信号")
    else:
        _log("无可用历史分析记录进行信号质量评估")

    # 4.6 跨股票基因分析 — 从个股基因库学习优化因子权重
    gene_suggestions = _cross_stock_gene_analysis()
    if gene_suggestions:
        _log(f"基因分析: 发现{len(gene_suggestions)}条优化建议")

    # 5. 调用 Critic 分析
    _log("调用 Critic 分析实盘表现...")
    try:
        from agents.critic import critique_backtest
        critic_result = critique_backtest(backtest_result, use_mock=use_mock)
    except Exception as e:
        _log(f"Critic 分析失败: {e}")
        critic_result = {"overall_score": 0, "main_issue": str(e),
                         "must_fix": [], "operations": []}

    score = critic_result.get("overall_score", 0)
    main_issue = critic_result.get("main_issue", "")
    _log(f"Critic 评分: {score}/10 | 主要问题: {main_issue[:80]}")

    # 6. 熔断过滤：禁止修改阈值，改为收紧仓位
    skipped_threshold = False
    operations = critic_result.get("operations", [])

    # 检查是否触发了严重熔断（回撤>8% + 胜率<15%）
    _wr_val = metrics.get("win_rate_pct", -1)
    severe_circuit = (metrics.get("max_drawdown_pct", 0) > 8
                      and _wr_val >= 0 and _wr_val < 15)

    if severe_circuit:
        _log("严重熔断触发: 禁止降阈值，改为收紧仓位上限")
        filtered_ops = []
        for op in operations:
            target = op.get("target", "")
            if "threshold" in target.lower():
                skipped_threshold = True
                _log(f"  拦截阈值修改: {op.get('file')}:{target}")
                continue
            filtered_ops.append(op)
        # 添加仓位收紧指令
        filtered_ops.append({
            "file": "agents/decision.py",
            "target": "position_pct",
            "new_value": 35,
        })
        operations = filtered_ops
        critic_result["operations"] = operations

    # 熔断约束：任何 operations 都不能突破熔断上限
    safe_ops = []
    for op in operations:
        target = op.get("target", "")
        new_val = op.get("new_value")
        if isinstance(new_val, (int, float)):
            if target == "position_pct" and new_val > CIRCUIT_BREAKERS["total_position_max_pct"]:
                _log(f"  拦截仓位超限: {target}={new_val} > {CIRCUIT_BREAKERS['total_position_max_pct']}")
                op["new_value"] = CIRCUIT_BREAKERS["total_position_max_pct"]
            if "single" in target.lower() and new_val > CIRCUIT_BREAKERS["single_stock_max_pct"]:
                _log(f"  拦截单票超限: {target}={new_val}")
                op["new_value"] = CIRCUIT_BREAKERS["single_stock_max_pct"]
        safe_ops.append(op)
    operations = safe_ops

    # 7. 执行修改
    ops_applied = 0
    if operations:
        _log(f"执行 {len(operations)} 条修改指令...")
        try:
            from evolution.improver import apply_fix
            ops_applied = apply_fix(critic_result)
            _log(f"成功应用 {ops_applied} 条修改")
        except Exception as e:
            _log(f"修改执行失败: {e}")
    else:
        _log("无修改指令")

    # 8. 保存优化记录
    _save_evolution_record(metrics, critic_result, circuit_breakers, ops_applied, signal_quality)

    result = {
        "metrics": metrics,
        "circuit_breakers": circuit_breakers,
        "critic_result": {
            "overall_score": score,
            "main_issue": main_issue,
            "must_fix": critic_result.get("must_fix", []),
            "all_issues": critic_result.get("all_issues", []),
        },
        "operations_applied": ops_applied,
        "skipped_threshold_changes": skipped_threshold,
        "signal_quality": signal_quality,
        "gene_suggestions": gene_suggestions,
    }

    _log(f"实盘 Critic 完成: 评分{score}/10 应用{ops_applied}条修改")
    _log("=" * 60)

    return result


def _save_evolution_record(metrics: dict, critic_result: dict,
                           circuit_breakers: list, ops_applied: int,
                           signal_quality: dict = None):
    """保存本次优化记录到 real_evolution_history.json。"""
    history_path = os.path.join(_project_root(), "portfolio", "real_evolution_history.json")
    try:
        if os.path.exists(history_path):
            with open(history_path, 'r', encoding='utf-8') as f:
                history = json.load(f)
        else:
            history = []
    except Exception:
        history = []

    record = {
        "timestamp": datetime.now().isoformat(),
        "metrics": metrics,
        "score": critic_result.get("overall_score", 0),
        "main_issue": critic_result.get("main_issue", ""),
        "must_fix": critic_result.get("must_fix", []),
        "circuit_breakers": circuit_breakers,
        "operations_applied": ops_applied,
        "signal_quality": signal_quality or {},
    }
    history.append(record)

    try:
        with open(history_path, 'w', encoding='utf-8') as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    except Exception as e:
        _log(f"保存优化记录失败: {e}")


def get_evolution_history() -> list:
    """获取实盘优化历史记录。"""
    history_path = os.path.join(_project_root(), "portfolio", "real_evolution_history.json")
    if not os.path.exists(history_path):
        return []
    try:
        with open(history_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return []


if __name__ == "__main__":
    result = trigger_real_critic()
    print(json.dumps(result, ensure_ascii=False, indent=2))
