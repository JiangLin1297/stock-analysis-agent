"""
多空辩论引擎 — 2轮交锋 + 仲裁。
极简实现：每轮只传上一轮文本，无记忆压缩，无Mock降级。
"""

import json
import re
from agents.prompts import DEBATE_PROMPTS
from data.deepseek import deepseek_chat
from utils.format import card_header, card_line, card_bottom, section_div, CARD_W


def _reports_to_context(agent_reports: list[dict]) -> str:
    """将 agent_reports 压缩为一段摘要文本。"""
    lines = []
    for r in agent_reports:
        name = r.get("agent", "?")
        sig = r.get("signal", "?")
        sc = r.get("score", 0)
        cf = r.get("confidence", 0)
        reason = r.get("reasoning", "")
        tp = r.get("type", "?")
        risk = r.get("risk_level", "")
        pos = r.get("position_ratio", "")
        vol = r.get("volatility_pct", "")
        boll_pos = r.get("price_vs_boll", "")
        lines.append(
            f"[{name}] signal={sig} score={sc} conf={cf} type={tp} "
            f"reasoning={reason}"
        )
        if risk:
            lines.append(f"  风险等级={risk} 仓位上限={pos} 波动率={vol}% 布林位置={boll_pos}")
    return "\n".join(lines)


def _parse_json(raw: str) -> dict:
    """从 LLM 回复中提取 JSON。"""
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


def _fmt_json(d: dict) -> str:
    """将 dict 转为紧凑 JSON 文本（作为下一轮输入）。"""
    return json.dumps(d, ensure_ascii=False, separators=(',', ':'))


def run_debate(agent_reports: list[dict], use_mock: bool = False,
               time_frame_opinions: dict = None) -> dict:
    """
    执行多空辩论流程：
      Round1: 多头初始 → 空头反驳
      Round2: 多头回应 → 空头最终回应
      仲裁:   汇总双方论点 → 仲裁员裁决

    Args:
        agent_reports: Agent分析报告列表
        use_mock: True=使用启发式模拟，False=调用API
        time_frame_opinions: 可选，三线时间观点 {short_term:..., mid_term:..., long_term:...}

    Returns:
        {"rounds": [...], "moderation": {...}, "all_rounds_text": "...",
         "leaning_short": 0.0, "leaning_mid": 0.0, "leaning_long": 0.0}
    """
    if use_mock:
        return _mock_debate(agent_reports, time_frame_opinions)

    ctx = _reports_to_context(agent_reports)

    # 注入三线观点
    if time_frame_opinions:
        ctx += "\n--- 三线时间观点 ---\n"
        for frame, opinion in time_frame_opinions.items():
            sig = opinion.get("signal", "?")
            cv = opinion.get("conviction", 0)
            reasoning = opinion.get("reasoning", "")[:60]
            ctx += f"[{frame}] {sig} conviction={cv} {reasoning}\n"
    bull_prompt = DEBATE_PROMPTS["bull_researcher"]
    bear_prompt = DEBATE_PROMPTS["bear_researcher"]
    mod_prompt = DEBATE_PROMPTS["moderator"]

    rounds = []

    # ── Round 1: 多头初始 → 空头反驳 ──
    print(f"\n{section_div(' ROUND 1: 多头研究员 -> 空头研究员 ')}")

    bull_r1_raw = deepseek_chat(bull_prompt, f"分析报告:\n{ctx}\n\n请给出你的看多论点。")
    bull_r1 = _parse_json(bull_r1_raw)
    rounds.append({"round": 1, "role": "bull", "output": bull_r1})
    print(card_line(f"[多 BULL] {_fmt_json(bull_r1)[:280]}"))

    bear_r1_raw = deepseek_chat(bear_prompt, f"多头论点:\n{_fmt_json(bull_r1)}\n\n请逐条反驳。")
    bear_r1 = _parse_json(bear_r1_raw)
    rounds.append({"round": 1, "role": "bear", "output": bear_r1})
    print(card_line(f"[空 BEAR] {_fmt_json(bear_r1)[:280]}"))
    print(card_bottom())

    # ── Round 2: 多头回应 → 空头最终 ──
    print(f"\n{section_div(' ROUND 2: 多头回应 -> 空头最终 ')}")

    bull_r2_raw = deepseek_chat(bull_prompt, f"空头反驳:\n{_fmt_json(bear_r1)}\n\n请逐条回应。")
    bull_r2 = _parse_json(bull_r2_raw)
    rounds.append({"round": 2, "role": "bull", "output": bull_r2})
    print(card_line(f"[多 BULL] {_fmt_json(bull_r2)[:280]}"))

    bear_r2_raw = deepseek_chat(bear_prompt, f"多头回应:\n{_fmt_json(bull_r2)}\n\n请做最终反驳。")
    bear_r2 = _parse_json(bear_r2_raw)
    rounds.append({"round": 2, "role": "bear", "output": bear_r2})
    print(card_line(f"[空 BEAR] {_fmt_json(bear_r2)[:280]}"))
    print(card_bottom())

    # ── 仲裁 ──
    print(f"\n{section_div(' 仲裁员裁决 ')}")

    all_rounds_text = (
        f"多头论点1:\n{_fmt_json(bull_r1)}\n\n"
        f"空头反驳1:\n{_fmt_json(bear_r1)}\n\n"
        f"多头回应2:\n{_fmt_json(bull_r2)}\n\n"
        f"空头最终2:\n{_fmt_json(bear_r2)}"
    )

    # 包含三线观点的仲裁prompt
    mod_input = f"辩论记录:\n{all_rounds_text}\n\n"
    if time_frame_opinions:
        mod_input += "\n三线观点:\n"
        for frame, opinion in time_frame_opinions.items():
            sig = opinion.get("signal", "?")
            cv = opinion.get("conviction", 0)
            mod_input += f"  {frame}: {sig} conviction={cv}/10\n"
        mod_input += "\n请在仲裁输出中额外包含 leaning_short/leaning_mid/leaning_long (0.0-1.0) 三个评分，表示对短线/中线/长线的倾向程度。"
    mod_input += "\n\n请仲裁。"

    mod_raw = deepseek_chat(mod_prompt, mod_input)
    moderation = _parse_json(mod_raw)
    print(card_line(f"[裁 MODERATOR] {_fmt_json(moderation)[:400]}"))
    print(card_bottom())

    # 提取三线倾向评分
    leaning_short = float(moderation.get("leaning_short", 0.5)) if not moderation.get("parse_error") else 0.5
    leaning_mid = float(moderation.get("leaning_mid", 0.5)) if not moderation.get("parse_error") else 0.5
    leaning_long = float(moderation.get("leaning_long", 0.5)) if not moderation.get("parse_error") else 0.5

    return {
        "rounds": rounds,
        "moderation": moderation,
        "all_rounds_text": all_rounds_text,
        "leaning_short": leaning_short,
        "leaning_mid": leaning_mid,
        "leaning_long": leaning_long,
    }


