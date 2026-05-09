"""
最终决策引擎 — 融合所有分析做交易决策。
极简实现：无Mock降级，无错误处理。
"""

import json
import re
import sys
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

from agents.prompts import ALL_PROMPTS
from data.deepseek import deepseek_chat
from utils.format import (card_header, card_line, card_empty, card_bottom, card_field,
                          card_dual, section_div, format_signal, format_price, format_pct,
                          CARD_W, HALF_W)


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


def _build_context(compressed_data: dict, agent_reports: list[dict],
                   debate_result: dict, portfolio_context: dict = None,
                   time_frame_opinions: dict = None) -> str:
    """将所有信息压缩为一段文本传给决策Agent。"""
    q = compressed_data.get("quote", {})
    t = compressed_data.get("technical", {})
    f = compressed_data.get("financial", {})

    parts = []

    # 行情摘要
    parts.append(f"行情: {q.get('name')} 价格{q.get('price')} 涨跌{q.get('change_pct')}% "
                 f"PE{q.get('pe')} PB{q.get('pb')}")

    # 技术指标
    parts.append(f"技术: MA5={t.get('ma5')} MA20={t.get('ma20')} RSI14={t.get('rsi14')} "
                 f"MACD={t.get('macd')} 布林上={t.get('boll_upper')} 中={t.get('boll_mid')} 下={t.get('boll_lower')}")

    # 突破信号
    bs = compressed_data.get("breakout_signals", {})
    if bs:
        parts.append(f"突破信号: boll_breakout={bs.get('boll_breakout')} "
                     f"volume_ratio={bs.get('volume_ratio')} vcp={bs.get('vcp')} "
                     f"surge_confirm={bs.get('surge_confirm')} "
                     f"breakout_score={bs.get('breakout_score', 0)}")

    # Agent报告摘要
    parts.append("Agent报告:")
    for r in agent_reports:
        parts.append(f"  [{r['agent']}] {r['signal']} score={r['score']} "
                     f"conf={r['confidence']} {r.get('reasoning','')[:60]}")
        if r.get('risk_level'):
            parts.append(f"    风险={r['risk_level']} 仓位上限={r['position_ratio']}")

    # 辩论结果
    mod = debate_result.get("moderation", {})
    if not mod.get("parse_error"):
        parts.append(f"辩论: winner={mod.get('winner')} summary={mod.get('summary','')[:100]}")
        ls = debate_result.get("leaning_short", 0.5)
        lm = debate_result.get("leaning_mid", 0.5)
        ll = debate_result.get("leaning_long", 0.5)
        parts.append(f"三线倾向: 短线={ls:.1f} 中线={lm:.1f} 长线={ll:.1f}")

    # 三线观点
    if time_frame_opinions:
        parts.append("--- 三线时间观点 ---")
        for frame in ["short_term", "mid_term", "long_term"]:
            o = time_frame_opinions.get(frame, {})
            sig = o.get("signal", "?")
            cv = o.get("conviction", 0)
            r = o.get("reasoning", "")[:60]
            parts.append(f"  [{frame}] {sig} conviction={cv}/10 {r}")

    # 持仓上下文
    if portfolio_context:
        parts.append("--- 当前持仓 ---")
        parts.append(f"总资产: {portfolio_context.get('total_assets')} "
                     f"现金: {portfolio_context.get('cash')} "
                     f"已用仓位比例: {portfolio_context.get('position_used_pct', 0):.0f}%")
        held = portfolio_context.get('holdings', [])
        if held:
            parts.append("现有持仓:")
            for h in held:
                parts.append(f"  {h['symbol']}({h.get('name','')}) 成本{h['entry_price']} "
                             f"数量{h['quantity']}股 占比{h.get('weight_pct',0):.1f}%")
            parts.append("注意: 如分析标的已在持仓中，应结合现有仓位给出持有/加仓/减仓建议，避免单一股票过度集中(上限25%)。")
        else:
            parts.append("当前空仓，可考虑新建仓位。")

    return "\n".join(parts)


def make_decision(compressed_data: dict, agent_reports: list[dict], debate_result: dict,
                  use_mock: bool = False, portfolio_context: dict = None,
                  time_frame_opinions: dict = None, adapted_params: dict = None) -> dict:
    """
    生成最终交易决策（三时间维度），含硬规则修正。

    Args:
        portfolio_context: 可选，持仓上下文
        time_frame_opinions: 可选，三线时间观点
        adapted_params: 可选，自适应策略参数（来自 {symbol}_adapted_params.json）
    """
    if use_mock:
        return _mock_decision_3d(compressed_data, agent_reports, debate_result, time_frame_opinions, adapted_params)

    ctx = _build_context(compressed_data, agent_reports, debate_result, portfolio_context, time_frame_opinions)

    # 使用三线合成决策 Prompt
    prompt_3d = ALL_PROMPTS.get("synthesis_3d_agent",
        ALL_PROMPTS.get("synthesis_agent", "")).replace("{{context}}", ctx)
    raw = deepseek_chat(prompt_3d, "请输出三个时间维度的交易决策JSON。")
    decision_3d = _parse_json(raw)

    # 兜底：如果LLM输出结构不对，回退到原始决策格式
    if "short_term" not in decision_3d and "action" not in decision_3d:
        # 尝试使用旧的 synthesis_agent
        old_prompt = ALL_PROMPTS["synthesis_agent"].replace("{{context}}", ctx)
        raw = deepseek_chat(old_prompt, "请输出交易决策JSON。")
        old_decision = _parse_json(raw)
        decision_3d = _convert_to_3d(old_decision, time_frame_opinions)

    if "short_term" not in decision_3d and "action" in decision_3d:
        decision_3d = _convert_to_3d(decision_3d, time_frame_opinions)

    result = _apply_hard_rules_3d(compressed_data, decision_3d, adapted_params)
    if portfolio_context:
        result["_portfolio_aware"] = True
    if adapted_params:
        result["_adapted_params_used"] = True
    return result


