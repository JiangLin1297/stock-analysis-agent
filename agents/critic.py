"""
Critic Agent — 代表"我"（极度追求收益的投资者）审查整个系统。
真实地跑一遍全流程，然后尖刻点评并给出修改指令。
"""
import sys
import os
import io
import json
import re
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    import certifi
    os.environ['SSL_CERT_FILE'] = certifi.where()
    os.environ['REQUESTS_CA_BUNDLE'] = certifi.where()
except Exception:
    pass

from agents.prompts import ALL_PROMPTS
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
    return {"raw": clean, "parse_error": True, "overall_score": 3,
            "verdict": "解析失败，Critic Agent输出格式异常", "must_fix": []}


def _capture_run(fn, *args, **kwargs):
    """运行函数并捕获 stdout 输出。"""
    old = sys.stdout
    captured = io.StringIO()
    sys.stdout = captured
    try:
        result = fn(*args, **kwargs)
    except Exception as e:
        import traceback
        result = {"ERROR": str(e), "traceback": traceback.format_exc()}
    sys.stdout = old
    output = captured.getvalue()
    return result, output


def critic_evaluate(symbol: str = "600519", market: str = "A",
                    use_mock: bool = True) -> dict:
    """
    代表"我"审查整个分析系统：跑全流程 → 尖刻点评 → 给出修改指令。

    Args:
        symbol: 股票代码
        market: 市场类型
        use_mock: True=使用Mock模式（更快），False=调用DeepSeek API

    Returns:
        {"overall_score": 0-10, "data_ok": bool, "data_issues": [...],
         "logic_ok": bool, "conservative_flaws": [...], "verdict": "刻薄点评",
         "must_fix": ["修改指令1", ...], "score_breakdown": {...},
         "_run_log": "完整运行日志", "_data": {...}, "_reports": [...],
         "_time_opinions": {...}, "_debate": {...}, "_decision": {...}}
    """
    print(f"\n{'='*60}")
    print(f"  🔍 CRITIC AGENT — 审查系统对 {symbol} 的分析")
    print(f"{'='*60}\n")

    # ═══ 1. 数据层检查 ═══
    print("── [1/5] 数据完整性检查 ──")
    from data.pipeline import get_compressed_data
    data, data_log = _capture_run(get_compressed_data, symbol, market)

    quote = data.get("quote", {})
    tech = data.get("technical", {})
    fin = data.get("financial", {})
    news = data.get("news", [])

    data_issues = []
    if not quote.get("price"):
        data_issues.append(f"quote.price 缺失 (got {quote.get('price')})")
    if not tech.get("ma5"):
        data_issues.append(f"technical.ma5 缺失 (got {tech.get('ma5')})")
    if not fin.get("roe"):
        data_issues.append(f"financial.roe 缺失 (got {fin.get('roe')})")
    if not news:
        data_issues.append("news 为空")
    data_ok = len(data_issues) == 0
    if not data_ok:
        print(f"  ⚠ 数据问题: {'; '.join(data_issues)}")
    else:
        print(f"  ✅ 数据完整: {quote.get('name','?')} PE={quote.get('pe','?')} ROE={fin.get('roe','?')}%")

    # ═══ 2. Agent 运行 ═══
    print("\n── [2/5] Agent 分析检查 ──")
    from agents.runner import run_all_agents
    reports, agent_log = _capture_run(run_all_agents, data, use_mock=use_mock)

    if not reports or len(reports) < 4:
        print(f"  ⚠ Agent返回不足: {len(reports) if reports else 0}/5")
        data_issues.append(f"Agent返回{len(reports) if reports else 0}/5个报告")
    else:
        sigs = [r.get("signal", "?") for r in reports]
        print(f"  ✅ {len(reports)}个Agent完成: {', '.join(sigs)}")

    # ═══ 3. 三线时间分析 ═══
    print("\n── [3/5] 三线时间维度检查 ──")
    from agents.time_frame import run_time_frame_agents
    time_opinions, tf_log = _capture_run(run_time_frame_agents, data, reports, use_mock=use_mock)

    for tf in ["short_term", "mid_term", "long_term"]:
        o = time_opinions.get(tf, {})
        sig = o.get("signal", "?")
        cv = o.get("conviction", 0)
        print(f"  {tf}: {sig} conviction={cv}/10")

    # ═══ 4. 辩论检查 ═══
    print("\n── [4/5] 多空辩论检查 ──")
    from agents.debate import run_debate
    debate, debate_log = _capture_run(run_debate, reports, use_mock=use_mock,
                                      time_frame_opinions=time_opinions)

    mod = debate.get("moderation", {})
    winner = mod.get("winner", "?")
    print(f"  辩论结果: {winner}方胜出 | 倾向: 短{debate.get('leaning_short',0.5):.1f}/中{debate.get('leaning_mid',0.5):.1f}/长{debate.get('leaning_long',0.5):.1f}")

    # ═══ 5. 最终决策 ═══
    print("\n── [5/5] 三维决策检查 ──")
    from agents.decision import make_decision
    decision, decision_log = _capture_run(make_decision, data, reports, debate,
                                           use_mock=use_mock, time_frame_opinions=time_opinions)

    for dim in ["short_term", "mid_term", "long_term"]:
        d = decision.get(dim, {})
        if d:
            a = d.get("action", "?")
            c = d.get("confidence", 0)
            p = d.get("position_pct", 0)
            pm = d.get("potential_multiplier", "")
            pm_str = f" | 潜力: {pm}" if pm else ""
            print(f"  {dim}: {a} conf={c:.0%} pos={p}%{pm_str}")
    verdict = decision.get("overall_verdict", "")
    print(f"  综合: {verdict}")

    # ═══ 构建完整运行日志 ═══
    run_log = f"""[数据层]
{data_log[:500]}

[Agent层]
{agent_log[:1000]}

[三线时间分析]
{tf_log[:600]}

[辩论]
{debate_log[:600]}

[三维决策]
{decision_log[:600]}
"""

    # ═══ 6. Critic 点评 ═══
    print("\n── ★ CRITIC 点评 ──")

    # 构建context
    context_lines = [
        f"标的: {symbol} {quote.get('name','?')}",
        f"价格: {quote.get('price','?')} PE={quote.get('pe','?')} PB={quote.get('pb','?')}",
        f"ROE={fin.get('roe','?')}% 营收增速={fin.get('revenue_growth','?')}% 净利增速={fin.get('net_profit_growth','?')}%",
        f"MA5={tech.get('ma5','?')} MA20={tech.get('ma20','?')} RSI={tech.get('rsi14','?')}",
        f"数据完整: {data_ok} 问题: {';'.join(data_issues) if data_issues else '无'}",
        "",
        "--- Agent信号 ---",
    ]
    for r in (reports or []):
        context_lines.append(f"  [{r.get('agent','?')}] {r.get('signal','?')} score={r.get('score',0)} conf={r.get('confidence',0)} {r.get('reasoning','')[:60]}")
    context_lines.append("")
    context_lines.append("--- 三线观点 ---")
    for tf in ["short_term", "mid_term", "long_term"]:
        o = time_opinions.get(tf, {})
        context_lines.append(f"  {tf}: {o.get('signal','?')} conv={o.get('conviction',5)}/10 {o.get('reasoning','')[:60]}")
    context_lines.append("")
    context_lines.append("--- 辩论 ---")
    context_lines.append(f"  winner={winner} 短={debate.get('leaning_short',0.5)} 中={debate.get('leaning_mid',0.5)} 长={debate.get('leaning_long',0.5)}")
    context_lines.append("")
    context_lines.append("--- 三维决策 ---")
    for dim in ["short_term", "mid_term", "long_term"]:
        d = decision.get(dim, {})
        context_lines.append(f"  {dim}: {d.get('action','?')} conf={d.get('confidence',0)} pos={d.get('position_pct',0)}% {d.get('rationale','')[:60]}")
    context_lines.append(f"  综合: {decision.get('overall_verdict','')}")

    context = "\n".join(context_lines)
    prompt = ALL_PROMPTS["critic_agent"]

    if use_mock:
        # ── Critic 深度评估（Mock模式）──
        conservative_flaws = []
        aggressive_flaws = []
        must_fix = []

        # ── 1. 信号质量检查 ──
        hold_count = 0
        buy_count = 0
        sell_count = 0
        for r in (reports or []):
            sig = r.get("signal", "HOLD")
            score = r.get("score", 0)
            conf = r.get("confidence", 0.5)
            agent = r.get("agent", "?")

            if sig == "HOLD":
                hold_count += 1
                if score >= -1:
                    conservative_flaws.append(f"{agent}给出HOLD但score={score}，可能错失机会")
            elif sig in ("BUY", "CAUTIOUS_BUY"):
                buy_count += 1
                if conf < 0.6:
                    conservative_flaws.append(f"{agent}看多但置信度仅{conf:.0%}，信号不够坚决")
            elif sig in ("SELL", "CAUTIOUS_SELL"):
                sell_count += 1

        if hold_count >= 3:
            conservative_flaws.append(f"过半Agent({hold_count}/5)给出HOLD，系统整体过于保守")
            must_fix.append("agent_runner.py:'score >= 3' → 'score >= 2' 降低综合BUY阈值")

        # ── 2. 三线BUY信号覆盖检查 ──
        buy_dims = [tf for tf in ["short_term", "mid_term", "long_term"]
                    if time_opinions.get(tf, {}).get("signal") == "BUY"]
        hold_dims = [tf for tf in ["short_term", "mid_term", "long_term"]
                     if time_opinions.get(tf, {}).get("signal") == "HOLD"]
        if not buy_dims:
            conservative_flaws.append("三线均无BUY信号，系统过于保守")
            must_fix.append("agents/decision.py:'score >= 6' → 'score >= 5' 降低三线BUY阈值")
        if len(buy_dims) >= 2:
            aggressive_flaws.append(f"{len(buy_dims)}个时间维度同时看多，注意追高风险")

        # ── 3. 仓位与风险收益比检查 ──
        for dim in ["short_term", "mid_term", "long_term"]:
            d = decision.get(dim, {})
            if not d:
                continue
            pos = d.get("position_pct", 0)
            conf = d.get("confidence", 0)
            action = d.get("action", "HOLD")
            entry = d.get("entry_price", 0)
            stop = d.get("stop_loss_price", 0)
            target = d.get("take_profit_price", 0)

            # 仓位检查
            if action in ("BUY", "CAUTIOUS_BUY"):
                min_pos = {"short_term": 8, "mid_term": 12, "long_term": 18}
                if pos < min_pos.get(dim, 10):
                    conservative_flaws.append(f"{dim}仓位{pos}%偏小，应≥{min_pos[dim]}%")
                    must_fix.append(f"agents/decision.py:'\"position_pct\": {pos}' → '\"position_pct\": {min_pos[dim]}' 提高{dim}仓位")

                # 风险收益比检查（target可能为None-动态退出模式）
                if entry and entry > 0 and stop and stop > 0 and target and target > 0:
                    risk = entry - stop
                    reward = target - entry
                    if risk > 0 and reward > 0:
                        rr = reward / risk
                        if rr < 1.5:
                            aggressive_flaws.append(f"{dim}风险收益比仅{rr:.1f}:1，不值得交易")
                        elif rr > 5:
                            aggressive_flaws.append(f"{dim}风险收益比{rr:.1f}:1，目标可能过于乐观")

            # 置信度检查
            if action in ("BUY", "CAUTIOUS_BUY") and conf < 0.55:
                conservative_flaws.append(f"{dim}置信度{conf:.0%}偏低，信号质量存疑")

        # ── 4. 止损宽度检查 ──
        short = decision.get("short_term", {})
        long_t = decision.get("long_term", {})
        if short and long_t:
            s_entry = short.get("entry_price", 0)
            s_stop = short.get("stop_loss_price", 0)
            l_entry = long_t.get("entry_price", 0)
            l_stop = long_t.get("stop_loss_price", 0)
            if s_entry > 0 and s_stop > 0 and l_entry > 0 and l_stop > 0:
                s_stop_pct = abs(s_entry - s_stop) / s_entry * 100
                l_stop_pct = abs(l_entry - l_stop) / l_entry * 100
                if s_stop_pct < 2:
                    conservative_flaws.append(f"短线止损{s_stop_pct:.1f}%过窄，容易被震出")
                elif s_stop_pct > 8:
                    aggressive_flaws.append(f"短线止损{s_stop_pct:.1f}%过宽，单笔亏损过大")
                if l_stop_pct < 10:
                    conservative_flaws.append(f"长线止损{l_stop_pct:.1f}%过窄，不给成长空间")
                    must_fix.append("agents/decision.py:'0.85' → '0.75' 放宽长线止损")

        # ── 5. 长线成长潜力检查 ──
        long_tf = time_opinions.get("long_term", {})
        if not long_tf.get("potential_multiplier"):
            conservative_flaws.append("长线未给出成长潜力评估，错过数倍股机会")

        # ── 6. 多空辩论质量检查 ──
        mod = debate.get("moderation", {})
        bull_score = mod.get("bull_score", 0.5)
        bear_score = mod.get("bear_score", 0.5)
        if abs(bull_score - bear_score) < 0.1:
            conservative_flaws.append(f"多空辩论接近平局(bull={bull_score}, bear={bear_score})，系统缺乏明确方向")

        # ── 7. 目标达成潜力检查 (can_hit_target) ──
        can_hit_target = {"short": {"expected": 0, "target": 10, "ok": True},
                          "mid": {"expected": 0, "target": 30, "ok": True},
                          "long": {"expected": 0, "target": 100, "ok": True, "has_doubling_logic": False}}

        for dim_key, dim_target, dim_label in [
            ("short_term", 10, "短线"),
            ("mid_term", 30, "中线"),
            ("long_term", 100, "长线"),
        ]:
            dim_decision = decision.get(dim_key, {})
            dim_time = time_opinions.get(dim_key, {})
            action = dim_decision.get("action", "HOLD")
            expected = dim_decision.get("expected_return_pct") or dim_time.get("expected_return_pct") or 0
            doubling_logic = dim_decision.get("doubling_logic") or dim_time.get("doubling_logic", "")

            can_key = "short" if dim_key == "short_term" else ("mid" if dim_key == "mid_term" else "long")
            can_hit_target[can_key]["expected"] = expected

            if action in ("BUY", "CAUTIOUS_BUY"):
                if expected < dim_target:
                    can_hit_target[can_key]["ok"] = False
                    can_hit_target[can_key]["gap"] = round(expected - dim_target, 1)
                    conservative_flaws.append(
                        f"{dim_label}BUY信号但预期收益{expected}%<目标{dim_target}%，差距{dim_target - expected}%→扣3分"
                    )
                    must_fix.append(
                        f"agents/prompts.py: {dim_label.upper()}_TERM_PROMPT应提高{dim_label}预期收益目标，当前{expected}%不足{dim_target}%"
                    )
                if dim_key == "long_term" and not doubling_logic:
                    can_hit_target["long"]["has_doubling_logic"] = False
                    can_hit_target["long"]["ok"] = False
                    conservative_flaws.append("长线BUY但无翻倍逻辑(doubling_logic)，未给出2倍以上路径→扣3分")
                    must_fix.append("agents/prompts.py: LONG_TERM_PROMPT必须要求输出doubling_logic翻倍路径")
                elif dim_key == "long_term" and doubling_logic:
                    can_hit_target["long"]["has_doubling_logic"] = True
            elif action == "HOLD" and dim_key == "long_term" and not doubling_logic:
                can_hit_target["long"]["has_doubling_logic"] = False

        # 日志记录"预计上涨空间 vs 目标"对比
        target_log = (f"目标达成检查: "
                      f"短线预期{can_hit_target['short']['expected']}% vs 目标10% {'✓' if can_hit_target['short']['ok'] else '✗'}, "
                      f"中线预期{can_hit_target['mid']['expected']}% vs 目标30% {'✓' if can_hit_target['mid']['ok'] else '✗'}, "
                      f"长线预期{can_hit_target['long']['expected']}% vs 目标100% {'✓' if can_hit_target['long']['ok'] else '✗'}"
                      f" | 翻倍逻辑={'有' if can_hit_target['long']['has_doubling_logic'] else '无'}")
        print(f"  📊 {target_log}")

        # ── 综合评分 ──
        # 先计算基础问题数（不含can_hit_target的扣分）
        base_flaw_count = sum(1 for f in conservative_flaws if "预期收益" not in f and "翻倍逻辑" not in f)
        total_issues = base_flaw_count + len(aggressive_flaws)
        if total_issues == 0:
            score = 9
        elif total_issues <= 2:
            score = 7
        elif total_issues <= 4:
            score = 5
        elif total_issues <= 6:
            score = 3
        else:
            score = 1

        # 如果只有保守问题无激进问题，额外扣1分（说明系统缺乏进攻性）
        if conservative_flaws and not aggressive_flaws:
            score = max(1, score - 1)

        # can_hit_target 扣分
        if not can_hit_target["short"]["ok"]:
            score = max(1, score - 3)
        if not can_hit_target["mid"]["ok"]:
            score = max(1, score - 3)
        if not can_hit_target["long"]["ok"]:
            score = max(1, score - 3)

        # ── 动态刻薄点评 ──
        all_flaws = conservative_flaws + aggressive_flaws
        if not all_flaws:
            verdict_text = "系统状态良好！攻守兼备，继续保持。但别骄傲，市场永远在变。"
        elif score >= 7:
            verdict_text = f"还行，但{len(all_flaws)}个小问题，优化后能更好：{'; '.join(all_flaws[:2])}"
        elif score >= 5:
            verdict_text = f"平庸！{len(all_flaws)}个问题——保守({len(conservative_flaws)})/激进({len(aggressive_flaws)})，必须改！"
        else:
            verdict_text = f"差劲！{len(conservative_flaws)}个保守+{len(aggressive_flaws)}个激进问题。主要问题: {'; '.join(all_flaws[:2])}"

        result = {
            "overall_score": score,
            "data_ok": data_ok,
            "data_issues": data_issues,
            "logic_ok": len(conservative_flaws) <= 2,
            "conservative_flaws": conservative_flaws,
            "aggressive_flaws": aggressive_flaws,
            "can_hit_target": can_hit_target,
            "verdict": verdict_text,
            "must_fix": must_fix if must_fix else (
                ["agents/decision.py:'score >= 6' → 'score >= 4' 进一步降低BUY阈值"]
                if conservative_flaws else []
            ),
            "score_breakdown": {
                "data_integrity": 9 if data_ok else 4,
                "analysis_depth": 8 if len(reports) >= 4 else 3,
                "actionability": 7 if len(buy_dims) > 0 else 3,
                "aggressiveness": min(10, 10 - len(conservative_flaws)),
                "risk_control": min(10, 10 - len(aggressive_flaws)),
                "potential_return": 7 if long_tf.get("potential_multiplier") else 4,
                "can_hit_target": sum(1 for k in ["short", "mid", "long"] if can_hit_target[k]["ok"]) * 3 + 1,
            },
        }
    else:
        # 使用 DeepSeek API 进行Critic评审
        try:
            raw = deepseek_chat(prompt, f"请严格审查以下完整分析输出:\n\n{context}\n\n完整日志:\n{run_log[:3000]}")
            result = _parse_json(raw)
        except Exception as e:
            result = {
                "overall_score": 5,
                "data_ok": data_ok,
                "data_issues": data_issues,
                "logic_ok": True,
                "conservative_flaws": [],
                "aggressive_flaws": [],
                "verdict": f"Critic API调用失败: {e}",
                "must_fix": [],
                "score_breakdown": {},
            }

    # 附加运行数据
    result["_run_log"] = run_log
    result["_data"] = data
    result["_reports"] = reports
    result["_time_opinions"] = time_opinions
    result["_debate"] = debate
    result["_decision"] = decision

    # 打印点评
    print(f"\n  ╔══ CRITIC 评分 ══╗")
    print(f"  ║ 总分: {result.get('overall_score', '?')}/10         ║")
    print(f"  ║ 数据: {'✅' if data_ok else '❌'}  逻辑: {'✅' if result.get('logic_ok') else '❌'}      ║")
    print(f"  ╚══════════════════╝")
    print(f"  💬 {result.get('verdict', '')}")
    c_flaws = result.get("conservative_flaws", [])
    a_flaws = result.get("aggressive_flaws", [])
    if c_flaws:
        print(f"  📋 保守问题 ({len(c_flaws)}):")
        for f in c_flaws[:5]:
            print(f"    • {f}")
    if a_flaws:
        print(f"  ⚡ 激进问题 ({len(a_flaws)}):")
        for f in a_flaws[:5]:
            print(f"    • {f}")
    sb = result.get("score_breakdown", {})
    if sb:
        dims = " | ".join(f"{k}={v}" for k, v in sb.items())
        print(f"  📊 维度: {dims}")
    fixes = result.get("must_fix", [])
    if fixes:
        print(f"  🔧 修改指令:")
        for f in fixes:
            print(f"    • {f}")
    print()

    return result


