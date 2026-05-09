"""
持仓退出策略模块 — 纯本地计算，不调用 LLM。
根据 MA5/MA20/RSI/换手率等数据，生成卖出建议。
"""


def assess_exit(symbol: str, entry_price: float, quantity: int,
               current_price: float, compressed_data: dict) -> dict:
    """
    基于当前行情和技术指标计算持仓卖出建议。

    Args:
        symbol: 股票代码
        entry_price: 入场价
        quantity: 持仓数量
        current_price: 当前价格
        compressed_data: data_pipeline 输出的压缩数据

    Returns:
        {"action": "HOLD/SELL/PARTIAL_SELL", "sell_ratio": 0-100, "reasons": [...]}
    """
    t = compressed_data.get("technical", {})
    q = compressed_data.get("quote", {})

    profit_pct = (current_price - entry_price) / entry_price * 100 if entry_price > 0 else 0

    ma5 = t.get("ma5")
    ma20 = t.get("ma20")
    rsi = t.get("rsi14")
    turnover = q.get("turnover")
    change_pct = q.get("change_pct")

    reasons = []
    sell_ratio = 0
    action = "HOLD"

    # ── 1. 止盈：盈利 > 8% 且 RSI > 75 → 卖出 50%仓位 ──
    if profit_pct > 8 and rsi is not None and rsi > 75:
        sell_ratio = max(sell_ratio, 50)
        action = "PARTIAL_SELL"
        reasons.append(f"止盈触发: 盈利{profit_pct:.1f}%>8% 且 RSI={rsi}>75超买，建议卖出50%仓位")

    # ── 2. 止损：亏损 > 5% 或价格跌破 MA5 且 MA5 向下拐头 → 清仓 ──
    ma5_trending_down = False
    if ma5 is not None and ma20 is not None:
        # MA5 向下拐头判断：当前价格 < MA5，且 MA5-MA20 差距在缩小
        if current_price < ma5 and ma5 <= ma20:
            ma5_trending_down = True

    if profit_pct < -5:
        sell_ratio = 100
        action = "SELL"
        reasons.append(f"止损触发: 亏损{profit_pct:.1f}%<-5%，建议清仓")
    elif ma5 is not None and current_price < ma5 and ma5_trending_down:
        sell_ratio = 100
        action = "SELL"
        reasons.append(f"止损触发: 价格{current_price}跌破MA5({ma5})且MA5向下拐头，建议清仓")

    # ── 3. 危险信号：换手率 > 15% 且涨幅 < 1% → 滞涨警告 ──
    if turnover is not None and turnover > 15 and change_pct is not None and change_pct < 1:
        if action == "HOLD":
            action = "PARTIAL_SELL"
            sell_ratio = 50
        elif action == "PARTIAL_SELL":
            sell_ratio = max(sell_ratio, 50)
        reasons.append(f"滞涨警告: 换手率{turnover}%>15%但涨幅仅{change_pct}%，主力可能出货，建议减仓")

    # ── 4. 跟踪止盈：从最高点回撤超过 3% → 卖出剩余仓位 ──
    # 使用布林上轨作为近期"最高点"的近似
    boll_upper = t.get("boll_upper")
    if boll_upper is not None and boll_upper > 0:
        drawdown_from_high = (boll_upper - current_price) / boll_upper * 100
        if profit_pct > 0 and drawdown_from_high > 3:
            reasons.append(f"跟踪止盈提示: 从布林上轨{boll_upper}回撤{drawdown_from_high:.1f}%>3%，建议卖出剩余仓位")
            if action == "HOLD" and sell_ratio == 0:
                action = "PARTIAL_SELL"
                sell_ratio = 50

    if not reasons:
        reasons.append(f"当前无卖出信号: 盈亏{profit_pct:+.1f}%, RSI={rsi}, MA5={ma5}")

    return {
        "symbol": symbol,
        "entry_price": entry_price,
        "current_price": current_price,
        "profit_pct": round(profit_pct, 2),
        "quantity": quantity,
        "action": action,
        "sell_ratio": sell_ratio,
        "reasons": reasons,
    }
