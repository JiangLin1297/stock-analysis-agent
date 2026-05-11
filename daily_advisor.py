#!/usr/bin/env python3
"""
StockMind 日常投资顾问 — 每天打开后的标准工作流。
步骤1: 持仓诊断 → 步骤2: 全市场选股
启动: python daily_advisor.py
"""

import os, sys, json
from datetime import datetime, date
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils.format import section_div, card_header, card_line, card_dual, card_bottom, format_signal, format_price, format_pct

CARD_W = 78
HALF_W = 42

# ═══════════════════════════════════════════════════════════════
# 步骤1: 持仓诊断
# ═══════════════════════════════════════════════════════════════

def _diagnose_holdings(use_mock: bool = False) -> list[dict]:
    """逐只分析用户持仓，返回诊断结果列表。"""
    from portfolio.manager import load_user_portfolio
    from data.pipeline import get_compressed_data
    from agents.decision import run_full_analysis
    from analysis.holding import evaluate_holding

    pf = load_user_portfolio()
    holdings = pf.get("holdings", [])
    total_cash = pf.get("total_cash", 0)

    if not holdings:
        print("  (无持仓记录)\n")
        return []

    print(f"\n  持仓数量: {len(holdings)} | 现金: {total_cash:,.0f}\n")
    results = []

    for i, h in enumerate(holdings):
        sym = h["symbol"]
        name = h.get("name", sym)
        entry = float(h["entry_price"])
        qty = int(h["quantity"])

        print(f"  [{i+1}/{len(holdings)}] 分析 {sym} {name}...")

        try:
            # 获取实时行情
            data = get_compressed_data(sym)
            price = float(data.get("quote", {}).get("price", 0) or 0)
            if price == 0:
                price = float(data.get("technical", {}).get("close", 0) or entry)

            # 全链路分析获取三线决策
            analysis = run_full_analysis(sym, use_mock=use_mock, use_llm_pipeline=False)
            decision = analysis.get("final_decision", {})

            # 持仓动态评估 (三线综合)
            holding_advice = []
            for tf, tf_label in [("short_term", "短线"), ("mid_term", "中线"), ("long_term", "长线")]:
                try:
                    ev = evaluate_holding(sym, entry, price, qty, tf, data)
                    holding_advice.append({
                        "timeframe": tf,
                        "label": tf_label,
                        "action": ev.get("action", "HOLD"),
                        "ratio": ev.get("ratio", 0),
                        "reasons": ev.get("reasons", [])[:2],
                    })
                except Exception:
                    holding_advice.append({
                        "timeframe": tf, "label": tf_label,
                        "action": "HOLD", "ratio": 0,
                        "reasons": ["评估数据不足"],
                    })

            pnl_pct = round((price - entry) / entry * 100, 2)

            results.append({
                "symbol": sym, "name": name,
                "entry_price": entry, "current_price": price,
                "quantity": qty, "pnl_pct": pnl_pct,
                "short_signal": decision.get("short_term", {}).get("action", "?"),
                "mid_signal": decision.get("mid_term", {}).get("action", "?"),
                "long_signal": decision.get("long_term", {}).get("action", "?"),
                "short_score": decision.get("short_term", {}).get("_factor_score", 0),
                "mid_score": decision.get("mid_term", {}).get("_factor_score", 0),
                "long_score": decision.get("long_term", {}).get("_factor_score", 0),
                "holding_advice": holding_advice,
            })
            print(f"    {sym} 现价={price} 盈亏={pnl_pct:+.2f}%")
        except Exception as e:
            print(f"    {sym} 分析失败: {e}")
            results.append({
                "symbol": sym, "name": name,
                "entry_price": entry, "current_price": 0,
                "quantity": qty, "pnl_pct": 0,
                "short_signal": "?", "mid_signal": "?", "long_signal": "?",
                "holding_advice": [],
                "error": str(e),
            })

    return results