def _compress_backtest_context(metrics: dict, closed_trades: list, symbol: str,
                                start_date: str, end_date: str) -> str:
    """将回测结果压缩为精简摘要，避免 prompt 过长导致 API 超时。

    只保留: 核心指标 + 最近10笔交易明细 + 三线达成率。
    删除: 逐日权益曲线、重复的开仓/平仓计数、原始 trade_log 全量。
    """
    lines = [
        f"标的:{symbol} 区间:{start_date}→{end_date}",
        f"收益:{metrics.get('total_return_pct',0):+.2f}% 回撤:{metrics.get('max_drawdown_pct',0):.1f}% "
        f"夏普:{metrics.get('sharpe_ratio',0):.2f} 胜率:{metrics.get('win_rate_pct',0):.1f}%",
        f"交易:{metrics.get('total_trades',0)}笔(胜{metrics.get('win_trades',0)}/负{metrics.get('loss_trades',0)}) "
        f"盈亏比:{metrics.get('profit_factor',0):.2f}",
        f"三线达成:短{metrics.get('achievement_short',0):.0f}% 中{metrics.get('achievement_mid',0):.0f}% "
        f"长{metrics.get('achievement_long',0):.0f}%",
        f"三线收益:短{metrics.get('return_short',0):+.2f}% 中{metrics.get('return_mid',0):+.2f}% "
        f"长{metrics.get('return_long',0):+.2f}%",
    ]
    if closed_trades:
        lines.append(f"最近{min(10, len(closed_trades))}笔平仓:")
        for t in closed_trades[-10:]:
            lines.append(f"  {t['date']} {t['timeframe']} {t['action']} "
                        f"PnL={t['pnl_pct']:+.2f}% 原因:{t.get('reason','')[:30]}")
    return "\n".join(lines)


