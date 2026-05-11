#!/usr/bin/env python3
"""
Agent 运行器 — 调度4个LLM分析Agent + 1个本地风控模块。
用法:
    py agent_runner.py <data_json_file>          # 从文件读取
    py data_pipeline.py 600519 | py agent_runner.py -   # 管道输入
"""

import sys
import json
import re
from datetime import datetime

from agents.prompts import AGENT_PROMPTS
from data.deepseek import DeepSeekClient
from utils.format import (card_header, card_line, card_empty, card_bottom, card_field,
                          card_dual, section_div, thin_sep, table_header, table_row,
                          table_sep, format_signal, format_pct, format_price, CARD_W)

# ═══════════════════════════════════════════════════════════════
# 1. 数据格式化 — 将 pipeline JSON 转为各 Agent 的文本输入
# ═══════════════════════════════════════════════════════════════

def format_technical_context(data: dict) -> str:
    """将技术指标转为技术分析师的文本输入。"""
    q = data.get("quote", {})
    t = data.get("technical", {})

    def v(key, default="N/A"):
        return t.get(key, q.get(key, default))

    lines = [
        f"股票: {q.get('name', '?')} ({data.get('symbol', '?')})",
        f"最新价: {v('price')} | 涨跌幅: {q.get('change_pct', 'N/A')}%",
        f"昨收: {q.get('prev_close', 'N/A')} | 今开: {q.get('open', 'N/A')}",
        f"最高: {q.get('high', 'N/A')} | 最低: {q.get('low', 'N/A')}",
        f"换手率: {q.get('turnover', 'N/A')}%",
        f"--- 技术指标 ---",
        f"MA5: {v('ma5')} | MA20: {v('ma20')}",
        f"MA5 vs MA20: {_ma_relation(v('ma5'), v('ma20'))}",
        f"RSI(14): {v('rsi14')}",
        f"MACD DIF: {v('macd')} | DEA: {v('macd_signal')} | 柱: {v('macd_histogram')}",
        f"布林带上轨: {v('boll_upper')} | 中轨: {v('boll_mid')} | 下轨: {v('boll_lower')}",
        f"价格在布林带位置: {_boll_position(v('price'), v('boll_upper'), v('boll_mid'), v('boll_lower'))}",
        f"KDJ K: {v('kdj_k')} | D: {v('kdj_d')} | J: {v('kdj_j')}",
    ]
    return "\n".join(lines)


def format_fundamental_context(data: dict) -> str:
    """将财务数据转为基本面分析师的文本输入。"""
    q = data.get("quote", {})
    f = data.get("financial", {})
    news = data.get("news", [])
    if not isinstance(news, list):
        news = list(news.values()) if isinstance(news, dict) else []

    # 新闻可能是字符串列表或字典列表
    def _news_title(n):
        return n if isinstance(n, str) else n.get("title", "")
    # 筛选业绩相关新闻
    earnings_news = [n for n in news if any(kw in _news_title(n) for kw in
        ['业绩', '利润', '营收', '净利润', '增长', '下滑', '亏损', '盈利', '季报', '年报', '分红'])]

    lines = [
        f"股票: {q.get('name', '?')} ({data.get('symbol', '?')})",
        f"PE(市盈率): {q.get('pe', 'N/A')} | PB(市净率): {q.get('pb', 'N/A')}",
        f"总市值: {q.get('market_cap', 'N/A')}亿",
        f"ROE(净资产收益率): {f.get('roe', 'N/A')}%",
        f"净利润增长率: {f.get('net_profit_growth', 'N/A')}%",
        f"营收增长率: {f.get('revenue_growth', 'N/A')}%",
        f"毛利率: {f.get('gross_margin', 'N/A')}% | 净利率: {f.get('net_margin', 'N/A')}%",
        f"资产负债率: {f.get('debt_ratio', 'N/A')}%",
        f"--- 业绩相关新闻 ---",
    ]
    if earnings_news:
        for i, n in enumerate(earnings_news[:5]):
            title = _news_title(n)
            lines.append(f"  [{i+1}] {title}")
    else:
        lines.append("  (无业绩相关新闻)")
    return "\n".join(lines)


def format_sentiment_context(data: dict) -> str:
    """将新闻标题列表转为情绪分析师的文本输入。"""
    news = data.get("news", [])
    if not isinstance(news, list):
        news = list(news.values()) if isinstance(news, dict) else []
    if not news:
        return "（无新闻数据）"
    lines = ["以下是与该股票相关的近期新闻标题：", ""]
    for i, item in enumerate(news[:5]):
        title = item if isinstance(item, str) else item.get("title", "")
        src = "" if isinstance(item, str) else f" [来源:{item.get('source','?')}]"
        lines.append(f"[{i+1}] {title}{src}")
    return "\n".join(lines)


def format_macro_context(data: dict) -> str:
    """将宏观指标转为宏观分析师的文本输入。"""
    m = data.get("macro", {})
    q = data.get("quote", {})
    lines = [
        f"分析标的: {q.get('name', '?')} ({data.get('symbol', '?')})",
        f"所属市场: {data.get('market', 'A')}股",
        f"--- 宏观指标 ---",
        f"Shibor隔夜: {m.get('shibor_overnight', 'N/A')}",
        f"Shibor 1周: {m.get('shibor_1w', 'N/A')}",
        f"CPI同比: {m.get('cpi_yoy', 'N/A')}",
        f"PMI制造业: {m.get('pmi_manufacturing', 'N/A')}",
        f"GDP增速: {m.get('gdp_growth', 'N/A')}",
        f"--- 备注 ---",
        m.get('note', ''),
    ]
    return "\n".join(lines)


def _ma_relation(ma5, ma20):
    if ma5 is None or ma20 is None:
        return "数据不足"
    if ma5 > ma20:
        return f"MA5 > MA20 (金叉/多头排列，差值{round(ma5 - ma20, 2)})"
    else:
        return f"MA5 < MA20 (死叉/空头排列，差值{round(ma5 - ma20, 2)})"