def _apply_hard_rules(compressed_data: dict, decision: dict) -> dict:
    """硬规则修正：仓位上限25%、低置信度强制HOLD。"""
    pos = decision.get("position_pct", 10)
    try:
        pos = int(pos)
    except (ValueError, TypeError):
        pos = 10
    if pos > 25:
        pos = 25
    decision["position_pct"] = pos

    conf = decision.get("confidence", 0.5)
    try:
        conf = float(conf)
    except (ValueError, TypeError):
        conf = 0.5
    action = decision.get("action", "HOLD")
    if conf < 0.5 and action not in ("CAUTIOUS_BUY", "CAUTIOUS_SELL"):
        decision["action"] = "HOLD"
    decision["confidence"] = conf

    # 计算入场/止损/止盈
    q = compressed_data.get("quote", {})
    t = compressed_data.get("technical", {})
    price = q.get("price") or t.get("close")
    if decision.get("entry_price") is None and price:
        decision["entry_price"] = round(float(price), 2)
    if decision.get("stop_loss_price") is None and price:
        boll_lower = t.get("boll_lower")
        if boll_lower:
            decision["stop_loss_price"] = round(float(boll_lower), 2)
        else:
            decision["stop_loss_price"] = round(float(price) * 0.92, 2)
    if decision.get("take_profit_price") is None and price:
        boll_upper = t.get("boll_upper")
        if boll_upper:
            decision["take_profit_price"] = round(float(boll_upper), 2)
        else:
            decision["take_profit_price"] = round(float(price) * 1.20, 2)

    return decision


def _mock_decision(compressed_data: dict, agent_reports: list[dict], debate_result: dict) -> dict:
    """基于Agent报告生成模拟交易决策（无需API调用）。"""
    q = compressed_data.get("quote", {})
    t = compressed_data.get("technical", {})
    price = q.get("price") or t.get("close")
    boll_lower = t.get("boll_lower")
    boll_upper = t.get("boll_upper")

    buy_count = sum(1 for r in agent_reports if r.get("signal") in ("BUY", "CAUTIOUS_BUY"))
    sell_count = sum(1 for r in agent_reports if r.get("signal") in ("SELL", "CAUTIOUS_SELL"))
    has_cautious = any(r.get("signal") in ("CAUTIOUS_BUY", "CAUTIOUS_SELL") for r in agent_reports)

    mod = debate_result.get("moderation", {})
    winner = mod.get("winner", "TIE")

    if buy_count > sell_count and winner == "BULL":
        action = "CAUTIOUS_BUY" if has_cautious else "BUY"
        confidence = 0.70 if has_cautious else 0.75
    elif sell_count > buy_count and winner == "BEAR":
        action = "CAUTIOUS_SELL" if has_cautious else "SELL"
        confidence = 0.70 if has_cautious else 0.75
    else:
        action = "HOLD"
        confidence = 0.55

    # 从 risk_manager 获取仓位上限
    max_position = _get_risk_position_cap(agent_reports)

    return _apply_hard_rules(compressed_data, {
        "action": action,
        "entry_price": round(float(price), 2) if price else None,
        "stop_loss_price": round(float(boll_lower), 2) if boll_lower else (round(float(price) * 0.92, 2) if price else None),
        "take_profit_price": round(float(boll_upper), 2) if boll_upper else (round(float(price) * 1.20, 2) if price else None),
        "position_pct": min(18 if action in ("BUY", "CAUTIOUS_BUY") else 0, max_position),
        "confidence": confidence,
        "rationale": f"综合{buy_count}个看多信号和{sell_count}个看空信号，辩论{winner}方胜出，建议{action}。",
    })


def _get_risk_position_cap(agent_reports: list[dict]) -> float:
    """从 risk_manager 报告中提取仓位上限。"""
    for r in agent_reports:
        if r.get("agent") == "risk_manager":
            cap = r.get("position_ratio", 0.25)
            return float(cap) if cap else 0.0
    return 0.25


def _convert_to_3d(decision: dict, time_frame_opinions: dict = None) -> dict:
    """将旧格式的单一决策转换为三时间维度格式。"""
    price = decision.get("entry_price", 0)
    action = decision.get("action", "HOLD")
    conf = decision.get("confidence", 0.5)
    rationale = decision.get("rationale", "")

    # 从 time_frame_opinions 提取各维度信号
    short_sig = "HOLD"; mid_sig = "HOLD"; long_sig = "HOLD"
    short_cv = 5; mid_cv = 5; long_cv = 5
    if time_frame_opinions:
        short_sig = time_frame_opinions.get("short_term", {}).get("signal", "HOLD")
        mid_sig = time_frame_opinions.get("mid_term", {}).get("signal", "HOLD")
        long_sig = time_frame_opinions.get("long_term", {}).get("signal", "HOLD")
        short_cv = time_frame_opinions.get("short_term", {}).get("conviction", 5)
        mid_cv = time_frame_opinions.get("mid_term", {}).get("conviction", 5)
        long_cv = time_frame_opinions.get("long_term", {}).get("conviction", 5)

    # 如果原决策看多，三线都偏向看多
    if action in ("BUY", "CAUTIOUS_BUY"):
        if short_sig == "HOLD": short_sig = action
        if mid_sig == "HOLD": mid_sig = action
        if long_sig == "HOLD": long_sig = action
    elif action in ("SELL", "CAUTIOUS_SELL"):
        if short_sig == "HOLD": short_sig = action
        if mid_sig == "HOLD": mid_sig = action
        if long_sig == "HOLD": long_sig = action

    return {
        "short_term": {"action": short_sig, "entry_price": price, "stop_loss_price": decision.get("stop_loss_price"),
                       "take_profit_price": decision.get("take_profit_price"), "confidence": conf, "rationale": rationale},
        "mid_term": {"action": mid_sig, "entry_price": price, "stop_loss_price": decision.get("stop_loss_price"),
                     "take_profit_price": decision.get("take_profit_price"), "confidence": conf, "rationale": rationale},
        "long_term": {"action": long_sig, "entry_price": price, "stop_loss_price": decision.get("stop_loss_price"),
                      "take_profit_price": decision.get("take_profit_price"), "confidence": conf,
                      "rationale": rationale, "potential_multiplier": ""},
        "overall_verdict": rationale[:80],
    }