def _heuristic_critique_fallback(metrics: dict, closed_trades: list) -> dict:
    """本地启发式 Critic — 当 DeepSeek API 超时时降级使用。

    保证至少产出 1 条有效修改指令。
    """
    must_fix = []
    issues = []

    total_return = metrics.get("total_return_pct", 0)
    max_dd = metrics.get("max_drawdown_pct", 0)
    win_rate = metrics.get("win_rate_pct", 0)
    total_trades = metrics.get("total_trades", 0)

    # 规则1: 胜率 < 20% → 下调 BUY 阈值 3 分
    if win_rate < 20:
        issues.append(f"胜率仅{win_rate:.0f}%过低，信号质量严重不足")
        must_fix.append("factor_weights.json: short.threshold 降低3分以放宽入场")

    # 规则2: 最大回撤 > 20% → 止损收紧 20%
    if max_dd > 20:
        issues.append(f"最大回撤{max_dd:.1f}%过大，风控失效")
        must_fix.append("agents/decision.py: 止损宽度收紧20%，强化风险控制")

    # 规则3: 交易次数 > 15 且胜率 < 30% → 减少仓位上限 10%
    if total_trades > 15 and win_rate < 30:
        issues.append(f"{total_trades}笔交易胜率仅{win_rate:.0f}%，频繁交易但质量差")
        must_fix.append("agents/decision.py: 单线最大仓位上限减少10%")

    # 规则4: 负收益 → 降低阈值
    if total_return < 0 and not any("阈值" in f or "threshold" in f for f in must_fix):
        issues.append(f"总收益{total_return:+.1f}%为负")
        must_fix.append("factor_weights.json: 各线threshold降低2分以捕捉更多机会")

    # 规则5: 交易过少
    if total_trades < 3:
        issues.append(f"仅{total_trades}笔交易，系统过于保守")
        must_fix.append("factor_weights.json: short.threshold降低5分以增加交易频率")

    # 保底: 确保至少有 1 条指令
    if not must_fix:
        if total_return < 20:
            must_fix.append("factor_weights.json: short.threshold降低2分以提升进攻性")
            issues.append(f"收益率{total_return:+.1f}%未达预期")
        else:
            must_fix.append("agents/decision.py: 动态止盈触发阈值提高5%以放大盈利")
            issues.append("系统运行正常但止盈可能过早")

    # 评分
    score = 5
    if total_return > 30 and win_rate > 40:
        score = 7
    elif total_return < -10 or win_rate < 15:
        score = 2
    elif total_return < 0:
        score = 3

    loss_trades = [t for t in closed_trades if t.get("pnl", 0) < 0]
    if loss_trades:
        avg_loss = sum(t["pnl_pct"] for t in loss_trades) / len(loss_trades)
        lost_analysis = f"{len(loss_trades)}笔亏损，平均亏{avg_loss:+.1f}%"
    else:
        lost_analysis = "无亏损交易"

    return {
        "overall_score": score,
        "main_issue": issues[0] if issues else "启发式分析完成",
        "all_issues": issues,
        "lost_trades_analysis": lost_analysis,
        "target_achievement": {
            "short": metrics.get("achievement_short", 0) >= 30,
            "mid": metrics.get("achievement_mid", 0) >= 30,
            "long": metrics.get("achievement_long", 0) >= 30,
        },
        "must_fix": must_fix[:4],
        "score_breakdown": {
            "return_quality": min(10, max(1, int(6 + total_return / 10))),
            "risk_control": min(10, max(1, int(10 - max_dd / 5))),
            "win_quality": min(10, max(1, int(win_rate / 10))),
        },
        "_source": "heuristic_fallback",
    }