def _boll_position(price, upper, mid, lower):
    if any(x is None for x in [price, upper, mid, lower]):
        return "数据不足"
    if price >= upper:
        return f"突破上轨(价格{price} >= 上轨{upper})，超买/强势突破"
    if price <= lower:
        return f"跌破下轨(价格{price} <= 下轨{lower})，超卖/弱势破位"
    if price > mid:
        pct = round((price - mid) / (upper - mid) * 100, 0)
        return f"上轨区间(距中轨+{pct}%)"
    else:
        pct = round((mid - price) / (mid - lower) * 100, 0)
        return f"下轨区间(距中轨-{pct}%)"


# ═══════════════════════════════════════════════════════════════
# 2. 风控模块 (纯 Python，不调用 LLM)
# ═══════════════════════════════════════════════════════════════

def run_risk_manager(data: dict) -> dict:
    """
    基于技术指标计算风险等级和仓位建议。
    不调用LLM，纯本地计算。
    """
    t = data.get("technical", {})
    q = data.get("quote", {})

    close = t.get("close") or q.get("price")
    boll_upper = t.get("boll_upper")
    boll_mid = t.get("boll_mid")
    boll_lower = t.get("boll_lower")
    ma20 = t.get("ma20")
    rsi = t.get("rsi14")
    change_pct = q.get("change_pct")

    # 计算各项风险子指标
    volatility = None
    price_position = None
    if boll_upper and boll_mid and boll_lower and close and boll_mid > 0:
        bandwidth = (boll_upper - boll_lower) / boll_mid
        volatility = round(bandwidth * 100, 2)  # 布林带宽度百分比
        price_range = boll_upper - boll_lower
        if price_range > 0:
            price_position = round((close - boll_lower) / price_range, 4)  # 0=下轨, 1=上轨

    ma20_deviation = None
    if ma20 and close and ma20 > 0:
        ma20_deviation = round((close - ma20) / ma20 * 100, 2)  # 偏离MA20百分比

    # 风险评分 (0-100，越高风险越大)
    risk_score = 50  # 基准分

    reasons = []
    if volatility is not None:
        if volatility > 8:
            risk_score += 20
            reasons.append(f"布林带宽度{volatility}%>8%，高波动")
        elif volatility > 5:
            risk_score += 10
            reasons.append(f"布林带宽度{volatility}%中等波动")
        else:
            risk_score -= 10
            reasons.append(f"布林带宽度{volatility}%低波动")

    if rsi is not None:
        if rsi > 80:
            risk_score += 20
            reasons.append(f"RSI={rsi}极度超买，回调风险高")
        elif rsi > 70:
            risk_score += 10
            reasons.append(f"RSI={rsi}超买区域")
        elif rsi < 20:
            risk_score += 15
            reasons.append(f"RSI={rsi}极度超卖，接飞刀风险")
        elif rsi < 30:
            risk_score += 5
            reasons.append(f"RSI={rsi}超卖区域，但可能继续下跌")
        else:
            risk_score -= 5
            reasons.append(f"RSI={rsi}正常区间")

    if price_position is not None:
        if price_position <= 0:
            risk_score += 15
            reasons.append("价格跌破布林下轨，极端弱势")
        elif price_position >= 1:
            risk_score += 10
            reasons.append("价格突破布林上轨，追高风险")
        elif price_position < 0.2:
            risk_score += 5
            reasons.append("价格贴近下轨，弱势")

    if ma20_deviation is not None:
        if abs(ma20_deviation) > 10:
            risk_score += 10
            reasons.append(f"偏离MA20达{ma20_deviation}%，极端偏离")
        elif abs(ma20_deviation) > 5:
            risk_score += 5
            reasons.append(f"偏离MA20达{ma20_deviation}%")

    if change_pct is not None:
        if abs(change_pct) > 5:
            risk_score += 10
            reasons.append(f"当日涨跌幅{change_pct}%，异常波动")

    # 风险等级映射 — 提升各等级仓位上限以追求更高收益
    risk_score = max(0, min(100, risk_score))
    if risk_score >= 70:
        risk_level = "高"
        position_ratio = 0.20  # 原0.15
    elif risk_score >= 50:
        risk_level = "中"
        position_ratio = 0.45  # 原0.35
    elif risk_score >= 20:
        risk_level = "偏低"
        position_ratio = 0.65  # 原0.55
    else:
        risk_level = "低"
        position_ratio = 0.80  # 原0.75

    # ── 资产负债率硬规则（优先于技术指标仓位）──
    debt_ratio = data.get("financial", {}).get("debt_ratio")
    if debt_ratio is not None:
        if debt_ratio > 90:
            position_ratio = min(position_ratio, 0.02)
            risk_score = max(risk_score, 90)
            risk_level = "高"
            reasons.append(f"资产负债率{debt_ratio}%>90%，极端高杠杆，仓位上限强制降至2%")
        elif debt_ratio > 85:
            position_ratio = min(position_ratio, 0.05)
            risk_score = max(risk_score, 75)
            risk_level = "高"
            reasons.append(f"资产负债率{debt_ratio}%>85%，严重高杠杆，仓位上限强制降至5%")
        elif debt_ratio > 80:
            risk_score = max(risk_score, 65)
            reasons.append(f"资产负债率{debt_ratio}%>80%，高杠杆风险")

    # 信号映射
    if risk_score >= 70:
        signal = "SELL"
    elif risk_score >= 50:
        signal = "HOLD"
    else:
        signal = "BUY"

    return {
        "agent": "risk_manager",
        "type": "本地计算",
        "signal": signal,
        "score": int(round((50 - risk_score) / 5)),  # 映射到 -10~+10
        "confidence": 0.90,
        "risk_level": risk_level,
        "risk_score": risk_score,
        "position_ratio": position_ratio,
        "volatility_pct": volatility,
        "price_vs_boll": price_position,
        "ma20_deviation_pct": ma20_deviation,
        "reasoning": "；".join(reasons) if reasons else "各项指标正常",
    }


# ═══════════════════════════════════════════════════════════════
# 3. LLM Agent 调用包装
# ═══════════════════════════════════════════════════════════════

