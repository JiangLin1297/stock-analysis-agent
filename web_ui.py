"""
StockMind Web UI — Streamlit 全功能网页应用
替换桌面端，提供深度分析、智能选股、回测、持仓管理、策略基因库。
启动: python -m streamlit run web_ui.py --server.port 8501
"""

import streamlit as st
import sys
import os
import time
import json
import traceback
import threading
from io import StringIO
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ═══════════════════════════════════════════════════════════════
st.set_page_config(
    page_title="StockMind · 智能股析",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ═══════════════════════════════════════════════════════════════
# CSS
# ═══════════════════════════════════════════════════════════════
st.markdown("""
<style>
    .stButton > button { font-weight: 600; }
    .live-console {
        background: #1e1e1e; color: #d4d4d4; padding: 16px;
        border-radius: 8px; font-family: 'Consolas', 'Courier New', monospace;
        font-size: 0.85rem; line-height: 1.5; max-height: 480px;
        overflow-y: auto; white-space: pre-wrap; word-break: break-all;
    }
</style>
""", unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════════
# Session State 初始化
# ═══════════════════════════════════════════════════════════════
DEFAULTS = {
    "deep_result": None,
    "deep_symbol": "",
    "screen_results": [],
    "screen_scope": "hs300",
    "backtest_result": None,
    "page": "深度分析",
    "portfolio_data": None,
    # Live analysis state
    "_analysis_running": False,
    "_analysis_output": [],
    "_analysis_result": None,
    "_analysis_error": None,
    "_analysis_symbol": "",
    "_analysis_use_mock": True,
    "_analysis_use_portfolio": False,
    "_analysis_last_poll": 0.0,
}
for k, v in DEFAULTS.items():
    if k not in st.session_state:
        st.session_state[k] = v


# ═══════════════════════════════════════════════════════════════
# 实时输出捕获器
# ═══════════════════════════════════════════════════════════════

class _LiveCapture:
    """线程安全的 stdout 捕获器，写入共享列表供 Streamlit 轮询读取."""

    def __init__(self, output_list: list, lock: threading.Lock):
        self._output = output_list
        self._lock = lock
        self._terminal = getattr(sys, '__stdout__', sys.stdout)

    def write(self, s: str):
        if s and s.strip():
            with self._lock:
                self._output.append(s)
        try:
            self._terminal.write(s)
        except Exception:
            pass

    def flush(self):
        try:
            self._terminal.flush()
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════════

def action_badge(action: str):
    action = str(action).upper()
    if action in ("BUY", "STRONG_BUY", "CAUTIOUS_BUY"):
        return ("BUY", "#e74c3c")
    elif action in ("SELL", "STRONG_SELL", "CAUTIOUS_SELL"):
        return ("SELL", "#27ae60")
    else:
        return ("HOLD", "#7f8c8d")


def _sanitize(text: str, max_len: int = 200) -> str:
    """移除 HTML 标签，返回纯文本."""
    import re
    text = str(text)
    text = re.sub(r"<[^>]+>", "", text)
    return text[:max_len] if max_len else text


def decision_card(label, dim):
    if not dim or not isinstance(dim, dict):
        st.info(f"{label}: 暂无数据")
        return

    act_text, color = action_badge(dim.get("action", "HOLD"))
    entry = dim.get("entry_price", "N/A")
    stop = dim.get("stop_loss_price", "N/A")
    take = dim.get("take_profit_price", "N/A")
    pos = dim.get("position_pct", 0)
    conf = dim.get("confidence", 0)
    rationale = dim.get("rationale", "")
    expected = dim.get("expected_return_pct", "")
    pm = dim.get("potential_multiplier", "")
    exit_strategy = dim.get("exit_strategy", {})

    conf_pct = 0
    try:
        conf_pct = int(float(conf) * 100)
    except (ValueError, TypeError):
        pass

    # 标题行 (act_text 来自 action_badge，仅含 BUY/SELL/HOLD，安全)
    st.markdown(
        f'<span style="font-weight:700;">{label}</span> · '
        f'<b style="color:{color};font-size:1.1em;">{act_text}</b>',
        unsafe_allow_html=True,
    )

    # 指标行
    r1 = st.columns(3)
    r1[0].metric("入场价", str(entry))
    r1[1].metric("止损价", str(stop))
    r1[2].metric("止盈价", str(take))

    r2_cols = [st.columns(3), st.columns(3)] if expected else [st.columns(3)]
    r2_cols[0][0].metric("仓位", f"{pos}%")
    r2_cols[0][1].metric("置信度", f"{conf_pct}%")
    if expected:
        r2_cols[0][2].metric("预期收益", f"{expected}%")

    # 附加信息（纯文本，无 HTML）
    if pm:
        st.caption(f"潜力: {_sanitize(pm, 100)}")
    if rationale:
        st.caption(_sanitize(rationale, 200))
    if exit_strategy:
        etype = _sanitize(exit_strategy.get("type", "N/A"), 50)
        rules = "; ".join(_sanitize(r, 80) for r in exit_strategy.get("rules", [])[:3])
        st.caption(f"退出: {etype} | {rules}")

    st.divider()


# ═══════════════════════════════════════════════════════════════
# 后台分析线程
# ═══════════════════════════════════════════════════════════════

def _run_analysis_thread(symbol, use_mock, use_portfolio, shared_state):
    """在后台线程中运行 run_full_analysis，输出写入共享字典（不访问 st.session_state）."""
    capture_lock = threading.Lock()
    output_lines = shared_state["output"]
    output_lines.clear()

    capture = _LiveCapture(output_lines, capture_lock)
    old_stdout = sys.stdout
    sys.stdout = capture

    try:
        from agents.decision import run_full_analysis

        result = run_full_analysis(
            symbol,
            use_mock=use_mock,
            use_portfolio=use_portfolio,
            use_adapted_params=True,
        )
        shared_state["result"] = result
        shared_state["error"] = None

        # 更新新闻源状态
        data = result.get("compressed_data", {})
        news = data.get("news", []) if isinstance(data, dict) else []
        shared_state["news_status"] = f"{len(news)}条可用" if news else "无数据"
    except Exception as e:
        shared_state["result"] = None
        shared_state["error"] = f"{type(e).__name__}: {e}"
        shared_state["news_status"] = "未获取"
    finally:
        sys.stdout = old_stdout
        shared_state["running"] = False


# ═══════════════════════════════════════════════════════════════
# 侧边栏导航
# ═══════════════════════════════════════════════════════════════

with st.sidebar:
    st.title("StockMind")
    st.caption("多Agent智能股析系统")

    pages = ["日常顾问", "深度分析", "智能选股", "回测实验室", "持仓管理", "策略基因库"]
    icons = ["📋", "🔬", "🔍", "⚗️", "💼", "🧬"]

    for i, (page, icon) in enumerate(zip(pages, icons)):
        if st.sidebar.button(
            f"{icon}  {page}",
            key=f"nav_{page}",
            use_container_width=True,
            type="primary" if st.session_state.page == page else "secondary",
        ):
            st.session_state.page = page
            st.rerun()

    st.divider()
    st.caption("⚙️ 系统状态")

    # ── API 连通性检测 ──
    api_status_key = "_api_status"
    if api_status_key not in st.session_state:
        try:
            from data.deepseek import DeepSeekClient
            c = DeepSeekClient()
            if c.api_key:
                r = c.chat("回复OK", "OK", max_tokens=10)
                st.session_state[api_status_key] = "ok" if "OK" in r else "slow"
            else:
                st.session_state[api_status_key] = "no_key"
        except Exception:
            st.session_state[api_status_key] = "down"

    api_icon = {"ok": "🟢", "slow": "🟡", "down": "🔴", "no_key": "⚪"}
    api_label = {"ok": "API 正常", "slow": "API 慢", "down": "API 断连", "no_key": "未配置Key"}
    st.caption(f"{api_icon.get(st.session_state[api_status_key], '⚪')} {api_label.get(st.session_state[api_status_key], '?')}")

    # ── 运行模式 ──
    import os as _os
    allow_mock = _os.environ.get("ALLOW_MOCK", "false").lower() in ("true", "1", "yes")
    mode_icon = "🟡" if allow_mock else "🟢"
    mode_label = "Mock允许" if allow_mock else "仅实调"
    st.caption(f"{mode_icon} {mode_label}")

    # ── 新闻源状态 ──
    news_key = "_news_status"
    if news_key not in st.session_state:
        st.session_state[news_key] = "未知"
    st.caption(f"📰 新闻: {st.session_state[news_key]}")

    st.divider()
    st.caption(f"{datetime.now().strftime('%Y-%m-%d %H:%M')}")

# ═══════════════════════════════════════════════════════════════
current_page = st.session_state.page

# ═══════════════════════════════════════════════════════════════
# 第 0 页: 日常顾问 (持仓诊断 + 全市场选股)
# ═══════════════════════════════════════════════════════════════
if current_page == "日常顾问":
    st.title("📋 日常投资顾问")
    st.caption("每日标准工作流：持仓诊断 → 全市场选股")

    advisor_key = "_advisor_result"

    col_a, col_b, col_c = st.columns([2, 1, 1])
    with col_a:
        scope = st.selectbox("选股范围", ["hs300", "zz500"], key="advisor_scope")
    with col_b:
        use_mock = st.checkbox("Mock快速", value=True, key="advisor_mock",
                               help="勾选=本地快速模式，不调用LLM API")
    with col_c:
        run_btn = st.button("🚀 开始顾问分析", type="primary", use_container_width=True)

    if run_btn:
        with st.spinner("正在运行日常顾问工作流..."):
            from daily_advisor import run_daily_advisory
            placeholder = st.empty()
            try:
                result = run_daily_advisory(use_mock=use_mock, scope=scope, top_n=10)
                st.session_state[advisor_key] = result
                placeholder.success("顾问分析完成")
            except Exception as e:
                placeholder.error(f"顾问分析失败: {e}")
                st.session_state[advisor_key] = None

    advisor_data = st.session_state.get(advisor_key)
    if advisor_data:
        st.divider()
        st.subheader(f"📊 {advisor_data.get('date', '')} 顾问报告")
        st.caption(f"现金余额: ¥{advisor_data.get('total_cash', 0):,.0f}")

        # ── 持仓诊断区 ──
        holdings = advisor_data.get("holdings_report", [])
        st.subheader("🔍 持仓诊断")
        if holdings:
            rows = []
            for r in holdings:
                if r.get("error"):
                    rows.append([r["symbol"], r["name"], "ERR", "", "", "", "", r["error"][:30]])
                    continue
                advice_map = {"ADD": "加仓", "TRIM": "减仓", "CLOSE": "清仓",
                              "HOLD": "持有", "BUY": "买入", "SELL": "卖出"}
                actions = [a["action"] for a in r.get("holding_advice", [])]
                if "CLOSE" in actions: advice = "⚠️ 清仓"
                elif "TRIM" in actions: advice = "📉 减仓"
                elif "ADD" in actions: advice = "📈 加仓"
                else: advice = "➖ 持有"

                pnl = r.get("pnl_pct", 0)
                pnl_str = f"🔴 {pnl:+.1f}%" if pnl < 0 else f"🟢 {pnl:+.1f}%"
                rows.append([
                    r["symbol"], r["name"],
                    f"{r['current_price']:.2f}", pnl_str,
                    r["short_signal"], r["mid_signal"], r["long_signal"],
                    advice,
                ])

            import pandas as _pd
            df = _pd.DataFrame(rows, columns=["代码", "名称", "现价", "盈亏", "短线", "中线", "长线", "建议"])
            st.dataframe(df, use_container_width=True, hide_index=True)

            # 详细建议
            for r in holdings:
                if r.get("error"):
                    continue
                with st.expander(f"{r['symbol']} {r['name']} — 盈亏{r.get('pnl_pct', 0):+.1f}%"):
                    for a in r.get("holding_advice", []):
                        reasons = "；".join(a.get("reasons", [])[:3])
                        st.caption(f"[{a['label']}] {a['action']} | {reasons}")
        else:
            st.info("暂无持仓记录。请在 portfolio/positions.json 中添加持仓。")

        # ── 选股结果区 ──
        screening = advisor_data.get("screening_report", [])
        st.subheader("🎯 潜力标的 Top 5")
        if screening:
            srows = []
            for s in screening:
                change = s.get("change_pct", 0) or 0
                chg_str = f"🔴 {change:+.1f}%" if change < 0 else f"🟢 {change:+.1f}%"
                signal = s["mid_signal"]
                sig_icon = "🟢" if signal == "BUY" else "🟡" if "CAUTIOUS" in str(signal) else "⚪"
                srows.append([
                    s["symbol"], s["name"],
                    f"{s['current_price']:.2f}", chg_str,
                    f"{s.get('screener_score', 0):.0f}",
                    f"{sig_icon} {signal}",
                    f"{s['mid_score']:.0f}",
                    f"{s['entry_price']:.2f}",
                    f"{s.get('expected_return_pct', 0):.0f}%",
                ])
            sdf = _pd.DataFrame(srows, columns=[
                "代码", "名称", "现价", "涨跌", "筛选分", "中线信号", "因子分", "入场价", "预期收益%"
            ])
            st.dataframe(sdf, use_container_width=True, hide_index=True)

            for s in screening:
                with st.expander(f"{s['symbol']} {s['name']} — {s['mid_signal']} ({s['mid_score']:.0f}分)"):
                    st.metric("入场参考价", f"{s['entry_price']:.2f}")
                    st.metric("预期收益空间", f"{s.get('expected_return_pct', 0):.0f}%")
                    if s.get("rationale"):
                        st.caption(s["rationale"][:150])
        else:
            st.info("选股无结果，请检查数据源。")

        # 汇总
        buy_count = sum(1 for s in screening if s.get("mid_signal") == "BUY")
        st.info(f"总结: {len(holdings)}只持仓已诊断 | {buy_count}只中线BUY信号 / {len(screening)}只候选")

        if st.button("🔄 刷新分析", use_container_width=True):
            st.session_state[advisor_key] = None
            st.rerun()

# ═══════════════════════════════════════════════════════════════
# 第 1 页: 深度分析 (带实时流式输出)
# ═══════════════════════════════════════════════════════════════
if current_page == "深度分析":
    st.title("🔬 深度分析")
    st.caption("融合多Agent + 三线时间维度 + 多空辩论，生成综合交易决策")

    # ── 正在分析中 → 从共享状态同步并显示实时输出 ──
    if st.session_state["_analysis_running"]:
        shared = st.session_state.get("_analysis_shared")
        if shared is None:
            st.session_state["_analysis_running"] = False
            st.rerun()

        # 线程结束 → 同步结果到 session_state
        if not shared["running"]:
            st.session_state["_analysis_output"] = shared["output"]
            st.session_state["_analysis_result"] = shared["result"]
            st.session_state["_analysis_error"] = shared["error"]
            if shared["news_status"]:
                st.session_state["_news_status"] = shared["news_status"]
            st.session_state["_analysis_running"] = False
            st.rerun()

        symbol = st.session_state["_analysis_symbol"]
        st.info(f"正在分析 **{symbol}** ... 请等待各阶段完成")

        # 阶段进度条（从共享列表读取，线程安全）
        output_text = "".join(shared["output"])
        stage_map = {
            "STAGE:1/5": ("数据获取", 0.1),
            "STAGE:2/5": ("多Agent分析", 0.35),
            "STAGE:3/5": ("三线评估", 0.55),
            "STAGE:4/5": ("因子引擎决策", 0.8),
            "STAGE:5/5": ("LLM复核", 0.95),
        }
        current_stage = 0
        current_label = "准备中..."
        for marker, (label, progress) in stage_map.items():
            if marker in output_text:
                current_stage = progress
                current_label = label

        st.progress(current_stage, text=current_label)

        # 实时控制台 (escape HTML entities to prevent tag rendering)
        console_placeholder = st.empty()
        with console_placeholder.container():
            import html as _html_mod
            safe_text = _html_mod.escape(output_text[-6000:]) or "等待输出..."
            st.markdown(
                f'<div class="live-console">{safe_text}</div>',
                unsafe_allow_html=True,
            )
            st.caption(f"已输出 {len(shared['output'])} 行")

        # 超时检测
        now = time.time()
        last = st.session_state.get("_analysis_last_poll", now)
        if now - last > 30 and len(shared["output"]) == 0:
            st.warning("等待数据拉取中，若持续无输出请检查网络...")

        st.session_state["_analysis_last_poll"] = now

        # 每 0.4 秒刷新
        time.sleep(0.4)
        st.rerun()

    # ── 分析完成，显示结果 ──
    elif st.session_state.get("_analysis_result") is not None or st.session_state.get("_analysis_error") is not None:
        error_msg = st.session_state["_analysis_error"]
        result = st.session_state["_analysis_result"]

        # 显示完整日志
        output_lines = st.session_state.get("_analysis_output", [])
        if output_lines:
            with st.expander("📋 分析日志", expanded=len(output_lines) < 20):
                st.code("".join(output_lines), language=None)

        if error_msg:
            st.error(f"分析过程出错: {error_msg}")

        if result and isinstance(result, dict):
            decision = result.get("final_decision", result)
            st.divider()
            st.subheader("🎯 三维交易决策")

            c1, c2, c3 = st.columns(3)
            with c1:
                decision_card("短线 (1-5日)", decision.get("short_term"))
            with c2:
                decision_card("中线 (1-4周)", decision.get("mid_term"))
            with c3:
                decision_card("长线 (1-6月)", decision.get("long_term"))

            engine = result.get("_decision_engine", "unknown")
            engine_label = "因子模型统计引擎" if engine == "factor_model" else "LLM综合管线"
            st.caption(f"决策引擎: {engine_label} | 纯统计决策，无LLM参与")

            # ── 因子评分明细 ──
            with st.expander("📊 因子评分明细", expanded=False):
                f_tabs = st.tabs(["短线", "中线", "长线"])
                for idx, tf_key in enumerate(["short_term", "mid_term", "long_term"]):
                    with f_tabs[idx]:
                        tf_data = decision.get(tf_key, {})
                        contributions = tf_data.get("contributions", {})
                        score = tf_data.get("_factor_score", tf_data.get("score", 0))
                        threshold = tf_data.get("threshold", "?")
                        signal = tf_data.get("action", tf_data.get("signal", "?"))

                        st.metric(
                            f"综合评分: {score:.0f}/100 (阈值: {threshold})",
                            f"→ {signal}",
                            delta=f"{score - float(threshold) if isinstance(threshold, (int, float)) else 0:+.0f} vs 阈值" if isinstance(threshold, (int, float)) else None,
                        )

                        if contributions:
                            contrib_df = []
                            for fname, val in sorted(contributions.items(), key=lambda x: abs(x[1]), reverse=True):
                                impact = "📈" if val > 0 else "📉" if val < 0 else "➖"
                                contrib_df.append({
                                    "因子": fname,
                                    "影响": impact,
                                    "贡献值": f"{val:+.1f}",
                                })
                            import pandas as pd
                            st.dataframe(
                                pd.DataFrame(contrib_df),
                                use_container_width=True,
                                hide_index=True,
                                height=min(len(contrib_df) * 36 + 38, 300),
                            )
                        else:
                            st.caption("因子贡献数据未生成 (使用旧版LLM管线)")

            verdict = decision.get("overall_verdict", "")
            if verdict:
                st.info(f"综合裁决: {verdict}")

            if result.get("portfolio_summary"):
                with st.expander("💼 持仓上下文", expanded=False):
                    ps = result["portfolio_summary"]
                    c1, c2, c3 = st.columns(3)
                    c1.metric("总资产", f"{ps.get('total_assets', 0):,.0f}")
                    c2.metric("现金", f"{ps.get('cash', 0):,.0f}")
                    c3.metric("总市值", f"{ps.get('market_value', 0):,.0f}")

            if result.get("exit_advice"):
                with st.expander("🚪 持仓退出建议", expanded=False):
                    ea = result["exit_advice"]
                    st.metric("退出动作", ea.get("action", "N/A"))
                    st.metric("卖出比例", f"{ea.get('sell_ratio', 0)}%")
                    st.metric("盈亏", f"{ea.get('profit_pct', 0):+.2f}%")
                    for reason in ea.get("reasons", []):
                        st.caption(f"• {reason}")

            st.session_state.deep_result = result
            st.session_state.deep_symbol = st.session_state.get("_analysis_symbol", "")

        # 清除分析状态以便下次分析
        if st.button("🔄 开始新分析", use_container_width=True):
            st.session_state["_analysis_result"] = None
            st.session_state["_analysis_error"] = None
            st.session_state["_analysis_output"] = []
            st.rerun()

    # ── 显示上次结果 ──
    elif st.session_state.deep_result:
        st.divider()
        st.info(f"上次分析: {st.session_state.deep_symbol}")
        decision = st.session_state.deep_result.get("final_decision", {})
        if decision:
            c1, c2, c3 = st.columns(3)
            with c1:
                decision_card("短线", decision.get("short_term"))
            with c2:
                decision_card("中线", decision.get("mid_term"))
            with c3:
                decision_card("长线", decision.get("long_term"))

    # ── 输入区 (非分析中时显示) ──
    if not st.session_state["_analysis_running"] and st.session_state.get("_analysis_result") is None:
        col1, col2, col3 = st.columns([3, 1, 1])
        with col1:
            symbol = st.text_input(
                "股票代码",
                value=st.session_state.deep_symbol or "000001",
                max_chars=6,
                key="deep_symbol_input",
            )
        with col2:
            use_mock = st.checkbox("Mock模式", value=True, help="不调用LLM API，快速本地分析")
        with col3:
            use_portfolio = st.checkbox(
                "含持仓上下文", value=False, help="加载 portfolio.json 持仓信息"
            )

        col_a, col_b = st.columns([1, 3])
        with col_a:
            run_btn = st.button("🚀 开始深度分析", type="primary", use_container_width=True)

        if run_btn and symbol.strip():
            st.session_state["_analysis_running"] = True
            st.session_state["_analysis_output"] = []
            st.session_state["_analysis_result"] = None
            st.session_state["_analysis_error"] = None
            st.session_state["_analysis_symbol"] = symbol.strip()
            st.session_state["_analysis_use_mock"] = use_mock
            st.session_state["_analysis_use_portfolio"] = use_portfolio
            st.session_state["_analysis_last_poll"] = time.time()
            st.session_state.deep_symbol = symbol.strip()

            # 共享状态 — 线程写入此字典，主线程轮询同步到 session_state
            shared = {
                "output": [],
                "result": None,
                "error": None,
                "news_status": None,
                "running": True,
            }
            st.session_state["_analysis_shared"] = shared

            thread = threading.Thread(
                target=_run_analysis_thread,
                args=(symbol.strip(), use_mock, use_portfolio, shared),
                daemon=True,
            )
            thread.start()
            st.rerun()


# ═══════════════════════════════════════════════════════════════
# 第 2 页: 智能选股
# ═══════════════════════════════════════════════════════════════
elif current_page == "智能选股":
    st.title("🔍 智能选股")
    st.caption("扫描指数成分股，技术面+基本面+财务安全+流动性+趋势综合打分排序")

    col1, col2, col3 = st.columns([2, 1, 1])
    with col1:
        scope = st.selectbox(
            "选择指数",
            options=["hs300", "zz500"],
            format_func=lambda x: {"hs300": "沪深300", "zz500": "中证500"}.get(x, x),
            key="screen_scope_select",
        )
    with col2:
        top_n = st.slider("返回数量", min_value=5, max_value=30, value=10, step=5)
    with col3:
        use_mock_sc = st.checkbox("纯本地模式", value=True, help="不调用LLM增强")

    if st.button("🔍 开始选股", type="primary"):
        st.divider()
        progress_bar = st.progress(0, text="准备中...")
        log_placeholder = st.empty()

        from analysis.screener import screen_stocks

        output_lines = []
        capture_lock = threading.Lock()
        capture = _LiveCapture(output_lines, capture_lock)
        old_stdout = sys.stdout
        try:
            sys.stdout = capture
            results = screen_stocks(scope=scope, top_n=top_n, use_mock=use_mock_sc)
        except Exception as e:
            progress_bar.progress(1.0, text="选股失败")
            st.error(f"选股过程出错: {e}")
            st.code(traceback.format_exc())
            st.stop()
        finally:
            sys.stdout = old_stdout

        progress_bar.progress(1.0, text="选股完成")
        log_placeholder.code("".join(output_lines), language=None)

        if not results:
            st.warning("未筛选出符合条件的股票")
            st.stop()

        st.session_state.screen_results = results
        st.session_state.screen_scope = scope
        st.success(f"共筛选出 {len(results)} 只股票")

        import pandas as pd
        rows = []
        for i, stock in enumerate(results):
            quote = stock.get("quote", {})
            technical = stock.get("technical", {})
            rows.append({
                "排名": i + 1,
                "代码": stock.get("symbol", "?"),
                "名称": stock.get("name", ""),
                "评分": stock.get("score", 0),
                "现价": quote.get("price") or technical.get("close", "N/A"),
                "涨跌%": quote.get("change_pct", 0),
                "PE": quote.get("pe", "N/A"),
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

        st.divider()
        st.caption("点击跳转到深度分析")
        cols = st.columns(min(len(results), 6))
        for i, stock in enumerate(results[:12]):
            sym = stock.get("symbol", "?")
            name = stock.get("name", sym)
            score = stock.get("score", 0)
            with cols[i % 6]:
                if st.button(f"{sym} {name} ⭐{score}", key=f"goto_{sym}", use_container_width=True):
                    st.session_state.deep_symbol = sym
                    st.session_state.page = "深度分析"
                    st.rerun()

    elif st.session_state.screen_results:
        st.divider()
        st.info(f"上次选股: {st.session_state.screen_scope}, {len(st.session_state.screen_results)} 只")
        import pandas as pd
        rows = [{
            "排名": i + 1,
            "代码": s.get("symbol", "?"),
            "名称": s.get("name", ""),
            "评分": s.get("score", 0),
        } for i, s in enumerate(st.session_state.screen_results)]
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


# ═══════════════════════════════════════════════════════════════
# 第 3 页: 回测实验室
# ═══════════════════════════════════════════════════════════════
elif current_page == "回测实验室":
    st.title("⚗️ 回测实验室")
    st.caption("回测 + Critic联动进化引擎: 回测 -> 评审 -> 修改 -> 再回测")

    col1, col2, col3 = st.columns(3)
    with col1:
        bt_symbol = st.text_input("股票代码", value="600744", max_chars=6, key="bt_symbol")
    with col2:
        bt_timeframe = st.selectbox(
            "时间维度",
            options=["short", "mid", "long"],
            format_func=lambda x: {"short": "短线", "mid": "中线", "long": "长线"}.get(x, x),
        )
    with col3:
        bt_days = st.selectbox("回测天数", options=[60, 90, 120, 180, 250], index=2)

    col_a, col_b, col_c = st.columns(3)
    with col_a:
        bt_rounds = st.slider("最大进化轮数", min_value=1, max_value=5, value=2)
    with col_b:
        bt_capital = st.number_input("初始资金", min_value=10000.0, max_value=1000000.0, value=100000.0, step=10000.0)
    with col_c:
        bt_mock = st.checkbox("Mock模式", value=True)

    if st.button("⚗️ 开始回测进化", type="primary"):
        st.divider()
        round_placeholders = [st.empty() for _ in range(int(bt_rounds))]

        from backtest.runner import run_backtest_with_critic

        output_lines = []
        capture_lock = threading.Lock()
        capture = _LiveCapture(output_lines, capture_lock)
        old_stdout = sys.stdout
        try:
            sys.stdout = capture
            result = run_backtest_with_critic(
                symbol=bt_symbol.strip(),
                time_frame=bt_timeframe,
                days=int(bt_days),
                max_rounds=int(bt_rounds),
                initial_capital=float(bt_capital),
                use_mock=bt_mock,
            )
        except Exception as e:
            st.error(f"回测过程出错: {e}")
            st.code(traceback.format_exc())
            st.stop()
        finally:
            sys.stdout = old_stdout

        st.session_state.backtest_result = result

        with st.expander("📋 回测日志", expanded=False):
            st.code("".join(output_lines[-8000:]), language=None)

        rounds = result.get("rounds", [])
        for i, rd in enumerate(rounds):
            if i < len(round_placeholders):
                m = rd.get("backtest_metrics", {})
                score = rd.get("critic_score", 0)
                with round_placeholders[i].container():
                    st.subheader(f"第 {rd.get('round', i+1)} 轮")
                    c1, c2, c3, c4 = st.columns(4)
                    c1.metric("总收益", f"{m.get('total_return_pct', 0):+.2f}%")
                    c2.metric("夏普比率", f"{m.get('sharpe_ratio', 0):.2f}")
                    c3.metric("胜率", f"{m.get('win_rate_pct', 0):.1f}%")
                    c4.metric("Critic评分", f"{score:.1f}/100")
                    dd = m.get("max_drawdown_pct", 0)
                    ach_s = m.get("achievement_short", 0)
                    ach_m = m.get("achievement_mid", 0)
                    ach_l = m.get("achievement_long", 0)
                    st.caption(f"最大回撤: {dd:.2f}% | 三线达成率: 短{ach_s:.0f}% 中{ach_m:.0f}% 长{ach_l:.0f}%")
                    must_fix = rd.get("must_fix", [])
                    if must_fix:
                        with st.expander(f"Critic优化建议 ({len(must_fix)}条)", expanded=False):
                            for fix in must_fix:
                                st.caption(f"• {fix}")

        st.divider()
        final_score = result.get("final_score", 0)
        improvement = result.get("improvement", 0)
        st.metric("最终评分", f"{final_score:.1f}/100",
                  delta=f"{improvement:+.1f}" if improvement else None)

    elif st.session_state.backtest_result:
        st.divider()
        st.info("上次回测结果")
        for rd in st.session_state.backtest_result.get("rounds", []):
            m = rd.get("backtest_metrics", {})
            st.caption(
                f"第{rd.get('round','?')}轮: "
                f"收益{m.get('total_return_pct',0):+.2f}% | "
                f"夏普{m.get('sharpe_ratio',0):.2f} | "
                f"Critic {rd.get('critic_score',0):.0f}分"
            )


# ═══════════════════════════════════════════════════════════════
# 第 4 页: 持仓管理
# ═══════════════════════════════════════════════════════════════
elif current_page == "持仓管理":
    st.title("💼 持仓管理")
    st.caption("实时查看和管理您的投资组合")

    refresh_col, _ = st.columns([1, 3])
    with refresh_col:
        do_refresh = st.button("🔄 刷新数据", use_container_width=True)

    try:
        from portfolio.manager import load_portfolio, get_portfolio_summary, add_position, remove_position

        pf = load_portfolio(refresh=do_refresh)
        summary = get_portfolio_summary()
        st.session_state.portfolio_data = summary

        st.divider()
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            st.metric("总资产", f"{summary.get('total_assets', 0):,.2f}")
        with c2:
            st.metric("现金", f"{summary.get('cash', 0):,.2f}")
        with c3:
            st.metric("持仓市值", f"{summary.get('market_value', 0):,.2f}")
        with c4:
            total_cost = sum(
                p.get("entry_price", 0) * p.get("quantity", 0)
                for p in summary.get("positions", [])
            )
            total_mv = summary.get("market_value", 0)
            total_pnl = total_mv - total_cost if total_cost > 0 else 0
            pnl_pct = (total_pnl / total_cost * 100) if total_cost > 0 else 0
            st.metric("浮动盈亏", f"{total_pnl:+,.2f}", delta=f"{pnl_pct:+.2f}%")

        st.divider()
        st.subheader("📋 持仓明细")

        positions = summary.get("positions", [])
        if not positions:
            st.info("当前无持仓记录")
        else:
            for pos in positions:
                sym = pos.get("symbol", "")
                name = pos.get("name", sym)
                entry = pos.get("entry_price", 0)
                qty = pos.get("quantity", 0)
                current = pos.get("current_price", entry)
                mv = pos.get("market_value", entry * qty)
                pnl = pos.get("unrealized_pnl", 0)
                cost = entry * qty
                pnl_pct_pos = (pnl / cost * 100) if cost > 0 else 0
                with st.container():
                    c1, c2, c3, c4, c5 = st.columns([2, 1, 1, 1, 1])
                    c1.subheader(f"{sym} {name}")
                    c2.metric("成本价", f"{entry:.4f}")
                    c3.metric("现价", f"{current:.4f}")
                    c4.metric("数量", f"{qty}股")
                    c5.metric("盈亏", f"{pnl:+,.2f}", delta=f"{pnl_pct_pos:+.2f}%")
                st.divider()

        st.divider()
        st.subheader("➕ 新增持仓")
        with st.form("add_position_form", clear_on_submit=True):
            ca, cb, cc, cd = st.columns(4)
            with ca:
                new_sym = st.text_input("股票代码", max_chars=6, key="new_sym")
            with cb:
                new_name = st.text_input("名称(可选)", key="new_name")
            with cc:
                new_price = st.number_input("入场价", min_value=0.01, value=10.0, step=0.01, key="new_price")
            with cd:
                new_qty = st.number_input("数量(股)", min_value=100, value=100, step=100, key="new_qty")
            if st.form_submit_button("确认添加", type="primary"):
                if new_sym.strip():
                    try:
                        add_position(new_sym.strip(), float(new_price), int(new_qty), name=new_name.strip())
                        st.success(f"已添加 {new_sym} x {new_qty}股 @ {new_price}")
                        st.rerun()
                    except ValueError as ve:
                        st.error(f"添加失败: {ve}")
                    except Exception as e:
                        st.error(f"添加失败: {e}")
                else:
                    st.warning("请输入股票代码")

        st.divider()
        st.subheader("➖ 卖出持仓")
        if positions:
            with st.form("sell_position_form", clear_on_submit=True):
                sell_sym = st.selectbox(
                    "选择持仓",
                    options=[f"{p['symbol']} {p.get('name','')} ({p['quantity']}股)" for p in positions],
                )
                sell_price = st.number_input("卖出价", min_value=0.01, value=10.0, step=0.01)
                sell_qty = st.number_input("卖出数量(股)", min_value=100, value=100, step=100)
                if st.form_submit_button("确认卖出", type="primary"):
                    try:
                        sym_only = sell_sym.split()[0]
                        result = remove_position(sym_only, float(sell_price), int(sell_qty))
                        st.success(
                            f"已卖出 {sym_only} x {sell_qty}股 @ {sell_price}"
                            f" | 实现盈亏: {result['realized_pnl']:+,.2f}"
                        )
                        st.rerun()
                    except ValueError as ve:
                        st.error(f"卖出失败: {ve}")
                    except Exception as e:
                        st.error(f"卖出失败: {e}")
                        st.code(traceback.format_exc())

    except Exception as e:
        st.error(f"加载持仓数据失败: {e}")
        st.code(traceback.format_exc())


# ═══════════════════════════════════════════════════════════════
# 第 5 页: 策略基因库
# ═══════════════════════════════════════════════════════════════
elif current_page == "策略基因库":
    st.title("🧬 策略基因库")
    st.caption("Alpha因子权重、自适应参数、进化历史")

    tab1, tab2, tab3 = st.tabs(["因子权重", "自适应参数", "进化历史"])

    with tab1:
        st.subheader("Alpha 因子权重配置")
        try:
            factor_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "factor_weights.json")
            if os.path.exists(factor_path):
                with open(factor_path, "r", encoding="utf-8") as f:
                    fw = json.load(f)
                for tf_key, tf_label in [("short", "短线"), ("mid", "中线"), ("long", "长线")]:
                    data = fw.get(tf_key, {})
                    if not data:
                        continue
                    with st.expander(f"{tf_label} 因子组", expanded=(tf_key == "mid")):
                        threshold = data.get("threshold", "N/A")
                        st.caption(f"信号阈值: {threshold}")
                        weights = data.get("weights", data)
                        if isinstance(weights, dict):
                            cols = st.columns(3)
                            for i, (k, v) in enumerate(weights.items()):
                                if k in ("threshold",):
                                    continue
                                with cols[i % 3]:
                                    st.metric(k, f"{float(v):.2f}" if isinstance(v, (int, float)) else str(v))
            else:
                st.info("未找到 factor_weights.json")
        except Exception as e:
            st.error(f"读取因子权重失败: {e}")

    with tab2:
        st.subheader("股票自适应参数")
        try:
            project_dir = os.path.dirname(os.path.abspath(__file__))
            adapted_files = [f for f in os.listdir(project_dir) if f.endswith("_adapted_params.json")]
            if not adapted_files:
                st.info("暂无自适应参数文件，运行深度分析后自动生成。")
            else:
                for af in sorted(adapted_files):
                    sym = af.replace("_adapted_params.json", "")
                    with st.expander(f"{sym}", expanded=False):
                        try:
                            with open(os.path.join(project_dir, af), "r", encoding="utf-8") as f:
                                params = json.load(f)
                            params_data = params.get("params", params)
                            for tf_key, tf_label in [("short_term", "短线"), ("mid_term", "中线"), ("long_term", "长线")]:
                                tf_data = params_data.get(tf_key, {})
                                if tf_data:
                                    st.caption(
                                        f"**{tf_label}** — 仓位: {tf_data.get('position_pct','?')}% | "
                                        f"置信度阈值: {tf_data.get('confidence_threshold','?')} | "
                                        f"止盈: {tf_data.get('take_profit_pct','?')}% | "
                                        f"止损: {tf_data.get('stop_loss_pct','?')}%"
                                    )
                            rationale = params.get("adaptation_rationale", "")
                            if rationale:
                                st.caption(f"适配理由: {rationale[:200]}")
                        except Exception as e:
                            st.caption(f"读取失败: {e}")
        except Exception as e:
            st.error(f"读取自适应参数失败: {e}")

    with tab3:
        st.subheader("进化迭代历史")
        try:
            evo_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "evolution_state.json")
            if os.path.exists(evo_path):
                with open(evo_path, "r", encoding="utf-8") as f:
                    evo = json.load(f)
                st.json(evo)
            for log_name in ["evolution_log.txt", "backtest_evolution_log.txt"]:
                log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), log_name)
                if os.path.exists(log_path):
                    with open(log_path, "r", encoding="utf-8") as f:
                        content = f.read()
                    if content.strip():
                        st.caption(f"**{log_name}**")
                        st.code(content[-5000:], language=None)
        except Exception as e:
            st.error(f"读取进化历史失败: {e}")

# ═══════════════════════════════════════════════════════════════
st.divider()
st.caption("StockMind · 多Agent智能股析系统 | 数据来源: 腾讯行情 + akshare | 分析引擎: DeepSeek V4 Pro")