def _generate_code_changes(metrics: dict, closed_trades: list) -> list:
    """基于回测指标，读取实际文件，生成精确的 old_code → new_code 替换指令。"""
    code_changes = []

    total_return = metrics.get("total_return_pct", 0)
    max_dd = metrics.get("max_drawdown_pct", 0)
    win_rate = metrics.get("win_rate_pct", 0)
    total_trades = metrics.get("total_trades", 0)

    # 项目根目录
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # ── 1. factor_weights.json 阈值调整 ──
    weights_path = os.path.join(project_root, "analysis", "factor_weights.json")
    try:
        with open(weights_path, 'r', encoding='utf-8') as f:
            weights = json.load(f)
    except Exception:
        weights = None

    if weights:
        if win_rate < 20 and total_trades >= 3:
            for tf in ["short", "mid", "long"]:
                threshold = weights.get(tf, {}).get("threshold", 55)
                new_threshold = max(40, threshold - 3)
                if new_threshold != threshold:
                    code_changes.append({
                        "file": "analysis/factor_weights.json",
                        "function": "",
                        "old_code": f'"threshold": {threshold}',
                        "new_code": f'"threshold": {new_threshold}',
                        "reason": f"胜率仅{win_rate:.0f}%，降低{tf}线阈值{threshold}→{new_threshold}",
                    })

        if total_return < 0 and not any(c["file"] == "analysis/factor_weights.json" for c in code_changes):
            for tf in ["short", "mid", "long"]:
                threshold = weights.get(tf, {}).get("threshold", 55)
                new_threshold = max(40, threshold - 2)
                if new_threshold != threshold:
                    code_changes.append({
                        "file": "analysis/factor_weights.json",
                        "function": "",
                        "old_code": f'"threshold": {threshold}',
                        "new_code": f'"threshold": {new_threshold}',
                        "reason": f"负收益{total_return:+.1f}%，降低{tf}线阈值{threshold}→{new_threshold}",
                    })

        if total_trades < 3:
            for tf in ["short", "mid"]:
                threshold = weights.get(tf, {}).get("threshold", 55)
                new_threshold = max(40, threshold - 5)
                if new_threshold != threshold:
                    code_changes.append({
                        "file": "analysis/factor_weights.json",
                        "function": "",
                        "old_code": f'"threshold": {threshold}',
                        "new_code": f'"threshold": {new_threshold}',
                        "reason": f"仅{total_trades}笔交易，降低{tf}线阈值{threshold}→{new_threshold}增加交易频率",
                    })

    # ── 2. agents/decision.py 常量调整 ──
    decision_path = os.path.join(project_root, "agents", "decision.py")
    try:
        with open(decision_path, 'r', encoding='utf-8') as f:
            decision_content = f.read()
    except Exception:
        decision_content = ""

    if decision_content:
        # 仓位上限：回撤过大 → 降低
        if max_dd > 30:
            import re as _re
            m = _re.search(r'MAX_POSITION_SHORT\s*=\s*(\d+)', decision_content)
            if m:
                old_val = int(m.group(1))
                new_val = max(10, old_val - 5)
                if new_val != old_val:
                    code_changes.append({
                        "file": "agents/decision.py",
                        "function": "generate_factor_signal",
                        "old_code": f'MAX_POSITION_SHORT = {old_val}',
                        "new_code": f'MAX_POSITION_SHORT = {new_val}',
                        "reason": f"最大回撤{max_dd:.1f}%过大，降低短线仓位上限{old_val}→{new_val}",
                    })

        # 冷却期：连续亏损多 → 缩短冷却
        loss_trades = [t for t in closed_trades if t.get("pnl", 0) < 0]
        if len(loss_trades) >= 5:
            import re as _re
            m = _re.search(r'COOLDOWN_DAYS\s*=\s*(\d+)', decision_content)
            if m:
                old_val = int(m.group(1))
                new_val = min(10, old_val + 2)
                if new_val != old_val:
                    code_changes.append({
                        "file": "agents/decision.py",
                        "function": "generate_factor_signal",
                        "old_code": f'COOLDOWN_DAYS = {old_val}',
                        "new_code": f'COOLDOWN_DAYS = {new_val}',
                        "reason": f"亏损交易{len(loss_trades)}笔过多，延长冷却期{old_val}→{new_val}天",
                    })

    # ── 3. analysis/holding.py ATR止损调整 ──
    holding_path = os.path.join(project_root, "analysis", "holding.py")
    try:
        with open(holding_path, 'r', encoding='utf-8') as f:
            holding_content = f.read()
    except Exception:
        holding_content = ""

    if holding_content:
        stop_loss_count = sum(1 for t in closed_trades if "止损" in t.get("reason", ""))
        if stop_loss_count >= 3:
            import re as _re
            m = _re.search(r'ATR_STOP_MID\s*=\s*(\d+\.?\d*)', holding_content)
            if m:
                old_val = float(m.group(1))
                new_val = round(old_val + 0.5, 1)
                code_changes.append({
                    "file": "analysis/holding.py",
                    "function": "evaluate_holding",
                    "old_code": f'ATR_STOP_MID = {old_val}',
                    "new_code": f'ATR_STOP_MID = {new_val}',
                    "reason": f"止损触发{stop_loss_count}次过多，放宽中线ATR止损{old_val}→{new_val}",
                })

    return code_changes[:5]  # 最多5条