def _call_llm_agent(agent_name: str, system_prompt: str, context_text: str,
                    use_mock: bool = False) -> dict:
    """
    调用 LLM Agent，解析返回的 JSON。
    use_mock=True 时使用启发式规则模拟输出。
    """
    if use_mock:
        return _mock_agent_response(agent_name, context_text)

    prompt_filled = system_prompt.replace("{{context}}", context_text)
    try:
        raw = DeepSeekClient().chat(prompt_filled, "请输出JSON分析结果。", timeout=30)
        return _parse_agent_json(raw, agent_name)
    except Exception as e:
        print(f"  [WARN] {agent_name} 分析超时/失败: {e}，使用本地启发式替代")
        return _mock_agent_response(agent_name, context_text)


def _parse_agent_json(raw_text: str, agent_name: str) -> dict:
    """从 LLM 回复中提取 JSON，兼容各种格式问题。"""
    # 尝试直接解析
    clean = raw_text.strip()
    try:
        result = json.loads(clean)
        return _validate_agent_result(result, agent_name)
    except json.JSONDecodeError:
        pass

    # 尝试从 markdown 代码块中提取
    match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', clean, re.DOTALL)
    if match:
        try:
            result = json.loads(match.group(1))
            return _validate_agent_result(result, agent_name)
        except json.JSONDecodeError:
            pass

    # 尝试找任意 {} 包裹的 JSON
    match = re.search(r'\{.*\}', clean, re.DOTALL)
    if match:
        try:
            result = json.loads(match.group(0))
            return _validate_agent_result(result, agent_name)
        except json.JSONDecodeError:
            pass

    # 降级：返回默认结构化数据，reasoning 保留原始回复前100字
    return {
        "agent": agent_name,
        "type": "LLM",
        "confidence": 0.30,
        "reasoning": clean[:100],
        "parse_error": True,
    }


def _validate_agent_result(result: dict, agent_name: str) -> dict:
    """校验并补全 Agent 输出字段。兼容新旧两种格式。

    新格式(量化基金): agent 输出结构化数据(trend_strength/valuation_score等)
    旧格式(legacy): agent 输出 BUY/SELL 信号 — 仍兼容但标记为 legacy
    """
    result["agent"] = agent_name
    result["type"] = "LLM(DeepSeek V4)"

    # 判断新旧格式
    has_structured = any(k in result for k in (
        "trend_strength", "valuation_score", "sentiment_score_raw", "market_regime",
        "momentum_score", "trend_quality_score", "growth_potential_score"
    ))
    has_signal = "signal" in result

    if has_structured:
        result["type"] = "LLM(DeepSeek V4) — 结构化数据"
        # 从结构化字段推断置信度
        if "confidence" not in result:
            result["confidence"] = 0.7
    elif has_signal:
        # 旧格式兼容: 保留信号但标记来源
        result["type"] = "LLM(DeepSeek V4) — Legacy信号(兼容)"
        signal = str(result.get("signal", "HOLD")).upper().strip()
        result["signal"] = signal if signal in ("BUY", "SELL", "HOLD", "CAUTIOUS_BUY", "CAUTIOUS_SELL") else "HOLD"
        try:
            result["score"] = max(-10, min(10, int(result.get("score", 0))))
        except (ValueError, TypeError):
            result["score"] = 0
    else:
        result["type"] = "LLM(DeepSeek V4) — 解析失败"
        result["confidence"] = 0.30

    try:
        result["confidence"] = max(0.0, min(1.0, float(result.get("confidence", 0.5))))
    except (ValueError, TypeError):
        result["confidence"] = 0.5

    result["reasoning"] = str(result.get("reasoning", ""))[:120]
    return result


# ═══════════════════════════════════════════════════════════════
# 4. Mock/启发式 回退 (API不可用时使用实际数据推断)
# ═══════════════════════════════════════════════════════════════

def _mock_agent_response(agent_name: str, context_text: str) -> dict:
    """基于实际数据的启发式分析，输出结构化数据格式（与LLM新Prompt一致）。"""
    if agent_name == "technical_analyst":
        result = _mock_technical(context_text)
    elif agent_name == "fundamental_analyst":
        result = _mock_fundamental(context_text)
    elif agent_name == "sentiment_analyst":
        result = _mock_sentiment(context_text)
    elif agent_name == "macro_analyst":
        result = _mock_macro(context_text)
    else:
        result = {
            "agent": agent_name, "type": "Mock(启发式)",
            "confidence": 0.30,
            "reasoning": "无法生成分析。"
        }
    result["source"] = "fallback"
    return result


def _parse_value(text, key):
    """从格式化文本中提取数值。"""
    pattern = rf'{key}:\s*([\d.-]+)'
    match = re.search(pattern, text)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            pass
    return None