def _apply_hard_rules_3d(compressed_data: dict, decision_3d: dict, adapted_params: dict = None) -> dict:
    """三维度硬规则修正：仓位上限短18%/中30%/长45%，低置信度强制HOLD，动态退出替代固定止盈。
    若提供 adapted_params，则使用其中的仓位上限和置信度阈值替代默认值。"""
    q = compressed_data.get("quote", {})
    t = compressed_data.get("technical", {})
    price = q.get("price") or t.get("close")
    boll_lower = t.get("boll_lower")

    # 自适应参数注入：仓位上限、置信度阈值
    ap = adapted_params.get("params", {}) if adapted_params else {}
    ap_short = ap.get("short_term", {})
    ap_mid = ap.get("mid_term", {})
    ap_long = ap.get("long_term", {})
    ap_risk = ap.get("risk_control", {})
    ap_trend = ap.get("trend_filter", {})

    # 仓位上限：优先使用自适应参数，否则默认值
    cap_map = {
        "short_term": ap_short.get("position_pct", 18),
        "mid_term": ap_mid.get("position_pct", 30),
        "long_term": ap_long.get("position_pct", 45),
    }
    # 单票总上限
    max_single_pct = ap_risk.get("max_single_position_pct", 30)

    # 置信度阈值
    conf_min_map = {
        "short_term": ap_short.get("confidence_min", 0.4),
        "mid_term": ap_mid.get("confidence_min", 0.4),
        "long_term": ap_long.get("confidence_min", 0.4),
    }

    for dim in ["short_term", "mid_term", "long_term"]:
        d = decision_3d.get(dim, {})
        if not isinstance(d, dict):
            d = {}
        pos = d.get("position_pct", 10)
        try:
            pos = int(pos)
        except (ValueError, TypeError):
            pos = 10
        d["position_pct"] = min(pos, cap_map[dim], max_single_pct)

        conf = d.get("confidence", 0.5)
        try:
            conf = float(conf)
        except (ValueError, TypeError):
            conf = 0.5
        d["confidence"] = conf

        action = d.get("action", "HOLD")
        # 低置信度但保留 CAUTIOUS 信号，阈值来自自适应参数
        conf_floor = conf_min_map.get(dim, 0.4)
        if conf < conf_floor and action not in ("CAUTIOUS_BUY", "CAUTIOUS_SELL"):
            d["action"] = "HOLD"

        # 计算入场/止损（如果缺失）
        if d.get("entry_price") is None and price:
            d["entry_price"] = round(float(price), 2)
        if d.get("stop_loss_price") is None and price:
            if dim == "long_term":
                d["stop_loss_price"] = round(float(boll_lower) * 0.85, 2) if boll_lower else round(float(price) * 0.80, 2)
            elif dim == "mid_term":
                d["stop_loss_price"] = round(float(boll_lower), 2) if boll_lower else round(float(price) * 0.90, 2)
            else:
                d["stop_loss_price"] = round(float(boll_lower), 2) if boll_lower else round(float(price) * 0.95, 2)

        # 不再强制设定固定 take_profit_price — 使用 exit_strategy 替代
        if d.get("take_profit_price") is None and price:
            # 仅作为参考值保留，实际退出由 exit_strategy 控制
            if dim == "long_term":
                d["take_profit_price"] = None  # 不设固定止盈
            elif dim == "mid_term":
                d["take_profit_price"] = None
            else:
                d["take_profit_price"] = None

        # 确保 exit_strategy 存在
        if not d.get("exit_strategy"):
            if dim == "short_term":
                d["exit_strategy"] = {"type": "trailing",
                    "rules": ["盈利>10%启用移动止盈回撤3%卖一半", "跌破MA5全清"],
                    "re_evaluation_triggers": ["冲高回落5%+放量", "RSI>80死叉"]}
            elif dim == "mid_term":
                d["exit_strategy"] = {"type": "trailing",
                    "rules": ["盈利>15%启用移动止损回撤5%全清", "MA20拐头减半仓"],
                    "re_evaluation_triggers": ["MA20方向变化", "财报不及预期"]}
            else:
                d["exit_strategy"] = {"type": "trailing",
                    "rules": ["盈利>15%启用移动止损回撤5%", "季度ROE连续下滑>20%减半仓"],
                    "re_evaluation_triggers": ["每季财报ROE检查", "行业政策重大变化"]}

        # 收益目标硬约束：预期收益不达标则HOLD
        expected = d.get("expected_return_pct")
        if action in ("BUY", "CAUTIOUS_BUY") and expected is not None:
            if dim == "short_term" and expected < 10:
                d["action"] = "HOLD"
                d["rationale"] = (d.get("rationale", "") + " [预期收益不足10%→强制HOLD]")[:100]
            elif dim == "mid_term" and expected < 30:
                d["action"] = "HOLD"
                d["rationale"] = (d.get("rationale", "") + " [预期收益不足30%→强制HOLD]")[:100]
            elif dim == "long_term" and expected < 100:
                d["action"] = "HOLD"
                d["rationale"] = (d.get("rationale", "") + " [预期收益不足100%→强制HOLD]")[:100]

        decision_3d[dim] = d

    # 综合 action（取置信度最高的维度）
    best_dim = max(["short_term", "mid_term", "long_term"],
                   key=lambda x: decision_3d.get(x, {}).get("confidence", 0))
    decision_3d["action"] = decision_3d.get(best_dim, {}).get("action", "HOLD")
    # 综合仓位 = 各维度加权平均
    w = {"short_term": 0.2, "mid_term": 0.3, "long_term": 0.5}
    total_pos = sum(decision_3d.get(d, {}).get("position_pct", 0) * w[d] for d in w)
    decision_3d["position_pct"] = round(total_pos, 0)
    decision_3d["confidence"] = decision_3d.get(best_dim, {}).get("confidence", 0.5)
    decision_3d["entry_price"] = decision_3d.get(best_dim, {}).get("entry_price")
    decision_3d["stop_loss_price"] = decision_3d.get(best_dim, {}).get("stop_loss_price")
    decision_3d["take_profit_price"] = None  # 动态退出，不设固定止盈
    decision_3d["rationale"] = decision_3d.get(best_dim, {}).get("rationale", "")
    decision_3d["exit_strategy"] = decision_3d.get(best_dim, {}).get("exit_strategy")

    return decision_3d


