"""
三时间维度分析运行器 — 短线/中线/长线。
每个维度调用独立的 Prompt + LLM，返回结构化观点。
"""
import json
import re
from agents.prompts import TIME_FRAME_PROMPTS
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


def _build_context(compressed_data: dict, agent_reports: list[dict]) -> str:
    """将数据+现有报告压缩为一段摘要文本。"""
    q = compressed_data.get("quote", {})
    t = compressed_data.get("technical", {})
    f = compressed_data.get("financial", {})
    news = compressed_data.get("news", [])
    bs = compressed_data.get("breakout_signals", {})

    parts = [f"股票: {q.get('name','?')} ({compressed_data.get('symbol','?')})",
             f"价格: {q.get('price','?')} | 涨跌: {q.get('change_pct','?')}% | 换手率: {q.get('turnover','?')}%"]
    parts.append(f"MA5={t.get('ma5','?')} MA20={t.get('ma20','?')} RSI={t.get('rsi14','?')} "
                 f"MACD柱={t.get('macd_histogram','?')}")
    parts.append(f"布林上={t.get('boll_upper','?')} 中={t.get('boll_mid','?')} 下={t.get('boll_lower','?')}")
    parts.append(f"PE={q.get('pe','?')} PB={q.get('pb','?')} 市值={q.get('market_cap','?')}亿")
    parts.append(f"ROE={f.get('roe','?')}% 营收增速={f.get('revenue_growth','?')}% "
                 f"净利增速={f.get('net_profit_growth','?')}% 毛利率={f.get('gross_margin','?')}% "
                 f"负债率={f.get('debt_ratio','?')}%")

    # 突破信号 + 动量因子
    parts.append(f"突破信号: boll_breakout={bs.get('boll_breakout',False)} "
                 f"volume_ratio={bs.get('volume_ratio','?')} "
                 f"vcp={bs.get('vcp',False)} "
                 f"surge_confirm={bs.get('surge_confirm',False)} "
                 f"breakout_score={bs.get('breakout_score',0)} "
                 f"动量评分={bs.get('momentum_score',0)}")
    parts.append(f"动量: 20日={t.get('momentum_20d','?')}% "
                 f"60日={t.get('momentum_60d','?')}% "
                 f"52周新高={t.get('is_52w_high','?')} "
                 f"120日高点={t.get('high_120d','?')}")

    parts.append("--- 分析师报告摘要 ---")
    for r in agent_reports:
        parts.append(f"[{r['agent']}] {r['signal']} score={r['score']} conf={r['confidence']} {r.get('reasoning','')[:60]}")

    # 趋势状态注入
    trend = compressed_data.get("trend_state", {})
    trend_state = trend.get("trend_state", "SIDEWAYS")
    ma60_slope = trend.get("ma60_slope")
    parts.append(f"当前市场状态: {trend_state} | MA60斜率: {ma60_slope}%")
    if trend_state == "BEAR":
        parts.append("BEAR警告: 你的首要职责是保护资本。除极端超卖(RSI<20且放量反转)可小仓位博弈外，一律输出HOLD。长线应寻找最抗跌标的，等待反转确认。")
    elif trend_state == "SIDEWAYS":
        parts.append("SIDEWAYS提示: 震荡市买入信号保留，但仓位应更谨慎，优先做确定性高的波段。")
    elif trend_state == "BULL":
        parts.append("BULL提示: 牛市环境，仓位可更积极，优先趋势跟踪和突破追入策略。")

    news_titles = [n if isinstance(n, str) else n.get("title", "") for n in news[:5]]
    if news_titles:
        parts.append("--- 近期新闻 ---")
        parts.extend(news_titles[:3])

    return "\n".join(parts)


