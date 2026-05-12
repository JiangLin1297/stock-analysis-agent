#!/usr/bin/env python3
"""
持仓动态管理模块 — 加仓/减仓/持有/清仓的全维度评估。
纯本地计算，供 decision_engine 和 executive_agent 调用。

三线规则：
  短线（1天~2周）：盈利3%加第一次、5%加第二次、上限20%，浮亏不允许加仓
  中线（2周~6个月）：盈利8%加仓、上限30%，浮亏仅基本面无变化+布林下轨站稳可补一次
  长线（6个月~数年）：分3次建仓、回踩MA20回弹确认后加、上限45%

动态退出：
  短线：冲高回落5%+放量→止盈50%、盈利>10%移动止盈回撤3%
  中线：盈利>15%移动止损回撤5%、MA20拐头减半仓
  长线：盈利>15%移动止损回撤5%、季度ROE连续下滑>20%减半仓
"""


def evaluate_holding(symbol: str, entry_price: float, current_price: float,
                     quantity: int, timeframe: str,
                     compressed_data: dict,
                     peak_price: float = None,
                     quarterly_roe_history: list = None) -> dict:
    """
    评估当前持仓，返回动态管理建议。

    Args:
        symbol: 股票代码
        entry_price: 入场均价
        current_price: 当前价格
        quantity: 持仓数量
        timeframe: "short_term" / "mid_term" / "long_term"
        compressed_data: data_pipeline 输出的完整数据
        peak_price: 持仓期间最高价（用于移动止盈计算），默认=current_price
        quarterly_roe_history: [(季度标签, ROE%), ...] 按时间倒序，用于长线ROE下滑检测

    Returns:
        {
            "action": "HOLD" / "ADD" / "TRIM" / "CLOSE",
            "ratio": 0-100,  # 加仓比例(相对当前仓位) 或 减仓比例
            "add_quantity": int,  # 建议加仓股数
            "sell_quantity": int,  # 建议卖出股数
            "exit_strategy": {...},
            "reasons": [...],
            "triggered_rules": [...],
        }
    """
    if peak_price is None:
        peak_price = current_price

    profit_pct = (current_price - entry_price) / entry_price * 100 if entry_price > 0 else 0
    drawdown_from_peak = (peak_price - current_price) / peak_price * 100 if peak_price > 0 else 0

    t = compressed_data.get("technical", {})
    q = compressed_data.get("quote", {})
    f = compressed_data.get("financial", {})
    bs = compressed_data.get("breakout_signals", {})
    trend = compressed_data.get("trend_state", {})
    weekly_trend_data = compressed_data.get("weekly_trend", {})

    ma5 = t.get("ma5")
    ma10 = t.get("ma10")
    ma20 = t.get("ma20")
    ma60 = t.get("ma60")
    rsi = t.get("rsi14")
    atr = t.get("atr14")
    boll_upper = t.get("boll_upper")
    boll_lower = t.get("boll_lower")
    boll_mid = t.get("boll_mid")
    turnover = q.get("turnover")
    change_pct = q.get("change_pct")
    ma20_slope = t.get("ma20_slope")
    ma5_slope = t.get("ma5_slope")
    trend_state = trend.get("trend_state", "SIDEWAYS")

    reasons = []
    triggered_rules = []
    sell_ratio = 0
    add_ratio = 0
    action = "HOLD"

    # ═══════════════════════════════════════════════════════════
    # 通用退出规则（所有时间维度）
    # ═══════════════════════════════════════════════════════════

    # 冲高回落5%+放量 → 止盈50%（短线+中线）
    vol_ratio = bs.get("volume_ratio")
    if profit_pct > 0 and drawdown_from_peak > 5 and vol_ratio and vol_ratio > 1.5:
        sell_ratio = max(sell_ratio, 50)
        if action in ("HOLD", "ADD"):
            action = "TRIM"
        reasons.append(f"冲高回落{drawdown_from_peak:.1f}%+放量(量比{vol_ratio})→止盈50%")
        triggered_rules.append("drawdown_surge_exit")

    # 移动止损：盈利>15%后从最高点回撤5%→全清
    if profit_pct > 15 and drawdown_from_peak > 5:
        sell_ratio = 100
        action = "CLOSE"
        reasons.append(f"移动止损触发:盈利{profit_pct:.1f}%但回撤{drawdown_from_peak:.1f}%>5%→全清锁定利润")
        triggered_rules.append("trailing_stop_5pct")

    # 放量滞涨警告
    if turnover and turnover > 10 and change_pct is not None and abs(change_pct) < 1 and profit_pct > 0:
        if sell_ratio < 30:
            sell_ratio = 30
        if action == "HOLD":
            action = "TRIM"
        reasons.append(f"放量滞涨:换手{turnover}%但日涨跌仅{change_pct}%→建议减仓30%")
        triggered_rules.append("volume_stagnation")

    # ═══════════════════════════════════════════════════════════
    # 短线规则
    # ═══════════════════════════════════════════════════════════
    if timeframe == "short_term":
        # 加仓：盈利3%后加第一次
        if profit_pct >= 3 and profit_pct < 5 and add_ratio == 0:
            add_ratio = 50
            action = "ADD"
            reasons.append(f"短线盈利{profit_pct:.1f}%≥3%→可加第一次仓(≤原仓位50%)")
            triggered_rules.append("short_add_3pct")
        elif profit_pct >= 5:
            add_ratio = 50
            action = "ADD"
            reasons.append(f"短线盈利{profit_pct:.1f}%≥5%→可加第二次仓(≤原仓位50%)")
            triggered_rules.append("short_add_5pct")

        # 浮亏不允许加仓
        if profit_pct < 0 and action == "ADD":
            action = "HOLD"
            add_ratio = 0
            reasons.append("短线浮亏禁止加仓")
            triggered_rules.append("short_no_loss_add")

        # 盈利>10%移动止盈回撤3%卖一半
        if profit_pct > 10 and drawdown_from_peak > 3:
            sell_ratio = max(sell_ratio, 50)
            if action not in ("CLOSE",):
                action = "TRIM"
            reasons.append(f"短线移动止盈:盈利{profit_pct:.1f}%>10%且回撤{drawdown_from_peak:.1f}%>3%→卖一半")
            triggered_rules.append("short_trailing_3pct")

        # ATR动态止损：入场价 - 1.5 * ATR(14)
        if atr and atr > 0:
            atr_stop = round(entry_price - 1.5 * atr, 2)
            if current_price < atr_stop:
                sell_ratio = 100
                action = "CLOSE"
                reasons.append(f"短线ATR止损:价格{current_price}<ATR止损{atr_stop}(入场{entry_price}-1.5*ATR{atr})→清仓")
                triggered_rules.append("short_atr_stop")

        # 跌破MA5 → 若MA5下行则清仓，否则减半仓
        if ma5 and current_price < ma5:
            if ma5_slope is not None and ma5_slope < 0:
                sell_ratio = 100
                action = "CLOSE"
                reasons.append(f"短线MA5破位+下行:价格{current_price}<MA5({ma5}),MA5斜率{ma5_slope}→清仓")
                triggered_rules.append("short_ma5_break_close")
            elif action != "CLOSE":
                sell_ratio = max(sell_ratio, 50)
                if action not in ("CLOSE",):
                    action = "TRIM"
                reasons.append(f"短线MA5破位:价格{current_price}<MA5({ma5})→减半仓")
                triggered_rules.append("short_ma5_warning")

        # RSI>80且MACD死叉 → 清仓
        macd_hist = t.get("macd_histogram")
        if rsi and rsi > 80 and macd_hist is not None and macd_hist < 0:
            sell_ratio = 100
            action = "CLOSE"
            reasons.append(f"短线见顶:RSI={rsi}>80+MACD柱转负→清仓")
            triggered_rules.append("short_rsi_macd_exit")

        # 单票累计上限20%
        max_position_pct = 20

        # 退出策略
        exit_strategy = {
            "type": "trailing",
            "rules": [
                f"ATR止损:入场价-1.5*ATR({atr})={round(entry_price-1.5*atr,2) if atr else '?'}",
                "盈利>10%启用移动止盈，回撤3%卖一半",
                "跌破MA5减半仓",
                "RSI>80且MACD死叉清仓",
                "冲高回落5%+放量→止盈50%",
            ],
            "re_evaluation_triggers": ["跌破ATR止损", "RSI>80且死叉", "放量滞涨"],
        }

    # ═══════════════════════════════════════════════════════════
    # 中线规则
    # ═══════════════════════════════════════════════════════════
    elif timeframe == "mid_term":
        # 加仓：盈利8%后
        if profit_pct >= 8:
            add_ratio = 50
            action = "ADD"
            reasons.append(f"中线盈利{profit_pct:.1f}%≥8%→可加仓(≤原仓位50%)")
            triggered_rules.append("mid_add_8pct")

        # 浮亏补仓：仅基本面无变化+布林下轨站稳
        if profit_pct < 0 and boll_lower and current_price >= boll_lower * 0.98:
            roe = f.get("roe")
            debt = f.get("debt_ratio")
            if roe and roe > 10 and (debt is None or debt < 70):
                add_ratio = 30
                action = "ADD"
                reasons.append(f"中线浮亏补仓:价格{current_price}在布林下轨{boll_lower}站稳+基本面良好(ROE={roe}%)→谨慎补仓30%")
                triggered_rules.append("mid_loss_add_boll")

        # ATR动态止损：入场价 - 2.5 * ATR(14)
        if atr and atr > 0:
            atr_stop = round(entry_price - 2.5 * atr, 2)
            if current_price < atr_stop:
                sell_ratio = 100
                action = "CLOSE"
                reasons.append(f"中线ATR止损:价格{current_price}<ATR止损{atr_stop}(入场{entry_price}-2.5*ATR{atr})→清仓")
                triggered_rules.append("mid_atr_stop")

        # MA20走平或向下拐头 → 直接清仓
        if ma20_slope is not None and ma20_slope <= 0:
            sell_ratio = 100
            action = "CLOSE"
            reasons.append(f"中线趋势转弱:MA20斜率{ma20_slope}%≤0→清仓")
            triggered_rules.append("mid_ma20_flat_close")

        # 净利润增速转负 → 清仓
        np_growth = f.get("net_profit_growth")
        if np_growth is not None and np_growth < 0:
            sell_ratio = 100
            action = "CLOSE"
            reasons.append(f"中线基本面恶化:净利润增速{np_growth}%转负→清仓")
            triggered_rules.append("mid_np_negative")

        # RSI>75且MACD死叉 → 分批卖出
        macd_hist = t.get("macd_histogram")
        if rsi and rsi > 75 and macd_hist is not None and macd_hist < 0:
            sell_ratio = max(sell_ratio, 50)
            if action not in ("CLOSE",):
                action = "TRIM"
            reasons.append(f"中线超买:RSI={rsi}>75+MACD死叉→分批卖出")
            triggered_rules.append("mid_rsi_macd_exit")

        max_position_pct = 30

        # ── SIDEWAYS网格建仓（中线3批×8%，上限25%）──
        if trend_state == "SIDEWAYS" and action == "HOLD":
            grid_batches = 3
            grid_size_pct = 8
            sideway_entry_triggered = False

            # 触发条件：价格从入场价回落3%+ 且 RSI反弹（RSI<40后回升）
            if profit_pct <= -3 and rsi and rsi < 40:
                sideway_entry_triggered = True
                reasons.append(f"SIDEWAYS中线网格触发: 回撤{profit_pct:.1f}%+RSI={rsi}<40超卖反弹")

            # 或 布林下轨回踩确认
            if boll_lower and current_price <= boll_lower * 1.02:
                if profit_pct < 0:
                    sideway_entry_triggered = True
                    reasons.append(f"SIDEWAYS中线网格触发: 价格{current_price}触及布林下轨{boll_lower}")

            if sideway_entry_triggered:
                add_ratio = grid_size_pct
                action = "ADD"
                triggered_rules.append("sideways_mid_grid_add")
                reasons.append(f"SIDEWAYS中线网格建仓: 第1批{grid_size_pct}%仓位（共{grid_batches}批，上限25%）")

        # ── SIDEWAYS中线退出：布林上轨卖30% + 移动止盈 ──
        sideways_exit_applied = False
        if trend_state == "SIDEWAYS" and profit_pct > 0 and boll_upper:
            # 布林上轨止盈30%
            if current_price >= boll_upper * 0.98 and profit_pct > 3:
                sell_ratio = max(sell_ratio, 30)
                if action not in ("CLOSE",):
                    action = "TRIM"
                reasons.append(f"SIDEWAYS中线布林上轨止盈: 价格{current_price}≈上轨{boll_upper}→卖30%")
                triggered_rules.append("sideways_mid_boll_exit")
                sideways_exit_applied = True

            # 盈利5%后移动止盈回撤2%
            if profit_pct > 5 and drawdown_from_peak > 2:
                sell_ratio = max(sell_ratio, 70)
                if action not in ("CLOSE",):
                    action = "TRIM"
                reasons.append(f"SIDEWAYS中线移动止盈: 盈利{profit_pct:.1f}%回撤{drawdown_from_peak:.1f}%→卖70%")
                triggered_rules.append("sideways_mid_trailing")

        exit_strategy = {
            "type": "trailing",
            "rules": [
                f"ATR止损:入场价-2.5*ATR({atr})={round(entry_price-2.5*atr,2) if atr else '?'}",
                "盈利>15%启用移动止损，回撤5%全清",
                "MA20走平或向下拐头→减半仓",
                "净利润增速转负→清仓",
                "RSI>75且MACD死叉→分批卖出",
                "SIDEWAYS:布林上轨止盈30%+盈利5%回撤2%卖70%",
                "SIDEWAYS:3批×8%网格建仓(回撤3%+RSI<40触发)",
            ],
            "re_evaluation_triggers": ["MA20方向拐头", "RSI>75周线死叉", "财报净利润增速转负", "SIDEWAYS布林轨突破"],
        }

    # ═══════════════════════════════════════════════════════════
    # 长线规则
    # ═══════════════════════════════════════════════════════════
    else:  # long_term
        # 加仓：回踩MA20回弹确认
        if ma20 and boll_lower:
            ma20_distance = abs(current_price - ma20) / ma20 * 100
            if ma20_distance < 3 and current_price > ma20:
                # 检查近3日是否从MA20下方反弹
                add_ratio = 30
                action = "ADD"
                reasons.append(f"长线回踩MA20({ma20})确认:价格{current_price}距MA20仅{ma20_distance:.1f}%→加仓30%")
                triggered_rules.append("long_ma20_bounce_add")

        # 季度ROE连续两季下滑>20% → 减半仓
        if quarterly_roe_history and len(quarterly_roe_history) >= 3:
            q0 = quarterly_roe_history[0][1]  # 最新
            q1 = quarterly_roe_history[1][1]  # 上季
            q2 = quarterly_roe_history[2][1]  # 上上季
            if q1 > 0 and q0 > 0:
                decline_1 = (q1 - q0) / q1 * 100
                decline_2 = (q2 - q1) / q2 * 100
                if decline_1 > 20 and decline_2 > 20:
                    sell_ratio = max(sell_ratio, 50)
                    if action not in ("CLOSE",):
                        action = "TRIM"
                    reasons.append(f"长线ROE恶化:连续两季下滑{decline_2:.0f}%→{decline_1:.0f}%>20%→减半仓")
                    triggered_rules.append("long_roe_decline")

        # 营收增速连续两季<10% → 减至观察仓
        rev_growth = f.get("revenue_growth")
        if rev_growth is not None and rev_growth < 10:
            sell_ratio = max(sell_ratio, 75)
            if action not in ("CLOSE",):
                action = "TRIM"
            reasons.append(f"长线成长放缓:营收增速{rev_growth}%<10%→减至观察仓")
            triggered_rules.append("long_revenue_slow")

        # ATR动态止损：min(入场价 - 3.5*ATR(14), MA60 * 0.95)
        if atr and atr > 0:
            atr_stop = round(entry_price - 3.5 * atr, 2)
            ma60_stop = round(ma60 * 0.95, 2) if ma60 else None
            effective_stop = min(atr_stop, ma60_stop) if ma60_stop else atr_stop
            if current_price < effective_stop:
                sell_ratio = 100
                action = "CLOSE"
                reasons.append(f"长线ATR止损:价格{current_price}<有效止损{effective_stop}(ATR止损{atr_stop},MA60止损{ma60_stop})→清仓")
                triggered_rules.append("long_atr_stop")

        # 移动止损
        if profit_pct > 15 and drawdown_from_peak > 5:
            sell_ratio = 100
            action = "CLOSE"
            reasons.append(f"长线移动止损:盈利{profit_pct:.1f}%但回撤{drawdown_from_peak:.1f}%>5%→全清")
            triggered_rules.append("long_trailing_stop")

        max_position_pct = 45

        # ── SIDEWAYS长线网格建仓（5批×9%，上限45%）──
        if trend_state == "SIDEWAYS" and action == "HOLD":
            weekly_state = weekly_trend_data.get("weekly_trend", "UNKNOWN")
            weekly_ma20 = weekly_trend_data.get("weekly_ma20")

            sideway_long_triggered = False

            # 触发条件1：周线MA20回踩确认（价格在周MA20附近 + 周线趋势UP）
            if weekly_state == "UP" and weekly_ma20 and ma20:
                weekly_ma20_dist = abs(current_price - weekly_ma20) / weekly_ma20 * 100
                if weekly_ma20_dist < 5 and current_price > ma20:
                    sideway_long_triggered = True
                    reasons.append(f"长线SIDEWAYS周线回踩: 价{current_price}距周MA20({weekly_ma20}){weekly_ma20_dist:.1f}%")

            # 触发条件2：价格在布林下轨 + 周线非DOWN
            if boll_lower and current_price <= boll_lower * 1.03 and weekly_state != "DOWN":
                sideway_long_triggered = True
                reasons.append(f"长线SIDEWAYS布林下轨: 价格{current_price}≈下轨{boll_lower}+周线{weekly_state}")

            if sideway_long_triggered:
                add_ratio = 9  # 5批×9%=45%
                action = "ADD"
                triggered_rules.append("sideways_long_grid_add")
                reasons.append(f"SIDEWAYS长线网格建仓: 第1批{add_ratio}%仓位（共5批，上限45%，周线={weekly_state}）")

        exit_strategy = {
            "type": "trailing",
            "rules": [
                f"ATR止损:min(入场价-3.5*ATR({atr}),MA60*0.95)={round(min(entry_price-3.5*atr, ma60*0.95),2) if atr and ma60 else '?'}",
                "盈利>15%启用移动止损，回撤5%全清",
                "季度ROE连续两季下滑>20%→减半仓",
                "营收增速连续<10%→减至观察仓(10%)",
                "行业逻辑破坏→清仓",
                "SIDEWAYS长线:周线MA20回踩+布林下轨网格建仓×5批",
            ],
            "re_evaluation_triggers": ["每季财报ROE检查", "行业政策重大变化", "技术替代风险", "周线趋势变化"],
        }

    # ═══════════════════════════════════════════════════════════
    # 趋势过滤：BEAR时短线/中线只平不买
    # ═══════════════════════════════════════════════════════════
    if trend_state == "BEAR" and timeframe in ("short_term", "mid_term"):
        if action == "ADD":
            action = "HOLD"
            add_ratio = 0
            reasons.append(f"BEAR趋势:{timeframe}禁止加仓，只平不买")
            triggered_rules.append("bear_no_add")
        if action == "BUY":
            action = "HOLD"
            reasons.append(f"BEAR趋势:{timeframe}禁止新开仓")
            triggered_rules.append("bear_no_buy")

    # ═══════════════════════════════════════════════════════════
    # 通用：如果触发清仓，覆盖其他决策
    # ═══════════════════════════════════════════════════════════
    if action == "CLOSE":
        add_ratio = 0
        sell_ratio = 100

    # ═══════════════════════════════════════════════════════════
    # 如果无任何信号
    # ═══════════════════════════════════════════════════════════
    if not reasons:
        reasons.append(f"{timeframe}持仓评估:盈亏{profit_pct:+.1f}%，当前无触发信号，维持持有")
        triggered_rules.append("no_signal")

    # 计算股数
    add_qty = int(quantity * add_ratio / 100 / 100) * 100 if add_ratio > 0 else 0
    sell_qty = int(quantity * sell_ratio / 100 / 100) * 100 if sell_ratio > 0 else 0

    return {
        "symbol": symbol,
        "entry_price": entry_price,
        "current_price": current_price,
        "profit_pct": round(profit_pct, 2),
        "peak_price": peak_price,
        "drawdown_from_peak": round(drawdown_from_peak, 2),
        "quantity": quantity,
        "timeframe": timeframe,
        "action": action,
        "ratio": sell_ratio if action in ("TRIM", "CLOSE") else add_ratio,
        "add_quantity": add_qty,
        "sell_quantity": sell_qty,
        "max_position_pct": max_position_pct,
        "exit_strategy": exit_strategy,
        "reasons": reasons,
        "triggered_rules": triggered_rules,
    }
