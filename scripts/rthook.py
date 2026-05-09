"""
PyInstaller runtime hook — 在打包后的 exe 启动时运行。
确保 SSL 证书路径正确，设置日志记录。
"""
import os
import sys

# ── 1. SSL 证书路径 ──────────────────────────────────────
# certifi 打包后通过 sys._MEIPASS 提供 cacert.pem
try:
    import certifi
    ca_path = certifi.where()
    if os.path.exists(ca_path):
        os.environ['SSL_CERT_FILE'] = ca_path
        os.environ['REQUESTS_CA_BUNDLE'] = ca_path
except Exception:
    pass

# ── 2. 确保项目模块可导入 ──────────────────────────────
if getattr(sys, 'frozen', False):
    # PyInstaller 已将项目模块加入路径
    pass
else:
    # 开发模式
    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if project_dir not in sys.path:
        sys.path.insert(0, project_dir)

# ── 3. 设置日志文件（控制台隐藏时也能看到错误） ──────
import logging
_log_dir = None
if getattr(sys, 'frozen', False):
    _log_dir = os.path.dirname(sys.executable)
else:
    _log_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    filename=os.path.join(_log_dir, 'stock_mind.log'),
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    encoding='utf-8',
)
logging.info(f"StockMind 启动 | Python {sys.version}")
logging.info(f"MEIPASS: {getattr(sys, '_MEIPASS', 'N/A')}")
logging.info(f"Executable: {sys.executable}")
logging.info(f"SSL_CERT_FILE: {ca_path if 'ca_path' in dir() else 'not set'}")