def _mock_decision_3d(compressed_data: dict, agent_reports: list[dict],
                       debate_result: dict, time_frame_opinions: dict = None,
                       adapted_params: dict = None) -> dict:
    """基于Agent报告+三线观点生成模拟3D交易决策（无需API调用）。"""
    q = compressed_data.get("quote", {})
    t = compressed_data.get("technical", {})
    price = q.get("price") or t.get("close")
    boll_lower = t.get("boll_lower")
    boll_upper = t.get("boll_upper")

    buy_count = sum(1 for r in agent_reports if r.get("signal") in ("BUY", "CAUTIOUS_BUY"))
    sell_count = sum(1 for r in agent_reports if r.get("signal") in ("SELL", "CAUTIOUS_SELL"))
    mod = debate_result.get("moderation", {})
    winner = mod.get("winner", "TIE")

    # 基础信号 — 偏向多空优势方，不轻易HOLD
    if buy_count > sell_count:
        base_action = "BUY" if winner != "BEAR" else "CAUTIOUS_BUY"
        base_conf = 0.70
    elif sell_count > buy_count:
        base_action = "SELL" if winner != "BULL" else "CAUTIOUS_SELL"
        base_conf = 0.70
    elif winner == "BULL":
        base_action = "CAUTIOUS_BUY"; base_conf = 0.60
    elif winner == "BEAR":
        base_action = "CAUTIOUS_SELL"; base_conf = 0.60
    else:
        base_action = "HOLD"; base_conf = 0.50

    # 从三线观点微调
    short_action = base_action; mid_action = base_action; long_action = base_action
    short_conf = base_conf; mid_conf = base_conf; long_conf = base_conf
    multiplier = ""

    if time_frame_opinions:
        for frame, mapping in [("short_term", "short_"), ("mid_term", "mid_"), ("long_term", "long_")]:
            o = time_frame_opinions.get(frame, {})
            sig = o.get("signal", "HOLD")
            cv = o.get("conviction", 5)
            if sig in ("BUY", "SELL"):
                if frame == "short_term": short_action = sig; short_conf = 0.5 + cv * 0.05
                elif frame == "mid_term": mid_action = sig; mid_conf = 0.5 + cv * 0.05
                else: long_action = sig; long_conf = 0.5 + cv * 0.05

        lt = time_frame_opinions.get("long_term", {})
        multiplier = lt.get("potential_multiplier", "")

    short_conf = min(short_conf, 0.95)
    mid_conf = min(mid_conf, 0.95)
    long_conf = min(long_conf, 0.95)

    # 预期收益率
    short_expected = round(float(price) * 1.10 / float(price) * 100 - 100, 1) if price and short_action == "BUY" else 0
    mid_expected = round(float(price) * 1.30 / float(price) * 100 - 100, 1) if price and mid_action == "BUY" else 0
    long_expected = round(float(price) * 2.0 / float(price) * 100 - 100, 1) if price and long_action == "BUY" else 0

    # 自适应仓位
    ap = adapted_params.get("params", {}) if adapted_params else {}
    ap_short = ap.get("short_term", {})
    ap_mid = ap.get("mid_term", {})
    ap_long = ap.get("long_term", {})

    decision_3d = {
        "short_term": {
            "action": short_action, "entry_price": round(float(price), 2) if price else None,
            "stop_loss_price": round(float(boll_lower), 2) if boll_lower else (
                round(float(price) * 0.96, 2) if price else None),
            "take_profit_price": None,
            "position_pct": ap_short.get("position_pct", 19), "confidence": round(short_conf, 2),
            "rationale": f"短线{short_action}，基于技术面信号和资金动量",
            "expected_return_pct": short_expected,
            "exit_strategy": {"type": "trailing",
                "rules": ["盈利>10%启用移动止盈回撤3%卖一半", "跌破MA5全清"],
                "re_evaluation_triggers": ["冲高回落5%+放量", "RSI>80死叉"]}
        },
        "mid_term": {
            "action": mid_action, "entry_price": round(float(price), 2) if price else None,
            "stop_loss_price": round(float(boll_lower), 2) if boll_lower else (
                round(float(price) * 0.90, 2) if price else None),
            "take_profit_price": None,
            "position_pct": ap_mid.get("position_pct", 18), "confidence": round(mid_conf, 2),
            "rationale": f"中线{mid_action}，基于趋势和行业轮动判断",
            "expected_return_pct": mid_expected,
            "exit_strategy": {"type": "trailing",
                "rules": ["盈利>15%启用移动止损回撤5%全清", "MA20拐头减半仓"],
                "re_evaluation_triggers": ["MA20方向变化", "财报不及预期"]}
        },
        "long_term": {
            "action": long_action, "entry_price": round(float(price), 2) if price else None,
            "stop_loss_price": round(float(boll_lower) * 0.85, 2) if boll_lower else (
                round(float(price) * 0.80, 2) if price else None),
            "take_profit_price": None,
            "position_pct": ap_long.get("position_pct", 25), "confidence": round(long_conf, 2),
            "rationale": f"长线{long_action}，基于成长性和行业前景判断",
            "potential_multiplier": multiplier,
            "expected_return_pct": long_expected,
            "exit_strategy": {"type": "trailing",
                "rules": ["盈利>15%启用移动止损回撤5%", "季度ROE连续下滑>20%减半仓"],
                "re_evaluation_triggers": ["每季财报ROE检查", "行业政策重大变化"]}
        },
        "overall_verdict": f"综合{base_action}，短线{short_action}/中线{mid_action}/长线{long_action}，见各维度详情",
    }

    return _apply_hard_rules_3d(compressed_data, decision_3d, adapted_params)