def _mock_time_frame(frame: str, context_text: str) -> dict:
    """启发式模拟三线分析（无需API调用）。"""
    # 从 context 中提取关键数值
    import re as _re
    price = None; ma5 = None; ma20 = None; rsi = None
    m = _re.search(r'价格:\s*([\d.]+)', context_text)
    if m: price = float(m.group(1))
    m = _re.search(r'MA5=([\d.]+)', context_text)
    if m: ma5 = float(m.group(1))
    m = _re.search(r'MA20=([\d.]+)', context_text)
    if m: ma20 = float(m.group(1))
    m = _re.search(r'RSI=([\d.]+)', context_text)
    if m: rsi = float(m.group(1))
    m = _re.search(r'换手率:\s*([\d.]+)', context_text)
    turnover = float(m.group(1)) if m else None
    m = _re.search(r'MACD柱=([-.\d]+)', context_text)
    macd_hist = float(m.group(1)) if m else None
    # 突破信号
    m = _re.search(r'breakout_score=(\d+)', context_text)
    breakout_score = int(m.group(1)) if m else 0
    m = _re.search(r'boll_breakout=(True|False)', context_text)
    boll_breakout = m.group(1) == "True" if m else False
    m = _re.search(r'vcp=(True|False)', context_text)
    vcp = m.group(1) == "True" if m else False
    m = _re.search(r'surge_confirm=(True|False)', context_text)
    surge_confirm = m.group(1) == "True" if m else False
    m = _re.search(r'volume_ratio=([\d.]+)', context_text)
    vol_ratio = float(m.group(1)) if m else None
    m = _re.search(r'涨跌:\s*([-.\d]+)', context_text)
    change_pct = float(m.group(1)) if m else None
    m = _re.search(r'动量评分=(\d+)', context_text)
    momentum_score = int(m.group(1)) if m else 0
    m = _re.search(r'20日=([-.\d]+)%', context_text)
    momentum_20d = float(m.group(1)) if m else None
    m = _re.search(r'60日=([-.\d]+)%', context_text)
    momentum_60d = float(m.group(1)) if m else None
    m = _re.search(r'52周新高=(True|False)', context_text)
    is_52w_high = m.group(1) == "True" if m else False
    # 趋势状态
    m = _re.search(r'当前市场状态:\s*(BULL|BEAR|SIDEWAYS)', context_text)
    trend_state = m.group(1) if m else "SIDEWAYS"

    if frame == "short_term":
        score = 6  # 中性偏高基准
        reasons = []
        if ma5 and ma20 and ma5 > ma20:
            score += 3; reasons.append("MA5>MA20多头")
        if rsi is not None:
            if rsi < 30:
                score += 4; reasons.append(f"RSI={rsi}超卖反弹机会")
            elif rsi < 45:
                score += 2; reasons.append(f"RSI={rsi}偏低可博反弹")
            elif rsi > 70:
                score -= 2; reasons.append(f"RSI={rsi}偏高不追")
        if turnover and turnover > 2:
            score += 1; reasons.append("换手率活跃")
        if macd_hist and macd_hist > 0:
            score += 2; reasons.append("MACD柱翻红")
        else:
            score -= 1; reasons.append("MACD柱翻绿")
        # 主动入场信号加分
        if boll_breakout:
            score += 3; reasons.append("突破追入:价格放量破布林上轨+量比>1.5")
        if vcp:
            score += 2; reasons.append("VCP:布林带宽缩至最窄20%+价格在支撑位")
        if surge_confirm:
            score += 4; reasons.append("放量上攻:涨幅>3%+量比>1.5→短线BUY")
        score += breakout_score  # 综合突破评分
        # 动量因子增强
        if momentum_20d is not None and momentum_20d > 5:
            score += 1; reasons.append(f"20日动量{momentum_20d}%>5%")
        if momentum_60d is not None and momentum_60d > 30:
            score += 2; reasons.append(f"60日趋势加速{momentum_60d}%>30%")
        if is_52w_high:
            score += 2; reasons.append("创52周新高")
        # 趋势调整
        if trend_state == "BEAR":
            score -= 3
            if score >= 2 and not (rsi is not None and rsi < 20 and surge_confirm):
                score = 3  # 强制HOLD，除非极端超卖+放量
        elif trend_state == "BULL":
            score += 1
        conv = max(1, min(10, score))
        sig = "BUY" if score >= 4 else "SELL" if score <= 3 else "HOLD"
        # 计算预期收益率
        if price and sig == "BUY":
            expected_return = round((price * 1.10 - price) / price * 100, 1) if price else 0
        else:
            expected_return = 0
        return {"signal": sig, "conviction": conv, "reasoning": "；".join(reasons[:4])[:80],
                "entry_zone": "现价附近" if sig == "BUY" else "观望",
                "target": f"{price*1.12:.2f}" if price else "?",
                "stop_loss": f"{price*0.97:.2f}" if price else "?",
                "expected_return_pct": expected_return,
                "exit_strategy": {"type": "trailing",
                    "rules": ["盈利>10%启用移动止盈回撤3%卖一半","跌破MA5全清"],
                    "re_evaluation_triggers": ["冲高回落5%+放量","RSI>80死叉"]}}

    elif frame == "mid_term":
        score = 6
        reasons = []
        if ma20 and price:
            if price > ma20:
                score += 3; reasons.append("价格>MA20中期偏多")
                if ma20 and price > ma20 * 1.1:
                    score += 1; reasons.append("强势偏离MA20")
            else:
                score -= 2; reasons.append("价格<MA20中期偏弱")
        if rsi is not None and 40 <= rsi <= 60:
            score += 1; reasons.append("RSI中性格局")
        if turnover and turnover > 1.5:
            score += 1; reasons.append("交投活跃")
        # 突破信号加分
        if boll_breakout:
            score += 3; reasons.append("突破确认:boll_breakout+MA20向上")
        if vcp:
            score += 2; reasons.append("VCP收缩待突破")
        if surge_confirm:
            score += 2; reasons.append("放量上攻确认中线动能")
        # 动量因子增强（中线关注60日趋势）
        if momentum_60d is not None and momentum_60d > 30:
            score += 2; reasons.append(f"60日趋势加速{momentum_60d}%>30%")
        if momentum_20d is not None and momentum_20d > 5:
            score += 1; reasons.append(f"20日动量{momentum_20d}%>5%")
        if is_52w_high:
            score += 1; reasons.append("创新高动量确认")
        # 趋势调整
        if trend_state == "BEAR":
            score -= 3
            if score >= 4:
                score = 3
        elif trend_state == "BULL":
            score += 1
        conv = max(1, min(10, score))
        sig = "BUY" if score >= 4 else "SELL" if score <= 3 else "HOLD"
        expected_return = round((price * 1.30 - price) / price * 100, 1) if price and sig == "BUY" else 0
        return {"signal": sig, "conviction": conv, "reasoning": "；".join(reasons[:3])[:80],
                "entry_zone": f"{price*0.98:.2f}-{price*1.02:.2f}" if price else "?",
                "target": f"{price*1.35:.2f}" if price else "?",
                "stop_loss": f"{ma20*0.95:.2f}" if ma20 else "?",
                "expected_return_pct": expected_return,
                "exit_strategy": {"type": "trailing",
                    "rules": ["盈利>15%启用移动止损回撤5%全清","MA20拐头减半仓"],
                    "re_evaluation_triggers": ["MA20方向变化","财报不及预期"]}}

    else:  # long_term
        score = 5
        reasons = []
        doubling_path_parts = []

        # ═══ 长线深度分析维度 ═══
        # 1. 行业TAM评估
        m_gm = _re.search(r'毛利率=([-.\d]+)%', context_text)
        gross_margin = float(m_gm.group(1)) if m_gm else None
        m_mc = _re.search(r'市值=([\d.]+)亿', context_text)
        market_cap = float(m_mc.group(1)) if m_mc else None

        # 小市值加分（翻倍空间更大）
        if market_cap is not None and market_cap < 500:
            score += 2; reasons.append(f"市值{market_cap}亿<500亿，翻倍空间充足")
        elif market_cap is not None and market_cap > 2000:
            score -= 2; reasons.append(f"市值{market_cap}亿>2000亿，翻倍难度大")
            doubling_path_parts.append("市值过大→翻倍路径受限")

        # 2. 护城河评估（毛利率+ROE作为代理指标）
        if gross_margin is not None and gross_margin > 40:
            score += 2; reasons.append(f"毛利率{gross_margin}%>40%，强护城河特征")
            doubling_path_parts.append(f"毛利率{gross_margin}%→定价权强→盈利持续增长")
        elif gross_margin is not None and gross_margin > 25:
            score += 1; reasons.append(f"毛利率{gross_margin}%中等")

        # 3. 估值弹性（PE分位数作为代理）
        m_pe = _re.search(r'PE=([\d.]+)', context_text)
        pe_val = float(m_pe.group(1)) if m_pe else None
        m_roe = _re.search(r'ROE=([\d.]+)%', context_text)
        roe = float(m_roe.group(1)) if m_roe else None
        m_rev = _re.search(r'营收增速=([-.\d]+)%', context_text)
        rev_growth = float(m_rev.group(1)) if m_rev else None
        m_np = _re.search(r'净利增速=([-.\d]+)%', context_text)
        np_growth = float(m_np.group(1)) if m_np else None
        m_debt = _re.search(r'负债率=([\d.]+)%', context_text)
        debt = float(m_debt.group(1)) if m_debt else None
        m_gm = _re.search(r'毛利率=([-.\d]+)%', context_text)
        gross_margin = float(m_gm.group(1)) if m_gm else None

        if roe and roe > 20:
            score += 4; reasons.append(f"ROE={roe}%极优秀")
        elif roe and roe > 15:
            score += 3; reasons.append(f"ROE={roe}%优秀")
        if rev_growth and rev_growth > 30:
            score += 4; reasons.append(f"营收增速{rev_growth}%高成长")
        elif rev_growth and rev_growth > 15:
            score += 2; reasons.append(f"营收增速{rev_growth}%稳健")
        elif rev_growth and rev_growth > 5:
            score += 1; reasons.append(f"营收增速{rev_growth}%正增长")
        if np_growth and np_growth > 30:
            score += 3; reasons.append(f"净利增速{np_growth}%高增长")
        if debt is not None and debt > 70:
            score -= 2; reasons.append(f"负债率{debt}%偏高")
        if roe and roe > 15 and rev_growth and rev_growth > 20:
            score += 1; reasons.append("高ROE+高成长=成长股特征")
        if roe and roe > 10 and rev_growth and rev_growth > 10:
            reasons.append("优秀基本面，具备长期持有价值")

        # 趋势调整
        if trend_state == "BEAR":
            score -= 2
        elif trend_state == "BULL":
            score += 1

        # 建立翻倍路径图
        if doubling_path_parts:
            doubling_path_map = "翻倍路径图: " + "；".join(doubling_path_parts[:5])
        else:
            doubling_path_map = "需要更多数据构建翻倍路径"

        conv = max(1, min(10, score))
        sig = "BUY" if score >= 4 else "SELL" if score <= 3 else "HOLD"
        multiplier = ""
        doubling_logic = ""
        expected_return = 0
        if sig == "BUY":
            if rev_growth and rev_growth > 30:
                multiplier = "3年3-5倍"
                expected_return = 300.0
                doubling_logic = f"翻倍路径:①营收增速{rev_growth}%持续3年→规模翻倍 ②利润率提升→利润翻2-3倍 ③估值重估→总回报3-5倍"
            elif roe and roe > 20:
                multiplier = "3年2-3倍"
                expected_return = 200.0
                doubling_logic = f"翻倍路径:①ROE={roe}%高质量盈利 ②复利增长→3年净资产翻倍 ③PE合理→股价同步翻倍"
            elif sig == "BUY":
                multiplier = "2年1.5-2倍"
                expected_return = 150.0
                doubling_logic = "翻倍路径:稳健增长→2年盈利+50%→估值温和扩张→1.5-2倍回报"

        return {"signal": sig, "conviction": conv, "reasoning": "；".join(reasons[:3])[:100],
                "potential_multiplier": multiplier,
                "doubling_logic": doubling_logic,
                "doubling_path_map": doubling_path_map,
                "key_catalyst": "成长驱动" if sig == "BUY" else "等待催化剂",
                "risk": "市场系统性风险",
                "expected_return_pct": expected_return,
                "exit_strategy": {"type": "trailing",
                    "rules": ["盈利>15%启用移动止损回撤5%","季度ROE连续下滑>20%减半仓","营收增速连续<10%减至观察仓"],
                    "re_evaluation_triggers": ["每季财报ROE检查","行业政策重大变化","技术替代风险"]}}