def _print_holdings_report(results: list[dict]):
    """打印持仓诊断表格。"""
    print(f"\n{section_div(' 持仓诊断报告 ')}")

    # 表头
    header = (f"{'代码':<8} {'名称':<8} {'现价':>7} {'盈亏%':>8} "
              f"{'短线':>6} {'中线':>6} {'长线':>6} {'建议':>10}")
    print(header)
    print("-" * 78)

    for r in results:
        if r.get("error"):
            print(f"{r['symbol']:<8} {r['name']:<8} {'ERROR':>7} {r['error'][:40]}")
            continue

        # 汇总建议：取最偏空的那个
        actions = [a["action"] for a in r.get("holding_advice", [])]
        if "CLOSE" in actions:
            advice = "清仓"
        elif "TRIM" in actions:
            advice = "减仓"
        elif "ADD" in actions:
            advice = "加仓"
        else:
            advice = "持有"

        pnl_str = f"{r['pnl_pct']:+.1f}%"
        print(f"{r['symbol']:<8} {r['name']:<8} {r['current_price']:>7.2f} {pnl_str:>8} "
              f"{r['short_signal']:>6} {r['mid_signal']:>6} {r['long_signal']:>6} {advice:>10}")

    print()

    # 详细建议
    for r in results:
        if r.get("error"):
            continue
        advice = r.get("holding_advice", [])
        if not advice:
            continue
        pnl_str = f"{r['pnl_pct']:+.1f}%"
        print(card_header(f" {r['symbol']} {r['name']}  现价{r['current_price']:.2f}  盈亏{pnl_str} "))
        for a in advice:
            act_label = {"BUY": "买入", "SELL": "卖出", "HOLD": "持有", "ADD": "加仓",
                         "TRIM": "减仓", "CLOSE": "清仓"}.get(a["action"], a["action"])
            reasons = "；".join(a.get("reasons", []))
            print(card_line(f"[{a['label']}] {act_label} | {reasons}", width=HALF_W))
        print(card_bottom(width=HALF_W))


# ═══════════════════════════════════════════════════════════════
# 步骤2: 全市场选股
# ═══════════════════════════════════════════════════════════════

def _screen_and_rank(top_n: int = 10, scope: str = "hs300",
                     use_mock: bool = False) -> list[dict]:
    """筛选潜力股，对 Top 5 运行快速分析。"""
    from analysis.screener import screen_stocks
    from data.pipeline import get_compressed_data
    from agents.decision import run_full_analysis

    print(f"  正在扫描 {scope.upper()} 成分股...")
    screened = screen_stocks(scope=scope, top_n=top_n, use_mock=use_mock)

    if not screened:
        print("  (选股无结果)\n")
        return []

    print(f"  筛选出 {len(screened)} 只候选股，对 Top 5 运行深度分析...\n")

    results = []
    for i, s in enumerate(screened[:5]):
        sym = s["symbol"]
        name = s.get("name", sym)
        score = s.get("score", 0)
        close = s.get("close", 0)
        change = s.get("change_pct", 0)

        print(f"  [{i+1}/5] 分析 {sym} {name} (筛选评分={score})...")

        try:
            analysis = run_full_analysis(sym, use_mock=use_mock, use_llm_pipeline=False)
            decision = analysis.get("final_decision", {})

            mid = decision.get("mid_term", {})
            mid_signal = mid.get("action", "?")
            mid_score = mid.get("_factor_score", 0)
            entry_price = mid.get("entry_price", close)
            expected_return = mid.get("expected_return_pct", 0)

            results.append({
                "symbol": sym, "name": name,
                "current_price": close,
                "change_pct": change,
                "screener_score": score,
                "mid_signal": mid_signal,
                "mid_score": mid_score,
                "entry_price": entry_price,
                "expected_return_pct": expected_return,
                "rationale": str(mid.get("rationale", ""))[:80],
            })
            print(f"    {sym} 中线: {mid_signal} ({mid_score:.0f}/100)")
        except Exception as e:
            print(f"    {sym} 分析失败: {e}")
            results.append({
                "symbol": sym, "name": name,
                "current_price": close,
                "change_pct": change,
                "screener_score": score,
                "mid_signal": "?", "mid_score": 0,
                "entry_price": close,
                "expected_return_pct": 0,
                "rationale": f"分析失败: {e}",
            })

    return results