def run_full_analysis(symbol: str, market: str = "A", use_mock: bool = False,
                      position: dict = None, use_portfolio: bool = False,
                      historical_date=None, use_adapted_params: bool = False) -> dict:
    """
    主流程：数据 → Agent → 辩论 → 决策。
    返回包含所有中间结果的完整字典。

    Args:
        symbol: 股票代码
        market: 市场类型 (A=A股)
        use_mock: False=调用DeepSeek API, True=启发式模拟(快速但精度低)
        position: 可选，单票持仓 {"entry_price": xx, "quantity": xx}
                  传入后额外计算退出建议
        use_portfolio: True=从 portfolio.json 加载完整持仓上下文
        historical_date: 可选，历史日期字符串'2024-06-15'。传入后使用历史快照模式
        use_adapted_params: True=自动加载 {symbol}_adapted_params.json 并注入仓位/风控/信号阈值
    """
    from data.pipeline import get_compressed_data, get_historical_snapshot
    from agents.runner import run_all_agents
    from agents.time_frame import run_time_frame_agents
    from agents.debate import run_debate

    # 自适应参数加载
    adapted_params = None
    if use_adapted_params:
        import os as _os
        adapted_path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                                     f"{symbol}_adapted_params.json")
        if _os.path.exists(adapted_path):
            with open(adapted_path, 'r', encoding='utf-8') as _f:
                adapted_params = json.load(_f)
            print(f"  [Adaptive] 加载 {symbol}_adapted_params.json → "
                  f"仓位(短{adapted_params.get('params',{}).get('short_term',{}).get('position_pct','?')}%/"
                  f"中{adapted_params.get('params',{}).get('mid_term',{}).get('position_pct','?')}%/"
                  f"长{adapted_params.get('params',{}).get('long_term',{}).get('position_pct','?')}%) "
                  f"适配理由: {adapted_params.get('adaptation_rationale', '?')[:50]}")
        else:
            print(f"  [Adaptive] 未找到 {symbol}_adapted_params.json，使用默认参数")

    if historical_date:
        print(f"\n{section_div(f' FULL ANALYSIS (HISTORICAL): {symbol} @ {historical_date} ')}")
    else:
        print(f"\n{section_div(f' FULL ANALYSIS: {symbol} ')}")
    print(f"{'  [DeepSeek V4 Pro] 实时数据 + LLM 深度分析' if not use_mock else '  [Mock] 本地启发式分析（不调用 LLM）'}")
    print(f"{'  [三线时间维度] 短线/中线/长线 同步分析' if not use_mock else '  [三线时间维度] Mock 模式'}")

    # 1. 数据
    print(f"\n  [1/5] Fetching data...")
    if historical_date:
        data = get_historical_snapshot(symbol, str(historical_date), market)
    else:
        data = get_compressed_data(symbol, market)

    # 2. Agent
    print(f"\n  [2/5] Running agent analysis...")
    reports = run_all_agents(data, use_mock=use_mock)

    # 3. 三线时间观点（NEW）
    print(f"\n  [3/5] Running time-frame analysis (short/mid/long)...")
    time_opinions = run_time_frame_agents(data, reports, use_mock=use_mock)

    # 4. 辩论（注入三线观点）
    print(f"\n  [4/5] Running bull-bear debate (with time-frame context)...")
    debate = run_debate(reports, use_mock=use_mock, time_frame_opinions=time_opinions)

    # 5. 决策（注入持仓上下文+三线观点）
    print(f"\n  [5/5] Synthesizing final decision (3D)...")
    portfolio_context = None
    portfolio_summary = None
    exit_advices = []

    if use_portfolio:
        from portfolio.manager import load_portfolio, get_portfolio_summary
        pf = load_portfolio()
        # 获取持仓股票的当前价格用于汇总
        held_prices = {}
        held_symbols = [p["symbol"] for p in pf.get("positions", [])]
        if held_symbols and held_symbols[0] != symbol:
            # 如果分析的不是持仓股，仍需要持仓股的价格用于汇总
            pass
        ps = get_portfolio_summary({symbol: float(data.get("quote", {}).get("price", 0) or 0)})
        portfolio_summary = ps

        total_mv = ps["market_value"]
        total = ps["total_assets"]
        position_used_pct = (total_mv / total * 100) if total > 0 else 0

        portfolio_context = {
            "total_assets": total,
            "cash": ps["cash"],
            "position_used_pct": position_used_pct,
            "holdings": [{
                "symbol": p["symbol"],
                "name": p.get("name", ""),
                "entry_price": p["entry_price"],
                "quantity": p["quantity"],
                "weight_pct": round(p["market_value"] / total * 100, 1) if total > 0 else 0,
            } for p in ps["positions"]],
        }

    decision = make_decision(data, reports, debate, use_mock=use_mock,
                             portfolio_context=portfolio_context,
                             time_frame_opinions=time_opinions,
                             adapted_params=adapted_params)

    print(f"\n{section_div(' 三维决策 · 最终交易方案 ')}")

    # 三维决策显示
    for dim_key, dim_label in [("short_term", "短线"), ("mid_term", "中线"), ("long_term", "长线")]:
        dim = decision.get(dim_key, {})
        if not dim: continue
        d_action = format_signal(dim.get('action', '?'))
        d_entry = format_price(dim.get('entry_price'))
        d_stop = format_price(dim.get('stop_loss_price'))
        d_take = format_price(dim.get('take_profit_price'))
        d_pos = f"{dim.get('position_pct', 0)}%"
        d_conf = f"{dim.get('confidence', 0):.0%}"
        d_rationale = dim.get('rationale', '')[:100]
        pm = dim.get("potential_multiplier", "")
        d_expected = dim.get("expected_return_pct", 0)
        d_exit = dim.get("exit_strategy", {})

        print(card_header(f" {dim_label} ", width=HALF_W))
        print(card_dual("Action", d_action, "Pos", d_pos, width=HALF_W))
        print(card_dual("Entry", d_entry, "Stop", d_stop, width=HALF_W))
        print(card_dual("Conf", d_conf, "Exp.Ret", f"{d_expected}%", width=HALF_W))
        if pm:
            print(card_line(f"潜力: {pm}", width=HALF_W))
        print(card_line(f"{d_rationale}", width=HALF_W))
        if d_exit:
            print(card_line(f"退出: {d_exit.get('type','?')} | 规则: {'; '.join(d_exit.get('rules',[])[:2])}", width=HALF_W))
        print(card_bottom(width=HALF_W))
    print(card_empty())

    # 综合
    verdict = decision.get('overall_verdict', '')
    print(card_line(f"综合裁决: {verdict}"))
    print(card_bottom())
    print()

    result = {
        "symbol": symbol,
        "market": market,
        "compressed_data": data,
        "agent_reports": reports,
        "time_frame_opinions": time_opinions,
        "debate_result": debate,
        "final_decision": decision,
        "adapted_params": adapted_params,
    }

    if portfolio_summary:
        result["portfolio_summary"] = portfolio_summary

    # 5. 单票持仓退出建议 + 动态持仓管理
    if position and isinstance(position, dict):
        from analysis.exit_strategy import assess_exit
        from analysis.holding import evaluate_holding
        entry_price = float(position.get("entry_price", 0))
        quantity = int(position.get("quantity", 0))
        _quote = data.get("quote", {})
        _tech = data.get("technical", {})
        current_price = float(_quote.get("price", 0) or 0) or float(_tech.get("close", 0) or 0)
        timeframe = position.get("timeframe", "short_term")

        exit_advice = assess_exit(symbol, entry_price, quantity, current_price, data)
        result["exit_advice"] = exit_advice

        # 动态持仓评估
        holding_eval = evaluate_holding(symbol, entry_price, current_price, quantity,
                                        timeframe, data)
        result["holding_evaluation"] = holding_eval

        print(f"\n{section_div(' POSITION EXIT ADVICE ')}")
        print(card_dual("Entry", format_price(entry_price), "Current", format_price(current_price)))
        print(card_field("Shares", f"{quantity} shares"))
        print(card_empty())
        print(card_dual("Exit Action", exit_advice['action'], "Sell Ratio", f"{exit_advice['sell_ratio']}%"))
        print(card_field("P&L", f"{exit_advice['profit_pct']:+.2f}%"))
        print(card_empty())
        for reason in exit_advice["reasons"]:
            print(card_line(f"  - {reason}"))
        print(card_bottom())

        # 显示动态持仓管理
        print(f"\n{section_div(' DYNAMIC HOLDING MANAGEMENT ')}")
        print(card_dual("Holding Action", holding_eval['action'], "Ratio", f"{holding_eval['ratio']}%"))
        print(card_field("Profit", f"{holding_eval['profit_pct']:+.2f}%"))
        print(card_field("Drawdown from Peak", f"{holding_eval['drawdown_from_peak']:.2f}%"))
        print(card_empty())
        print(card_line(f"Exit Strategy: {holding_eval['exit_strategy'].get('type', '?')}"))
        for rule in holding_eval['exit_strategy'].get('rules', []):
            print(card_line(f"  → {rule}"))
        print(card_empty())
        print(card_line("Re-evaluation Triggers:"))
        for trigger in holding_eval['exit_strategy'].get('re_evaluation_triggers', []):
            print(card_line(f"  ⚡ {trigger}"))
        print(card_empty())
        for reason in holding_eval["reasons"]:
            print(card_line(f"  - {reason}"))
        print(card_bottom())
        print()

    # 6. Portfolio 模式下对所有持仓生成退出建议
    if use_portfolio and portfolio_summary:
        from analysis.exit_strategy import assess_exit
        from portfolio.manager import load_portfolio
        pf = load_portfolio()
        _quote = data.get("quote", {})
        _tech = data.get("technical", {})
        current_price = float(_quote.get("price", 0) or 0) or float(_tech.get("close", 0) or 0)

        print(f"\n{section_div(' PORTFOLIO DIAGNOSIS ')}")

        for held in pf.get("positions", []):
            held_sym = held["symbol"]
            held_entry = held["entry_price"]
            held_qty = held["quantity"]
            if held_sym == symbol:
                price_for_exit = current_price
            else:
                price_for_exit = held_entry

            adv = assess_exit(held_sym, held_entry, held_qty, price_for_exit, data)
            exit_advices.append(adv)

            print(card_line(f"[{held_sym}] {adv['action']} | P&L {adv['profit_pct']:+.2f}% | Sell {adv['sell_ratio']}%"))
            for reason in adv["reasons"]:
                print(card_line(f"    - {reason}"))
            print(card_empty())

        if exit_advices:
            result["exit_advices"] = exit_advices
        print(card_bottom())
        print()

    return result