def _mock_debate(agent_reports: list[dict], time_frame_opinions: dict = None) -> dict:
    """基于Agent报告生成模拟辩论（无需API调用）。"""
    buy_count = sum(1 for r in agent_reports if r.get("signal") == "BUY")
    sell_count = sum(1 for r in agent_reports if r.get("signal") == "SELL")
    total = len(agent_reports)
    bull_score = round(buy_count / total, 2) if total else 0.5
    bear_score = round(sell_count / total, 2) if total else 0.5

    buy_reasons = [r.get("reasoning", "") for r in agent_reports if r.get("signal") == "BUY"]
    sell_reasons = [r.get("reasoning", "") for r in agent_reports if r.get("signal") == "SELL"]

    bull_points = buy_reasons[:2] if buy_reasons else ["估值具备安全边际，技术指标显示底部特征"]
    bear_points = sell_reasons[:2] if sell_reasons else ["市场情绪偏弱，短期缺乏催化因素"]

    winner = "BULL" if bull_score > bear_score else "BEAR" if bear_score > bull_score else "TIE"

    # 从三线观点中提取倾向
    leaning_short = 0.5
    leaning_mid = 0.5
    leaning_long = 0.5
    if time_frame_opinions:
        tf_map = {"BUY": 0.7, "SELL": 0.3, "HOLD": 0.5}
        short = time_frame_opinions.get("short_term", {})
        mid = time_frame_opinions.get("mid_term", {})
        long_tf = time_frame_opinions.get("long_term", {})
        leaning_short = tf_map.get(short.get("signal", "HOLD"), 0.5)
        leaning_mid = tf_map.get(mid.get("signal", "HOLD"), 0.5)
        leaning_long = tf_map.get(long_tf.get("signal", "HOLD"), 0.5)

    return {
        "rounds": [
            {"round": 1, "role": "bull", "output": {
                "points": bull_points, "conviction": bull_score, "response": ""
            }},
            {"round": 1, "role": "bear", "output": {
                "points": bear_points, "conviction": bear_score, "response_to_bull": "多头论据需更多数据支撑"
            }},
            {"round": 2, "role": "bull", "output": {
                "points": ["综合多Agent分析，看多信号占优"], "conviction": bull_score,
                "response_to_bear": "空头担忧已在价格中反映"
            }},
            {"round": 2, "role": "bear", "output": {
                "points": ["风险因素不可忽视"], "conviction": bear_score,
                "response": "短期波动可能加剧"
            }},
        ],
        "moderation": {
            "bull_score": bull_score, "bear_score": bear_score, "winner": winner,
            "final_leaning": "偏多" if winner == "BULL" else "偏空" if winner == "BEAR" else "中性",
            "summary": f"多空辩论完成：多头得分{bull_score}，空头得分{bear_score}，{winner}方略占优势。",
            "key_divergence": "多空双方对当前估值水平和市场情绪存在分歧",
            "blind_spots": "极端行情下的流动性风险未充分讨论",
            "leaning_short": leaning_short,
            "leaning_mid": leaning_mid,
            "leaning_long": leaning_long,
        },
        "all_rounds_text": "Mock模式辩论记录",
        "leaning_short": leaning_short,
        "leaning_mid": leaning_mid,
        "leaning_long": leaning_long,
    }