def critique_backtest(backtest_result: dict, use_mock: bool = True) -> dict:
    """
    审查回测结果，给出评分和改进指令。

    Args:
        backtest_result: run_backtest() 的输出字典
        use_mock: True=启发式分析，False=调用DeepSeek API

    Returns:
        {"overall_score": 0-10, "main_issue": "...", "lost_trades_analysis": "...",
         "target_achievement": {"short": bool, "mid": bool, "long": bool},
         "must_fix": ["指令1", ...], "score_breakdown": {...}}
    """
    metrics = backtest_result.get("metrics", {})
    trade_log = backtest_result.get("trade_log", [])
    equity_curve = backtest_result.get("equity_curve", [])
    symbol = backtest_result.get("symbol", "?")

    print(f"\n{'='*60}")
    print(f"  🔍 CRITIC BACKTEST — 审查回测: {symbol}")
    print(f"  {backtest_result.get('start_date','?')} → {backtest_result.get('end_date','?')}")
    print(f"{'='*60}\n")

    # 构建分析上下文
    context_lines = [
        f"回测标的: {symbol}",
        f"区间: {backtest_result.get('start_date','?')} → {backtest_result.get('end_date','?')}",
        f"初始资金: ¥{backtest_result.get('initial_capital', 100000):,.0f}",
        f"最终权益: ¥{backtest_result.get('final_equity', 0):,.0f}",
        f"总收益率: {metrics.get('total_return_pct', 0):+.2f}%",
        f"最大回撤: {metrics.get('max_drawdown_pct', 0):.2f}%",
        f"夏普比率: {metrics.get('sharpe_ratio', 0):.2f}",
        f"胜率: {metrics.get('win_rate_pct', 0):.1f}%",
        f"盈亏比: {metrics.get('profit_factor', 0):.2f}",
        f"总交易: {metrics.get('total_trades', 0)} | 胜: {metrics.get('win_trades', 0)} | 负: {metrics.get('loss_trades', 0)}",
        f"三线达成率 — 短{metrics.get('achievement_short', 0):.0f}% 中{metrics.get('achievement_mid', 0):.0f}% 长{metrics.get('achievement_long', 0):.0f}%",
        f"三线收益 — 短{metrics.get('return_short', 0):+.2f}% 中{metrics.get('return_mid', 0):+.2f}% 长{metrics.get('return_long', 0):+.2f}%",
    ]

    # 交易明细摘要
    closed_trades = [t for t in trade_log if t["action"] in ("CLOSE", "CLOSE_FINAL", "TRIM")]
    if closed_trades:
        context_lines.append(f"\n--- 已平仓交易 ({len(closed_trades)}笔) ---")
        for t in closed_trades[-10:]:
            context_lines.append(f"  {t['date']} {t['timeframe']} {t['action']} "
                               f"P={t['price']:.2f} Q={t['quantity']} PnL={t['pnl']:+.2f} ({t['pnl_pct']:+.2f}%)")

    buy_trades = [t for t in trade_log if t["action"] == "BUY"]
    context_lines.append(f"\n开仓次数: {len(buy_trades)}")
    context_lines.append(f"平仓次数: {len(closed_trades)}")

    context = "\n".join(context_lines)

    if use_mock:
        # ── Mock 模式：启发式分析回测结果 ──
        must_fix = []
        issues = []

        total_return = metrics.get("total_return_pct", 0)
        max_dd = metrics.get("max_drawdown_pct", 0)
        sharpe = metrics.get("sharpe_ratio", 0)
        win_rate = metrics.get("win_rate_pct", 0)
        profit_factor = metrics.get("profit_factor", 0)
        total_trades = metrics.get("total_trades", 0)

        # 1. 收益率评估
        if total_return > 50:
            pass  # 优秀
        elif total_return > 20:
            pass  # 良好
        elif total_return > 0:
            issues.append(f"收益率仅{total_return:+.1f}%，缺乏进攻性")
            must_fix.append("请打开 agents/decision.py，找到 generate_factor_signal 函数中的 base_pos 字典，将 short 对应的值从 20 修改为 30，因为短线仓位过保守导致收益不足。")
        else:
            issues.append(f"负收益{total_return:+.1f}%，系统严重保守或信号错误")
            must_fix.append("请打开 analysis/factor_weights.json，找到 short 对象中的 threshold，将其从 55 修改为 48，因为当前阈值过高导致短线信号过少、错过入场机会。")

        # 2. 夏普比率评估
        if sharpe < 0:
            issues.append(f"夏普比率{sharpe:.2f}<0，风险调整后无超额收益")
            must_fix.append("请打开 analysis/holding.py，找到 evaluate_holding 函数中的 ATR_STOP_SHORT 常量，将其从 1.5 修改为 2.5，因为止损太紧导致频繁止损、无法持有盈利仓位。")

        # 3. 最大回撤评估
        if max_dd > 30:
            issues.append(f"最大回撤{max_dd:.1f}%过大，风险控制失效")
            must_fix.append("请打开 agents/decision.py，找到 generate_factor_signal 函数中 position_pct 计算的上限值，将其从 80 修改为 50，因为单线仓位过大导致回撤失控。")

        # 4. 胜率与交易频率
        if total_trades < 3:
            issues.append(f"仅{total_trades}笔交易，系统过于保守/懒惰")
            must_fix.append("请打开 analysis/factor_weights.json，找到 short 对象中的 threshold，将其从 55 修改为 45，因为交易频率过低说明入场条件过严。")
            must_fix.append("请打开 agents/decision.py，找到 generate_factor_signal 函数中 score_strength 的计算公式，降低分母值以放大评分信号。")
        elif win_rate < 30:
            issues.append(f"胜率仅{win_rate:.0f}%，信号质量差")
            must_fix.append("请打开 agents/decision.py，找到 generate_factor_signal 函数中的趋势过滤条件，将 BEAR 判断从仅限制 short/mid 改为三线全部限制，因为熊市环境下信号质量差。")

        # 5. 三线达成率
        target_achievement = {"short": True, "mid": True, "long": True}
        if metrics.get("achievement_short", 0) < 30:
            issues.append(f"短线达成率{metrics['achievement_short']:.0f}%过低，短线策略失效")
            target_achievement["short"] = False
            must_fix.append("请打开 analysis/factor_weights.json，找到 short 对象中的 threshold，将其从 55 修改为 48，因为短线达成率过低说明入场信号不足。")
        if metrics.get("achievement_mid", 0) < 30:
            issues.append(f"中线达成率{metrics['achievement_mid']:.0f}%过低，中线策略失效")
            target_achievement["mid"] = False
            must_fix.append("请打开 agents/decision.py，找到 generate_factor_signal 函数中 mid 线的 weekly_state 判断条件，将其从 'UP' 放宽为 'UP 或 UNKNOWN'，因为周线过滤过严导致中线信号被误杀。")
        if metrics.get("achievement_long", 0) < 30:
            issues.append(f"长线达成率{metrics['achievement_long']:.0f}%过低，长线策略失效")
            target_achievement["long"] = False
            must_fix.append("请打开 analysis/factor_weights.json，找到 long 对象中的 threshold，将其从 55 修改为 50，因为长线阈值过高导致无法捕捉长期趋势机会。")

        # 6. 亏损交易分析
        loss_trades = [t for t in closed_trades if t["pnl"] < 0]
        lost_trades_analysis = ""
        if loss_trades:
            total_loss = sum(t["pnl"] for t in loss_trades)
            avg_loss_pct = sum(t["pnl_pct"] for t in loss_trades) / len(loss_trades)
            lost_trades_analysis = (f"{len(loss_trades)}笔亏损，总亏¥{total_loss:,.0f}，"
                                    f"平均亏损{avg_loss_pct:+.1f}%。")
            large_losses = [t for t in loss_trades if t["pnl_pct"] < -10]
            if large_losses:
                lost_trades_analysis += f" {len(large_losses)}笔巨亏(<-10%)，止损执行不力。"
                must_fix.append("请打开 analysis/holding.py，找到 evaluate_holding 函数中短线的 ATR 止损计算，将 ATR 倍数从 1.5 修改为 2.0，因为止损过紧导致频繁触发、应适当放宽止损空间。")
            stop_losses = [t for t in loss_trades if "止损" in t.get("reason", "")]
            if stop_losses:
                lost_trades_analysis += f" {len(stop_losses)}笔止损触发，止损位设置可能过紧。"
        else:
            lost_trades_analysis = "无亏损交易，风控表现良好。"

        # 7. 盈利交易分析
        win_trades_list = [t for t in closed_trades if t["pnl"] > 0]
        if win_trades_list:
            avg_win_pct = sum(t["pnl_pct"] for t in win_trades_list) / len(win_trades_list)
            if avg_win_pct < 15:
                issues.append(f"平均盈利仅{avg_win_pct:.1f}%，止盈过早或目标太低")
                must_fix.append("请打开 analysis/holding.py，找到 evaluate_holding 函数中盈利>10%启用移动止盈的条件，将其从 10% 修改为 15%，因为止盈触发过早导致利润被截断。")

        # ── 综合评分 ──
        issue_count = len(issues)
        if issue_count <= 1 and total_return > 20:
            score = 9
        elif issue_count <= 2:
            score = 7
        elif issue_count <= 3:
            score = 5
        elif issue_count <= 5:
            score = 3
        else:
            score = 1

        # 收益率加权
        if total_return > 50:
            score = min(10, score + 2)
        elif total_return < -10:
            score = max(1, score - 2)

        main_issue = issues[0] if issues else "回测表现良好，无明显问题"

        result = {
            "overall_score": score,
            "main_issue": main_issue,
            "all_issues": issues,
            "lost_trades_analysis": lost_trades_analysis,
            "target_achievement": target_achievement,
            "code_changes": [],
            "must_fix": must_fix[:5],  # 最多5条人工执行清单
            "score_breakdown": {
                "return_quality": min(10, max(1, int(6 + total_return / 10))),
                "risk_control": min(10, max(1, int(10 - max_dd / 5))),
                "trade_frequency": min(10, max(1, total_trades)),
                "win_quality": min(10, max(1, int(win_rate / 10))),
                "target_achievement": sum(1 for v in target_achievement.values() if v) * 3 + 1,
            },
        }
    else:
        # 使用 DeepSeek API 进行回测评审（压缩上下文 + 长超时 + 降级兜底）
        prompt = ALL_PROMPTS["critic_agent"]
        compressed = _compress_backtest_context(
            metrics, closed_trades, symbol,
            backtest_result.get('start_date', '?'),
            backtest_result.get('end_date', '?'),
        )
        try:
            raw = deepseek_chat(
                prompt,
                f"请严格审查以下回测结果:\n\n{compressed}\n\n"
                f"请输出JSON分析结果，必须包含overall_score/main_issue/code_changes/must_fix字段。\n"
                f"code_changes是最重要的字段，包含可执行的代码替换指令数组。每条指令格式：\n"
                f'{{"file":"文件路径","function":"函数名","old_code":"原代码","new_code":"新代码","reason":"原因"}}',
                timeout=(10, 120),  # connect 10s, read 120s
            )
            result = _parse_json(raw)
            if "target_achievement" not in result:
                result["target_achievement"] = {"short": True, "mid": True, "long": True}
            if "lost_trades_analysis" not in result:
                result["lost_trades_analysis"] = "API分析模式"
            if "code_changes" not in result:
                result["code_changes"] = []
        except Exception as e:
            print(f"  ⚠ DeepSeek API 超时/失败 ({type(e).__name__}: {e})，降级为本地启发式分析")
            result = _heuristic_critique_fallback(metrics, closed_trades)

    # 打印点评
    print(f"\n  ╔══ BACKTEST CRITIC 评分 ══╗")
    print(f"  ║ 总分: {result.get('overall_score', '?')}/10            ║")
    print(f"  ╚═════════════════════════╝")
    print(f"  💬 {result.get('main_issue', '')}")
    ta = result.get("target_achievement", {})
    if ta:
        print(f"  📊 三线达成: 短{'✅' if ta.get('short') else '❌'} 中{'✅' if ta.get('mid') else '❌'} 长{'✅' if ta.get('long') else '❌'}")
    print(f"  📋 亏损分析: {result.get('lost_trades_analysis', '')[:120]}")
    fixes = result.get("must_fix", [])
    if fixes:
        print(f"\n  📋 人工执行清单 ({len(fixes)}条):")
        for i, f in enumerate(fixes, 1):
            print(f"    {i}. {f}")
    sb = result.get("score_breakdown", {})
    if sb:
        dims = " | ".join(f"{k}={v}" for k, v in sb.items())
        print(f"\n  📊 维度: {dims}")
    print()

    return result


