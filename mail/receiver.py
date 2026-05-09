#!/usr/bin/env python3
"""
QQ邮箱 IMAP 指令接收模块。

通过 IMAP 检查未读邮件，直接读取 [StockMind] 标题邮件的正文内容作为指令，
执行后将结果通过邮件回复，最后标记邮件为已读。

正文即指令，无需 CMD: 前缀。系统自动识别意图并执行。

用法:
    py mail_receiver.py                    # 单次检查
    py mail_receiver.py --listen            # 持续监听(每60秒)
"""
import imaplib
import email
import re
import os
import sys
import json
import time
import traceback
from email.header import decode_header
from email.utils import parseaddr
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.config import get_config_value

LOG_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mail_command_log.txt")

# ── 关键词路由表 ──
# (关键词列表, 意图, 是否需要股票代码)
INTENT_KEYWORDS = [
    (["回测", "backtest"], "backtest", True),
    (["分析", "analyze", "诊断", "怎么看"], "analyze", True),
    (["选股", "screen", "筛选", "扫描", "推荐"], "screen", False),
    (["持仓", "portfolio", "仓位", "账户"], "portfolio", False),
    (["状态", "status", "运行", "概况"], "status", False),
]

# 股票代码正则 (A股6位数字)
RE_SYMBOL = re.compile(r'\b(\d{6})\b')
# 回测天数正则
RE_DAYS = re.compile(r'(\d+)\s*(天|日|days?)')
# 时间维度关键词
TIMEFRAME_MAP = {
    "短线": "short", "short": "short",
    "中线": "mid", "mid": "mid",
    "长线": "long", "long": "long",
}


def _log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _get_credentials():
    addr = get_config_value("EMAIL_ADDRESS") or os.environ.get("QQ_EMAIL", "")
    pwd = get_config_value("EMAIL_PASSWORD") or os.environ.get("QQ_IMAP_PASSWORD", "")
    return addr, pwd


def _decode_header(raw) -> str:
    if raw is None:
        return ""
    parts = decode_header(str(raw))
    result = []
    for text, charset in parts:
        if isinstance(text, bytes):
            try:
                result.append(text.decode(charset or "utf-8", errors="replace"))
            except Exception:
                result.append(text.decode("utf-8", errors="replace"))
        else:
            result.append(str(text))
    return "".join(result)


def _extract_body(msg) -> str:
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            if content_type == "text/plain":
                try:
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        body += payload.decode(charset, errors="replace")
                except Exception:
                    pass
    else:
        try:
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or "utf-8"
                body = payload.decode(charset, errors="replace")
        except Exception:
            body = ""
    return body.strip()


def _interpret_body(body: str) -> dict:
    """
    解析邮件正文，识别意图并提取参数。
    返回 {"intent": str, "symbol": str, "timeframe": str, "days": int}
    """
    result = {"intent": "unknown", "symbol": "", "timeframe": "mid", "days": 180}

    # 1. 提取股票代码
    symbols = RE_SYMBOL.findall(body)
    if symbols:
        result["symbol"] = symbols[0]  # 取第一个

    # 2. 提取时间维度
    for keyword, tf in TIMEFRAME_MAP.items():
        if keyword in body.lower():
            result["timeframe"] = tf
            break

    # 3. 提取回测天数
    days_match = RE_DAYS.search(body)
    if days_match:
        result["days"] = int(days_match.group(1))

    # 4. 意图识别
    for keywords, intent, needs_symbol in INTENT_KEYWORDS:
        for kw in keywords:
            if kw in body.lower():
                result["intent"] = intent
                break
        if result["intent"] != "unknown":
            break

    # 5. 特殊处理：纯6位数字 → 默认分析
    if result["intent"] == "unknown" and result["symbol"]:
        result["intent"] = "analyze"

    # 6. 特殊处理：有股票代码+"天" → 默认回测
    if result["intent"] == "unknown" and result["symbol"] and days_match:
        result["intent"] = "backtest"

    return result