def _mock_technical(ctx: str) -> dict:
    """启发式技术面评估 → 结构化数据格式 (不输出BUY/SELL)。"""
    price = _parse_value(ctx, "最新价")
    ma5 = _parse_value(ctx, "MA5")
    ma20 = _parse_value(ctx, "MA20")
    rsi = _parse_value(ctx, r"RSI\(14\)")
    macd_hist = _parse_value(ctx, "柱")
    turnover = _parse_value(ctx, "换手率")
    boll_info = ""
    m = re.search(r'价格在布林带位置:\s*(.+)$', ctx, re.MULTILINE)
    if m:
        boll_info = m.group(1)

    # 趋势强度
    trend_strength = 0
    signals = []
    if ma5 and ma20 and price:
        if ma5 > ma20:
            trend_strength += 3; signals.append("MA5>MA20多头排列")
        else:
            trend_strength -= 3; signals.append("MA5<MA20空头排列")

    # RSI 区间
    rsi_zone = "neutral"
    if rsi is not None:
        if rsi < 30: rsi_zone = "oversold"
        elif rsi > 70: rsi_zone = "overbought"

    # MACD
    macd_signal = "none"
    if macd_hist is not None:
        macd_signal = "bullish" if macd_hist > 0 else "bearish"

    # 成交量
    volume_assessment = "normal"
    if turnover is not None:
        if turnover > 5: volume_assessment = "active"
        elif turnover < 1: volume_assessment = "weak"

    # 布林带位置
    boll_position = "within"
    if "跌破下轨" in boll_info or "下轨区间" in boll_info:
        boll_position = "below"
    elif "突破上轨" in boll_info or "上轨区间" in boll_info:
        boll_position = "above"

    trend_strength = max(-10, min(10, trend_strength))
    return {
        "agent": "technical_analyst", "type": "Mock(启发式)",
        "trend_strength": trend_strength,
        "trend_direction": "up" if trend_strength > 2 else ("down" if trend_strength < -2 else "sideways"),
        "rsi_reading": rsi,
        "rsi_zone": rsi_zone,
        "macd_signal": macd_signal,
        "volume_assessment": volume_assessment,
        "support_distance_pct": 3.0 if boll_position == "below" else -2.0,
        "resistance_distance_pct": -3.0 if boll_position == "above" else 2.0,
        "bollinger_position": boll_position + "_band" if boll_position in ("above", "below") else "within",
        "breakout_active": False,
        "technical_risks": [],
        "confidence": 0.55,
        "reasoning": "；".join(signals[:3])[:120] if signals else "技术面数据解析完成"
    }


def _mock_fundamental(ctx: str) -> dict:
    """启发式基本面评估 → 结构化数据格式 (不输出BUY/SELL)。"""
    pe = _parse_value(ctx, r"PE\(市盈率\)")
    pb = _parse_value(ctx, r"PB\(市净率\)")
    roe = _parse_value(ctx, r"ROE\(净资产收益率\)")
    np_growth = _parse_value(ctx, "净利润增长率")
    rev_growth = _parse_value(ctx, "营收增长率")
    debt = _parse_value(ctx, "资产负债率")

    valuation_score = 0
    growth_score = 0
    health_score = 0
    signals = []
    risk_flags = []

    if pe is not None:
        if pe < 10: valuation_score += 3; signals.append(f"PE={pe}极低估值")
        elif pe < 20: valuation_score += 1
        elif pe > 50: valuation_score -= 3; signals.append(f"PE={pe}高估值")

    if pb is not None:
        if pb < 0.8: valuation_score += 3; signals.append(f"PB={pb}破净")
        elif pb > 8: valuation_score -= 2

    if roe is not None:
        if roe > 25: growth_score += 3
        elif roe > 15: growth_score += 2
        elif roe < 5: growth_score -= 2; risk_flags.append(f"ROE={roe}%偏低")

    if np_growth is not None:
        if np_growth > 20: growth_score += 2
        elif np_growth < 0: growth_score -= 3; risk_flags.append("净利润负增长")

    # 负债率
    debt_warning = False
    debt_severity = "safe"
    if debt is not None:
        if debt > 85:
            health_score -= 6; debt_warning = True; debt_severity = "danger"
            risk_flags.append(f"负债率{debt}%>85%严重高杠杆")
        elif debt > 70:
            health_score -= 2; debt_warning = True; debt_severity = "caution"
            risk_flags.append(f"负债率{debt}%偏高")
        elif debt > 50:
            health_score -= 1

    roe_quality = "excellent" if (roe and roe > 20) else ("good" if (roe and roe > 15) else ("fair" if (roe and roe > 5) else "weak"))

    return {
        "agent": "fundamental_analyst", "type": "Mock(启发式)",
        "valuation_score": max(-10, min(10, valuation_score)),
        "growth_score": max(-10, min(10, growth_score)),
        "financial_health_score": max(-10, min(10, health_score)),
        "pe_assessment": "undervalued" if (pe and pe < 10) else ("overvalued" if (pe and pe > 50) else "fair"),
        "pb_assessment": "undervalued" if (pb and pb < 0.8) else "fair",
        "roe_quality": roe_quality,
        "debt_warning": debt_warning,
        "debt_severity": debt_severity,
        "earnings_momentum": "accelerating" if (np_growth and np_growth > 20) else ("declining" if (np_growth and np_growth < 0) else "stable"),
        "risk_flags": risk_flags,
        "confidence": 0.55,
        "reasoning": "；".join(signals[:3])[:120] if signals else "基本面数据解析完成"
    }


def _mock_sentiment(ctx: str) -> dict:
    """启发式新闻情绪评估 → 结构化数据格式."""
    # 无新闻数据时返回中性评分
    if "无新闻数据" in ctx:
        return {
            "agent": "sentiment_analyst", "type": "Mock(启发式·无数据)",
            "sentiment_score": 0,
            "impact_level": "low",
            "positive_count": 0, "negative_count": 0,
            "positive_themes": [], "negative_themes": [],
            "sentiment_trend": "stable",
            "extreme_warning": False,
            "confidence": 0.35,
            "reasoning": "新闻数据不可用，情绪评分中性"
        }

    positive_kw = ['增长', '突破', '回购', '中标', '签约', '增持', '买入', '利好',
                   '上涨', '新高', '分红', '盈利', '获奖', '通过', '获批', '超预期']
    negative_kw = ['下滑', '亏损', '处罚', '调查', '减持', '卖出', '跌停', '利空',
                   '下跌', '新低', '违规', '退市', '破产', '暴雷', '造假', '问询']

    lines = ctx.split('\n')
    pos_count = 0; neg_count = 0
    pos_themes = []; neg_themes = []
    for line in lines:
        if not line.strip().startswith('['):
            continue
        for kw in positive_kw:
            if kw in line:
                pos_count += 1; pos_themes.append(kw)
                break
        else:
            for kw in negative_kw:
                if kw in line:
                    neg_count += 1; neg_themes.append(kw)
                    break

    total = max(pos_count + neg_count, 1)
    pos_ratio = pos_count / total
    sentiment = round((pos_ratio - 0.5) * 20)
    trend = "improving" if pos_ratio > 0.6 else ("deteriorating" if pos_ratio < 0.4 else "stable")

    return {
        "agent": "sentiment_analyst", "type": "Mock(启发式)",
        "sentiment_score": max(-10, min(10, sentiment)),
        "impact_level": "high" if abs(sentiment) >= 6 else ("medium" if abs(sentiment) >= 3 else "low"),
        "positive_count": pos_count,
        "negative_count": neg_count,
        "positive_themes": list(set(pos_themes))[:3],
        "negative_themes": list(set(neg_themes))[:3],
        "sentiment_trend": trend,
        "extreme_warning": pos_count + neg_count > 0 and (pos_ratio >= 0.8 or pos_ratio <= 0.2),
        "confidence": 0.55,
        "reasoning": f"{pos_count}条利好 vs {neg_count}条利空，情绪{'偏正面' if sentiment>2 else ('偏负面' if sentiment<-2 else '中性')}"
    }


