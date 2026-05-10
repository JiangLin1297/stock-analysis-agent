@echo off
chcp 65001 >nul
title StockMind - 多Agent深度股析系统

echo  正在启动 StockMind ...

if exist "%~dp0dist\StockMind\StockMind.exe" (
    start "" "%~dp0dist\StockMind\StockMind.exe"
) else if exist "%~dp0dist\StockAgent\StockAgent.exe" (
    start "" "%~dp0dist\StockAgent\StockAgent.exe"
) else (
    echo  未找到打包的 exe，使用 Python 启动...
    python "%~dp0desktop_app.py"
)
exit 0