def deep_critique(full_backtest_report: dict, use_mock: bool = False,
                  save_report: bool = True) -> dict:
    """
    量化基金经理深度诊断 — 全程调用 deepseek_chat 进行策略剖析。

    Args:
        full_backtest_report: 完整的回测结果（来自 run_backtest_with_critic 或 run_backtest）
        use_mock: True=启发式诊断（API不可用时）
        save_report: True=保存 strategy_diagnosis.md

    Returns:
        {"overall_health", "diagnosis": {...}, "improvement_plan": {...},
         "stock_specific_genes": [...]}
    """
    symbol = full_backtest_report.get("symbol", "?")
    period = full_backtest_report.get("period", {})
    rounds = full_backtest_report.get("rounds", [])

    print(f"\n{'='*70}")
    print(f"  DEEP CRITIQUE — 量化基金经理深度诊断")
    print(f"  标的: {symbol} | 区间: {period.get('start_date','?')} → {period.get('end_date','?')}")
    print(f"{'='*70}\n")

    # 构建详细分析上下文
    ctx_lines = [
        f"# 策略回测报告 — {symbol}",
        f"回测区间: {period.get('start_date','?')} → {period.get('end_date','?')}",
        f"进化轮数: {len(rounds)}",
        f"最终评分: {full_backtest_report.get('final_score', '?')}/10",
        f"评分历史: {full_backtest_report.get('score_history', [])}",
        "",
    ]

    for r in rounds:
        m = r.get("backtest_metrics", {})
        ctx_lines.append(f"## 第{r['round']}轮")
        ctx_lines.append(f"- 收益率: {m.get('total_return_pct', 0):+.2f}%")
        ctx_lines.append(f"- 最大回撤: {m.get('max_drawdown_pct', 0):.2f}%")
        ctx_lines.append(f"- 夏普比率: {m.get('sharpe_ratio', 0):.2f}")
        ctx_lines.append(f"- 胜率: {m.get('win_rate_pct', 0):.1f}%")
        ctx_lines.append(f"- 盈亏比: {m.get('profit_factor', 0):.2f}")
        ctx_lines.append(f"- 总交易: {m.get('total_trades', 0)} | 胜: {m.get('win_trades', 0)} | 负: {m.get('loss_trades', 0)}")
        ctx_lines.append(f"- 三线达成率: 短{m.get('achievement_short', 0):.0f}% 中{m.get('achievement_mid', 0):.0f}% 长{m.get('achievement_long', 0):.0f}%")
        ctx_lines.append(f"- Critic评分: {r.get('critic_score', '?')}/10")
        ctx_lines.append(f"- 主要问题: {r.get('main_issue', '')}")
        ctx_lines.append(f"- 应用修改: {len(r.get('fixes_applied', []))}条")
        ctx_lines.append("")

    # 最后一轮的交易明细
    last_round = rounds[-1] if rounds else {}
    ctx_lines.append("## 最终轮交易诊断需求")
    ctx_lines.append("请基于以上数据，输出深度诊断JSON。重点分析：")
    ctx_lines.append("1. 策略是否过拟合（参数是否针对这段行情过度优化）")
    ctx_lines.append("2. 最致命的软肋是什么（风控/信号/执行）")
    ctx_lines.append("3. 在什么行情下必定亏损")
    ctx_lines.append("4. 行为偏差（频繁交易/扛单/过早止盈）")
    ctx_lines.append("5. 该股票特有的策略基因（哪些参数需要保留并迁移到其他股票）")

    context = "\n".join(ctx_lines)

    if use_mock:
        # 启发式深度诊断
        m = rounds[-1].get("backtest_metrics", {}) if rounds else {}
        total_return = m.get("total_return_pct", 0)
        max_dd = m.get("max_drawdown_pct", 0)
        sharpe = m.get("sharpe_ratio", 0)
        win_rate = m.get("win_rate_pct", 0)
        total_trades = m.get("total_trades", 0)

        if sharpe > 1 and max_dd < 20:
            health = "健康"
        elif sharpe > 0.3 or max_dd < 35:
            health = "亚健康"
        else:
            health = "危险"

        if total_trades < 5 and abs(total_return) > 30:
            overfit = "高"
        elif total_trades < 15:
            overfit = "中"
        else:
            overfit = "低"

        if max_dd > 30:
            key_vuln = f"风控失效：最大回撤{max_dd:.1f}%远超容忍上限，单次灾难性亏损即可毁灭账户"
        elif win_rate < 30:
            key_vuln = f"信号质量极差：胜率仅{win_rate:.0f}%，多数交易在亏损"
        elif sharpe < 0:
            key_vuln = f"无超额收益：夏普{sharpe:.2f}<0，承担风险却无回报"
        else:
            key_vuln = "止损设置过紧导致频繁小额亏损，累积侵蚀利润"

        if max_dd > 30:
            adverse = "单边下跌趋势——策略在BEAR行情中持续接飞刀，无有效空仓机制"
        elif total_return < -10:
            adverse = "高波动+低胜率环境——放量下跌中被反复止损"
        else:
            adverse = "低成交量横盘——策略依赖的趋势信号在震荡中频繁假突破"

        if total_trades > 30 and win_rate < 40:
            behavioral = "频繁交易——交易次数过多(>{total_trades})但胜率低，手续费侵蚀利润"
        elif max_dd > 25:
            behavioral = "扛单——亏损头寸未及时止损，小亏变大亏"
        elif win_rate > 50 and total_return < 0:
            behavioral = "过早止盈——盈利交易利润太小，无法覆盖亏损交易的手续费+滑点"
        else:
            behavioral = "信号过激——在无明确趋势时仍频繁开仓"

        genes = [
            f"基因1: 高波动适应——该股票波动率{m.get('sharpe_ratio', 0):.1f}，ATR止损倍数需动态调整",
            f"基因2: 趋势跟随强度——MA60斜率敏感度决定开仓时机，BEAR必须空仓",
            f"基因3: 交易频率因子——日均{m.get('total_trades', 0) / max(len(rounds), 1):.1f}笔交易，需控制频率<1笔/周",
        ]

        result = {
            "overall_health": health,
            "diagnosis": {
                "overfitting_risk": overfit,
                "key_vulnerability": key_vuln,
                "adverse_market": adverse,
                "behavioral_flaw": behavioral,
            },
            "improvement_plan": {
                "short_term": [
                    "BEAR趋势下强制空仓(短线+中线)，仅保留长线观察仓",
                    f"ATR止损倍数从固定值改为动态：高波动×1.3，低波动×0.8",
                ],
                "mid_term": [
                    "引入市场状态过滤器（已有trend_state），确保BEAR不开新仓",
                    "增加交易冷却期：连续2笔亏损后暂停2个交易日",
                ],
                "long_term": [
                    "积累该股票至少3个完整牛熊周期的交易数据",
                    "训练股票特异性参数：最优ATR倍数、最优RSI阈值、最优仓位比",
                ],
            },
            "stock_specific_genes": genes,
        }
    else:
        # 调用 DeepSeek API 进行深度诊断
        from agents.prompts import ALL_PROMPTS
        prompt = ALL_PROMPTS.get("deep_critique", ALL_PROMPTS.get("critic_agent", ""))
        try:
            raw = deepseek_chat(prompt, f"请对这份回测报告进行深度诊断:\n\n{context}\n\n请输出JSON诊断结果。")
            result = _parse_json(raw)
            # 确保必要字段存在
            result.setdefault("overall_health", "亚健康")
            result.setdefault("diagnosis", {})
            result.setdefault("improvement_plan", {})
            result.setdefault("stock_specific_genes", [])
        except Exception as e:
            print(f"  [WARN] deep_critique API调用失败: {e}")
            result = {
                "overall_health": "亚健康",
                "diagnosis": {
                    "overfitting_risk": "中",
                    "key_vulnerability": f"API分析失败: {e}",
                    "adverse_market": "未知",
                    "behavioral_flaw": "未知",
                },
                "improvement_plan": {"short_term": [], "mid_term": [], "long_term": []},
                "stock_specific_genes": [],
            }

    # ── 打印诊断要点 ──
    diag = result.get("diagnosis", {})
    print(f"  ╔══ DEEP CRITIQUE 诊断 ══╗")
    print(f"  ║ 健康度: {result.get('overall_health', '?')}")
    print(f"  ║ 过拟合风险: {diag.get('overfitting_risk', '?')}")
    print(f"  ║ 致命软肋: {diag.get('key_vulnerability', '?')[:60]}")
    print(f"  ║ 不利行情: {diag.get('adverse_market', '?')[:60]}")
    print(f"  ║ 行为偏差: {diag.get('behavioral_flaw', '?')[:60]}")
    print(f"  ╚══════════════════════════╝")

    plan = result.get("improvement_plan", {})
    st = plan.get("short_term", [])
    mt = plan.get("mid_term", [])
    lt = plan.get("long_term", [])
    if st:
        print(f"  📋 短期改进 ({len(st)}条):")
        for s in st:
            print(f"    • {s}")
    if mt:
        print(f"  📋 中期改进 ({len(mt)}条):")
        for m_item in mt:
            print(f"    • {m_item}")
    if lt:
        print(f"  📋 长期改进 ({len(lt)}条):")
        for l in lt:
            print(f"    • {l}")

    genes = result.get("stock_specific_genes", [])
    if genes:
        print(f"  🧬 策略基因 ({len(genes)}个):")
        for g in genes:
            print(f"    • {g}")

    # ── 保存 strategy_diagnosis.md ──
    if save_report:
        def _critic_data_dir():
            if getattr(sys, 'frozen', False):
                return os.path.dirname(sys.executable)
            return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        md_path = os.path.join(_critic_data_dir(), "strategy_diagnosis.md")
        _save_diagnosis_md(md_path, symbol, full_backtest_report, result)
        print(f"\n  📄 诊断报告已保存: {md_path}")

    print()
    return result