def _mock_macro(ctx: str) -> dict:
    """启发式宏观评估 → 结构化数据格式."""
    return {
        "agent": "macro_analyst", "type": "Mock(推断)",
        "market_regime": "SIDEWAYS",
        "liquidity_score": 2,
        "policy_stance": "supportive",
        "sector_tailwind": False,
        "position_cap_pct": 25,
        "macro_risks": ["宏观数据占位符阶段"],
        "confidence": 0.35,
        "reasoning": "基于2026年A股震荡修复+货币宽松预期+CPI低位背景推断，非实时数据。"
    }


# ═══════════════════════════════════════════════════════════════
# 5. 主运行函数
# ═══════════════════════════════════════════════════════════════

def run_all_agents(compressed_data: dict, use_mock: bool = False) -> list[dict]:
    """
    主入口：运行全部 4 个 LLM Agent + 1 个本地风控，返回结构化数据报告列表。

    量化基金架构: Agent 提取结构化数据（趋势强度、估值评分、情绪评分等），
    最终买卖决策由因子模型 generate_3d_factor_signals 生成。

    Args:
        compressed_data: data_pipeline.py 输出的压缩 JSON
        use_mock: True=用启发式规则模拟LLM，False=调用DeepSeek API
                  受 ALLOW_MOCK 环境变量控制（默认 false，即必须实调 API）

    Returns:
        list[dict]: 5个Agent的分析报告（结构化数据格式）
    """
    # 环境变量控制: ALLOW_MOCK=false 时强制实调 API
    import os as _os
    _allow_mock_env = _os.environ.get("ALLOW_MOCK", "false").lower() in ("true", "1", "yes")
    if not _allow_mock_env and use_mock:
        print("  [MODE] ALLOW_MOCK=false，强制切换为 DeepSeek V4 Pro 实调模式")
        use_mock = False

    api_mode = "Mock模拟(本地启发式)" if use_mock else "DeepSeek V4 Pro 实调"
    print(f"\n  {'='*60}")
    print(f"  当前分析模式: {api_mode}")
    print(f"  Agent 角色: 结构化数据提取 (不输出 BUY/SELL)")
    print(f"  最终决策: 因子模型统计引擎")
    print(f"  {'='*60}")

    agents_config = [
        ("technical_analyst", AGENT_PROMPTS["technical_analyst"],
         format_technical_context(compressed_data)),
        ("fundamental_analyst", AGENT_PROMPTS["fundamental_analyst"],
         format_fundamental_context(compressed_data)),
        ("sentiment_analyst", AGENT_PROMPTS["sentiment_analyst"],
         format_sentiment_context(compressed_data)),
        ("macro_analyst", AGENT_PROMPTS["macro_analyst"],
         format_macro_context(compressed_data)),
    ]

    reports = []

    for name, prompt, context in agents_config:
        mode_tag = "Mock(启发式)" if use_mock else "DeepSeek V4 Pro"
        print(f"\n{card_header(f'[{name}] ' + mode_tag)}")
        print(f"{card_line('Extracting structured data...')}")
        print(f"{card_bottom()}")

        report = _call_llm_agent(name, prompt, context, use_mock=use_mock)
        reports.append(report)

        # 显示提取的结构化数据摘要
        rtype = report.get('type', '?')
        print(f"  Type: {rtype} | Confidence: {report.get('confidence', 0):.0%}")
        reasoning = str(report.get('reasoning', ''))[:100]
        if reasoning:
            print(f"  {reasoning}")

    # ── 5. 风控 (本地) ──────────────────────────────────────
    print(f"\n{card_header('[risk_manager] 本地风控计算')}")
    risk_report = run_risk_manager(compressed_data)
    reports.append(risk_report)
    r = risk_report

    risk_color = {"高": "[!]", "中": "[~]", "偏低": "[=]", "低": "[+]"}.get(r['risk_level'], "[?]")
    print(f"  {risk_color} 风险: {r['risk_level']}({r['risk_score']}/100) | "
          f"仓位上限: {r['position_ratio']:.0%} | 波动率: {r.get('volatility_pct','?')}%")
    reasoning = r.get('reasoning', '')[:120]
    if reasoning:
        print(f"  {reasoning}")
    print(f"{card_bottom()}")

    # ── 资产负债率硬规则后验（兜底 LLM 未遵守的情况）──
    _enforce_debt_ratio_rules(reports, compressed_data)

    # ── 趋势感知硬规则（BEAR降级/SIDEWAYS降仓/BULL加仓）──
    _apply_trend_hard_rules(reports, compressed_data)

    # ── 中长线多时间框架共振 + 基本面硬过滤 ──
    _apply_weekly_mid_long_filter(reports, compressed_data)

    return reports


