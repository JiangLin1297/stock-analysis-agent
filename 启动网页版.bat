@echo off
chcp 65001 >nul
title StockMind 网页版 - 正在启动...

echo ============================================
echo    StockMind 网页版启动中...
echo ============================================
echo.

cd /d "%~dp0"

:: 启动 Streamlit 服务（自动打开浏览器）
python -m streamlit run web_ui.py --server.port 8501 --browser.gatherUsageStats false

:: 如果 Streamlit 退出了，暂停让用户看到错误信息
if %errorlevel% neq 0 (
    echo.
    echo 启动失败，请检查 Python 和 Streamlit 是否已安装。
    echo 可尝试运行: pip install streamlit
    pause
)
