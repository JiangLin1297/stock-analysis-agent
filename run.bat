@echo off
chcp 65001 >nul
title 多Agent 股析系统

REM ── 检查 DEEPSEEK_API_KEY ──────────────────────────────────
if "%DEEPSEEK_API_KEY%"=="" (
    echo ╔══════════════════════════════════════════════╗
    echo ║  提示：未设置 DEEPSEEK_API_KEY 环境变量       ║
    echo ║                                              ║
    echo ║  你可以在应用内"设置"页面直接输入保存。        ║
    echo ║  或在此设置环境变量：                          ║
    echo ║    set DEEPSEEK_API_KEY=sk-xxxxxxxx           ║
    echo ║                                              ║
    echo ║  继续启动...                                  ║
    echo ╚══════════════════════════════════════════════╝
    echo.
)

REM ── 可选：QQ邮箱配置 ──────────────────────────────────
REM 推荐在应用内"设置"页面直接配置，无需手动编辑此文件。
REM QQ邮箱 SMTP: smtp.qq.com:465 SSL
REM 授权码在 QQ邮箱网页版「设置→账户→POP3/SMTP服务」中生成。
REM 环境变量 (也可在应用设置页输入):
REM set EMAIL_ADDRESS=your_email@qq.com
REM set EMAIL_PASSWORD=your_authorization_code
REM set REPORT_RECIPIENT=your_email@qq.com

REM ── 启动桌面应用 ─────────────────────────────────────────
echo  正在启动 StockMind ...

if exist "%~dp0dist\StockMind.exe" (
    start "" "%~dp0dist\StockMind.exe"
) else if exist "%~dp0dist\StockAgent.exe" (
    start "" "%~dp0dist\StockAgent.exe"
) else (
    echo  未找到打包的 exe，使用 Python 启动...
    python "%~dp0desktop_app.py"
)
exit 0