def _print_screening_report(results: list[dict]):
    """打印选股结果表格。"""
    print(f"\n{section_div(' 全市场选股 Top 5 ')}")

    header = (f"{'代码':<8} {'名称':<8} {'现价':>7} {'涨跌%':>7} "
              f"{'筛选分':>6} {'中线信号':>8} {'因子分':>6} {'入场价':>7} {'预期收益':>8}")
    print(header)
    print("-" * 78)

    for r in results:
        change_str = f"{r['change_pct']:+.1f}%" if r['change_pct'] else "?"
        print(f"{r['symbol']:<8} {r['name']:<8} {r['current_price']:>7.2f} {change_str:>7} "
              f"{r['screener_score']:>5.0f} {r['mid_signal']:>8} {r['mid_score']:>5.0f} "
              f"{r['entry_price']:>7.2f} {r['expected_return_pct']:>7.0f}%")

    print()

    # 详细建议
    for r in results:
        print(card_header(f" {r['symbol']} {r['name']} "))
        print(card_dual("现价", format_price(r["current_price"]),
                        "涨跌", f"{r['change_pct']:+.2f}%", width=HALF_W))
        print(card_dual("筛选评分", f"{r['screener_score']:.0f}",
                        "因子评分", f"{r['mid_score']:.0f}/100", width=HALF_W))
        print(card_dual("中线信号", r["mid_signal"],
                        "预期收益", f"{r['expected_return_pct']:.0f}%", width=HALF_W))
        print(card_line(f"入场参考: {format_price(r['entry_price'])}", width=HALF_W))
        if r.get("rationale"):
            print(card_line(f"{r['rationale'][:80]}", width=HALF_W))
        print(card_bottom(width=HALF_W))


# ═══════════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════════

def run_daily_advisory(use_mock: bool = False, scope: str = "hs300",
                       top_n: int = 10) -> dict:
    """
    执行完整的日常顾问工作流。

    Args:
        use_mock: True=使用本地启发式加速，False=调用 LLM API
        scope: 选股范围 "hs300" / "zz500"
        top_n: 选股数量

    Returns:
        {"date": str, "holdings_report": list, "screening_report": list,
         "total_cash": float, "summary": str}
    """
    today = date.today().isoformat()
    print(f"\n{'='*78}")
    print(f"  StockMind 日常投资顾问 — {today}")
    print(f"  {'Mock 快速模式' if use_mock else 'DeepSeek V4 Pro 实调模式'}")
    print(f"{'='*78}")

    # ── 步骤1: 持仓诊断 ──
    print(f"\n  [STEP 1/2] 持仓诊断...")
    holdings_results = _diagnose_holdings(use_mock=use_mock)
    _print_holdings_report(holdings_results)

    # ── 步骤2: 全市场选股 ──
    print(f"\n  [STEP 2/2] 全市场选股 (scope={scope}, top_n={top_n})...")
    screening_results = _screen_and_rank(top_n=top_n, scope=scope, use_mock=use_mock)
    _print_screening_report(screening_results)

    # ── 汇总 ──
    from portfolio.manager import load_user_portfolio
    pf = load_user_portfolio()
    total_cash = pf.get("total_cash", 0)

    buy_count = sum(1 for r in screening_results if r.get("mid_signal") == "BUY")
    print(f"\n{'='*78}")
    print(f"  今日顾问总结")
    print(f"  现金余额: {total_cash:,.0f}")
    print(f"  持仓诊断: {len(holdings_results)} 只")
    print(f"  潜力标的: {buy_count} 只中线BUY信号 / {len(screening_results)} 只")
    print(f"{'='*78}\n")

    return {
        "date": today,
        "holdings_report": holdings_results,
        "screening_report": screening_results,
        "total_cash": total_cash,
        "summary": f"{len(holdings_results)}只持仓已诊断, {buy_count}只中线BUY信号",
    }


# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="StockMind 日常投资顾问")
    parser.add_argument("--mock", action="store_true", default=False, help="Mock模式 (快速)")
    parser.add_argument("--scope", default="hs300", choices=["hs300", "zz500"], help="选股范围")
    parser.add_argument("--top", type=int, default=10, help="选股数量")
    args = parser.parse_args()

    result = run_daily_advisory(use_mock=args.mock, scope=args.scope, top_n=args.top)