def _apply_trend_hard_rules(reports: list[dict], data: dict):
    """
    趋势感知硬规则：
    - BEAR：所有BUY/CAUTIOUS_BUY降级为HOLD，仅允许持有已开长线仓位
    - SIDEWAYS：BUY保留，但仓位建议×0.7
    - BULL：仓位建议×1.2
    """
    trend = data.get("trend_state", {})
    state = trend.get("trend_state", "SIDEWAYS")
    ma60_slope = trend.get("ma60_slope")

    if state == "BEAR":
        downgrade_count = 0
        for r in reports:
            sig = r.get("signal", "HOLD")
            if sig in ("BUY", "CAUTIOUS_BUY"):
                r["signal"] = "HOLD"
                r["score"] = min(r.get("score", 0), -2)
                r["confidence"] = round(r.get("confidence", 0.5) * 0.7, 2)
                r["reasoning"] = (r.get("reasoning", "")[:60]
                                  + f" [趋势硬规则:BEAR(MA60斜率{ma60_slope}%)→BUY降级HOLD]")[:120]
                downgrade_count += 1
            # 风险经理仓位强制降低
            if r.get("agent") == "risk_manager":
                r["position_ratio"] = min(r.get("position_ratio", 0.2), 0.15)
                r["risk_score"] = min(r.get("risk_score", 50) + 15, 100)
        if downgrade_count > 0:
            print(f"  [!!] BEAR趋势: {downgrade_count}个BUY信号降级为HOLD (MA60斜率{ma60_slope}%)")

    elif state == "SIDEWAYS":
        for r in reports:
            if r.get("agent") == "risk_manager":
                old_ratio = r.get("position_ratio", 0.3)
                r["position_ratio"] = round(old_ratio * 0.7, 2)
                r["reasoning"] = r.get("reasoning", "")[:80] + f" [SIDEWAYS:仓位{old_ratio:.0%}→{r['position_ratio']:.0%}]"
        print(f"  [~] SIDEWAYS趋势: 仓位建议降至70% (MA60斜率{ma60_slope}%)")

    elif state == "BULL":
        for r in reports:
            if r.get("agent") == "risk_manager":
                old_ratio = r.get("position_ratio", 0.3)
                r["position_ratio"] = min(round(old_ratio * 1.2, 2), 0.85)
                r["risk_score"] = max(r.get("risk_score", 50) - 10, 10)
                r["reasoning"] = r.get("reasoning", "")[:80] + f" [BULL:仓位{old_ratio:.0%}→{r['position_ratio']:.0%}]"
        print(f"  [+] BULL趋势: 仓位上限提升至120% (MA60斜率{ma60_slope}%)")


def _apply_weekly_mid_long_filter(reports: list[dict], data: dict):
    """
    中长线多时间框架共振 + 基本面硬过滤：

    中线开仓条件（缺一不可）：
      1. 日线 trend_state != BEAR
      2. weekly_trend = UP
      否则 → HOLD

    长线开仓条件（缺一不可）：
      1. 日线 trend_state != BEAR
      2. weekly_trend = UP
      3. fundamental_analyst score >= 4
      否则 → HOLD
    """
    trend = data.get("trend_state", {})
    weekly = data.get("weekly_trend", {})
    daily_state = trend.get("trend_state", "SIDEWAYS")
    weekly_state = weekly.get("weekly_trend", "UNKNOWN")

    fundamental_score = 0
    for r in reports:
        if r.get("agent") == "fundamental_analyst":
            fundamental_score = r.get("score", 0)
            break

    downgrade_count = 0
    for r in reports:
        agent = r.get("agent", "")
        sig = r.get("signal", "HOLD")

        # 只过滤中线和长线的时间维度Agent信号
        if agent not in ("mid_term_trader", "long_term_catcher"):
            continue

        if sig not in ("BUY", "CAUTIOUS_BUY"):
            continue

        should_block = False
        block_reasons = []

        # 条件1：日线趋势不能是BEAR
        if daily_state == "BEAR":
            should_block = True
            block_reasons.append(f"日线{daily_state}")

        # 条件2：周线趋势必须是UP
        if weekly_state != "UP":
            should_block = True
            block_reasons.append(f"周线{weekly_state}(需UP)")

        # 条件3 (仅长线)：基本面评分 >= 4（历史回测模式数据不可用时放宽）
        if agent == "long_term_catcher":
            fin = data.get("financial", {})
            has_fundamental = any(
                fin.get(k) is not None
                for k in ("pe", "pb", "roe", "net_profit_growth", "revenue_growth", "debt_ratio")
            )
            if has_fundamental and fundamental_score < 4:
                should_block = True
                block_reasons.append(f"基本面评分{fundamental_score}<4")

        if should_block:
            r["signal"] = "HOLD"
            r["score"] = min(r.get("score", 0), -3)
            r["confidence"] = round(r.get("confidence", 0.5) * 0.6, 2)
            r["reasoning"] = (r.get("reasoning", "")[:60]
                              + f" [周线过滤:{';'.join(block_reasons)}→HOLD]")[:120]
            downgrade_count += 1

    if downgrade_count > 0:
        print(f"  [!!] 中长线周线+基本面过滤: {downgrade_count}个信号降级 "
              f"(日线={daily_state} 周线={weekly_state} 基本面评分={fundamental_score})")


def _enforce_debt_ratio_rules(reports: list[dict], data: dict):
    """对 fundamental_analyst 和 risk_manager 的资产负债率硬规则兜底校验。"""
    debt_ratio = data.get("financial", {}).get("debt_ratio")
    if debt_ratio is None:
        return

    for r in reports:
        if r.get("agent") == "fundamental_analyst":
            sig = r.get("signal", "HOLD")
            if debt_ratio > 80 and sig == "BUY":
                r["signal"] = "HOLD"
                r["score"] = min(r.get("score", 0), -4)
                r["reasoning"] = r.get("reasoning", "")[:80] + f" [硬规则修正:负债率{debt_ratio}%>80%强制降级]"
                print(f"  [!!] fundamental_analyst BUY→HOLD (负债率{debt_ratio}%>80%硬规则兜底)")
            if debt_ratio > 85 and sig in ("BUY", "HOLD"):
                r["signal"] = "CAUTIOUS_BUY"
                r["score"] = min(r.get("score", 0), -3)
                print(f"  [!!] fundamental_analyst {sig}→CAUTIOUS_BUY (负债率{debt_ratio}%>85%硬规则兜底)")

        if r.get("agent") == "risk_manager":
            if debt_ratio > 90:
                if r.get("position_ratio", 0) > 0.02:
                    r["position_ratio"] = 0.02
                    print(f"  [!!] risk_manager 仓位强制降至2% (负债率{debt_ratio}%>90%)")
            elif debt_ratio > 85:
                if r.get("position_ratio", 0) > 0.05:
                    r["position_ratio"] = 0.05
                    print(f"  [!!] risk_manager 仓位强制降至5% (负债率{debt_ratio}%>85%)")