# ═══════════════════════════════════════════════════════════════
# 因子信号生成 — 用多因子统计模型替代 LLM 做买卖决策
# ═══════════════════════════════════════════════════════════════

def generate_factor_signal(symbol: str, composite: dict, market_state: dict = None,
                           portfolio_context: dict = None, compressed_data: dict = None) -> dict:
    """
    基于因子综合评分生成交易信号。纯统计决策，不调用 LLM。

    Args:
        symbol: 股票代码
        composite: alpha_factors.composite_score() 的输出 {"score", "signal", "contributions", ...}
        market_state: {"trend_state": "BULL"/"BEAR"/"SIDEWAYS", "weekly_trend": "UP"/"DOWN", ...}
        portfolio_context: 持仓上下文（用于仓位计算）
        compressed_data: data_pipeline 原始数据（用于 ATR 止损计算）

    Returns:
        三线决策字典，格式与 make_decision 兼容
    """
    score = composite.get("score", 50)
    signal = composite.get("signal", "HOLD")
    threshold = composite.get("threshold", 55)
    tf = composite.get("time_frame", "mid")

    # ── 趋势硬规则 ──
    trend_state = "SIDEWAYS"
    weekly_state = "UNKNOWN"
    if market_state:
        trend_state = market_state.get("trend_state", "SIDEWAYS")
        wt = market_state.get("weekly_trend", {})
        weekly_state = wt.get("weekly_trend", "UNKNOWN") if isinstance(wt, dict) else "UNKNOWN"

    # 提取行情数据用于计算止损
    price = None
    boll_lower = None
    boll_upper = None
    atr = None
    if compressed_data:
        q = compressed_data.get("quote", {})
        t = compressed_data.get("technical", {})
        price = q.get("price") or t.get("close")
        boll_lower = t.get("boll_lower")
        boll_upper = t.get("boll_upper")
        atr = t.get("atr14")

    # ── 仓位计算 ──
    # 基础仓位 = 评分强度 × 基准仓位
    score_strength = (score - threshold) / max(threshold, 1)  # -1.0 ~ +1.0
    base_pos = {"short": 10, "mid": 18, "long": 25}.get(tf, 15)

    if score_strength > 0:
        position_pct = min(base_pos * (1 + score_strength * 2), 45)
    elif score_strength > -0.3:
        position_pct = max(base_pos * 0.5, 5)
    else:
        position_pct = 0

    position_pct = round(position_pct, 0)

    # 波动率调整：高波动降仓
    if compressed_data:
        vol = compressed_data.get("technical", {}).get("volatility_20d")
        if vol is None:
            # Compute from technical data
            pass
        if vol and vol > 50:
            position_pct = round(position_pct * 0.7, 0)

    # ── 止损止盈（ATR 动态） ──
    stop_loss = None
    if price and atr and atr > 0:
        if tf == "short":
            stop_loss = round(price - atr * 1.5, 2)
        elif tf == "mid":
            stop_loss = round(price - atr * 3.0, 2)
        else:
            stop_loss = round(price - atr * 5.0, 2)
    elif price and boll_lower:
        stop_loss = round(boll_lower, 2)
    elif price:
        stop_loss = round(price * 0.92, 2)

    # ── 趋势过滤 ──
    override_action = None
    override_reason = ""

    if trend_state == "BEAR":
        if tf in ("short", "mid"):
            override_action = "HOLD"
            override_reason = f"BEAR趋势,{tf}线禁止开新仓"
            position_pct = 0
        elif tf == "long":
            if signal == "BUY":
                override_action = "HOLD"
                override_reason = "BEAR趋势,长线只平不买"
                position_pct = 0

    if tf in ("mid", "long") and weekly_state not in ("UP",) and signal == "BUY":
        override_action = "HOLD"
        override_reason = f"周线={weekly_state}(需UP),{tf}线不共振"
        position_pct = 0

    # ── 风险自适应 ──
    risk_state = None
    try:
        risk_state = get_risk_state()
    except Exception:
        pass

    if risk_state:
        dd = risk_state.get("drawdown_pct", 0)
        consec = risk_state.get("consecutive_losses", 0)
        if dd > 15 and signal == "BUY":
            override_action = "HOLD"
            override_reason = f"总资产回撤{dd}%>15%,新开仓暂停"
            position_pct = 0
        if consec >= 3:
            position_pct = max(1, int(position_pct / 2))

    final_action = override_action if override_action else signal

    # ── 构建输出 ──
    decision = {
        "action": final_action,
        "entry_price": round(float(price), 2) if price else None,
        "stop_loss_price": stop_loss,
        "take_profit_price": None,
        "position_pct": int(position_pct),
        "confidence": round(min(0.95, 0.5 + score_strength * 0.3), 2),
        "rationale": f"因子评分{score:.0f}/100(阈值{threshold})→{final_action}"
                     + (f" [{override_reason}]" if override_reason else ""),
        "expected_return_pct": {
            "short": 10, "mid": 30, "long": 200
        }.get(tf, 15) if final_action in ("BUY", "CAUTIOUS_BUY") else 0,
        "exit_strategy": {
            "type": "trailing",
            "rules": [
                f"盈利>10%启用移动止盈回撤3%卖一半",
                f"跌破入场价-{round(atr*1.5,1) if atr else 3}%止损"
            ],
            "re_evaluation_triggers": ["趋势状态变化", "因子评分下降>15分"]
        },
        "_factor_driven": True,
        "_factor_score": score,
        "_factor_contributions": composite.get("contributions", {}),
    }

    return decision


