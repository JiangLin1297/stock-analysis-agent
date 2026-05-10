#!/usr/bin/env python3
"""
StockMind Desktop Launcher
带崩溃保护、托盘优先启动、异步初始化的桌面入口。

启动顺序:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    window = MainWindow()          # 先创建窗口
    window.setup_tray()            # 创建托盘(动态绘制图标)
    window.show()
    sys.exit(app.exec())
"""

import sys
import os
import traceback
from datetime import datetime

# 确保项目根目录在路径中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _crash_log_path():
    if getattr(sys, 'frozen', False):
        return os.path.join(os.path.dirname(sys.executable), "crash_log.txt")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "crash_log.txt")


def _global_exception_hook(exc_type, exc_value, exc_tb):
    """将未捕获异常写入 crash_log.txt 并弹窗提示。"""
    tb_lines = traceback.format_exception(exc_type, exc_value, exc_tb)
    crash_msg = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 崩溃\n" + "".join(tb_lines)

    try:
        with open(_crash_log_path(), 'a', encoding='utf-8') as f:
            f.write(crash_msg + "\n")
    except Exception:
        pass

    try:
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.critical(
            None, "StockMind 崩溃",
            f"程序遇到严重错误，已记录到:\n{_crash_log_path()}\n\n"
            f"错误详情:\n{exc_type.__name__}: {exc_value}"
        )
    except Exception:
        pass

    sys.__excepthook__(exc_type, exc_value, exc_tb)


def main():
    # ── 1. 全局异常钩子 ──
    sys.excepthook = _global_exception_hook

    # ── 2. 单实例保护 ──
    from ui.app import _check_single_instance
    if not _check_single_instance():
        return

    # ── 3. 加载配置 ──
    from utils.config import load_config
    cfg = load_config()
    for k, v in cfg.items():
        if v and not os.environ.get(k):
            os.environ[k] = str(v)

    # ── 4. QApplication ──
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QFont

    app = QApplication(sys.argv)
    app.setApplicationName("StockMind")
    app.setOrganizationName("StockMind")
    app.setQuitOnLastWindowClosed(False)

    font = QFont("Microsoft YaHei", 9)
    font.setStyleStrategy(QFont.PreferAntialias)
    app.setFont(font)

    # ── 5. 先创建窗口，再创建托盘（传入窗口引用）──
    from ui.app import MainWindow

    window = MainWindow()
    window.setup_tray()          # 内部用 QPainter 动态绘制 32x32 托盘图标
    window.show()

    # ── 6. 事件循环 ──
    exit_code = app.exec()

    # ── 7. 清理 ──
    if hasattr(window, '_scheduler') and window._scheduler:
        window._scheduler.running = False

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
