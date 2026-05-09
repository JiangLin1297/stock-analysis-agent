#!/usr/bin/env python3
"""QQ邮箱发送模块 — 每日选股与收益报告。"""
import smtplib
import sys
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

from utils.config import get_config_value

# Windows GBK console workaround
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass


def _get_credentials():
    addr = get_config_value("EMAIL_ADDRESS")
    pwd = get_config_value("EMAIL_PASSWORD")
    recipient = get_config_value("REPORT_RECIPIENT", addr)
    return addr, pwd, recipient


def send_email(subject: str, body: str, to_addr: str = None, html: bool = True) -> bool:
    """通过 QQ邮箱 SMTP 发送邮件。"""
    addr, pwd, recipient = _get_credentials()
    if not addr or not pwd:
        print("⚠ QQ邮箱未配置，无法发送邮件")
        return False

    target = to_addr or recipient
    if not target:
        print("⚠ 未设置收件人")
        return False

    try:
        msg = MIMEMultipart("alternative")
        msg["From"] = addr
        msg["To"] = target
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "html" if html else "plain", "utf-8"))

        with smtplib.SMTP_SSL("smtp.qq.com", 465, timeout=15) as server:
            server.login(addr, pwd)
            server.sendmail(addr, [target], msg.as_string())
        print(f"📧 邮件已发送 → {target}")
        return True
    except smtplib.SMTPAuthenticationError:
        print("❌ QQ邮箱认证失败，请检查邮箱地址和授权码")
        return False
    except Exception as e:
        print(f"❌ 邮件发送失败: {e}")
        return False