def generate_3d_factor_signals(symbol: str, compressed_data: dict = None,
                                portfolio_context: dict = None) -> dict:
    """
    为三线（短/中/长）各自生成因子信号。

    Returns:
        {"short_term": {...}, "mid_term": {...}, "long_term": {...}, "overall_verdict": "..."}
    """
    market_state = {}
    if compressed_data:
        market_state["trend_state"] = compressed_data.get("trend_state", {}).get("trend_state", "SIDEWAYS")
        market_state["weekly_trend"] = compressed_data.get("weekly_trend", {})

    from analysis.alpha import calc_all_factors, composite_score

    # Detect stub financial data from historical mode and replace with None
    # so calc_all_factors will try to fetch real financial data
    fin = compressed_data.get("financial") if compressed_data else None
    if fin and isinstance(fin, dict) and fin.get("_source", "").startswith("历史回测"):
        fin = None

    factors = calc_all_factors(symbol, quote=compressed_data.get("quote") if compressed_data else None,
                               technical=compressed_data.get("technical") if compressed_data else None,
                               financials=fin)

    tf_name_map = {"short": "short_term", "mid": "mid_term", "long": "long_term"}
    decisions = {}
    for tf_key in ["short", "mid", "long"]:
        comp = composite_score(factors, tf_key)
        decision = generate_factor_signal(symbol, comp, market_state,
                                          portfolio_context, compressed_data)
        decision["timeframe"] = tf_key
        decisions[tf_name_map[tf_key]] = decision

    # 综合裁决
    actions = [d.get("action", "HOLD") for d in [decisions.get("short_term", {}),
                                                   decisions.get("mid_term", {}),
                                                   decisions.get("long_term", {})]]
    best_score = max(composite_score(factors, "short")["score"],
                     composite_score(factors, "mid")["score"],
                     composite_score(factors, "long")["score"])

    decisions["overall_verdict"] = (
        f"因子模型: 短线{actions[0]}/中线{actions[1]}/长线{actions[2]}"
        f" | 最高评分{best_score:.0f}/100"
    )
    decisions["_factor_driven"] = True
    decisions["_factors"] = {k: v for k, v in factors.items() if not k.startswith("_")}

    return decisions