def _save_diagnosis_md(path: str, symbol: str, backtest_report: dict, diagnosis: dict):
    """保存策略诊断报告为 Markdown 文件。"""
    diag = diagnosis.get("diagnosis", {})
    plan = diagnosis.get("improvement_plan", {})
    genes = diagnosis.get("stock_specific_genes", [])
    period = backtest_report.get("period", {})
    rounds = backtest_report.get("rounds", [])

    lines = [
        f"# 策略深度诊断报告 — {symbol}",
        f"",
        f"**回测区间**: {period.get('start_date', '?')} → {period.get('end_date', '?')}",
        f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"**进化轮数**: {len(rounds)}",
        f"",
        f"## 综合健康度: **{diagnosis.get('overall_health', '?')}**",
        f"",
        f"## 诊断详情",
        f"",
        f"| 维度 | 评估 |",
        f"|------|------|",
        f"| 过拟合风险 | **{diag.get('overfitting_risk', '?')}** |",
        f"| 致命软肋 | {diag.get('key_vulnerability', '?')} |",
        f"| 不利行情 | {diag.get('adverse_market', '?')} |",
        f"| 行为偏差 | {diag.get('behavioral_flaw', '?')} |",
        f"",
        f"## 改进计划",
        f"",
    ]

    for time_horizon, label in [("short_term", "短期(本周)"), ("mid_term", "中期(1-2周)"), ("long_term", "长期(架构调整)")]:
        items = plan.get(time_horizon, [])
        lines.append(f"### {label}")
        if items:
            for item in items:
                lines.append(f"- {item}")
        else:
            lines.append("- (无)")
        lines.append("")

    lines.append("## 股票特异性基因")
    lines.append("")
    if genes:
        for g in genes:
            lines.append(f"- {g}")
    else:
        lines.append("- (未识别)")

    lines.append("")
    lines.append("## 各轮进化摘要")
    lines.append("")
    lines.append("| 轮次 | 收益率 | 最大回撤 | 夏普 | 胜率 | Critic评分 |")
    lines.append("|------|--------|----------|------|------|-----------|")
    for r in rounds:
        m = r.get("backtest_metrics", {})
        lines.append(f"| {r['round']} | {m.get('total_return_pct', 0):+.2f}% | {m.get('max_drawdown_pct', 0):.2f}% | {m.get('sharpe_ratio', 0):.2f} | {m.get('win_rate_pct', 0):.1f}% | {r.get('critic_score', '?')}/10 |")

    lines.append("")
    lines.append("---")
    lines.append(f"*报告由 StockMind Critic Agent 自动生成*")

    with open(path, 'w', encoding='utf-8') as f:
        f.write("\n".join(lines))


