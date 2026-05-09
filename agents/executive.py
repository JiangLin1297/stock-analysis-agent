#!/usr/bin/env python3
"""
执行总裁 Agent — 综合持仓和全部分析，做出最终交易决策。
调用 DeepSeek V4 扮演理性投资者角色。
"""

import json
import re
import sys

from agents.prompts import EXECUTIVE_AGENT_PROMPT
from data.deepseek import deepseek_chat


def _parse_json(raw: str) -> dict:
    clean = raw.strip()
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        pass
    m = re.search(r'\{.*\}', clean, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return {"raw": clean, "parse_error": True}


def executive_decision(symbol: str, market: str = "A", use_mock: bool = False) -> dict:
    """
    执行总裁综合决策。

    流程:
      1. 加载持仓快照
      2. 运行完整分析（含Agent报告、多空辩论、初步决策）
      3. 将持仓+分析报告合成上下文发给 DeepSeek
      4. LLM 输出最终 action/quantity/limit_price
      5. 硬规则兜底（仓位上限25%、现金约束、数量约束）

    Args:
        use_mock: False=调用DeepSeek API, True=本地启发式模拟

    Returns:
        {
            "symbol": str,
            "final_action": "BUY"/"SELL"/"HOLD"/"CAUTIOUS_BUY"/"CAUTIOUS_SELL",
            "quantity": int,
            "limit_price": float,
            "reason": str,
            "portfolio_comment": str,
            "portfolio_snapshot": dict,
            "analysis_result": dict,
        }
    """
    from portfolio.manager import load_portfolio, get_portfolio_summary
    from agents.decision import run_full_analysis

    # 1. 加载持仓
    pf = load_portfolio()
    ps = get_portfolio_summary()

    # 2. 运行完整分析
    print(f"\n[执行总裁] 正在对 {symbol} 进行全流程分析...")
    analysis = run_full_analysis(symbol, market=market, use_portfolio=True, use_mock=use_mock)

    # 3. 构建上下文
    q = analysis.get("compressed_data", {}).get("quote", {})
    current_price = q.get("price") or analysis.get("compressed_data", {}).get("technical", {}).get("close")

    # 该股票是否已在持仓中
    held_position = None
    for p in pf.get("positions", []):
        if p.get("symbol") == symbol:
            held_position = p
            break

    context_parts = []

    # 持仓概览
    context_parts.append("=== 账户持仓概览 ===")
    context_parts.append(f"总资产: ¥{ps['total_assets']:,.2f}")
    context_parts.append(f"现金: ¥{ps['cash']:,.2f}")
    context_parts.append(f"持仓市值: ¥{ps['market_value']:,.2f}")
    context_parts.append(f"仓位比例: {ps['market_value']/ps['total_assets']*100:.1f}%")
    context_parts.append(f"持仓数量: {ps['position_count']}")
    context_parts.append(f"浮动盈亏: ¥{ps['total_floating_pnl']:+,.2f} ({ps['total_pnl_pct']:+.2f}%)")

    if ps["positions"]:
        context_parts.append("\n现有持仓明细:")
        for p in ps["positions"]:
            ctx = f"  [{p['symbol']}] {p.get('name','')} | "
            ctx += f"成本¥{p['entry_price']:.2f} "
            if p.get('current_price'):
                ctx += f"现价¥{p['current_price']:.2f} "
            ctx += f"x {p['quantity']}股 | "
            ctx += f"市值¥{p['market_value']:,.0f} | "
            ctx += f"盈亏{p['floating_pnl_pct']:+.1f}% | "
            ctx += f"占比{p['market_value']/ps['total_assets']*100:.1f}%"
            context_parts.append(ctx)
    else:
        context_parts.append("\n当前空仓。")

    # 目标股票持仓状态
    if held_position:
        context_parts.append(f"\n⚠ 目标股票 {symbol} 已在持仓中！")
        context_parts.append(f"  成本: ¥{held_position['entry_price']:.2f}, 持有: {held_position['quantity']}股")
        if current_price:
            pnl_pct = (float(current_price) / float(held_position['entry_price']) - 1) * 100
            context_parts.append(f"  当前价: ¥{current_price}, 盈亏: {pnl_pct:+.2f}%")
    else:
        context_parts.append(f"\n目标股票 {symbol} 未在持仓中。")

    # 完整分析报告
    context_parts.append("\n=== 完整分析报告 ===")

    # Agent报告
    agent_reports = analysis.get("agent_reports", [])
    context_parts.append("\nAgent分析:")
    for r in agent_reports:
        context_parts.append(f"  [{r.get('agent','?')}] 信号={r.get('signal','?')} "
                             f"评分={r.get('score',0)} 置信度={r.get('confidence',0):.0%}")
        reasoning = r.get('reasoning', '') or ''
        context_parts.append(f"    理由: {reasoning[:100]}")

    # 辩论结果
    debate = analysis.get("debate_result", {})
    mod = debate.get("moderation", {})
    if not mod.get("parse_error"):
        context_parts.append(f"\n辩论: {mod.get('winner','?')}方胜出")
        context_parts.append(f"  总结: {mod.get('summary','')[:120]}")
        context_parts.append(f"  分歧: {mod.get('key_divergence','')[:100]}")

    # 初步决策
    fd = analysis.get("final_decision", {})
    context_parts.append(f"\n初步决策建议: {fd.get('action','?')} "
                         f"仓位{fd.get('position_pct',0)}% "
                         f"入场¥{fd.get('entry_price','?')} "
                         f"止损¥{fd.get('stop_loss_price','?')}")

    # 财务/基本面数据
    fin = analysis.get("compressed_data", {}).get("financial", {})
    if fin:
        context_parts.append(f"\n财务数据: PE={q.get('pe','?')} PB={q.get('pb','?')} "
                             f"ROE={fin.get('roe','?')}% 负债率={fin.get('debt_ratio','?')}%")

    full_context = "\n".join(context_parts)

    # 4. 宏观风控检查（LLM调用前）
    macro_risk = _check_macro_risk()
    if not macro_risk["index_above_ma60"]:
        print(f"[宏观风控] ⚠ 大盘指数跌破60日均线！短线暂停，长线只平不买")
    if ps["market_value"] / max(ps["total_assets"], 1) > 0.6:
        print(f"[宏观风控] ⚠ 总仓位>{60}%，强制预留10%现金或建议买入反向ETF对冲")

    # 5. 横向比较持仓
    position_ranking = []
    fd = analysis.get("final_decision", {})
    short_d = fd.get("short_term", {})
    new_potential = short_d.get("expected_return_pct", 0) or 0
    position_ranking = _compare_positions(
        ps.get("positions", []), symbol, new_potential, 3
    )

    # 6. 调用 LLM / Mock
    if use_mock:
        print("[执行总裁] 使用 Mock 模式 (本地启发式推断)")
        result = _mock_executive(symbol, analysis, pf, ps, held_position, current_price,
                                 macro_risk=macro_risk)
    else:
        print("[执行总裁] 正在调用 DeepSeek 综合决策...")
        raw = deepseek_chat(EXECUTIVE_AGENT_PROMPT, full_context, temperature=0.3, max_tokens=1024)
        result = _parse_json(raw)

    # 7. 硬规则兜底（注入宏观风控结果）
    final = _apply_hard_constraints(result, pf, ps, symbol, held_position, current_price,
                                    macro_risk=macro_risk)

    return {
        "symbol": symbol,
        "final_action": final.get("final_action", "HOLD"),
        "quantity": final.get("quantity", 0),
        "limit_price": final.get("limit_price", current_price or 0),
        "reason": final.get("reason", ""),
        "portfolio_comment": final.get("portfolio_comment", ""),
        "portfolio_snapshot": ps,
        "analysis_result": analysis,
        "macro_risk": macro_risk,
        "position_ranking": position_ranking,
    }


def _check_macro_risk() -> dict:
    """
    检查大盘环境：获取上证综指/沪深300与60日均线关系。
    返回 {"index_above_ma60": bool, "short_term_allowed": bool, "long_term_buy_allowed": bool}
    """
    result = {
        "index_above_ma60": True,
        "short_term_allowed": True,
        "long_term_buy_allowed": True,
        "indices_checked": [],
    }
    try:
        from data.pipeline import fetch_kline_indicators, normalize_symbol
        for idx_code, idx_name in [("000001", "上证综指"), ("000300", "沪深300")]:
            sym, ex = normalize_symbol(idx_code)
            tech = fetch_kline_indicators(sym, ex, ndays=120)
            if tech.get("error"):
                continue
            close = tech.get("close")
            ma60 = tech.get("ma60")
            if close and ma60:
                above = close > ma60
                result["indices_checked"].append({
                    "name": idx_name, "code": idx_code,
                    "close": close, "ma60": ma60,
                    "above_ma60": above,
                })
                if not above:
                    result["index_above_ma60"] = False
    except Exception:
        pass

    if not result["index_above_ma60"]:
        result["short_term_allowed"] = False
        result["long_term_buy_allowed"] = False
    return result


def _compare_positions(positions: list, new_symbol: str,
                       new_potential: float, new_risk: float) -> list:
    """
    横向比较持仓：按收益潜力/风险评分排序，标注建议。
    若新标的明显优于现有持仓，建议淘汰最弱。
    """
    ranking = []
    for p in positions:
        sym = p.get("symbol", "?")
        entry = p.get("entry_price", 0)
        cur = p.get("current_price", entry)
        pnl_pct = (cur / entry - 1) * 100 if entry > 0 else 0
        # 简单评分：盈利越多越好，结合ROE近似
        score = pnl_pct / max(abs(pnl_pct), 1) * 5 + 5  # 5-10分
        risk = 5  # 中等风险默认
        ranking.append({
            "symbol": sym,
            "return_potential": round(pnl_pct + 10, 1),
            "risk_score": risk,
            "score": round(score, 1),
            "action": "HOLD",
        })

    # 按 score/risk 排序（高分在前）
    ranking.sort(key=lambda x: x["score"] / max(x["risk_score"], 1), reverse=True)

    # 最弱持仓标注
    if len(ranking) >= 2:
        ranking[-1]["action"] = "CONSIDER_TRIM"

    # 新标的比较
    if new_potential > 0 and new_risk > 0:
        new_ratio = new_potential / new_risk
        for r in ranking:
            existing_ratio = r["return_potential"] / max(r["risk_score"], 1)
            if new_ratio > existing_ratio * 1.5:
                r["action"] = "TRIM_FOR_BETTER"
                break

    return ranking


def _apply_hard_constraints(result: dict, pf: dict, ps: dict,
                             symbol: str, held_position: dict,
                             current_price, macro_risk: dict = None) -> dict:
    """硬规则兜底：仓位上限、现金约束、数量约束、宏观风控。"""
    action = result.get("final_action", "HOLD")
    quantity = 0
    limit_price = 0
    try:
        quantity = int(result.get("quantity", 0))
        limit_price = float(result.get("limit_price", 0))
    except (ValueError, TypeError):
        pass

    total_assets = ps["total_assets"]
    cash = ps["cash"]
    price = float(current_price) if current_price else limit_price
    position_ratio = ps["market_value"] / max(total_assets, 1)

    # 宏观风控：大盘破位 → 短线禁止开仓
    if macro_risk and not macro_risk.get("short_term_allowed", True):
        if action in ("BUY", "CAUTIOUS_BUY") and not held_position:
            action = "HOLD"
            quantity = 0
            result["reason"] = (result.get("reason", "") + " [宏观风控:大盘跌破60日线→短线暂停新开仓]")[:150]

    # 宏观风控：长线只平不买（可持有但不开新仓）
    if macro_risk and not macro_risk.get("long_term_buy_allowed", True):
        if action in ("BUY", "CAUTIOUS_BUY") and not held_position:
            action = "HOLD"
            quantity = 0
            result["reason"] = (result.get("reason", "") + " [宏观风控:长线只平不买]")[:150]

    # 总仓位>60% → 强制预留10%现金
    if position_ratio > 0.6 and action in ("BUY", "CAUTIOUS_BUY"):
        required_cash = total_assets * 0.10
        available_for_buy = max(0, cash - required_cash)
        if available_for_buy <= 0:
            action = "HOLD"
            quantity = 0
            result["reason"] = (result.get("reason", "") + " [仓位>{:0f}%→预留10%现金→暂停买入]".format(60))[:150]

    if action in ("BUY", "CAUTIOUS_BUY"):
        if price <= 0:
            action = "HOLD"
            quantity = 0
        else:
            # 单票仓位上限25%
            max_buy_value = total_assets * 0.25
            if held_position:
                existing_value = float(held_position.get("entry_price", 0)) * int(held_position.get("quantity", 0))
                max_buy_value = max(0, max_buy_value - existing_value)

            max_buy_value = min(max_buy_value, cash)
            # 总仓位>60%的额外现金约束
            if position_ratio > 0.6:
                max_buy_value = min(max_buy_value, max(0, cash - total_assets * 0.10))

            max_qty = int(max_buy_value / price)
            quantity_cap = int(max_qty / 100) * 100 if max_qty >= 100 else 0

            if quantity <= 0 or quantity_cap <= 0:
                action = "HOLD"
                quantity = 0
            elif quantity > quantity_cap:
                quantity = quantity_cap

    elif action in ("SELL", "CAUTIOUS_SELL"):
        if held_position:
            held_qty = int(held_position.get("quantity", 0))
            if quantity <= 0 or quantity > held_qty:
                quantity = held_qty
        else:
            action = "HOLD"
            quantity = 0

    else:
        quantity = 0

    result["final_action"] = action
    result["quantity"] = quantity
    if limit_price <= 0 and price > 0:
        result["limit_price"] = round(price, 2)
    else:
        result["limit_price"] = round(limit_price, 2)

    return result


def _mock_executive(symbol: str, analysis: dict, pf: dict, ps: dict,
                    held_position: dict, current_price,
                    macro_risk: dict = None) -> dict:
    """模拟执行总裁决策（无需API）。"""
    fd = analysis.get("final_decision", {})
    action = fd.get("action", "HOLD")
    pos_pct = fd.get("position_pct", 0)
    price = float(current_price) if current_price else float(fd.get("entry_price", 0))
    position_ratio = ps["market_value"] / max(ps["total_assets"], 1)

    # 宏观风控检查
    if macro_risk:
        if not macro_risk.get("short_term_allowed", True) and not held_position:
            action = "HOLD"
            ratio = "宏观风控:大盘跌破60日线→短线暂停新开仓"
            return {
                "final_action": action, "quantity": 0,
                "limit_price": round(price, 2), "reason": ratio,
                "portfolio_comment": "大盘破位，建议降低总仓位至50%以下或买入反向ETF对冲",
            }

    # 根据持仓状况调整
    if held_position:
        entry = float(held_position["entry_price"])
        pnl_pct = (price - entry) / entry * 100
        if pnl_pct > 30:
            action = "SELL"
            ratio = f"已盈利{pnl_pct:.0f}%，建议分批止盈锁定利润(动态评估趋势末端)"
        elif pnl_pct < -10:
            action = "SELL"
            ratio = f"已亏损{pnl_pct:.0f}%，建议止损控制风险"
        elif pnl_pct > 15 and action == "BUY":
            action = "HOLD"
            ratio = f"已盈利{pnl_pct:.0f}%，暂不加仓等待回调"
        elif pnl_pct < -5 and action == "BUY":
            action = "CAUTIOUS_BUY"
            ratio = f"浮亏{pnl_pct:.0f}%，可谨慎加仓摊低成本"
        else:
            ratio = f"浮动盈亏{pnl_pct:+.1f}%，维持现有持仓"
    else:
        if action in ("BUY", "CAUTIOUS_BUY") and position_ratio > 0.8:
            action = "CAUTIOUS_BUY"
            ratio = "总仓位已较重(>80%)，谨慎新建"
        elif action in ("BUY", "CAUTIOUS_BUY") and position_ratio > 0.6:
            action = "CAUTIOUS_BUY"
            ratio = "总仓位>60%，预留10%现金，小额建仓"
        elif action == "BUY" and ps["cash"] < ps["total_assets"] * 0.1:
            action = "HOLD"
            ratio = "现金不足10%，暂不新建仓位"
        else:
            ratio = fd.get("rationale", "基于分析报告")[:80]

    qty = 0
    if action in ("BUY", "CAUTIOUS_BUY") and price > 0:
        buy_budget = min(ps["total_assets"] * 0.25, ps["cash"])
        # 总仓位>60%时额外预留10%现金
        if position_ratio > 0.6:
            buy_budget = min(buy_budget, max(0, ps["cash"] - ps["total_assets"] * 0.10))
        qty = int(buy_budget / price / 100) * 100
    elif action in ("SELL", "CAUTIOUS_SELL") and held_position:
        qty = int(held_position.get("quantity", 0))

    return {
        "final_action": action,
        "quantity": qty,
        "limit_price": round(price, 2),
        "reason": ratio,
        "portfolio_comment": f"总资产¥{ps['total_assets']:,.0f}，仓位{position_ratio*100:.0f}%，建议保持关注。",
    }


if __name__ == "__main__":
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8')

    sym = sys.argv[1] if len(sys.argv) > 1 else "600519"
    result = executive_decision(sym)
    print(f"\n=== 执行总裁最终决策 ===")
    print(f"  标的: {result['symbol']}")
    print(f"  操作: {result['final_action']}")
    print(f"  数量: {result['quantity']}股")
    print(f"  限价: ¥{result['limit_price']:.2f}")
    print(f"  理由: {result['reason']}")
    print(f"  账户建议: {result['portfolio_comment']}")