def aggregate_signals(reports: list[dict]) -> dict:
    """
    汇总所有 Agent 信号，加权计算综合得分。

    权重分配:
        technical_analyst:  25%
        fundamental_analyst: 25%
        sentiment_analyst:   15%
        macro_analyst:       10%
        risk_manager:        25%
    """
    weights = {
        "technical_analyst": 0.30,   # 原0.25 — 技术面更直接反映买卖时机
        "fundamental_analyst": 0.30, # 原0.25 — 基本面决定长期价值
        "sentiment_analyst": 0.15,
        "macro_analyst": 0.10,
        "risk_manager": 0.15,        # 原0.25 — 降低风控权重，避免过度保守
    }

    total_weight = 0
    weighted_score = 0
    weighted_conf = 0

    signals = {"BUY": 0, "SELL": 0, "HOLD": 0, "CAUTIOUS_BUY": 0, "CAUTIOUS_SELL": 0}

    for r in reports:
        agent = r.get("agent", "")
        w = weights.get(agent, 0.10)
        s = r.get("score", 0)
        c = r.get("confidence", 0.5)
        sig = r.get("signal", "HOLD")

        weighted_score += s * w * c
        weighted_conf += w * c
        total_weight += w
        signals[sig] += 1

    if weighted_conf > 0:
        final_score = weighted_score / weighted_conf
    elif total_weight > 0:
        final_score = weighted_score / total_weight
    else:
        final_score = 0

    final_score = round(final_score, 1)
    if final_score >= 2:
        consensus = "BUY"
    elif final_score <= -3:
        consensus = "SELL"
    else:
        consensus = "HOLD"

    # 一致性检查
    buy_pct = signals["BUY"] / len(reports) * 100
    sell_pct = signals["SELL"] / len(reports) * 100

    if buy_pct >= 60:
        agreement = "看多一致"
    elif sell_pct >= 60:
        agreement = "看空一致"
    elif buy_pct + sell_pct >= 60:
        agreement = "多空分歧"
    else:
        agreement = "观望为主"

    return {
        "consensus": consensus,
        "final_score": final_score,
        "agreement": agreement,
        "signal_distribution": signals,
        "buy_ratio": round(buy_pct, 0),
        "sell_ratio": round(sell_pct, 0),
        "timestamp": datetime.now().isoformat(),
    }


def print_summary(reports: list[dict]):
    """打印所有 Agent 结果的汇总表。"""
    agg = aggregate_signals(reports)

    cols = [("Agent", 22), ("Signal", 16), ("Score", 7), ("Conf", 6), ("Type", 16)]
    print(f"\n{section_div(' MULTI-AGENT 综合分析汇总 ')}")
    print(table_header(cols))
    print(table_sep(cols))
    for r in reports:
        name = r.get("agent", "?")
        sig = format_signal(r.get("signal", "?"))
        sc = f"{int(r.get('score', 0)):+d}"
        cf = f"{r.get('confidence', 0):.0%}"
        tp = r.get("type", "?")
        print(table_row([name, sig, sc, cf, tp], cols))
    print(table_sep(cols, char="="))

    # 综合信号行
    s = agg["signal_distribution"]
    c_str = f"  Consensus: {agg['consensus']} | Score: {agg['final_score']:+} | Agreement: {agg['agreement']}"
    print(f"| {c_str:<{CARD_W - 2}} |")
    d_str = f"  BUY={s.get('BUY',0)} SELL={s.get('SELL',0)} HOLD={s.get('HOLD',0)} CAUTIOUS_BUY={s.get('CAUTIOUS_BUY',0)} CAUTIOUS_SELL={s.get('CAUTIOUS_SELL',0)}"
    print(f"| {d_str:<{CARD_W - 2}} |")
    print(f"{card_bottom()}\n")


# ═══════════════════════════════════════════════════════════════
# 6. 结构化因子提取 — LLM 提供因子输入，不输出交易信号
# ═══════════════════════════════════════════════════════════════

def extract_news_sentiment(compressed_data: dict, use_mock: bool = True) -> dict:
    """
    从新闻中提取结构化情绪评分，作为因子模型的输入。

    LLM 角色：提取 + 量化，不输出 BUY/SELL。

    Returns:
        {"sentiment_score": -5~+5, "key_events": [...], "impact_score": 0~10}
    """
    news = compressed_data.get("news", [])
    if not isinstance(news, list):
        news = list(news.values()) if isinstance(news, dict) else []
    if not news:
        return {"sentiment_score": 0, "key_events": [], "impact_score": 0}

    if use_mock:
        return _mock_news_sentiment(news)

    from agents.prompts import NEWS_SENTIMENT_PROMPT
    ctx = format_sentiment_context(compressed_data)
    try:
        raw = DeepSeekClient().chat(NEWS_SENTIMENT_PROMPT, ctx, max_tokens=512, timeout=30)
        result = _parse_agent_json(raw, "news_sentiment")
        sentiment_score = max(-5, min(5, int(result.get("sentiment_score", 0))))
        impact_score = max(0, min(10, int(result.get("impact_score", 5))))
        key_events = result.get("key_events", [])
        if not isinstance(key_events, list):
            key_events = []
        return {
            "sentiment_score": sentiment_score,
            "key_events": key_events[:5],
            "impact_score": impact_score,
        }
    except Exception:
        return _mock_news_sentiment(news)