def deep_dive_losing_trades(trade_log: list, time_frame: str = "mid",
                            use_mock: bool = True) -> dict:
    """
    深度剖析亏损交易，找出共同失败模式并给出具体修改指令。

    Args:
        trade_log: backtest 产生的完整交易日志（list of dict）
        time_frame: "mid" | "long" — 要剖析的时间维度
        use_mock: True=启发式分析，False=调用DeepSeek API

    Returns:
        {"common_patterns": "...", "root_causes": [...],
         "fix_suggestions": ["file:line:具体修改内容", ...]}
    """
    # 映射时间维度
    tf_map = {"mid": "mid_term", "long": "long_term", "short": "short_term"}
    tf_key = tf_map.get(time_frame, time_frame)

    # 过滤该时间维度的亏损交易
    losing = [t for t in trade_log
              if t.get("timeframe") == tf_key
              and t.get("action") in ("CLOSE", "TRIM", "CLOSE_FINAL")
              and t.get("pnl", 0) < 0]

    if not losing:
        return {
            "common_patterns": "无亏损交易",
            "root_causes": [],
            "fix_suggestions": []
        }

    # 按亏损幅度分组
    small_loss = [t for t in losing if t["pnl_pct"] > -5]
    mid_loss = [t for t in losing if -15 <= t["pnl_pct"] <= -5]
    large_loss = [t for t in losing if t["pnl_pct"] < -15]

    total_loss = sum(t["pnl"] for t in losing)
    avg_loss_pct = sum(t["pnl_pct"] for t in losing) / len(losing)

    # 分析亏损原因分类
    reasons_all = [t.get("reason", "") for t in losing]
    stop_loss_count = sum(1 for r in reasons_all if "止损" in r)
    trend_count = sum(1 for r in reasons_all if "趋势" in r or "BEAR" in r)
    signal_count = sum(1 for r in reasons_all if "信号" in r or "signal" in r.lower())

    context_lines = [
        f"## {time_frame}线亏损交易深度剖析",
        f"总亏损交易: {len(losing)}笔",
        f"总亏损金额: ¥{total_loss:,.0f}",
        f"平均亏损幅度: {avg_loss_pct:+.1f}%",
        f"小额亏损(<5%): {len(small_loss)}笔",
        f"中等亏损(5-15%): {len(mid_loss)}笔",
        f"大额亏损(>15%): {len(large_loss)}笔",
        f"止损触发: {stop_loss_count}笔",
        f"趋势相关: {trend_count}笔",
        f"信号相关: {signal_count}笔",
        "",
        "--- 亏损明细（最近10笔）---",
    ]
    for t in losing[-10:]:
        context_lines.append(
            f"  {t.get('date','?')} {t['action']} P={t['price']:.2f} "
            f"PnL={t['pnl']:+.0f}({t['pnl_pct']:+.1f}%) "
            f"原因: {t.get('reason','')[:60]}"
        )

    context = "\n".join(context_lines)

    if use_mock:
        # 启发式亏损分析
        patterns = []
        root_causes = []
        fix_suggestions = []

        # 模式1：小额频繁止损
        if len(small_loss) >= 3 and stop_loss_count >= 3:
            patterns.append(f"频繁小额止损({len(small_loss)}笔<5%)")
            root_causes.append("止损宽度过窄，正常波动被震出")
            fix_suggestions.append(
                f"analysis/holding.py:mid_atr_stop: 中线ATR止损倍数从2.5×→3.0×，"
                f"减少正常波动触发止损"
            )

        # 模式2：BEAR趋势中开仓
        if trend_count >= 2:
            patterns.append(f"BEAR趋势中开仓导致亏损({trend_count}笔)")
            root_causes.append("趋势过滤未生效或信号在BEAR初期仍被允许")
            fix_suggestions.append(
                "agent_runner.py:_apply_weekly_mid_long_filter: "
                "强化BEAR过滤，确保trend_state==BEAR时中长线绝不新开仓"
            )

        # 模式3：大额亏损
        if large_loss:
            patterns.append(f"灾难性亏损({len(large_loss)}笔<-15%)")
            root_causes.append("止损执行不及时或未设置硬止损")
            fix_suggestions.append(
                f"analysis/holding.py: 中长线增加硬止损线"
                f"({'中线-12%' if time_frame == 'mid' else '长线-25%'})，"
                f"无论什么理由触及即斩仓"
            )

        # 模式4：震荡市假突破
        if signal_count >= 2 and len(losing) >= 4:
            patterns.append(f"震荡市假突破信号({signal_count}笔信号相关亏损)")
            root_causes.append("SIDEWAYS市场中突破信号可靠性低，未做额外过滤")
            fix_suggestions.append(
                "analysis/holding.py: SIDEWAYS中线仅允许网格建仓，"
                "禁止在SIDEWAYS中追突破信号"
            )

        # 模式5：盈利后未及时止盈转亏损
        profit_to_loss = [t for t in losing if t.get("reason", "").find("回撤") != -1]
        if profit_to_loss:
            patterns.append(f"盈利回撤转亏损({len(profit_to_loss)}笔)")
            root_causes.append("移动止盈触发过晚或回撤容忍度过大")
            fix_suggestions.append(
                "analysis/holding.py: 中长线移动止盈回撤阈值从5%→3%，"
                "盈利>10%即启用保护"
            )

        if not patterns:
            patterns.append("亏损模式不明显，可能是个别偶发事件")
            root_causes.append("个券异动或大盘系统性风险")
            fix_suggestions.append(
                "agents/critic.py:deep_dive_losing_trades: 继续监控，暂无代码修改建议"
            )

        result = {
            "common_patterns": "; ".join(patterns),
            "root_causes": root_causes,
            "fix_suggestions": fix_suggestions[:3],  # 最多3条
        }
    else:
        # DeepSeek API 模式
        prompt = ALL_PROMPTS.get("critic_agent", "")
        try:
            raw = deepseek_chat(
                prompt,
                f"请分析以下{time_frame}线亏损交易，找出共同失败模式并给出最多3条具体代码修改建议：\n\n{context}\n\n"
                f"请输出JSON格式：{{\"common_patterns\":\"...\", \"root_causes\":[...], "
                f"\"fix_suggestions\":[\"file:line:具体修改内容\", ...]}}"
            )
            result = _parse_json(raw)
            result.setdefault("common_patterns", "分析失败")
            result.setdefault("root_causes", [])
            result.setdefault("fix_suggestions", [])
        except Exception as e:
            result = {
                "common_patterns": f"API分析失败: {e}",
                "root_causes": [],
                "fix_suggestions": []
            }

    # 打印诊断
    print(f"\n  ╔══ 亏损交易深度剖析 ({time_frame}线) ══╗")
    print(f"  ║ 亏损笔数: {len(losing)} | 总亏损: ¥{total_loss:,.0f} | 均幅: {avg_loss_pct:+.1f}%")
    print(f"  ╠══════════════════════════════════╣")
    print(f"  ║ 模式: {result['common_patterns'][:60]}")
    for rc in result.get("root_causes", [])[:3]:
        print(f"  ║ 根因: {rc[:60]}")
    for i, fix in enumerate(result.get("fix_suggestions", [])[:3]):
        print(f"  ║ 修改{i+1}: {fix[:60]}")
    print(f"  ╚══════════════════════════════════╝\n")

    return result


if __name__ == "__main__":
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8')

    import argparse
    parser = argparse.ArgumentParser(description='Critic Agent — 审查系统输出')
    parser.add_argument('--symbol', default='600519', help='股票代码')
    parser.add_argument('--mock', action='store_true', default=True,
                        help='使用Mock模式（更快）')
    args = parser.parse_args()

    result = critic_evaluate(args.symbol, use_mock=args.mock)
    print(f"\n{'='*60}")
    print(f"  CRITIC 评估完成: {result.get('overall_score', '?')}/10")
    print(f"  修改指令数: {len(result.get('must_fix', []))}")
    print(f"{'='*60}")
