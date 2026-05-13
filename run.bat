@echo off
cd /d %~dp0
echo 正在检查 Streamlit...
pip show streamlit >nul 2>&1
if %errorlevel% neq 0 (
    echo Streamlit 未安装，正在安装...
    pip install streamlit
)
echo 启动 StockMind 网页端...
start "" http://localhost:8501
streamlit run web_ui.py --server.port 8501
pause