def _build_html(content: str, title: str = "StockMind 执行结果") -> str:
    escaped = content.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    html_body = escaped.replace("\n", "<br>").replace("  ", "&nbsp;&nbsp;")
    return f"""
    <div style="font-family:'Microsoft YaHei',Consolas,monospace;max-width:680px;
                background:#1f2335;color:#c0caf5;padding:24px;border-radius:12px;">
        <h2 style="color:#7aa2f7;margin:0 0 4px;">StockMind 执行报告</h2>
        <p style="color:#7982a9;margin:0 0 20px;">{title}</p>
        <hr style="border-color:#33415c;">
        <pre style="white-space:pre-wrap;font-size:13px;line-height:1.6;
                    color:#a9b1d6;background:#1a1b26;padding:16px;border-radius:8px;">
{html_body}
        </pre>
        <p style="color:#565f89;margin-top:24px;font-size:11px;">
        StockMind v2.0 邮件指令通道 · {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
    </div>
    """


# ── 执行函数 ──

def _execute_analyze(symbol: str) -> str:
    if not symbol:
        return "[Error] 未识别到股票代码。请在邮件正文中包含6位股票代码，如 000001 或 600519。"
    _log(f"执行 analyze {symbol}")
    try:
        from agents.decision import run_full_analysis
        result = run_full_analysis(symbol, use_mock=True, use_adapted_params=True)
        lines = []
        decision = result.get("decision", {})
        for dim_key, dim_label in [("short_term", "短线"), ("mid_term", "中线"), ("long_term", "长线")]:
            d = decision.get(dim_key, {})
            if d:
                lines.append(f"{dim_label}: {d.get('action','?')} "
                           f"置信度={d.get('confidence',0):.0%} "
                           f"仓位={d.get('position_pct',0)}%")
        lines.append(f"综合判决: {decision.get('overall_verdict', '无')}")
        trend = result.get("data", {}).get("trend_state", {})
        if trend:
            lines.append(f"趋势状态: {trend.get('trend_state','?')} "
                       f"(MA60斜率={trend.get('ma60_slope','?')}%)")
        weekly = result.get("data", {}).get("weekly_trend", {})
        if weekly:
            lines.append(f"周线趋势: {weekly.get('weekly_trend','?')}")
        quote = result.get("data", {}).get("quote", {})
        if quote:
            lines.append(f"当前价: {quote.get('price','?')} "
                       f"涨跌: {quote.get('change_pct','?')}%")
        return "\n".join(lines)
    except Exception as e:
        return f"[Error] analyze {symbol} 失败: {e}"