def run_factor_analysis(symbol: str, market: str = "A",
                        portfolio_context: dict = None,
                        historical_date=None) -> dict:
    """
    基于因子模型的全流程分析 — 替代 run_full_analysis。

    流程: 数据 → 因子计算 → 综合评分 → 三线信号 → (可选)LLM复核

    Returns:
        {"symbol", "factors", "decision_3d", "llm_review": optional}
    """
    from data.pipeline import get_compressed_data, get_historical_snapshot

    print(f"\n{'='*70}")
    print(f"  FACTOR ANALYSIS: {symbol}")
    print(f"{'='*70}")

    # 1. 数据
    print(f"  [1/3] 获取数据...")
    if historical_date:
        data = get_historical_snapshot(symbol, str(historical_date), market)
    else:
        data = get_compressed_data(symbol, market)

    # 2. 因子计算 + 信号生成
    print(f"  [2/3] 计算因子 + 生成信号...")
    decision_3d = generate_3d_factor_signals(symbol, data, portfolio_context)

    # 打印因子信号
    for dim_key, dim_label in [("short_term", "短线"), ("mid_term", "中线"), ("long_term", "长线")]:
        d = decision_3d.get(dim_key, {})
        s = d.get("action", "?")
        pos = d.get("position_pct", 0)
        sc = d.get("_factor_score", 0)
        print(f"  [{dim_label}] {s} | 评分={sc:.0f}/100 | 仓位={pos}%")

    # 3. LLM 复核（可选：仅在因子评分处于临界区时调用）
    llm_review = None
    for tf_decision in [decision_3d.get("short_term", {}),
                         decision_3d.get("mid_term", {}),
                         decision_3d.get("long_term", {})]:
        score = tf_decision.get("_factor_score", 50)
        # 评分在阈值±10区间时，调用LLM复核
        if 50 <= score <= 65:
            try:
                print(f"  [3/3] LLM复核(评分临界区间)...")
                from agents.prompts import ALL_PROMPTS
                from data.deepseek import deepseek_chat
                from agents.runner import format_technical_context

                ctx = format_technical_context(data)
                prompt_3d = ALL_PROMPTS.get("synthesis_3d_agent", "")
                # 注入因子评分让LLM了解当前状态
                factor_ctx = f"{ctx}\n\n[因子模型评分]\n"
                for tf in ["short_term", "mid_term", "long_term"]:
                    d = decision_3d.get(tf, {})
                    factor_ctx += f"{tf}: 评分={d.get('_factor_score','?')}/100 信号={d.get('action','?')}\n"
                factor_ctx += "\n你是复核层。只有发现因子模型忽略的重大事件才能否决买入，不能主动发起买入。"

                raw = deepseek_chat(prompt_3d, factor_ctx, max_tokens=1024, timeout=30)
                llm_review = {"raw": raw, "note": "LLM复核层，仅可否决不可主动买入"}
                print(f"  LLM复核: {raw[:100]}...")
            except Exception as e:
                llm_review = {"error": str(e)}
            break  # 只复核一次

    result = {
        "symbol": symbol,
        "market": market,
        "compressed_data": data,
        "factors": decision_3d.pop("_factors", {}),
        "final_decision": decision_3d,
        "llm_review": llm_review,
    }

    print(f"  综合裁决: {decision_3d.get('overall_verdict', '')}")
    return result


def batch_analyze_top_stocks(top_n: int = 5, use_portfolio: bool = False,
                            scope: str = "hs300") -> list:
    """
    智能选股 + 批量深度分析。
    先调用 screen_stocks 获取评分最高的 top_n 只股票，
    再对每一只运行 run_full_analysis，返回汇总列表。
    """
    from analysis.screener import screen_stocks

    print(f"\n{'='*60}")
    print(f"  BATCH ANALYSIS — Top {top_n} stocks from {scope}")
    print(f"{'='*60}")

    candidates = screen_stocks(scope=scope, top_n=top_n)
    if not candidates:
        print("  ✗ 筛选无结果，退出批量分析")
        return []

    summaries = []
    for i, stock in enumerate(candidates):
        sym = stock["symbol"]
        name = stock.get("name", "")
        screener_score = stock.get("score", 0)

        print(f"\n{'#'*60}")
        print(f"  [{i+1}/{top_n}] Analyzing {sym} {name} (score={screener_score})")
        print(f"{'#'*60}")

        try:
            result = run_full_analysis(sym, market="A", use_portfolio=use_portfolio)
            decision = result.get("final_decision", {})
            summaries.append({
                "symbol": sym,
                "name": name,
                "screener_score": screener_score,
                "close": stock.get("close"),
                "pe": stock.get("pe"),
                "roe": stock.get("roe"),
                "debt_ratio": stock.get("debt_ratio"),
                "decision_action": decision.get("action"),
                "decision_confidence": decision.get("confidence"),
                "decision_entry": decision.get("entry_price"),
                "decision_stop_loss": decision.get("stop_loss_price"),
                "decision_take_profit": decision.get("take_profit_price"),
                "decision_position": decision.get("position_pct"),
                "decision_rationale": decision.get("rationale", "")[:200],
                "agent_reports": result.get("agent_reports", []),
                "debate_result": result.get("debate_result", {}),
                "final_decision": decision,
            })
        except Exception as e:
            print(f"  ⚠ {sym} 分析失败: {e}")
            summaries.append({
                "symbol": sym,
                "name": name,
                "screener_score": screener_score,
                "error": str(e),
            })

    # ── 打印汇总 ──
    print(f"\n{'='*60}")
    print(f"  BATCH ANALYSIS SUMMARY")
    print(f"{'='*60}")
    print(f"  {'Symbol':<8} {'Name':<10} {'Scr':>4} {'Decision':<16} {'Conf':>5} {'Pos':>4}")
    print(f"  {'-'*52}")
    for s in summaries:
        if "error" in s:
            print(f"  {s['symbol']:<8} {s['name']:<10} {s['screener_score']:>4} ERROR: {s['error'][:20]}")
        else:
            print(f"  {s['symbol']:<8} {s['name']:<10} {s['screener_score']:>4} "
                  f"{s['decision_action']:<16} {s['decision_confidence'] or 0:>4.0%} "
                  f"{s['decision_position'] or 0:>3}%")
    print(f"{'='*60}\n")

    return summaries


if __name__ == '__main__':
    import sys
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')

    sym = sys.argv[1] if len(sys.argv) > 1 else "600519"
    result = run_full_analysis(sym)
