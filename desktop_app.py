#!/usr/bin/env python3
"""
StockMind Desktop Launcher
带崩溃保护、托盘优先启动、异步初始化的桌面入口。
"""

import sys
import os
import traceback
import atexit
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

    # 尝试弹窗
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

    # ── 4. 创建 QApplication（关键：托盘不退出）──
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QFont

    app = QApplication(sys.argv)
    app.setApplicationName("StockMind")
    app.setOrganizationName("StockMind")
    app.setQuitOnLastWindowClosed(False)

    # 全局字体
    font = QFont("Microsoft YaHei", 9)
    font.setStyleStrategy(QFont.PreferAntialias)
    app.setFont(font)

    # ── 5. 先创建托盘图标（含临时 QMenu），再显示主窗口 ──
    from PySide6.QtWidgets import QSystemTrayIcon, QMenu
    from PySide6.QtGui import QAction

    tray = QSystemTrayIcon()
    tray.setToolTip("StockMind")

    # 托盘菜单
    tray_menu = QMenu()
    show_action = QAction("显示主窗口")
    quit_action = QAction("退出")
    tray_menu.addAction(show_action)
    tray_menu.addSeparator()
    tray_menu.addAction(quit_action)
    tray.setContextMenu(tray_menu)
    tray.show()

    # ── 6. 异步创建主窗口（让托盘先就位）──
    from PySide6.QtCore import QTimer
    from ui.app import MainWindow

    window = None

    def _create_window():
        nonlocal window
        try:
            window = MainWindow()
            window._tray_icon = tray
            show_action.triggered.connect(window.show_and_raise)
            quit_action.triggered.connect(window.quit_app)
            tray.activated.connect(
                lambda reason: window.show_and_raise()
                if reason == QSystemTrayIcon.DoubleClick else None
            )
            window.show()
        except Exception as e:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.critical(None, "启动失败", f"主窗口创建失败:\n{e}")
            _global_exception_hook(type(e), e, e.__traceback__)
            app.quit()

    QTimer.singleShot(100, _create_window)

    # ── 7. 事件循环 ──
    exit_code = app.exec()

    # ── 8. 清理 ──
    if window and hasattr(window, '_scheduler') and window._scheduler:
        window._scheduler.running = False
    tray.hide()

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