def _execute_screen() -> str:
    _log("执行 screen")
    try:
        from analysis.screener import screen_stocks
        stocks = screen_stocks(scope="hs300", top_n=10, use_mock=True)
        if not stocks:
            return "[Info] 当前未筛选出符合条件的股票"
        lines = [f"智能选股 Top {len(stocks)}:"]
        for i, s in enumerate(stocks):
            lines.append(
                f"{i+1}. {s.get('symbol','?')} {s.get('name','')} "
                f"价格{s.get('close','?')} 评分{s.get('score','-')} "
                f"信号={s.get('deepseek_signal','-')}"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"[Error] screen 失败: {e}"


def _execute_backtest(symbol: str, timeframe: str = "mid", days: int = 180) -> str:
    if not symbol:
        return "[Error] 未识别到股票代码。请在邮件正文中包含6位股票代码。"
    _log(f"执行 backtest {symbol} {timeframe} {days}")
    try:
        from backtest.runner import run_backtest_with_critic
        result = run_backtest_with_critic(
            symbol=symbol, time_frame=timeframe, days=days,
            max_rounds=1, use_mock=True
        )
        lines = [f"回测: {symbol} | {timeframe}线 | {days}天"]
        for r in result.get("rounds", []):
            m = r.get("backtest_metrics", {})
            lines.append(f"收益={m.get('total_return_pct',0):+.2f}% "
                       f"夏普={m.get('sharpe_ratio',0):.2f} "
                       f"胜率={m.get('win_rate_pct',0):.1f}% "
                       f"最大回撤={m.get('max_drawdown_pct',0):.2f}%")
            lines.append(f"Critic={r.get('critic_score','?')}/10 "
                       f"问题: {r.get('main_issue','')[:80]}")
        return "\n".join(lines)
    except Exception as e:
        return f"[Error] backtest 失败: {e}"


def _execute_portfolio() -> str:
    _log("执行 portfolio")
    try:
        from portfolio.manager import update_market_values, get_portfolio_summary
        update_market_values()
        ps = get_portfolio_summary()
        lines = [
            f"总资产: {ps.get('total_assets',0):,.0f}",
            f"可用现金: {ps.get('cash',0):,.0f}",
            f"浮动盈亏: {ps.get('total_floating_pnl',0):+,.0f} "
            f"({ps.get('total_pnl_pct',0):+.1f}%)",
            f"持仓数量: {ps.get('position_count',0)} 只",
        ]
        positions = ps.get("positions", [])
        if positions:
            lines.append("\n持仓明细:")
            for p in positions:
                pnl = p.get("floating_pnl_pct", 0)
                lines.append(
                    f"  {p['symbol']} {p.get('name','')} "
                    f"成本{p['entry_price']:.2f} 现价{p.get('current_price',0):.2f} "
                    f"盈亏{pnl:+.1f}%"
                )
        return "\n".join(lines)
    except Exception as e:
        return f"[Error] portfolio 失败: {e}"


def _execute_status() -> str:
    _log("执行 status")
    lines = [f"StockMind 运行状态 @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", ""]
    try:
        from portfolio.manager import get_portfolio_summary
        ps = get_portfolio_summary()
        lines.append(f"总资产: {ps.get('total_assets',0):,.0f} | "
                   f"现金: {ps.get('cash',0):,.0f} | "
                   f"持仓: {ps.get('position_count',0)}只")
        pnl = ps.get("total_floating_pnl", 0)
        pnl_pct = ps.get("total_pnl_pct", 0)
        lines.append(f"浮动盈亏: {pnl:+,.0f} ({pnl_pct:+.1f}%)")
    except Exception as e:
        lines.append(f"持仓: 获取失败 ({e})")
    addr, pwd = _get_credentials()
    lines.append(f"邮件通道: {'已配置' if addr and pwd else '未配置'}")
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            recent = f.readlines()[-5:]
        if recent:
            lines.append("\n最近5条日志:")
            for l in recent:
                lines.append(f"  {l.strip()}")
    return "\n".join(lines)


# ── 指令路由 ──

def check_and_execute() -> dict:
    """
    连接IMAP，检查未读邮件，读取正文内容直接作为指令执行。

    Returns:
        {"processed": int, "commands": [{"intent":..., "body":..., "result":...}, ...]}
    """
    addr, pwd = _get_credentials()
    if not addr or not pwd:
        _log("邮箱未配置，跳过检查")
        return {"processed": 0, "commands": [], "error": "邮箱未配置"}

    result = {"processed": 0, "commands": []}
    conn = None

    try:
        conn = imaplib.IMAP4_SSL("imap.qq.com", 993, timeout=15)
        conn.login(addr, pwd)
        conn.select("INBOX")

        status, data = conn.search(None, "UNSEEN")
        if status != "OK":
            _log("IMAP搜索失败")
            return {"processed": 0, "commands": [], "error": "IMAP搜索失败"}

        mail_ids = data[0].split() if data[0] else []
        if not mail_ids:
            print("  无未读邮件")
            return {"processed": 0, "commands": []}

        _log(f"发现 {len(mail_ids)} 封未读邮件")
        today = datetime.now().strftime("%Y-%m-%d")

        for mid in mail_ids:
            try:
                status, msg_data = conn.fetch(mid, "(RFC822)")
                if status != "OK":
                    continue

                raw_email = msg_data[0][1]
                msg = email.message_from_bytes(raw_email)

                subject = _decode_header(msg["Subject"])
                from_name, from_addr = parseaddr(msg.get("From", ""))

                if "[StockMind]" not in subject and "[stockmind]" not in subject.lower():
                    continue

                if from_addr.lower() != addr.lower():
                    _log(f"跳过外部邮件: {from_addr}")
                    continue

                body = _extract_body(msg)
                if not body:
                    _log(f"邮件正文为空: {subject}")
                    continue

                # 正文即指令
                intent_info = _interpret_body(body)
                intent = intent_info["intent"]
                symbol = intent_info["symbol"]
                timeframe = intent_info["timeframe"]
                days = intent_info["days"]

                _log(f"意图={intent} symbol={symbol} tf={timeframe} days={days}")
                print(f"  正文: {body[:100]}...")
                print(f"  解析: 意图={intent} 股票={symbol or '无'} 维度={timeframe} 天数={days}")

                # 执行
                if intent == "analyze":
                    output = _execute_analyze(symbol)
                elif intent == "backtest":
                    output = _execute_backtest(symbol, timeframe, days)
                elif intent == "screen":
                    output = _execute_screen()
                elif intent == "portfolio":
                    output = _execute_portfolio()
                elif intent == "status":
                    output = _execute_status()
                else:
                    output = (
                        f"未识别到明确指令。\n\n"
                        f"邮件正文即指令，无需 CMD: 前缀。支持：\n"
                        f"  - 分析 <股票代码>  → 深度分析（含趋势+三线决策）\n"
                        f"  - 选股            → 智能选股 Top 10\n"
                        f"  - 回测 <代码> <中线|长线> <天数>天\n"
                        f"  - 持仓            → 当前持仓与盈亏\n"
                        f"  - 状态            → 系统运行概况\n\n"
                        f"示例邮件正文：\n"
                        f"  「分析 000001」\n"
                        f"  「回测 600744 中线 180天」\n"
                        f"  「帮我看看平安银行的走势」"
                    )

                result["commands"].append({
                    "subject": subject,
                    "intent": intent,
                    "symbol": symbol,
                    "body_preview": body[:200],
                    "result": output[:2000],
                })
                result["processed"] += 1

                # 发送结果回复
                from mail.sender import send_email
                title = f"{intent} {symbol} | {today}"
                html = _build_html(output, title)
                send_email(f"Re: {subject}", html)

                # 标记为已读
                conn.store(mid, "+FLAGS", "\\Seen")
                _log(f"已处理: {subject} → {intent} {symbol}")

            except Exception as e:
                _log(f"处理邮件失败: {e}")
                traceback.print_exc()
                continue

    except imaplib.IMAP4.error as e:
        _log(f"IMAP登录失败: {e}")
        result["error"] = f"IMAP登录失败: {e}"
    except Exception as e:
        _log(f"IMAP连接失败: {e}")
        result["error"] = f"IMAP连接失败: {e}"
    finally:
        if conn:
            try:
                conn.close()
                conn.logout()
            except Exception:
                pass

    return result


def start_mail_listener(interval_sec: int = 60):
    _log(f"邮件监听已启动，每{interval_sec}秒检查一次")
    print(f"  [MailListener] 监听中... (间隔{interval_sec}s)")
    while True:
        try:
            result = check_and_execute()
            n = result.get("processed", 0)
            if n > 0:
                _log(f"本轮处理了 {n} 封指令邮件")
            print(f"  [MailListener] 检查完成，处理 {n} 封 | {datetime.now().strftime('%H:%M:%S')}")
        except Exception as e:
            _log(f"监听循环异常: {e}")
            traceback.print_exc()
        time.sleep(interval_sec)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    import argparse
    parser = argparse.ArgumentParser(description="StockMind QQ邮箱指令通道")
    parser.add_argument("--listen", action="store_true", help="持续监听模式")
    parser.add_argument("--interval", type=int, default=60, help="监听间隔(秒)")
    args = parser.parse_args()

    if args.listen:
        start_mail_listener(args.interval)
    else:
        result = check_and_execute()
        print(f"\n处理完成: {result.get('processed', 0)} 封")
        for c in result.get("commands", []):
            print(f"  [{c['intent']}] {c['symbol']} → {c['result'][:120]}")
        if result.get("error"):
            print(f"  ERROR: {result['error']}")