def send_test_email() -> bool:
    """发送一封测试邮件，验证邮箱配置是否正确。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    body = f"""
    <h2>StockMind 测试邮件</h2>
    <p>QQ邮箱配置成功！</p>
    <p>发送时间: {now}</p>
    <p style="color:#7aa2f7;">如果你收到这封邮件，说明邮件功能已正常配置。</p>
    """
    return send_email("[StockMind] 测试邮件", body)


def send_daily_report() -> bool:
    """发送每日报告：持仓收益 + 智能选股 Top 5。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    # ── 持仓汇总 ──
    portfolio_html = "<p>暂无持仓数据</p>"
    try:
        from portfolio.manager import get_portfolio_summary
        ps = get_portfolio_summary()
        pnl = ps.get("total_floating_pnl", 0)
        pnl_pct = ps.get("total_pnl_pct", 0)
        pnl_color = "#9ece6a" if pnl >= 0 else "#f7768e"
        positions = ps.get("positions", [])

        portfolio_html = f"""
        <table style="width:100%;border-collapse:collapse;margin-top:8px;">
            <tr style="background:#1a1b26;color:#c0caf5;">
                <th style="padding:6px 10px;text-align:left;">指标</th>
                <th style="padding:6px 10px;text-align:right;">数值</th>
            </tr>
            <tr><td style="padding:6px 10px;">💰 总资产</td>
                <td style="padding:6px 10px;text-align:right;">¥{ps['total_assets']:,.0f}</td></tr>
            <tr><td style="padding:6px 10px;">💵 可用现金</td>
                <td style="padding:6px 10px;text-align:right;">¥{ps['cash']:,.0f}</td></tr>
            <tr><td style="padding:6px 10px;">📈 浮动盈亏</td>
                <td style="padding:6px 10px;text-align:right;color:{pnl_color};">
                ¥{pnl:+,.0f} ({pnl_pct:+.1f}%)</td></tr>
            <tr><td style="padding:6px 10px;">📦 持仓数量</td>
                <td style="padding:6px 10px;text-align:right;">{ps['position_count']} 只</td></tr>
        </table>
        """

        if positions:
            rows = ""
            for p in positions:
                p_pnl = p.get("floating_pnl_pct", 0)
                p_color = "#9ece6a" if p_pnl >= 0 else "#f7768e"
                rows += f"""
                <tr>
                    <td style="padding:4px 8px;">{p['symbol']}</td>
                    <td style="padding:4px 8px;">{p.get('name','')}</td>
                    <td style="padding:4px 8px;text-align:right;">{p['entry_price']:.2f}</td>
                    <td style="padding:4px 8px;text-align:right;">{p.get('current_price',0):.2f}</td>
                    <td style="padding:4px 8px;text-align:right;">{p['quantity']}</td>
                    <td style="padding:4px 8px;text-align:right;">¥{p['market_value']:,.0f}</td>
                    <td style="padding:4px 8px;text-align:right;color:{p_color};">
                    {p_pnl:+.1f}%</td>
                </tr>"""
            portfolio_html += f"""
            <h4 style="margin-top:16px;">持仓明细</h4>
            <table style="width:100%;border-collapse:collapse;font-size:13px;">
                <tr style="background:#1a1b26;color:#c0caf5;">
                    <th style="padding:4px 8px;">代码</th><th style="padding:4px 8px;">名称</th>
                    <th style="padding:4px 8px;">成本</th><th style="padding:4px 8px;">现价</th>
                    <th style="padding:4px 8px;">数量</th><th style="padding:4px 8px;">市值</th>
                    <th style="padding:4px 8px;">盈亏</th>
                </tr>{rows}
            </table>"""
    except Exception as e:
        portfolio_html = f"<p>⚠ 获取持仓失败: {e}</p>"

    # ── 智能选股 ──
    screening_html = "<p>暂无选股数据</p>"
    try:
        from analysis.screener import screen_stocks
        stocks = screen_stocks(scope="hs300", top_n=5, use_mock=False)
        if stocks:
            rows = ""
            for s in stocks:
                signal = s.get("deepseek_signal", "-")
                sig_color = {"BUY": "#9ece6a", "HOLD": "#e0af68", "SELL": "#f7768e"}.get(signal, "#c0caf5")
                def _fmt(v):
                    try: return f"{float(v):.2f}"
                    except: return str(v) if v else "-"
                rows += f"""
                <tr>
                    <td style="padding:4px 8px;">{s['symbol']}</td>
                    <td style="padding:4px 8px;">{s.get('name','')}</td>
                    <td style="padding:4px 8px;text-align:right;">{_fmt(s.get('close',0))}</td>
                    <td style="padding:4px 8px;text-align:center;">{s.get('score','-')}</td>
                    <td style="padding:4px 8px;text-align:right;">{_fmt(s.get('entry_price',0))}</td>
                    <td style="padding:4px 8px;text-align:right;">{_fmt(s.get('stop_loss',0))}</td>
                    <td style="padding:4px 8px;text-align:center;color:{sig_color};font-weight:600;">
                    {signal}</td>
                </tr>"""
            screening_html = f"""
            <table style="width:100%;border-collapse:collapse;font-size:13px;">
                <tr style="background:#1a1b26;color:#c0caf5;">
                    <th style="padding:4px 8px;">代码</th><th style="padding:4px 8px;">名称</th>
                    <th style="padding:4px 8px;">现价</th><th style="padding:4px 8px;">评分</th>
                    <th style="padding:4px 8px;">入场价</th><th style="padding:4px 8px;">止损</th>
                    <th style="padding:4px 8px;">信号</th>
                </tr>{rows}
            </table>"""
        else:
            screening_html = "<p>⚠ 当前未筛选出符合条件的股票</p>"
    except Exception as e:
        screening_html = f"<p>⚠ 选股失败: {e}</p>"

    # ── 组装邮件 ──
    body = f"""
    <div style="font-family:'Microsoft YaHei',sans-serif;max-width:640px;
                background:#1f2335;color:#c0caf5;padding:24px;border-radius:12px;">
        <h2 style="color:#7aa2f7;margin:0 0 4px;">🧠 StockMind 每日报告</h2>
        <p style="color:#7982a9;margin:0 0 20px;">{now}</p>

        <h3 style="color:#9ece6a;">📊 当前收益</h3>
        {portfolio_html}

        <h3 style="color:#2ac3de;margin-top:24px;">💡 智能选股 Top 5</h3>
        {screening_html}

        <p style="color:#565f89;margin-top:24px;font-size:11px;">
        StockMind v2.0 · 数据来源: 腾讯行情/东方财富 · 仅供参考，不构成投资建议</p>
    </div>
    """

    subject = f"StockMind 每日报告 {now}"
    return send_email(subject, body)