def run_time_frame_agents(compressed_data: dict, agent_reports: list[dict],
                          use_mock: bool = False) -> dict:
    """
    运行三个时间维度的分析。

    Args:
        compressed_data: data_pipeline 输出的压缩数据字典
        agent_reports: 现有4个Agent的分析报告列表
        use_mock: True=启发式模拟，False=调用DeepSeek API

    Returns:
        {"short_term": {...}, "mid_term": {...}, "long_term": {...}}
    """
    ctx = _build_context(compressed_data, agent_reports)
    opinions = {}

    for frame_name in ["short_term", "mid_term", "long_term"]:
        prompt = TIME_FRAME_PROMPTS[frame_name]

        if use_mock:
            opinions[frame_name] = _mock_time_frame(frame_name, ctx)
        else:
            try:
                raw = deepseek_chat(prompt,
                    f"请对以下股票进行{frame_name}分析:\n\n{ctx}\n\n请输出JSON分析结果。")
                opinions[frame_name] = _parse_json(raw)
            except Exception as e:
                print(f"  [WARN] {frame_name} API调用失败: {e}，回退到本地推断")
                opinions[frame_name] = _mock_time_frame(frame_name, ctx)

        # 输出日志
        o = opinions[frame_name]
        labels = {"short_term": "短线猎手", "mid_term": "波段交易者", "long_term": "长线成长捕手"}
        sig = o.get("signal", "?")
        cv = o.get("conviction", 0)
        print(f"  [{labels[frame_name]}] {sig} | conviction={cv}/10")
        r = o.get("reasoning", "")[:80]
        if r:
            print(f"    {r}")

    return opinions