def _mock_news_sentiment(news: list) -> dict:
    """Heuristic news sentiment extraction without LLM."""
    positive_kw = ['增长', '突破', '回购', '中标', '签约', '增持', '买入', '利好',
                   '上涨', '新高', '分红', '盈利', '获奖', '通过', '获批', '超预期',
                   '扭亏', '预增', '翻倍', '创新高']
    negative_kw = ['下滑', '亏损', '处罚', '调查', '减持', '卖出', '跌停', '利空',
                   '下跌', '新低', '违规', '退市', '破产', '暴雷', '造假', '问询',
                   '预亏', '腰斩', '跌穿']

    pos_count = 0
    neg_count = 0
    events = []

    for item in news[:10]:
        title = item if isinstance(item, str) else item.get("title", "")
        pos_hits = sum(1 for kw in positive_kw if kw in title)
        neg_hits = sum(1 for kw in negative_kw if kw in title)
        impact = pos_hits - neg_hits
        if impact > 0:
            pos_count += 1
            events.append({"title": title[:60], "impact": min(3, impact), "category": "positive"})
        elif impact < 0:
            neg_count += 1
            events.append({"title": title[:60], "impact": max(-3, impact), "category": "negative"})

    net = pos_count - neg_count
    sentiment_score = max(-5, min(5, net * 2))
    impact_score = min(10, abs(net) * 3 + 3)

    return {
        "sentiment_score": sentiment_score,
        "key_events": events[:5],
        "impact_score": impact_score,
    }


def extract_qualitative_factors(compressed_data: dict, use_mock: bool = True) -> dict:
    """
    提取定性因子（护城河、管理层、行业地位），用于中长期因子模型。

    LLM 角色：定性评估 → 结构化输出，不输出 BUY/SELL。

    Returns:
        {"moat_score": 0~10, "management_score": 0~10,
         "industry_position": "leader"/"challenger"/"niche"/"declining",
         "growth_catalyst": str, "risk_factors": [str]}
    """
    if use_mock:
        return _mock_qualitative(compressed_data)

    from agents.prompts import QUALITATIVE_PROMPT
    ctx = format_fundamental_context(compressed_data)
    try:
        raw = DeepSeekClient().chat(QUALITATIVE_PROMPT, ctx, max_tokens=512, timeout=30)
        result = _parse_agent_json(raw, "qualitative")
        return {
            "moat_score": max(0, min(10, int(result.get("moat_score", 5)))),
            "management_score": max(0, min(10, int(result.get("management_score", 5)))),
            "industry_position": result.get("industry_position", "niche"),
            "growth_catalyst": str(result.get("growth_catalyst", ""))[:200],
            "risk_factors": result.get("risk_factors", [])[:5],
        }
    except Exception:
        return _mock_qualitative(compressed_data)


def _mock_qualitative(data: dict) -> dict:
    """Heuristic qualitative assessment from financial data."""
    f = data.get("financial", {})
    roe = f.get("roe")
    gross_margin = f.get("gross_margin")
    rev_growth = f.get("revenue_growth")
    debt = f.get("debt_ratio")

    moat = 5
    if gross_margin is not None:
        if gross_margin > 60:
            moat += 3
        elif gross_margin > 30:
            moat += 1
        elif gross_margin < 10:
            moat -= 2

    if roe is not None:
        if roe > 25:
            moat += 2
        elif roe < 5:
            moat -= 2

    mgmt = 5
    if rev_growth is not None and rev_growth > 20:
        mgmt += 2
    if debt is not None and debt > 80:
        mgmt -= 2

    position = "niche"
    if moat >= 7:
        position = "leader"
    elif moat >= 4:
        position = "challenger"
    elif moat < 3:
        position = "declining"

    return {
        "moat_score": max(0, min(10, moat)),
        "management_score": max(0, min(10, mgmt)),
        "industry_position": position,
        "growth_catalyst": "",
        "risk_factors": [],
    }


# ═══════════════════════════════════════════════════════════════
# CLI 入口
# ═══════════════════════════════════════════════════════════════
if __name__ == '__main__':
    import argparse, os

    # 强制UTF-8
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8')

    parser = argparse.ArgumentParser(description='多Agent股票分析运行器')
    parser.add_argument('input', nargs='?', default='-',
                        help='数据JSON文件路径，默认从stdin读取')
    parser.add_argument('--mock', action='store_true',
                        help='使用启发式规则模拟LLM（API不可用时自动启用）')
    parser.add_argument('--symbol', help='直接指定股票代码，内部调用data_pipeline')
    args = parser.parse_args()

    # 获取数据
    if args.symbol:
        print(f"从 data_pipeline 获取 {args.symbol} 数据...")
        import subprocess
        pipe_path = os.path.join(os.path.dirname(__file__), 'data_pipeline.py')
        result = subprocess.run(
            ['py', pipe_path, args.symbol],
            capture_output=True, text=True, encoding='utf-8'
        )
        if result.returncode != 0:
            print(f"data_pipeline 错误: {result.stderr}")
            sys.exit(1)
        raw_data = result.stdout.strip()
    elif args.input == '-':
        raw_data = sys.stdin.read().strip()
        if not raw_data:
            print("错误：stdin 无数据。请通过管道输入或使用 --symbol 参数。")
            print("用法: py data_pipeline.py 600519 | py agent_runner.py")
            print("  或: py agent_runner.py --symbol 600519")
            sys.exit(1)
    else:
        with open(args.input, 'r', encoding='utf-8') as f:
            raw_data = f.read().strip()

    data = json.loads(raw_data)
    stock_name = data.get("quote", {}).get("name", data.get("symbol", "?"))
    print(f"\n分析标的: {stock_name} ({data.get('symbol')})")
    print(f"数据时间: {data.get('timestamp', '?')}")

    # 判断是否用 Mock
    use_mock = args.mock
    if not use_mock:
        # 快速检测 API 可用性
        try:
            from data.deepseek import deepseek_chat
            deepseek_chat("", "ping", max_tokens=256, timeout=15)
            print("DeepSeek API: 已连接\n")
        except Exception as e:
            print(f"DeepSeek API: 不可用 ({e})，自动切换 Mock 模式\n")
            use_mock = True

    reports = run_all_agents(data, use_mock=use_mock)
    print_summary(reports)
