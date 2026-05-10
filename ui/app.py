#!/usr/bin/env python3
"""
StockMind — 多Agent 深度股析系统 (PySide6)
现代桌面客户端，深色/亮色主题，系统托盘，实时分析流输出。
"""
import sys
import os
import json
import queue
import re
import threading
import atexit
import time
from datetime import datetime
from typing import Optional

# ── 确保项目路径 ──
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QListWidget, QListWidgetItem, QStackedWidget,
    QFrame, QTableWidget, QTableWidgetItem, QHeaderView, QLineEdit,
    QComboBox, QSpinBox, QDoubleSpinBox, QCheckBox, QGroupBox, QProgressBar,
    QTabWidget, QTextEdit, QStatusBar,
    QMessageBox, QSlider, QAbstractItemView, QSizePolicy,
)
from PySide6.QtCore import (
    Qt, QObject, Signal, Slot, QThread, QPropertyAnimation,
    QEasingCurve, QTimer, QRect, QEvent,
)
from PySide6.QtGui import (
    QFont, QIcon, QAction, QColor, QPixmap, QPainter, QBrush,
    QLinearGradient,
)
from PySide6.QtWidgets import QSystemTrayIcon, QMenu

from ui.theme import DARK, LIGHT, qss

# ═══════════════════════════════════════════════════════════════
# 辅助：单实例保护
# ═══════════════════════════════════════════════════════════════

_LOCK_FILE = None
def _check_single_instance() -> bool:
    global _LOCK_FILE
    if getattr(sys, 'frozen', False):
        _LOCK_FILE = os.path.join(os.path.dirname(sys.executable), '.stockmind.lock')
    else:
        _LOCK_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.stockmind.lock')
    if os.path.exists(_LOCK_FILE):
        try:
            with open(_LOCK_FILE, 'r') as f:
                pid = f.read().strip()
            if pid:
                import subprocess
                r = subprocess.run(['tasklist', '/FI', f'PID eq {pid}'],
                                   capture_output=True, text=True, timeout=5)
                if pid in r.stdout:
                    print(f"[StockMind] 已在运行 (PID={pid})")
                    return False
        except Exception:
            pass
        try:
            os.remove(_LOCK_FILE)
        except Exception:
            pass
    with open(_LOCK_FILE, 'w') as f:
        f.write(str(os.getpid()))
    atexit.register(lambda: os.remove(_LOCK_FILE) if _LOCK_FILE and os.path.exists(_LOCK_FILE) else None)
    return True


# ═══════════════════════════════════════════════════════════════
# 工作线程 stdout 重定向
# ═══════════════════════════════════════════════════════════════

_stdout_lock = threading.Lock()

class WorkerStdout:
    """在 worker 线程中捕获 print()，节流发射到 GUI 防止信号洪泛导致 UI 冻结。"""
    def __init__(self, callback):
        self.callback = callback
        self.buffer = ""
        self._last_emit = 0.0
        self._min_interval = 0.15  # 最少 150ms 间隔，防止 UI 被海量信号淹没

    def write(self, text):
        if not text:
            return
        self.buffer += text
        now = time.time()
        # 节流：超过 150ms 或累积超过 4000 字符才发射信号
        if len(self.buffer) >= 4000 or (now - self._last_emit >= self._min_interval):
            self._do_emit()
            self._last_emit = now

    def flush(self):
        self._do_emit()

    def _do_emit(self):
        if self.buffer.strip() and self.callback:
            self.callback(self.buffer)
        self.buffer = ""


# ═══════════════════════════════════════════════════════════════
# 后台 Worker（QThread 安全）
# ═══════════════════════════════════════════════════════════════

class AnalysisWorker(QObject):
    """在后台线程执行分析任务，通过信号输出结果。"""
    log_signal = Signal(str)
    progress_signal = Signal(int, str)  # percent, stage_name
    finished = Signal(object)
    error = Signal(str)

    # ── 深度分析 ──
    def run_deep_analysis(self, symbol: str, use_portfolio: bool = False,
                           use_adapted_params: bool = False):
        from agents.decision import run_full_analysis
        self._run_with_stdout(
            lambda: run_full_analysis(symbol, use_mock=False, use_portfolio=use_portfolio,
                                       use_adapted_params=use_adapted_params)
        )

    # ── 执行总裁分析 ──
    def run_executive(self, symbol: str):
        from agents.executive import executive_decision
        self._run_with_stdout(
            lambda: executive_decision(symbol, use_mock=False)
        )

    # ── 智能选股 ──
    def run_screening(self, scope: str, top_n: int):
        from analysis.screener import screen_stocks
        self._run_with_stdout(
            lambda: screen_stocks(scope=scope, top_n=top_n, use_mock=False)
        )

    # ── 刷新持仓 ──
    def run_refresh_portfolio(self):
        from portfolio.manager import update_market_values, get_portfolio_summary
        self._run_with_stdout(lambda: (update_market_values(), get_portfolio_summary()))

    # ── 回测进化 ──
    def run_backtest(self, symbol: str, time_frame: str, days: int, max_rounds: int):
        from backtest.runner import run_backtest_with_critic
        self._run_with_stdout(
            lambda: run_backtest_with_critic(symbol=symbol, time_frame=time_frame,
                                              days=days, max_rounds=max_rounds, use_mock=True)
        )

    # ── 自适应迁移 ──
    def run_adaptation(self, symbol: str):
        from data.adapter import auto_adapt_and_backtest
        self._run_with_stdout(
            lambda: auto_adapt_and_backtest(symbol=symbol, time_frame="mid", days=180,
                                             max_rounds=3, use_mock=True)
        )

    # ── 基因库加载 ──
    def load_gene_library(self):
        import os as _os
        project_dir = _os.path.dirname(_os.path.abspath(__file__))
        genes = []
        for fname in _os.listdir(project_dir):
            if fname.endswith("_adapted_params.json"):
                fpath = _os.path.join(project_dir, fname)
                with open(fpath, 'r', encoding='utf-8') as _f:
                    data = json.load(_f)
                genes.append({
                    "file": fname,
                    "symbol": data.get("symbol", fname.split("_")[0]),
                    "adapted_from": data.get("adapted_from", ""),
                    "adapted_at": data.get("adapted_at", ""),
                    "params": data.get("params", {}),
                })
        return genes

    # 共享线程池，避免频繁创建/销毁线程
    _executor = None

    @classmethod
    def _get_executor(cls):
        if cls._executor is None:
            from concurrent.futures import ThreadPoolExecutor
            cls._executor = ThreadPoolExecutor(max_workers=4)
        return cls._executor

    def _run_with_stdout(self, fn, timeout=120):
        """通用：重定向 stdout → log_signal，执行 fn，发射 result。
        包含心跳机制防止 Windows 判定程序无响应。"""
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, Future

        if not _stdout_lock.acquire(blocking=False):
            try:
                result = fn()
                self.finished.emit(result)
            except Exception as e:
                import traceback
                self.log_signal.emit(f"\n❌ 错误: {e}\n{traceback.format_exc()}")
                self.error.emit(str(e))
            return

        def _emit(text):
            self.log_signal.emit(text)

        old = sys.stdout
        captured = WorkerStdout(_emit)
        sys.stdout = captured

        # 心跳线程：每 5 秒发射一次进度信号，防止 Windows 判定无响应
        heartbeat_running = [True]
        def _heartbeat():
            while heartbeat_running[0]:
                time.sleep(5)
                if heartbeat_running[0]:
                    self.progress_signal.emit(0, "分析中...")

        heartbeat_thread = threading.Thread(target=_heartbeat, daemon=True)
        heartbeat_thread.start()

        try:
            self.progress_signal.emit(10, "加载中...")
            executor = AnalysisWorker._get_executor()
            future = executor.submit(fn)
            try:
                result = future.result(timeout=timeout)
            except FuturesTimeoutError:
                captured.flush()
                self.log_signal.emit(f"\n⏰ 分析超时 ({timeout}s)，已停止等待")
                self.progress_signal.emit(0, "超时")
                self.error.emit(f"分析超时 ({timeout}s)")
                return
            captured.flush()
            self.progress_signal.emit(100, "完成")
            self.finished.emit(result)
        except Exception as e:
            captured.flush()
            import traceback
            self.log_signal.emit(f"\n❌ 错误: {e}\n{traceback.format_exc()}")
            self.progress_signal.emit(0, "失败")
            self.error.emit(str(e))
        finally:
            heartbeat_running[0] = False
            sys.stdout = old
            _stdout_lock.release()


class OverviewWorker(QObject):
    """后台线程：更新持仓市值，返回摘要数据。"""
    finished = Signal(object)  # portfolio_summary dict
    error = Signal(str)

    def run(self):
        try:
            from portfolio.manager import update_market_values, get_portfolio_summary
            update_market_values()
            ps = get_portfolio_summary()
            self.finished.emit(ps)
        except Exception as e:
            import traceback
            self.error.emit(f"{e}\n{traceback.format_exc()}")


# ═══════════════════════════════════════════════════════════════
# 可复用小组件
# ═══════════════════════════════════════════════════════════════

class MetricCard(QFrame):
    """单个指标卡片：标题 + 数值。"""
    def __init__(self, title: str, value: str = "—", parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.setMinimumHeight(90)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(4)
        self.title_lbl = QLabel(title)
        self.title_lbl.setObjectName("cardTitle")
        self.val_lbl = QLabel(value)
        self.val_lbl.setObjectName("cardValue")
        layout.addWidget(self.title_lbl)
        layout.addWidget(self.val_lbl)

    def set_value(self, text: str, color: str = None):
        self.val_lbl.setText(text)
        if color:
            self.val_lbl.setStyleSheet(f"color: {color}; font-size: 22px; font-weight: 700;")
        else:
            self.val_lbl.setStyleSheet("")


class SectionTitle(QLabel):
    """章节标题。"""
    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self.setStyleSheet("font-size: 16px; font-weight: 700; padding: 8px 0;")


class ModernButton(QPushButton):
    """带样式的按钮，自动设置 objectName 和最小高度。"""
    def __init__(self, text: str, primary=False, success=False, danger=False, parent=None):
        super().__init__(text, parent)
        if primary: self.setObjectName("btnPrimary")
        elif success: self.setObjectName("btnSuccess")
        elif danger: self.setObjectName("btnDanger")
        self.setMinimumHeight(36)
        self.setCursor(Qt.PointingHandCursor)


# ═══════════════════════════════════════════════════════════════
# 标题栏（自定义，支持拖拽）
# ═══════════════════════════════════════════════════════════════

class TitleBar(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("titleBar")
        self.setFixedHeight(44)
        self._drag_pos = None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 0, 8, 0)
        layout.setSpacing(4)

        # 应用名 — 鼠标事件穿透，由 TitleBar 统一处理拖拽
        icon_lbl = QLabel("🧠")
        icon_lbl.setStyleSheet("font-size: 18px;")
        icon_lbl.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.title_lbl = QLabel("StockMind")
        self.title_lbl.setObjectName("titleLabel")
        self.title_lbl.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        # 主题切换
        self.theme_btn = QPushButton("🌓")
        self.theme_btn.setObjectName("tbBtn")
        self.theme_btn.setToolTip("切换主题")
        self.theme_btn.setFixedSize(36, 28)

        # 窗口按钮
        self.min_btn = QPushButton("🗕")
        self.min_btn.setObjectName("tbBtn")
        self.min_btn.setToolTip("最小化")
        self.min_btn.setFixedSize(36, 28)

        self.max_btn = QPushButton("🗖")
        self.max_btn.setObjectName("tbBtn")
        self.max_btn.setToolTip("最大化")
        self.max_btn.setFixedSize(36, 28)

        self.close_btn = QPushButton("✕")
        self.close_btn.setObjectName("tbClose")
        self.close_btn.setToolTip("关闭到系统托盘")
        self.close_btn.setFixedSize(36, 28)

        layout.addWidget(icon_lbl)
        layout.addWidget(self.title_lbl)
        layout.addStretch()
        layout.addWidget(self.theme_btn)
        layout.addWidget(self.min_btn)
        layout.addWidget(self.max_btn)
        layout.addWidget(self.close_btn)

        self.min_btn.clicked.connect(self._on_minimize)
        self.max_btn.clicked.connect(self._toggle_maximize)
        self.close_btn.clicked.connect(self._on_close)

    def _on_minimize(self):
        p = self.window()
        if p:
            p._minimize_window()

    def _on_close(self):
        p = self.window()
        if p and hasattr(p, 'hide_to_tray'):
            p.hide_to_tray()

    def _toggle_maximize(self):
        p = self.window()
        if p.isMaximized():
            p.showNormal()
            self.max_btn.setText("🗖")
        else:
            p.showMaximized()
            self.max_btn.setText("🗗")

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.window().frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.LeftButton and self._drag_pos is not None:
            self.window().move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._toggle_maximize()

    def mouseReleaseEvent(self, event):
        self._drag_pos = None


# ═══════════════════════════════════════════════════════════════
# 页面1：总览
# ═══════════════════════════════════════════════════════════════

class OverviewPage(QFrame):
    analyze_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("page")
        self._editor_visible = False
        self._worker = None
        self._thread = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)

        # ── 指标卡片 ──
        cards = QHBoxLayout()
        cards.setSpacing(12)
        self.total_card = MetricCard("💰 总资产")
        self.pnl_card = MetricCard("📈 今日盈亏")
        self.pos_card = MetricCard("📦 持仓数量")
        self.cash_card = MetricCard("💵 可用现金")
        cards.addWidget(self.total_card)
        cards.addWidget(self.pnl_card)
        cards.addWidget(self.pos_card)
        cards.addWidget(self.cash_card)
        layout.addLayout(cards)

        # ── 操作栏 ──
        toolbar = QHBoxLayout()
        toolbar.setSpacing(10)
        title = SectionTitle("持仓明细")
        self.edit_toggle_btn = ModernButton("✏️ 手动编辑")
        self.edit_toggle_btn.setFixedWidth(110)
        self.edit_toggle_btn.clicked.connect(self._toggle_editor)
        self.refresh_btn = ModernButton("🔄 刷新市值", primary=True)
        self.refresh_btn.setFixedWidth(130)
        self.refresh_btn.clicked.connect(self.refresh)
        toolbar.addWidget(title)
        toolbar.addStretch()
        toolbar.addWidget(self.edit_toggle_btn)
        toolbar.addWidget(self.refresh_btn)
        layout.addLayout(toolbar)

        # ── 编辑面板（默认隐藏） ──
        self.editor_panel = self._create_editor_panel()
        self.editor_panel.setVisible(False)
        layout.addWidget(self.editor_panel)

        # ── 持仓表格 ──
        self.table = QTableWidget()
        self.table.setObjectName("card")
        self.table.setColumnCount(8)
        self.table.setHorizontalHeaderLabels(["代码", "名称", "成本价", "现价", "数量", "市值", "盈亏%", "操作"])
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setSortingEnabled(True)
        self.table.setMinimumHeight(200)
        layout.addWidget(self.table, stretch=1)

    def refresh(self):
        """加载持仓并更新 UI。先显示缓存数据，再后台刷新实时价格。"""
        # Step 1：立即显示缓存数据（不阻塞 UI）
        try:
            from portfolio.manager import get_portfolio_summary
            ps = get_portfolio_summary()
            self._update_ui(ps)
        except Exception:
            pass

        # Step 2：后台线程刷新实时市值
        self._start_bg_refresh()

    def _start_bg_refresh(self):
        """在后台线程更新市值，完成后通过信号更新 UI。"""
        if self._thread is not None and self._thread.isRunning():
            return  # 已有刷新在进行中
        self.refresh_btn.setEnabled(False)
        self.refresh_btn.setText("⏳ 刷新中...")

        self._thread = QThread()
        self._worker = OverviewWorker()
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._on_refresh_done)
        self._worker.error.connect(self._on_refresh_error)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.finished.connect(lambda: setattr(self, '_thread', None))
        self._thread.start()

    def _on_refresh_done(self, ps):
        self.refresh_btn.setEnabled(True)
        self.refresh_btn.setText("🔄 刷新市值")
        if ps:
            self._update_ui(ps)

    def _on_refresh_error(self, msg):
        self.refresh_btn.setEnabled(True)
        self.refresh_btn.setText("🔄 刷新市值")
        print(f"[Overview] 刷新失败: {msg}")

    def _update_ui(self, ps):
        """用持仓摘要数据更新卡片和表格。"""
        total = ps['total_assets']
        pnl = ps['total_floating_pnl']
        pnl_pct = ps['total_pnl_pct']
        cash = ps['cash']
        pos_count = ps['position_count']
        pnl_color = DARK['up'] if pnl >= 0 else DARK['down']

        self.total_card.set_value(f"¥{total:,.0f}")
        self.pnl_card.set_value(f"¥{pnl:+,.0f} ({pnl_pct:+.1f}%)", pnl_color)
        self.pos_card.set_value(f"{pos_count} 只")
        self.cash_card.set_value(f"¥{cash:,.0f}")

        # 表格
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        positions = ps.get("positions", [])
        self.table.setRowCount(len(positions))

        for i, p in enumerate(positions):
            items_data = [
                (p["symbol"], None),
                (p.get("name", ""), None),
                (f"{p['entry_price']:.2f}", None),
                (f"{p.get('current_price', 0):.2f}", None),
                (str(p["quantity"]), None),
                (f"¥{p['market_value']:,.0f}", None),
                (f"{p.get('floating_pnl_pct', 0):+.1f}%",
                 DARK['up'] if p.get('floating_pnl_pct', 0) >= 0 else DARK['down']),
            ]
            for col, (text, color) in enumerate(items_data):
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignCenter)
                if color:
                    item.setForeground(QColor(color))
                self.table.setItem(i, col, item)

            btn = QPushButton("🔍 分析")
            btn.setObjectName("btnPrimary")
            btn.setStyleSheet("padding: 4px 12px; font-size: 11px;")
            sym = p["symbol"]
            btn.clicked.connect(lambda checked, s=sym: self.analyze_requested.emit(s))
            self.table.setCellWidget(i, 7, btn)

        self.table.setSortingEnabled(True)

    # ── 手动编辑持仓 ──
    def _create_editor_panel(self):
        """构建手动编辑持仓的面板。"""
        panel = QGroupBox("✏️ 手动编辑持仓与资产")
        panel_layout = QVBoxLayout(panel)
        panel_layout.setSpacing(10)

        # ── 现金与总资产行 ──
        cash_assets_row = QHBoxLayout()
        cash_assets_row.setSpacing(16)

        cash_label = QLabel("可用现金:")
        self.cash_input = QLineEdit()
        self.cash_input.setPlaceholderText("如 50000")
        self.cash_input.setFixedWidth(140)

        assets_label = QLabel("总资产:")
        self.assets_input = QLineEdit()
        self.assets_input.setPlaceholderText("自动计算，也可手动覆盖")
        self.assets_input.setFixedWidth(160)

        self.save_cash_btn = ModernButton("💾 更新现金/资产", primary=True)
        self.save_cash_btn.setFixedWidth(150)
        self.save_cash_btn.clicked.connect(self._save_cash_assets)

        cash_assets_row.addWidget(cash_label)
        cash_assets_row.addWidget(self.cash_input)
        cash_assets_row.addWidget(assets_label)
        cash_assets_row.addWidget(self.assets_input)
        cash_assets_row.addWidget(self.save_cash_btn)
        cash_assets_row.addStretch()
        panel_layout.addLayout(cash_assets_row)

        # ── 分隔线 ──
        sep = QLabel("—" * 60)
        sep.setStyleSheet("color: #565f89; font-size: 10px;")
        panel_layout.addWidget(sep)

        # ── 新增持仓行 ──
        add_label = QLabel("➕ 新增持仓")
        add_label.setStyleSheet("font-weight: 600; color: #c0caf5;")
        panel_layout.addWidget(add_label)

        add_row = QHBoxLayout()
        add_row.setSpacing(8)

        self.add_symbol = QLineEdit()
        self.add_symbol.setPlaceholderText("代码 (如 600519)")
        self.add_symbol.setFixedWidth(120)

        self.add_name = QLineEdit()
        self.add_name.setPlaceholderText("名称 (如 贵州茅台)")
        self.add_name.setFixedWidth(130)

        self.add_price = QLineEdit()
        self.add_price.setPlaceholderText("成本价")
        self.add_price.setFixedWidth(90)

        self.add_qty = QLineEdit()
        self.add_qty.setPlaceholderText("数量(股)")
        self.add_qty.setFixedWidth(80)

        self.add_position_btn = ModernButton("✅ 添加持仓", success=True)
        self.add_position_btn.setFixedWidth(120)
        self.add_position_btn.clicked.connect(self._add_position_manual)

        add_row.addWidget(QLabel("代码"))
        add_row.addWidget(self.add_symbol)
        add_row.addWidget(QLabel("名称"))
        add_row.addWidget(self.add_name)
        add_row.addWidget(QLabel("成本价"))
        add_row.addWidget(self.add_price)
        add_row.addWidget(QLabel("数量"))
        add_row.addWidget(self.add_qty)
        add_row.addWidget(self.add_position_btn)
        add_row.addStretch()
        panel_layout.addLayout(add_row)

        # ── 删除持仓行 ──
        del_row = QHBoxLayout()
        del_row.setSpacing(8)
        del_label = QLabel("🗑 删除持仓")
        del_label.setStyleSheet("font-weight: 600; color: #f7768e;")

        self.del_symbol = QLineEdit()
        self.del_symbol.setPlaceholderText("代码")
        self.del_symbol.setFixedWidth(120)

        self.del_qty = QLineEdit()
        self.del_qty.setPlaceholderText("全部留空=全清")
        self.del_qty.setFixedWidth(120)

        self.del_btn = ModernButton("🗑 删除", danger=True)
        self.del_btn.setFixedWidth(110)
        self.del_btn.clicked.connect(self._delete_position_manual)

        del_row.addWidget(del_label)
        del_row.addWidget(self.del_symbol)
        del_row.addWidget(QLabel("数量(留空=全清)"))
        del_row.addWidget(self.del_qty)
        del_row.addWidget(self.del_btn)
        del_row.addStretch()
        panel_layout.addLayout(del_row)

        return panel

    def _toggle_editor(self):
        self._editor_visible = not self._editor_visible
        self.editor_panel.setVisible(self._editor_visible)
        self.edit_toggle_btn.setText("✏️ 关闭编辑" if self._editor_visible else "✏️ 手动编辑")
        if self._editor_visible:
            self._load_editor_data()

    def _load_editor_data(self):
        """从 portfolio.json 加载当前数据到编辑面板。"""
        from portfolio.manager import load_portfolio
        pf = load_portfolio(refresh=False)
        self.cash_input.setText(str(pf.get("cash", 0)))
        self.assets_input.setText(str(pf.get("total_assets", 0)))

    def _save_cash_assets(self):
        try:
            from portfolio.manager import load_portfolio, save_portfolio
            pf = load_portfolio(refresh=False)
            new_cash = float(self.cash_input.text().strip())
            new_assets = float(self.assets_input.text().strip()) if self.assets_input.text().strip() else None
            pf["cash"] = new_cash
            if new_assets is not None:
                pf["total_assets"] = new_assets
            save_portfolio(pf)
            QMessageBox.information(self, "成功", f"现金已更新为 ¥{new_cash:,.2f}")
            self.refresh()
        except ValueError:
            QMessageBox.warning(self, "错误", "请输入有效的数字")

    def _add_position_manual(self):
        sym = self.add_symbol.text().strip()
        name = self.add_name.text().strip()
        price_str = self.add_price.text().strip()
        qty_str = self.add_qty.text().strip()

        if not sym:
            QMessageBox.warning(self, "提示", "请输入股票代码")
            return
        try:
            price = float(price_str)
            qty = int(qty_str)
        except ValueError:
            QMessageBox.warning(self, "错误", "成本价和数量必须为数字")
            return

        from portfolio.manager import add_position
        try:
            add_position(sym, price, qty, name=name)
            QMessageBox.information(self, "成功", f"已添加 {sym} {name} ×{qty} @ ¥{price:.2f}")
            self.add_symbol.clear()
            self.add_name.clear()
            self.add_price.clear()
            self.add_qty.clear()
            self.refresh()
        except ValueError as e:
            QMessageBox.warning(self, "错误", str(e))

    def _delete_position_manual(self):
        sym = self.del_symbol.text().strip()
        qty_str = self.del_qty.text().strip()

        if not sym:
            QMessageBox.warning(self, "提示", "请输入要删除的股票代码")
            return

        from portfolio.manager import load_portfolio, save_portfolio
        pf = load_portfolio(refresh=False)
        target = None
        for pos in pf["positions"]:
            if pos["symbol"] == sym:
                target = pos
                break

        if not target:
            QMessageBox.warning(self, "错误", f"未找到 {sym} 的持仓")
            return

        if qty_str:
            try:
                qty = int(qty_str)
                if qty >= target["quantity"]:
                    pf["positions"].remove(target)
                    pf["cash"] += target["current_price"] * target["quantity"]
                else:
                    target["quantity"] -= qty
                    pf["cash"] += target["current_price"] * qty
            except ValueError:
                QMessageBox.warning(self, "错误", "数量必须为整数")
                return
            del_msg = f"已删除 {sym} ×{qty_str}"
        else:
            pf["cash"] += target["current_price"] * target["quantity"]
            pf["positions"].remove(target)
            del_msg = f"已清空 {sym} 全部持仓"

        pf["total_assets"] = pf["cash"] + sum(
            p.get("current_price", p["entry_price"]) * p["quantity"] for p in pf["positions"]
        )
        save_portfolio(pf)
        QMessageBox.information(self, "成功", del_msg)
        self.del_symbol.clear()
        self.del_qty.clear()
        self.refresh()



# ═══════════════════════════════════════════════════════════════
# 页面2：深度分析
# ═══════════════════════════════════════════════════════════════

class AnalysisPage(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("page")
        self._worker = None
        self._thread = None
        self._running = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(16)

        # ── 输入区 ──
        input_row = QHBoxLayout()
        input_row.setSpacing(10)
        symbol_label = QLabel("股票代码:")
        self.symbol_input = QLineEdit()
        self.symbol_input.setPlaceholderText("输入股票代码，如 600519")
        self.symbol_input.setMinimumWidth(140)
        self.symbol_input.setMaximumWidth(200)
        self.symbol_input.setFixedHeight(36)
        self.analyze_btn = ModernButton("🧠 开始深度分析", primary=True)
        self.analyze_btn.setFixedHeight(36)
        self.analyze_btn.setMinimumWidth(150)
        self.exec_btn = ModernButton("👔 执行总裁决策")
        self.exec_btn.setFixedHeight(36)
        self.portfolio_cb = QCheckBox("结合我的持仓")
        self.portfolio_cb.setChecked(True)
        self.adaptive_cb = QCheckBox("自适应参数")
        self.adaptive_cb.setToolTip("加载该股票的自适应策略参数(如存在)")
        input_row.addWidget(symbol_label)
        input_row.addWidget(self.symbol_input)
        input_row.addWidget(self.analyze_btn)
        input_row.addWidget(self.exec_btn)
        input_row.addWidget(self.portfolio_cb)
        input_row.addWidget(self.adaptive_cb)
        input_row.addStretch()
        self.copy_btn = ModernButton("📋 复制报告")
        self.copy_btn.setFixedHeight(36)
        self.copy_btn.setToolTip("复制分析输出内容到剪贴板")
        self.copy_btn.clicked.connect(self._copy_output)
        self.copy_btn.setEnabled(False)
        input_row.addWidget(self.copy_btn)
        layout.addLayout(input_row)

        # ── 进度条 ──
        self.progress = QProgressBar()
        self.progress.setFixedHeight(6)
        self.progress.setTextVisible(False)
        self.progress.setValue(0)
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        # ── 输出区域：使用 QTextEdit 替代逐行 QLabel，防止海量 widget 导致闪退 ──
        self.output_text = QTextEdit()
        self.output_text.setReadOnly(True)
        self.output_text.setPlaceholderText(
            "输入股票代码，点击「开始深度分析」查看完整分析过程。\n"
            "所有 Agent 思考、多空辩论和最终决策将实时输出。")
        self.output_text.setStyleSheet(
            "QTextEdit { background: transparent; color: #c0caf5; font-family: 'Consolas', monospace; "
            "font-size: 12px; border: none; padding: 16px; }")
        layout.addWidget(self.output_text, stretch=1)

        # ── 快捷键 ──
        self.symbol_input.returnPressed.connect(self.start_analysis)

    def _clear_output(self):
        self.copy_btn.setEnabled(False)
        self._running = True
        self.output_text.clear()

    def _append_output(self, text: str):
        if not self._running:
            return
        self.output_text.append(text.rstrip('\n'))

    def set_loading(self, loading: bool):
        self.analyze_btn.setEnabled(not loading)
        self.exec_btn.setEnabled(not loading)
        self.symbol_input.setEnabled(not loading)
        self.progress.setVisible(loading)
        if loading:
            self.progress.setValue(0)
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, 100)
            self.progress.setValue(100)

    def start_analysis(self):
        sym = self.symbol_input.text().strip()
        if not sym:
            QMessageBox.warning(self, "提示", "请输入股票代码")
            return
        self._clear_output()
        self._append_output(f"🚀 开始深度分析: {sym}\n")
        self.set_loading(True)
        self._run_worker("deep", sym)

    def start_executive(self):
        sym = self.symbol_input.text().strip()
        if not sym:
            QMessageBox.warning(self, "提示", "请输入股票代码")
            return
        self._clear_output()
        self._append_output(f"🚀 执行总裁决策: {sym}\n")
        self.set_loading(True)
        self._run_worker("executive", sym)

    def _run_worker(self, mode: str, symbol: str):
        self._current_symbol = symbol
        self._thread = QThread()
        self._worker = AnalysisWorker()
        self._worker.moveToThread(self._thread)

        if mode == "deep":
            use_pf = self.portfolio_cb.isChecked()
            use_ap = self.adaptive_cb.isChecked()
            self._thread.started.connect(lambda: self._worker.run_deep_analysis(symbol, use_pf, use_ap))
        else:
            self._thread.started.connect(lambda: self._worker.run_executive(symbol))

        self._worker.log_signal.connect(self._append_output)
        self._worker.finished.connect(self._on_worker_finished)
        self._worker.error.connect(self._on_worker_error)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()

    def _on_worker_finished(self, result):
        self.set_loading(False)
        self._append_output("\n✅ 分析完成！\n")
        self.copy_btn.setEnabled(True)
        self._running = False
        w = self.window()
        if hasattr(w, 'save_analysis_log'):
            w.save_analysis_log(self._current_symbol, self.output_text.toPlainText())

    def _on_worker_error(self, msg):
        self.set_loading(False)
        self._append_output(f"\n❌ 分析失败: {msg}\n")
        self.copy_btn.setEnabled(True)
        self._running = False
        w = self.window()
        if hasattr(w, 'save_analysis_log'):
            w.save_analysis_log(self._current_symbol, self.output_text.toPlainText())

    def _copy_output(self):
        self.window().copy_to_clipboard(self.output_text)
        if self.output_text.toPlainText().strip():
            self.copy_btn.setText("✅ 已复制")
        QTimer.singleShot(2000, lambda: self.copy_btn.setText("📋 复制报告"))


# ═══════════════════════════════════════════════════════════════
# 页面3：智能选股
# ═══════════════════════════════════════════════════════════════

class ScreeningPage(QFrame):
    analyze_requested = Signal(str)
    quick_adapt_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("page")
        self._worker = None
        self._thread = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(16)

        # ── 控制区 ──
        controls = QHBoxLayout()
        controls.setSpacing(12)
        controls.addWidget(QLabel("筛选范围:"))
        self.scope_combo = QComboBox()
        self.scope_combo.addItems(["沪深300", "中证500"])
        self.scope_combo.setFixedWidth(120)
        controls.addWidget(self.scope_combo)

        controls.addWidget(QLabel("数量:"))
        self.top_spin = QSpinBox()
        self.top_spin.setRange(3, 30)
        self.top_spin.setValue(10)
        self.top_spin.setFixedWidth(70)
        controls.addWidget(self.top_spin)

        self.screen_btn = ModernButton("🎯 开始智能选股", primary=True)
        self.screen_btn.setFixedHeight(36)
        controls.addWidget(self.screen_btn)
        self.screen_copy_btn = ModernButton("📋 复制报告")
        self.screen_copy_btn.setFixedHeight(36)
        self.screen_copy_btn.setToolTip("复制选股结果到剪贴板")
        self.screen_copy_btn.clicked.connect(self._copy_screen_output)
        self.screen_copy_btn.setEnabled(False)
        controls.addWidget(self.screen_copy_btn)
        controls.addStretch()
        layout.addLayout(controls)

        # ── 进度 ──
        self.progress = QProgressBar()
        self.progress.setFixedHeight(6)
        self.progress.setTextVisible(False)
        self.progress.setValue(0)
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        # ── 输出区 ──
        self.screen_output_text = QTextEdit()
        self.screen_output_text.setReadOnly(True)
        self.screen_output_text.setPlaceholderText("选股结果将在此显示...")
        self.screen_output_text.setStyleSheet("QTextEdit { background: transparent; color: #c0caf5; font-family: 'Consolas', monospace; font-size: 12px; border: 1px solid #3b4261; border-radius: 6px; padding: 16px; }")
        layout.addWidget(self.screen_output_text, stretch=1)

        self._results_container = QWidget()
        self._results_container.setStyleSheet("background: transparent;")
        self._results_layout = QVBoxLayout(self._results_container)
        self._results_layout.setContentsMargins(0, 0, 0, 0)
        self._results_layout.setSpacing(4)
        self._results_container.setVisible(False)
        layout.addWidget(self._results_container)
        self._running = False

    def _append(self, text: str):
        if not self._running:
            return
        self.screen_output_text.append(text.rstrip('\n'))

    def _clear(self):
        """清除之前的选股结果。"""
        self._running = True
        self.screen_copy_btn.setEnabled(False)
        self.screen_output_text.clear()
        while self._results_layout.count() > 0:
            item = self._results_layout.takeAt(0)
            if item.widget():
                item.widget().setParent(None)
                item.widget().deleteLater()
        self._results_container.setVisible(False)
        if hasattr(self, 'results_table'):
            self.results_table = None

    def start(self):
        self._clear()
        scope_label = self.scope_combo.currentText()
        scope_map = {"沪深300": "hs300", "中证500": "zz500"}
        scope = scope_map.get(scope_label, "hs300")
        top_n = self.top_spin.value()

        self._append(f"🎯 开始智能选股 — {scope_label} Top {top_n}\n")

        self.screen_btn.setEnabled(False)
        self.screen_btn.setText("⏳ 筛选中...")
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)  # indeterminate

        self._thread = QThread()
        self._worker = AnalysisWorker()
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(lambda: self._worker.run_screening(scope, top_n))
        self._worker.log_signal.connect(self._append)
        self._worker.finished.connect(self._on_screen_done)
        self._worker.error.connect(self._on_screen_error)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()

    def _on_screen_done(self, stocks):
        self.screen_btn.setEnabled(True)
        self.screen_btn.setText("🎯 开始智能选股")
        self.screen_copy_btn.setEnabled(True)
        self.progress.setRange(0, 100)
        self.progress.setValue(100)
        QTimer.singleShot(500, lambda: self.progress.setVisible(False))

        w = self.window()
        if hasattr(w, 'save_analysis_log'):
            scope_label = self.scope_combo.currentText()
            w.save_analysis_log(f"screen_{scope_label}", self.screen_output_text.toPlainText())

        if not stocks:
            self._append("⚠ 未筛选出符合条件的股票\n")
            self._running = False
            return

        # 构建结果表格
        from PySide6.QtWidgets import QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView
        self.results_table = QTableWidget()
        self.results_table.setColumnCount(8)
        self.results_table.setHorizontalHeaderLabels(["代码", "名称", "现价", "评分", "入场价", "止损", "入场类型", "操作"])
        self.results_table.setAlternatingRowColors(True)
        self.results_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.results_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.results_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.results_table.verticalHeader().setVisible(False)
        self.results_table.horizontalHeader().setStretchLastSection(True)
        self.results_table.setSortingEnabled(True)
        self.results_table.setMinimumHeight(200)

        self.results_table.setRowCount(len(stocks))
        for i, s in enumerate(stocks):
            for col, key in enumerate(["symbol", "name", "close", "score",
                                        "entry_price", "stop_loss", "entry_type"]):
                val = s.get(key, "")
                if key in ("close", "entry_price", "stop_loss") and val:
                    val = f"{float(val):.2f}"
                elif key == "score":
                    val = str(val)
                elif key == "entry_type":
                    val = str(val) if val else "-"
                else:
                    val = str(val) if val else ""
                item = QTableWidgetItem(val)
                if key == "symbol":
                    item.setForeground(QColor(DARK['accent']))
                    item.setToolTip("点击跳转深度分析")
                item.setTextAlignment(Qt.AlignCenter)
                self.results_table.setItem(i, col, item)

                # 操作按钮 — 快速适配回测
                adapt_btn = QPushButton("🧬 快速适配回测")
                adapt_btn.setStyleSheet("padding: 4px 10px; font-size: 10px;")
                adapt_btn.setCursor(Qt.PointingHandCursor)
                s_sym = s["symbol"]
                adapt_btn.clicked.connect(lambda checked, sym=s_sym: self._quick_adapt(sym))
                self.results_table.setCellWidget(i, 7, adapt_btn)

        self.results_table.cellDoubleClicked.connect(self._on_table_clicked)
        self.results_table.setFixedHeight(min(len(stocks) * 36 + 30, 400))

        # 添加到结果区域
        self._results_layout.addWidget(self.results_table)
        self._results_container.setVisible(True)

        # DeepSeek 分析明细
        ds_stocks = [s for s in stocks if s.get("deepseek_signal")]
        if ds_stocks:
            from PySide6.QtWidgets import QLabel
            sep = QLabel("\n" + "─" * 40)
            sep.setStyleSheet("color: #565f89;")
            self._results_layout.addWidget(sep)
            header = QLabel("DeepSeek V4 Pro 分析:")
            header.setStyleSheet("font-weight: 600; color: #7aa2f7; font-size: 13px; padding: 4px 0;")
            self._results_layout.addWidget(header)
            for s in ds_stocks:
                sig = s.get("deepseek_signal", "?")
                conf = s.get("deepseek_confidence", 0)
                rationale = s.get("deepseek_rationale", "")[:80]
                line = QLabel(f"  {s.get('symbol','')} {s.get('name','')} → {sig} (置信度{conf:.0%}) {rationale}")
                line.setWordWrap(True)
                line.setStyleSheet("color: #c0caf5; font-size: 12px; padding: 2px 0;")
                self._results_layout.addWidget(line)

        self._running = False

    def _on_screen_error(self, msg):
        self.screen_btn.setEnabled(True)
        self.screen_btn.setText("🎯 开始智能选股")
        self.progress.setVisible(False)
        self.screen_copy_btn.setEnabled(True)
        self._append(f"\n❌ 选股失败: {msg}\n")
        self._running = False
        w = self.window()
        if hasattr(w, 'save_analysis_log'):
            scope_label = self.scope_combo.currentText()
            w.save_analysis_log(f"screen_{scope_label}", self.screen_output_text.toPlainText())

    def _copy_screen_output(self):
        self.window().copy_to_clipboard(self.screen_output_text)
        if self.screen_output_text.toPlainText().strip():
            self.screen_copy_btn.setText("✅ 已复制")
        QTimer.singleShot(2000, lambda: self.screen_copy_btn.setText("📋 复制报告"))

    def _on_table_clicked(self, row, col):
        if not hasattr(self, 'results_table') or not self.results_table:
            return
        item = self.results_table.item(row, 0)
        if item and item.text():
            self.analyze_requested.emit(item.text())

    def _quick_adapt(self, symbol: str):
        self.quick_adapt_requested.emit(symbol)


# ═══════════════════════════════════════════════════════════════
# 页面5：策略进化 (Backtest Lab / Adaptive Migration / Gene Library)
# ═══════════════════════════════════════════════════════════════

class EvolutionPage(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("page")
        self._worker = None
        self._thread = None
        self._genes = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)

        title = SectionTitle("🧬 策略进化中心")
        layout.addWidget(title)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("evolutionTabs")
        self.tabs.setStyleSheet("QTabWidget::pane { border: 1px solid #3b4261; border-radius: 6px; }")

        self.backtest_tab = BacktestLabTab()
        self.adapt_tab = AdaptiveMigrationTab()
        self.gene_tab = GeneLibraryTab()

        self.tabs.addTab(self.backtest_tab, "🔬 回测实验室")
        self.tabs.addTab(self.adapt_tab, "🧬 自适应迁移")
        self.tabs.addTab(self.gene_tab, "📚 策略基因库")

        layout.addWidget(self.tabs, stretch=1)

        # Wire signals
        self.backtest_tab.start_requested.connect(self._start_backtest)
        self.adapt_tab.adapt_requested.connect(self._start_adaptation)
        self.gene_tab.refresh_requested.connect(self._refresh_genes)
        self.gene_tab.delete_requested.connect(self._delete_gene)
        self.gene_tab.load_requested.connect(self._load_gene_to_analysis)

    def _run_worker(self, mode: str, **kwargs):
        self._thread = QThread()
        self._worker = AnalysisWorker()
        self._worker.moveToThread(self._thread)
        if mode == "backtest":
            self._thread.started.connect(
                lambda: self._worker.run_backtest(
                    kwargs['symbol'], kwargs['time_frame'], kwargs['days'], kwargs['max_rounds']))
        elif mode == "adaptation":
            self._thread.started.connect(lambda: self._worker.run_adaptation(kwargs['symbol']))
        self._worker.log_signal.connect(self._on_log)
        self._worker.progress_signal.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._worker.error.connect(self._on_error)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()

    def _start_backtest(self, symbol, time_frame, days, max_rounds):
        self.backtest_tab.set_loading(True)
        self.backtest_tab._clear_results()
        self._run_worker("backtest", symbol=symbol, time_frame=time_frame, days=days, max_rounds=max_rounds)

    def _start_adaptation(self, symbol):
        self.adapt_tab.set_loading(True)
        self.adapt_tab._clear()
        self._run_worker("adaptation", symbol=symbol)

    def _refresh_genes(self):
        self._genes = AnalysisWorker().load_gene_library()
        self.gene_tab._populate(self._genes)

    def _delete_gene(self, filename):
        import os
        project_dir = os.path.dirname(os.path.abspath(__file__))
        fpath = os.path.join(project_dir, filename)
        if os.path.exists(fpath):
            os.remove(fpath)
        self._refresh_genes()

    def _load_gene_to_analysis(self, symbol):
        parent = self.window()
        if hasattr(parent, 'nav'):
            parent.nav.setCurrentRow(1)  # jump to analysis
            parent.analysis.symbol_input.setText(symbol)
            parent.analysis.adaptive_cb.setChecked(True)

    def _on_log(self, text):
        current_tab = self.tabs.currentWidget()
        if hasattr(current_tab, '_append'):
            current_tab._append(text)

    def _on_progress(self, pct, stage):
        self.backtest_tab.progress.setValue(pct)

    def _on_finished(self, result):
        current_tab = self.tabs.currentWidget()
        if current_tab == self.backtest_tab:
            self.backtest_tab.set_loading(False)
            if result and isinstance(result, dict):
                self.backtest_tab._show_results(result)
        elif current_tab == self.adapt_tab:
            self.adapt_tab.set_loading(False)
            if result and isinstance(result, dict):
                self.adapt_tab._show_results(result)
        w = self.window()
        if hasattr(w, 'save_analysis_log') and hasattr(current_tab, 'output_text') and hasattr(current_tab, 'symbol_input'):
            symbol = current_tab.symbol_input.text().strip() or "unknown"
            w.save_analysis_log(symbol, current_tab.output_text.toPlainText())

    def _on_error(self, msg):
        current_tab = self.tabs.currentWidget()
        if hasattr(current_tab, 'set_loading'):
            current_tab.set_loading(False)
        if hasattr(current_tab, '_append'):
            current_tab._append(f"\n❌ 错误: {msg}\n")
            if hasattr(current_tab, '_running'):
                current_tab._running = False
        if hasattr(current_tab, 'copy_btn'):
            current_tab.copy_btn.setEnabled(True)
        w = self.window()
        if hasattr(w, 'save_analysis_log') and hasattr(current_tab, 'output_text') and hasattr(current_tab, 'symbol_input'):
            symbol = current_tab.symbol_input.text().strip() or "unknown"
            w.save_analysis_log(symbol, current_tab.output_text.toPlainText())


# ── 子标签1: 回测实验室 ──

class BacktestLabTab(QFrame):
    start_requested = Signal(str, str, int, int)  # symbol, tf, days, max_rounds

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("page")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(10)

        # Controls
        ctrl = QHBoxLayout()
        ctrl.setSpacing(10)
        ctrl.addWidget(QLabel("股票代码:"))
        self.symbol_input = QLineEdit("600744")
        self.symbol_input.setFixedWidth(100)
        ctrl.addWidget(self.symbol_input)

        ctrl.addWidget(QLabel("时间框架:"))
        self.tf_combo = QComboBox()
        self.tf_combo.addItems(["short", "mid", "long"])
        self.tf_combo.setCurrentIndex(1)
        self.tf_combo.setFixedWidth(80)
        ctrl.addWidget(self.tf_combo)

        ctrl.addWidget(QLabel("天数:"))
        self.days_slider = QSlider(Qt.Horizontal)
        self.days_slider.setRange(30, 500)
        self.days_slider.setValue(180)
        self.days_slider.setFixedWidth(120)
        ctrl.addWidget(self.days_slider)
        self.days_label = QLabel("180")
        self.days_label.setFixedWidth(30)
        self.days_slider.valueChanged.connect(lambda v: self.days_label.setText(str(v)))
        ctrl.addWidget(self.days_label)

        ctrl.addWidget(QLabel("轮数:"))
        self.rounds_spin = QSpinBox()
        self.rounds_spin.setRange(1, 10)
        self.rounds_spin.setValue(3)
        self.rounds_spin.setFixedWidth(60)
        ctrl.addWidget(self.rounds_spin)

        self.start_btn = ModernButton("▶ 开始回测+进化", primary=True)
        self.start_btn.setFixedWidth(150)
        self.start_btn.clicked.connect(self._on_start)
        ctrl.addWidget(self.start_btn)

        self.copy_btn = ModernButton("📋 复制报告")
        self.copy_btn.setFixedWidth(120)
        self.copy_btn.setEnabled(False)
        self.copy_btn.clicked.connect(self._copy_output)
        ctrl.addWidget(self.copy_btn)
        ctrl.addStretch()
        layout.addLayout(ctrl)

        # Progress
        self.progress = QProgressBar()
        self.progress.setFixedHeight(6)
        self.progress.setTextVisible(True)
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        # Output area
        self.output_text = QTextEdit()
        self.output_text.setReadOnly(True)
        self.output_text.setPlaceholderText("回测结果将在此显示...")
        self.output_text.setStyleSheet("QTextEdit { background: transparent; color: #c0caf5; font-family: 'Consolas', monospace; font-size: 12px; border: 1px solid #3b4261; border-radius: 6px; padding: 16px; }")
        layout.addWidget(self.output_text, stretch=1)
        self._running = False

    def _on_start(self):
        sym = self.symbol_input.text().strip()
        if not sym:
            return
        tf = self.tf_combo.currentText()
        days = self.days_slider.value()
        rounds = self.rounds_spin.value()
        self.start_requested.emit(sym, tf, days, rounds)

    def set_loading(self, loading: bool):
        self.start_btn.setEnabled(not loading)
        self.progress.setVisible(loading)

    def _clear_results(self):
        self._running = True
        self.copy_btn.setEnabled(False)
        self.output_text.clear()

    def _append(self, text: str):
        if not self._running:
            return
        self.output_text.append(text.rstrip('\n'))

    def _show_results(self, result):
        rounds = result.get("rounds", [])
        if len(rounds) >= 2:
            first = rounds[0]["backtest_metrics"]
            last = rounds[-1]["backtest_metrics"]
            r1 = first.get("total_return_pct", 0)
            rn = last.get("total_return_pct", 0)
            s1 = first.get("sharpe_ratio", 0)
            sn = last.get("sharpe_ratio", 0)
            delta_ret = rn - r1
            delta_sharpe = sn - s1
            self._append(f"\n📊 改善幅度对比:")
            self._append(f"  第1轮: 收益 {r1:+.2f}%  夏普 {s1:.2f}")
            self._append(f"  最终轮: 收益 {rn:+.2f}%  夏普 {sn:.2f}")
            self._append(f"  Δ收益: {delta_ret:+.2f}%  Δ夏普: {delta_sharpe:+.2f}")
        self.progress.setRange(0, 100)
        self.progress.setValue(100)
        self.copy_btn.setEnabled(True)
        self._running = False

    def _copy_output(self):
        self.window().copy_to_clipboard(self.output_text)
        if self.output_text.toPlainText().strip():
            self.copy_btn.setText("✅ 已复制")
        QTimer.singleShot(2000, lambda: self.copy_btn.setText("📋 复制报告"))


# ── 子标签2: 自适应迁移 ──

class AdaptiveMigrationTab(QFrame):
    adapt_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("page")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(10)

        ctrl = QHBoxLayout()
        ctrl.setSpacing(10)
        ctrl.addWidget(QLabel("目标股票代码:"))
        self.symbol_input = QLineEdit()
        self.symbol_input.setPlaceholderText("如 000001")
        self.symbol_input.setFixedWidth(120)
        ctrl.addWidget(self.symbol_input)

        self.adapt_btn = ModernButton("🧬 提取基因并适配", primary=True)
        self.adapt_btn.setFixedWidth(170)
        self.adapt_btn.clicked.connect(lambda: self.adapt_requested.emit(self.symbol_input.text().strip()))
        ctrl.addWidget(self.adapt_btn)

        self.copy_btn = ModernButton("📋 复制报告")
        self.copy_btn.setFixedWidth(120)
        self.copy_btn.setEnabled(False)
        self.copy_btn.clicked.connect(self._copy_output)
        ctrl.addWidget(self.copy_btn)
        ctrl.addStretch()
        layout.addLayout(ctrl)

        self.progress = QProgressBar()
        self.progress.setFixedHeight(6)
        self.progress.setTextVisible(True)
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        self.output_text = QTextEdit()
        self.output_text.setReadOnly(True)
        self.output_text.setPlaceholderText("自适应迁移结果将在此显示...")
        self.output_text.setStyleSheet("QTextEdit { background: transparent; color: #c0caf5; font-family: 'Consolas', monospace; font-size: 12px; border: 1px solid #3b4261; border-radius: 6px; padding: 16px; }")
        layout.addWidget(self.output_text, stretch=1)

        self._results_container = QWidget()
        self._results_container.setStyleSheet("background: transparent;")
        self._results_layout = QVBoxLayout(self._results_container)
        self._results_layout.setContentsMargins(0, 0, 0, 0)
        self._results_layout.setSpacing(4)
        self._results_container.setVisible(False)
        layout.addWidget(self._results_container)
        self._running = False

    def set_loading(self, loading: bool):
        self.adapt_btn.setEnabled(not loading)
        self.progress.setVisible(loading)

    def _clear(self):
        self._running = True
        self.copy_btn.setEnabled(False)
        self.output_text.clear()
        while self._results_layout.count() > 0:
            item = self._results_layout.takeAt(0)
            if item.widget():
                item.widget().setParent(None)
                item.widget().deleteLater()
        self._results_container.setVisible(False)

    def _append(self, text: str):
        if not self._running:
            return
        self.output_text.append(text.rstrip('\n'))

    def _show_results(self, result):
        base_feats = result.get("base_features", {})
        params = result.get("params", {})
        changes = result.get("param_changes", [])
        rounds = result.get("rounds", [])

        # Feature comparison table
        if base_feats:
            self._append("\n📊 特征向量对比:")
            table = QTableWidget()
            table.setColumnCount(2)
            table.setHorizontalHeaderLabels(["特征", "值"])
            table.horizontalHeader().setStretchLastSection(True)
            feats_to_show = [
                ("年化波动率", f"{base_feats.get('annual_volatility_pct','?')}%"),
                ("熊市占比", f"{base_feats.get('market_state',{}).get('bear_ratio_pct','?')}%"),
                ("牛市占比", f"{base_feats.get('market_state',{}).get('bull_ratio_pct','?')}%"),
                ("行业", base_feats.get("industry", "?")),
                ("市值级别", base_feats.get("market_cap_level", "?")),
            ]
            table.setRowCount(len(feats_to_show))
            for i, (k, v) in enumerate(feats_to_show):
                table.setItem(i, 0, QTableWidgetItem(k))
                table.setItem(i, 1, QTableWidgetItem(str(v)))
            table.setFixedHeight(150)
            self._results_layout.addWidget(table)
            self._results_container.setVisible(True)

        # Parameter diff table
        if changes:
            self._append("\n🔧 参数调整明细:")
            diff_table = QTableWidget()
            diff_table.setColumnCount(4)
            diff_table.setHorizontalHeaderLabels(["参数", "原值", "新值", "调整理由"])
            diff_table.horizontalHeader().setStretchLastSection(True)
            diff_table.setRowCount(len(changes))
            for i, c in enumerate(changes):
                diff_table.setItem(i, 0, QTableWidgetItem(c.get("param", "")))
                diff_table.setItem(i, 1, QTableWidgetItem(str(c.get("base_value", ""))))
                diff_table.setItem(i, 2, QTableWidgetItem(str(c.get("new_value", ""))))
                diff_table.setItem(i, 3, QTableWidgetItem(c.get("reason", "")[:80]))
            diff_table.setFixedHeight(min(len(changes) * 30 + 35, 200))
            self._results_layout.addWidget(diff_table)
            self._results_container.setVisible(True)

        # Last round metrics
        if rounds:
            m = rounds[-1].get("backtest_metrics", {})
            self._append(f"\n📈 回测关键指标: 收益 {m.get('total_return_pct', 0):+.2f}% | "
                        f"夏普 {m.get('sharpe_ratio', 0):.2f} | 胜率 {m.get('win_rate_pct', 0):.1f}%")
            self._append(f"最大回撤: {m.get('max_drawdown_pct', 0):.2f}% | "
                        f"三线达成: 短{m.get('achievement_short', 0):.0f}% 中{m.get('achievement_mid', 0):.0f}% 长{m.get('achievement_long', 0):.0f}%")

        adjudication = result.get("adaptation_rationale", "")
        if adjudication:
            self._append(f"\n💬 Critic 点评: {adjudication[:120]}")
        self.copy_btn.setEnabled(True)
        self._running = False

    def _copy_output(self):
        self.window().copy_to_clipboard(self.output_text)
        if self.output_text.toPlainText().strip():
            self.copy_btn.setText("✅ 已复制")
        QTimer.singleShot(2000, lambda: self.copy_btn.setText("📋 复制报告"))


# ── 子标签3: 策略基因库 ──

class GeneLibraryTab(QFrame):
    refresh_requested = Signal()
    delete_requested = Signal(str)
    load_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("page")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(10)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(10)
        self.refresh_btn = ModernButton("🔄 刷新", primary=True)
        self.refresh_btn.clicked.connect(lambda: self.refresh_requested.emit())
        toolbar.addWidget(self.refresh_btn)
        toolbar.addStretch()
        layout.addLayout(toolbar)

        self.table = QTableWidget()
        self.table.setObjectName("card")
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(["股票代码", "保存日期", "止损(短/中/长ATR)", "仓位上限(短/中/长)", "信号阈值", "操作"])
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table, stretch=1)

    def _populate(self, genes):
        self.table.setRowCount(0)
        self.table.setRowCount(len(genes))
        for i, g in enumerate(genes):
            sp = g.get("params", {}).get("short_term", {})
            mp = g.get("params", {}).get("mid_term", {})
            lp = g.get("params", {}).get("long_term", {})
            atr_summary = f"{sp.get('atr_stop_multiplier','?')} / {mp.get('atr_stop_multiplier','?')} / {lp.get('atr_stop_multiplier','?')}"
            pos_summary = f"{sp.get('position_pct','?')}% / {mp.get('position_pct','?')}% / {lp.get('position_pct','?')}%"
            conf_summary = f"短{sp.get('confidence_min','?')} 中{mp.get('confidence_min','?')} 长{lp.get('confidence_min','?')}"

            items = [
                (g["symbol"], None),
                (g.get("adapted_at", "")[:10], None),
                (atr_summary, None),
                (pos_summary, None),
                (conf_summary, None),
            ]
            for col, (text, _) in enumerate(items):
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(i, col, item)

            # Action buttons
            btn_widget = QWidget()
            btn_layout = QHBoxLayout(btn_widget)
            btn_layout.setContentsMargins(4, 2, 4, 2)
            btn_layout.setSpacing(6)

            load_btn = QPushButton("加载")
            load_btn.setStyleSheet("padding: 3px 10px; font-size: 11px;")
            load_btn.setCursor(Qt.PointingHandCursor)
            sym = g["symbol"]
            load_btn.clicked.connect(lambda checked, s=sym: self.load_requested.emit(s))
            btn_layout.addWidget(load_btn)

            del_btn = QPushButton("删除")
            del_btn.setStyleSheet("padding: 3px 10px; font-size: 11px; color: #f7768e;")
            del_btn.setCursor(Qt.PointingHandCursor)
            fname = g["file"]
            del_btn.clicked.connect(lambda checked, f=fname: self.delete_requested.emit(f))
            btn_layout.addWidget(del_btn)

            self.table.setCellWidget(i, 5, btn_widget)

class SettingsPage(QFrame):
    theme_toggled = Signal(bool)  # True=dark, False=light
    scheduler_toggled = Signal(bool)
    mail_listener_toggled = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("page")
        self._dark_mode = True
        self._scheduler_running = False

        from utils.config import load_config, set_config_value, get_config_value
        from PySide6.QtWidgets import QScrollArea, QSizePolicy as QSP

        # ── 最外层布局（0边距，确保 ScrollArea 填满整个页面）──
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ── 滚动区域 ──
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        # ── 滚动内容容器 ──
        scroll_content = QWidget()
        scroll_content.setObjectName("settingsScroll")
        scroll_content.setStyleSheet("#settingsScroll { background: transparent; }")

        # ── 主布局：垂直排列所有模块 ──
        layout = QVBoxLayout(scroll_content)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(24)

        # ── 辅助函数：创建标准的行标签 ──
        def _make_label(text, fixed_width=100):
            lbl = QLabel(text)
            lbl.setFixedWidth(fixed_width)
            lbl.setStyleSheet("color: inherit; font-size: 13px;")
            return lbl

        # ═══════════════════════════════════════════════════════
        # 模块1：API 配置
        # ═══════════════════════════════════════════════════════
        api_group = QGroupBox("🔑 API 配置 (DeepSeek V4 Pro)")
        api_layout = QVBoxLayout(api_group)
        api_layout.setContentsMargins(16, 16, 16, 16)
        api_layout.setSpacing(12)

        self.api_status = QLabel()
        self._refresh_api_status()
        api_layout.addWidget(self.api_status)

        # Base URL
        url_row = QHBoxLayout(); url_row.setSpacing(10)
        url_row.addWidget(_make_label("API 地址:"))
        self.api_url_input = QLineEdit()
        self.api_url_input.setPlaceholderText("https://api.deepseek.com")
        self.api_url_input.setMinimumHeight(32)
        saved_url = get_config_value("DEEPSEEK_BASE_URL")
        if saved_url:
            self.api_url_input.setText(saved_url)
        url_row.addWidget(self.api_url_input, stretch=1)
        api_layout.addLayout(url_row)

        # Model
        model_row = QHBoxLayout(); model_row.setSpacing(10)
        model_row.addWidget(_make_label("模型名称:"))
        self.api_model_input = QLineEdit()
        self.api_model_input.setPlaceholderText("deepseek-v4-pro")
        self.api_model_input.setMinimumHeight(32)
        saved_model = get_config_value("DEEPSEEK_MODEL")
        if saved_model:
            self.api_model_input.setText(saved_model)
        self.api_model_input.setToolTip("DeepSeek: deepseek-v4-pro / deepseek-chat\n硅基流动: deepseek-ai/DeepSeek-V3\n本地部署: 自定义模型名")
        model_row.addWidget(self.api_model_input, stretch=1)
        api_layout.addLayout(model_row)

        # API Key
        key_row = QHBoxLayout(); key_row.setSpacing(10)
        key_row.addWidget(_make_label("API Key:"))
        self.api_key_input = QLineEdit()
        self.api_key_input.setPlaceholderText("输入 DeepSeek API Key (sk-...)")
        self.api_key_input.setEchoMode(QLineEdit.Password)
        self.api_key_input.setMinimumHeight(32)
        current_key = get_config_value("DEEPSEEK_API_KEY")
        if current_key:
            self.api_key_input.setText(current_key)
        self.api_show_btn = QPushButton("👁")
        self.api_show_btn.setFixedSize(32, 32)
        self.api_show_btn.setCursor(Qt.PointingHandCursor)
        self.api_show_btn.setStyleSheet("QPushButton { border: none; background: transparent; font-size: 16px; }")
        self.api_show_btn.clicked.connect(lambda: self._toggle_password_visible(self.api_key_input, self.api_show_btn))
        key_row.addWidget(self.api_key_input, stretch=1)
        key_row.addWidget(self.api_show_btn)
        api_layout.addLayout(key_row)

        # Temperature + Max Tokens + Timeout
        params_row = QHBoxLayout(); params_row.setSpacing(10)
        params_row.addWidget(_make_label("温度:", 50))
        self.api_temp_spin = QDoubleSpinBox()
        self.api_temp_spin.setRange(0.0, 2.0)
        self.api_temp_spin.setSingleStep(0.1)
        self.api_temp_spin.setDecimals(1)
        saved_temp = get_config_value("DEEPSEEK_TEMPERATURE")
        self.api_temp_spin.setValue(float(saved_temp) if saved_temp else 0.7)
        self.api_temp_spin.setFixedWidth(70)
        self.api_temp_spin.setMinimumHeight(32)
        params_row.addWidget(self.api_temp_spin)

        params_row.addWidget(_make_label("最大Token:", 75))
        self.api_tokens_spin = QSpinBox()
        self.api_tokens_spin.setRange(512, 32768)
        self.api_tokens_spin.setSingleStep(512)
        saved_tokens = get_config_value("DEEPSEEK_MAX_TOKENS")
        self.api_tokens_spin.setValue(int(saved_tokens) if saved_tokens else 8192)
        self.api_tokens_spin.setFixedWidth(90)
        self.api_tokens_spin.setMinimumHeight(32)
        params_row.addWidget(self.api_tokens_spin)

        params_row.addWidget(_make_label("超时(s):", 60))
        self.api_timeout_spin = QSpinBox()
        self.api_timeout_spin.setRange(30, 600)
        self.api_timeout_spin.setSingleStep(10)
        saved_timeout = get_config_value("DEEPSEEK_TIMEOUT")
        self.api_timeout_spin.setValue(int(saved_timeout) if saved_timeout else 30)
        self.api_timeout_spin.setFixedWidth(70)
        self.api_timeout_spin.setMinimumHeight(32)
        params_row.addWidget(self.api_timeout_spin)
        params_row.addStretch()
        api_layout.addLayout(params_row)

        # 按钮行：预设 + 测试 + 保存
        btn_row = QHBoxLayout(); btn_row.setSpacing(10)
        self.preset_deepseek_btn = ModernButton("🔄 DeepSeek官方")
        self.preset_deepseek_btn.clicked.connect(self._preset_deepseek)
        btn_row.addWidget(self.preset_deepseek_btn)
        self.preset_silicon_btn = ModernButton("🔬 硅基流动")
        self.preset_silicon_btn.clicked.connect(self._preset_siliconflow)
        btn_row.addWidget(self.preset_silicon_btn)
        btn_row.addStretch()
        self.api_test_btn = ModernButton("🔗 测试连接")
        self.api_test_btn.clicked.connect(self._test_api_connection)
        btn_row.addWidget(self.api_test_btn)
        self.api_save_btn = ModernButton("💾 保存配置", primary=True)
        self.api_save_btn.clicked.connect(self._save_api_config)
        btn_row.addWidget(self.api_save_btn)
        api_layout.addLayout(btn_row)

        api_note = QLabel("配置保存在本地 config.json。支持所有兼容 OpenAI 接口的 API 服务（DeepSeek、硅基流动、本地部署等）。")
        api_note.setObjectName("cardTitle")
        api_note.setWordWrap(True)
        api_layout.addWidget(api_note)
        layout.addWidget(api_group)

        # ═══════════════════════════════════════════════════════
        # 模块2：邮件配置
        # ═══════════════════════════════════════════════════════
        mail_group = QGroupBox("📧 邮件配置 (QQ邮箱)")
        mail_layout = QVBoxLayout(mail_group)
        mail_layout.setContentsMargins(16, 16, 16, 16)
        mail_layout.setSpacing(12)

        self.mail_status = QLabel()
        self._refresh_mail_status()
        mail_layout.addWidget(self.mail_status)

        addr_row = QHBoxLayout(); addr_row.setSpacing(10)
        addr_row.addWidget(_make_label("邮箱地址:"))
        self.mail_addr_input = QLineEdit()
        self.mail_addr_input.setPlaceholderText("your_email@qq.com")
        self.mail_addr_input.setMinimumHeight(32)
        saved_addr = get_config_value("EMAIL_ADDRESS")
        if saved_addr:
            self.mail_addr_input.setText(saved_addr)
        addr_row.addWidget(self.mail_addr_input, stretch=1)
        mail_layout.addLayout(addr_row)

        pwd_row = QHBoxLayout(); pwd_row.setSpacing(10)
        pwd_row.addWidget(_make_label("授权码:"))
        self.mail_pwd_input = QLineEdit()
        self.mail_pwd_input.setPlaceholderText("QQ邮箱授权码 (在QQ邮箱设置→账户中生成)")
        self.mail_pwd_input.setEchoMode(QLineEdit.Password)
        self.mail_pwd_input.setMinimumHeight(32)
        saved_pwd = get_config_value("EMAIL_PASSWORD")
        if saved_pwd:
            self.mail_pwd_input.setText(saved_pwd)
        self.mail_pwd_show_btn = QPushButton("👁")
        self.mail_pwd_show_btn.setFixedSize(32, 32)
        self.mail_pwd_show_btn.setCursor(Qt.PointingHandCursor)
        self.mail_pwd_show_btn.setStyleSheet("QPushButton { border: none; background: transparent; font-size: 16px; }")
        self.mail_pwd_show_btn.clicked.connect(lambda: self._toggle_password_visible(self.mail_pwd_input, self.mail_pwd_show_btn))
        pwd_row.addWidget(self.mail_pwd_input, stretch=1)
        pwd_row.addWidget(self.mail_pwd_show_btn)
        mail_layout.addLayout(pwd_row)

        recip_row = QHBoxLayout(); recip_row.setSpacing(10)
        recip_row.addWidget(_make_label("报告收件人:"))
        self.recip_input = QLineEdit()
        self.recip_input.setPlaceholderText("接收每日报告的邮箱 (默认同发件地址)")
        self.recip_input.setMinimumHeight(32)
        saved_recip = get_config_value("REPORT_RECIPIENT")
        if saved_recip:
            self.recip_input.setText(saved_recip)
        recip_row.addWidget(self.recip_input, stretch=1)
        mail_layout.addLayout(recip_row)

        # 邮件按钮行
        mail_btn_row = QHBoxLayout(); mail_btn_row.setSpacing(10)
        self.mail_save_btn = ModernButton("💾 保存邮件配置", primary=True)
        self.mail_save_btn.clicked.connect(self._save_mail_config)
        self.mail_test_btn = ModernButton("📧 测试发送")
        self.mail_test_btn.clicked.connect(self._test_mail)
        mail_btn_row.addWidget(self.mail_save_btn)
        mail_btn_row.addWidget(self.mail_test_btn)
        mail_btn_row.addStretch()
        mail_layout.addLayout(mail_btn_row)

        # 邮件监听
        listener_row = QHBoxLayout(); listener_row.setSpacing(10)
        self.listener_status = QLabel("邮件监听: 未启动")
        self.listener_toggle = ModernButton("▶ 启动邮件监听", primary=True)
        self.listener_toggle.clicked.connect(self._toggle_mail_listener_local)
        self._listener_running = False
        listener_row.addWidget(self.listener_status)
        listener_row.addWidget(self.listener_toggle)
        listener_row.addStretch()
        mail_layout.addLayout(listener_row)

        mail_note = QLabel("使用 QQ邮箱 SMTP (smtp.qq.com:465 SSL)。授权码在 QQ邮箱网页版「设置→账户→POP3/SMTP服务」中生成。\n"
                          "邮件监听启动后，每60秒自动检查未读邮件中的 [StockMind] 指令。")
        mail_note.setObjectName("cardTitle")
        mail_note.setWordWrap(True)
        mail_layout.addWidget(mail_note)
        layout.addWidget(mail_group)

        # ═══════════════════════════════════════════════════════
        # 模块3：定时任务
        # ═══════════════════════════════════════════════════════
        sched_group = QGroupBox("⏰ 定时任务")
        sched_layout = QVBoxLayout(sched_group)
        sched_layout.setContentsMargins(16, 16, 16, 16)
        sched_layout.setSpacing(12)

        self.sched_status = QLabel("状态: 未启动")
        self.sched_toggle = ModernButton("▶ 启动调度器", primary=True)
        self.sched_toggle.clicked.connect(self._toggle_scheduler)
        sched_layout.addWidget(self.sched_status)
        sched_layout.addWidget(self.sched_toggle)

        sched_info = QLabel("每日 09:30 开盘简报 | 11:30 午盘回顾 | 15:00 收盘总结\n定时刷新持仓 → 智能选股 → 发送邮件报告")
        sched_info.setObjectName("cardTitle")
        sched_info.setWordWrap(True)
        sched_layout.addWidget(sched_info)
        layout.addWidget(sched_group)

        # ═══════════════════════════════════════════════════════
        # 模块4：外观
        # ═══════════════════════════════════════════════════════
        theme_group = QGroupBox("🎨 外观")
        theme_layout = QVBoxLayout(theme_group)
        theme_layout.setContentsMargins(16, 16, 16, 16)
        theme_layout.setSpacing(12)

        self.theme_status = QLabel("当前主题: 暗黑模式")
        self.theme_toggle_btn = ModernButton("☀️ 切换亮色主题")
        self.theme_toggle_btn.clicked.connect(self._toggle_theme)
        theme_layout.addWidget(self.theme_status)
        theme_layout.addWidget(self.theme_toggle_btn)
        layout.addWidget(theme_group)

        # ═══════════════════════════════════════════════════════
        # 模块5：快捷方式
        # ═══════════════════════════════════════════════════════
        shortcut_group = QGroupBox("📌 快捷方式")
        shortcut_layout = QVBoxLayout(shortcut_group)
        shortcut_layout.setContentsMargins(16, 16, 16, 16)
        shortcut_layout.setSpacing(12)

        shortcut_btn_row = QHBoxLayout(); shortcut_btn_row.setSpacing(10)
        self.create_shortcut_btn = ModernButton("🖥 创建桌面快捷方式", primary=True)
        self.create_shortcut_btn.clicked.connect(self._create_desktop_shortcut)
        self.shortcut_status = QLabel("")
        self.shortcut_status.setObjectName("cardTitle")
        self.shortcut_status.setWordWrap(True)
        shortcut_btn_row.addWidget(self.create_shortcut_btn)
        shortcut_btn_row.addWidget(self.shortcut_status, stretch=1)
        shortcut_btn_row.addStretch()
        shortcut_layout.addLayout(shortcut_btn_row)

        QTimer.singleShot(500, self._check_shortcut_exists)
        layout.addWidget(shortcut_group)

        # ═══════════════════════════════════════════════════════
        # 模块6：关于
        # ═══════════════════════════════════════════════════════
        about_group = QGroupBox("ℹ️ 关于")
        about_layout = QVBoxLayout(about_group)
        about_layout.setContentsMargins(16, 16, 16, 16)
        about_layout.setSpacing(12)

        about_lbl = QLabel("StockMind v2.0 — 多Agent 深度股析系统\n"
                           "基于 PySide6 + DeepSeek V4 Pro\n"
                           "数据来源: 腾讯行情 | ifzq K线 | 东方财富新闻/财务")
        about_lbl.setWordWrap(True)
        about_layout.addWidget(about_lbl)
        layout.addWidget(about_group)

        # ── 底部弹性空间 ──
        layout.addStretch()

        # ── 组装滚动区域 ──
        scroll.setWidget(scroll_content)
        outer.addWidget(scroll)

    def _create_desktop_shortcut(self):
        import os
        import sys
        import pythoncom
        from win32com.client import Dispatch
        try:
            desktop = os.path.join(os.path.expanduser("~"), "Desktop")
            shortcut_path = os.path.join(desktop, "StockMind.lnk")

            if getattr(sys, 'frozen', False):
                target_path = sys.executable
                work_dir = os.path.dirname(sys.executable)
            else:
                target_path = sys.executable
                work_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                # 使用 pythonw 启动 desktop_app.py (无控制台窗口)
                script_path = os.path.join(work_dir, "desktop_app.py")

            pythoncom.CoInitialize()
            shell = Dispatch('WScript.Shell')
            shortcut = shell.CreateShortCut(shortcut_path)
            shortcut.Targetpath = target_path
            if getattr(sys, 'frozen', False):
                shortcut.Arguments = ""
            else:
                shortcut.Arguments = f'"{script_path}"'
            shortcut.WorkingDirectory = work_dir
            shortcut.IconLocation = target_path if getattr(sys, 'frozen', False) else os.path.join(work_dir, "assets", "stock.ico")
            shortcut.Description = "StockMind - 多Agent深度股析系统"
            shortcut.save()
            pythoncom.CoUninitialize()

            self.shortcut_status.setText("✅ 桌面快捷方式已创建")
            self.shortcut_status.setStyleSheet("color: #9ece6a;")
            QMessageBox.information(self, "成功", f"桌面快捷方式已创建:\n{shortcut_path}")
        except Exception as e:
            self.shortcut_status.setText(f"❌ 创建失败: {e}")
            self.shortcut_status.setStyleSheet("color: #f7768e;")
            QMessageBox.warning(self, "失败", f"创建快捷方式失败:\n{e}\n\n请确认已安装 pywin32 模块。")

    def _check_shortcut_exists(self):
        import os
        desktop = os.path.join(os.path.expanduser("~"), "Desktop")
        shortcut_path = os.path.join(desktop, "StockMind.lnk")
        if os.path.exists(shortcut_path):
            self.shortcut_status.setText("✅ 桌面快捷方式已存在")
            self.shortcut_status.setStyleSheet("color: #9ece6a;")
        else:
            self.shortcut_status.setText("💡 点击左侧按钮在桌面创建快捷方式")
            self.shortcut_status.setStyleSheet("color: #7982a9;")

    def _toggle_password_visible(self, input_widget, toggle_btn):
        if input_widget.echoMode() == QLineEdit.Password:
            input_widget.setEchoMode(QLineEdit.Normal)
            toggle_btn.setText("🙈")
        else:
            input_widget.setEchoMode(QLineEdit.Password)
            toggle_btn.setText("👁")

    def _refresh_api_status(self):
        from utils.config import get_config_value
        key = get_config_value("DEEPSEEK_API_KEY")
        url = get_config_value("DEEPSEEK_BASE_URL") or "https://api.deepseek.com"
        model = get_config_value("DEEPSEEK_MODEL") or "deepseek-v4-pro"
        if key:
            masked = key[:8] + "…" + key[-4:] if len(key) > 12 else "已设置"
            self.api_status.setText(f"✅ {model} @ {url} | Key: {masked}")
            self.api_status.setObjectName("statusOk")
        else:
            self.api_status.setText(f"❌ 未设置 API Key | {model} @ {url}")
            self.api_status.setObjectName("statusError")
        self.api_status.style().unpolish(self.api_status)
        self.api_status.style().polish(self.api_status)

    def _save_api_config(self):
        from utils.config import set_config_value
        from data.deepseek import reload_config
        url = self.api_url_input.text().strip()
        model = self.api_model_input.text().strip()
        key = self.api_key_input.text().strip()
        temperature = self.api_temp_spin.value()
        max_tokens = self.api_tokens_spin.value()
        timeout = self.api_timeout_spin.value()

        if not key:
            QMessageBox.warning(self, "提示", "请输入 API Key")
            return

        set_config_value("DEEPSEEK_BASE_URL", url or "https://api.deepseek.com")
        set_config_value("DEEPSEEK_MODEL", model or "deepseek-v4-pro")
        set_config_value("DEEPSEEK_API_KEY", key)
        set_config_value("DEEPSEEK_TEMPERATURE", str(temperature))
        set_config_value("DEEPSEEK_MAX_TOKENS", str(max_tokens))
        set_config_value("DEEPSEEK_TIMEOUT", str(timeout))
        reload_config()
        self._refresh_api_status()
        QMessageBox.information(self, "成功", "API 配置已保存并生效")

    def _test_api_connection(self):
        self.api_test_btn.setEnabled(False)
        self.api_test_btn.setText("⏳ 测试中...")
        from utils.config import set_config_value
        from data.deepseek import reload_config
        # 先临时保存当前配置
        url = self.api_url_input.text().strip()
        model = self.api_model_input.text().strip()
        key = self.api_key_input.text().strip()
        if not key:
            QMessageBox.warning(self, "提示", "请先输入 API Key")
            self.api_test_btn.setEnabled(True)
            self.api_test_btn.setText("🔗 测试连接")
            return
        set_config_value("DEEPSEEK_BASE_URL", url or "https://api.deepseek.com")
        set_config_value("DEEPSEEK_MODEL", model or "deepseek-v4-pro")
        set_config_value("DEEPSEEK_API_KEY", key)
        reload_config()

        def _test():
            from data.deepseek import deepseek_chat
            try:
                reply = deepseek_chat("用一句话回答。", "请说：连接成功")
                return True, reply[:200]
            except Exception as e:
                return False, str(e)

        import threading
        def _run_test():
            ok, msg = _test()
            self.api_test_btn.setEnabled(True)
            self.api_test_btn.setText("🔗 测试连接")
            if ok:
                QMessageBox.information(self, "连接成功", f"API 响应正常:\n{msg}")
            else:
                QMessageBox.warning(self, "连接失败", f"API 请求失败:\n{msg}\n\n请检查:\n1. API 地址是否正确\n2. API Key 是否有效\n3. 网络连接是否正常")

        t = threading.Thread(target=_run_test, daemon=True)
        t.start()

    def _preset_deepseek(self):
        self.api_url_input.setText("https://api.deepseek.com")
        self.api_model_input.setText("deepseek-v4-pro")
        QMessageBox.information(self, "已切换", "已设置为 DeepSeek 官方 API\n地址: https://api.deepseek.com\n模型: deepseek-v4-pro")

    def _preset_siliconflow(self):
        self.api_url_input.setText("https://api.siliconflow.cn/v1")
        self.api_model_input.setText("deepseek-ai/DeepSeek-V3")
        QMessageBox.information(self, "已切换", "已设置为硅基流动 API\n地址: https://api.siliconflow.cn/v1\n模型: deepseek-ai/DeepSeek-V3\n请填入硅基流动的 API Key")

    def _refresh_mail_status(self):
        from utils.config import get_config_value
        addr = get_config_value("EMAIL_ADDRESS")
        pwd = get_config_value("EMAIL_PASSWORD")
        if addr and pwd:
            self.mail_status.setText(f"QQ邮箱: ✅ {addr}")
            self.mail_status.setObjectName("statusOk")
        elif addr:
            self.mail_status.setText(f"QQ邮箱: ⚠ 已设地址，未设授权码")
            self.mail_status.setObjectName("statusWarn")
        else:
            self.mail_status.setText("QQ邮箱: ❌ 未配置")
            self.mail_status.setObjectName("statusError")
        self.mail_status.style().unpolish(self.mail_status)
        self.mail_status.style().polish(self.mail_status)

    def _save_mail_config(self):
        from utils.config import set_config_value
        addr = self.mail_addr_input.text().strip()
        pwd = self.mail_pwd_input.text().strip()
        recip = self.recip_input.text().strip()

        if not addr:
            QMessageBox.warning(self, "提示", "请输入QQ邮箱地址")
            return

        set_config_value("EMAIL_ADDRESS", addr)
        set_config_value("EMAIL_PASSWORD", pwd)
        if recip:
            set_config_value("REPORT_RECIPIENT", recip)
        self._refresh_mail_status()
        QMessageBox.information(self, "成功", "邮件配置已保存")

    def _test_mail(self):
        self.mail_test_btn.setEnabled(False)
        self.mail_test_btn.setText("⏳ 发送中...")
        from mail.sender import send_test_email
        ok = send_test_email()
        self.mail_test_btn.setEnabled(True)
        self.mail_test_btn.setText("📧 测试发送")
        if ok:
            QMessageBox.information(self, "成功", "测试邮件已发送，请检查收件箱。")
        else:
            QMessageBox.warning(self, "失败", "邮件发送失败，请检查：\n"
                                "1. QQ邮箱地址是否正确\n"
                                "2. 授权码是否正确（不是QQ密码）\n"
                                "3. QQ邮箱是否已开启 SMTP 服务")

    def _toggle_mail_listener_local(self):
        from utils.config import get_config_value
        addr = get_config_value("EMAIL_ADDRESS")
        pwd = get_config_value("EMAIL_PASSWORD")
        if not addr or not pwd:
            QMessageBox.warning(self, "提示", "请先配置并保存QQ邮箱地址和授权码")
            return
        self._listener_running = not self._listener_running
        if self._listener_running:
            self.listener_status.setText("邮件监听: ✅ 运行中 (每60秒)")
            self.listener_status.setObjectName("statusOk")
            self.listener_toggle.setText("⏹ 停止邮件监听")
            self.listener_toggle.setObjectName("btnDanger")
        else:
            self.listener_status.setText("邮件监听: ⏸ 已停止")
            self.listener_status.setObjectName("statusWarn")
            self.listener_toggle.setText("▶ 启动邮件监听")
            self.listener_toggle.setObjectName("btnPrimary")
        self.listener_toggle.style().unpolish(self.listener_toggle)
        self.listener_toggle.style().polish(self.listener_toggle)
        self.listener_status.style().unpolish(self.listener_status)
        self.listener_status.style().polish(self.listener_status)
        self.mail_listener_toggled.emit(self._listener_running)

    def _toggle_theme(self):
        self._dark_mode = not self._dark_mode
        self.theme_status.setText(f"当前主题: {'暗黑模式' if self._dark_mode else '亮色模式'}")
        self.theme_toggle_btn.setText(f"{'🌙' if not self._dark_mode else '☀️'} 切换{'亮色' if self._dark_mode else '暗黑'}主题")
        self.theme_toggled.emit(self._dark_mode)

    def _toggle_scheduler(self):
        self._scheduler_running = not self._scheduler_running
        if self._scheduler_running:
            self.sched_status.setText("状态: ✅ 运行中 (09:30 / 11:30 / 15:00)")
            self.sched_status.setObjectName("statusOk")
            self.sched_toggle.setText("⏹ 停止调度器")
            self.sched_toggle.setObjectName("btnDanger")
        else:
            self.sched_status.setText("状态: ⏸ 已停止")
            self.sched_status.setObjectName("statusWarn")
            self.sched_toggle.setText("▶ 启动调度器")
            self.sched_toggle.setObjectName("btnPrimary")
        self.sched_toggle.style().unpolish(self.sched_toggle)
        self.sched_toggle.style().polish(self.sched_toggle)
        self.sched_status.style().unpolish(self.sched_status)
        self.sched_status.style().polish(self.sched_status)
        self.scheduler_toggled.emit(self._scheduler_running)
        self.scheduler_toggled.emit(self._scheduler_running)


# ═══════════════════════════════════════════════════════════════
# 主窗口
# ═══════════════════════════════════════════════════════════════

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self._dark = True
        self._tray_icon = None
        self._scheduler = None
        self._scheduler_running = False
        self._mail_listener_thread = None
        self._mail_listener_running = False

        # 创建分析日志目录（剪贴板兜底的第一保障）
        self._analysis_logs_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "analysis_logs")
        os.makedirs(self._analysis_logs_dir, exist_ok=True)

        # 窗口属性
        self.setWindowTitle("StockMind")
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowSystemMenuHint | Qt.WindowMinMaxButtonsHint)
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        self.resize(1100, 760)
        self.setMinimumSize(800, 600)
        self._border_width = 8  # 边框可拖拽宽度

        # 中心 widget
        central = QWidget()
        central.setObjectName("centralFrame")
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # ── 标题栏 ──
        self.title_bar = TitleBar(self)
        self.title_bar.theme_btn.clicked.connect(self._toggle_theme)
        main_layout.addWidget(self.title_bar)

        # ── 内容区 ──
        content = QWidget()
        content.setObjectName("centralFrame")
        content_layout = QHBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)

        # ── 导航栏 ──
        self.nav = QListWidget()
        self.nav.setObjectName("navList")
        self.nav.setFixedWidth(160)
        self.nav.setFocusPolicy(Qt.NoFocus)

        nav_items = [
            ("📊  总览", 0),
            ("🔍  深度分析", 1),
            ("💡  智能选股", 2),
            ("🧬  策略进化", 3),
            ("⚙️  设置", 4),
        ]
        for text, idx in nav_items:
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, idx)
            self.nav.addItem(item)
        self.nav.setCurrentRow(0)
        self.nav.currentRowChanged.connect(self._switch_page)
        content_layout.addWidget(self.nav)

        # ── 页面栈 ──
        self.stack = QStackedWidget()
        self.stack.setObjectName("centralFrame")
        self.overview = OverviewPage()
        self.analysis = AnalysisPage()
        self.screening = ScreeningPage()
        self.evolution = EvolutionPage()
        self.settings = SettingsPage()

        self.stack.addWidget(self.overview)
        self.stack.addWidget(self.analysis)
        self.stack.addWidget(self.screening)
        self.stack.addWidget(self.evolution)
        self.stack.addWidget(self.settings)

        content_layout.addWidget(self.stack, stretch=1)
        main_layout.addWidget(content, stretch=1)

        # ── 状态栏 ──
        self._status_bar = QStatusBar()
        self._status_bar.setStyleSheet(
            "QStatusBar { background: #1a1b2e; color: #7982a9; border-top: 1px solid #3b4261; "
            "font-size: 11px; padding: 2px 8px; }"
        )
        self._status_bar.showMessage("就绪")
        main_layout.addWidget(self._status_bar)

        # ── 信号连接 ──
        self.overview.analyze_requested.connect(self._jump_to_analysis)
        self.screening.analyze_requested.connect(self._jump_to_analysis)
        self.screening.quick_adapt_requested.connect(self._jump_to_evolution_adapt)
        self.analysis.symbol_input.returnPressed.connect(self.analysis.start_analysis)
        self.analysis.analyze_btn.clicked.connect(self.analysis.start_analysis)
        self.analysis.exec_btn.clicked.connect(self.analysis.start_executive)
        self.screening.screen_btn.clicked.connect(self.screening.start)
        self.settings.theme_toggled.connect(self._apply_theme)
        self.settings.scheduler_toggled.connect(self._toggle_scheduler)
        self.settings.mail_listener_toggled.connect(self._toggle_mail_listener)

        # ── 应用默认主题 ──
        self._apply_theme(True)

        # ── 定时刷新 ──
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self.overview.refresh)
        self._refresh_timer.start(30000)  # 每30秒

        # ── 首次加载 ──
        QTimer.singleShot(200, self.overview.refresh)

        # ── 启动邮件监听 ──
        QTimer.singleShot(3000, self._auto_start_mail_listener)

    # ── 原生事件（窗口边框拖拽 + Aero Snap）──
    def nativeEvent(self, eventType, message):
        """处理 Windows WM_NCHITTEST 以支持边框缩放和 Aero Snap。"""
        if sys.platform != 'win32':
            return False, 0
        try:
            import ctypes
            from ctypes import wintypes
            msg = ctypes.wintypes.MSG.from_address(int(message))
            if msg.message != 0x0084:  # WM_NCHITTEST
                return False, 0

            # 最大化时不处理边框拖拽
            if self.isMaximized():
                x = msg.lParam & 0xFFFF
                y = (msg.lParam >> 16) & 0xFFFF
                title_bar_height = self.title_bar.height()
                if y < self.geometry().top() + title_bar_height:
                    return True, 2  # HTCAPTION — 允许拖动还原
                return False, 0

            x = msg.lParam & 0xFFFF
            y = (msg.lParam >> 16) & 0xFFFF
            rect = self.geometry()
            left, right = rect.left(), rect.right()
            top, bottom = rect.top(), rect.bottom()
            bw = self._border_width
            on_left = x < left + bw
            on_right = x > right - bw
            on_top = y < top + bw
            on_bottom = y > bottom - bw
            if on_top and on_left:
                return True, 13  # HTTOPLEFT
            if on_top and on_right:
                return True, 14  # HTTOPRIGHT
            if on_bottom and on_left:
                return True, 16  # HTBOTTOMLEFT
            if on_bottom and on_right:
                return True, 17  # HTBOTTOMRIGHT
            if on_left:
                return True, 10  # HTLEFT
            if on_right:
                return True, 11  # HTRIGHT
            if on_top:
                return True, 12  # HTTOP
            if on_bottom:
                return True, 15  # HTBOTTOM
            title_bar_height = self.title_bar.height()
            if y < top + title_bar_height:
                return True, 2   # HTCAPTION
        except Exception:
            pass
        return False, 0

    def changeEvent(self, event):
        if event.type() == QEvent.WindowStateChange:
            if self.isMaximized():
                self.title_bar.max_btn.setText("🗗")
            else:
                self.title_bar.max_btn.setText("🗖")
        super().changeEvent(event)

    def _minimize_window(self):
        """可靠的最小化：优先使用 Windows API。"""
        if sys.platform == 'win32':
            import ctypes
            hwnd = int(self.winId())
            ctypes.windll.user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE
        else:
            self.showMinimized()

    def _toggle_maximize(self):
        """可靠的最大化/还原切换。"""
        if self.isMaximized():
            self.showNormal()
            self.title_bar.max_btn.setText("🗖")
        else:
            self.showMaximized()
            self.title_bar.max_btn.setText("🗗")

    # ── 页面切换 ──
    def _switch_page(self, index: int):
        self.stack.setCurrentIndex(index)
        if index == 0:  # 总览页
            self.overview.refresh()

    def _jump_to_analysis(self, symbol: str):
        self.nav.setCurrentRow(1)
        self.analysis.symbol_input.setText(symbol)
        self.analysis.start_analysis()

    def _jump_to_evolution_adapt(self, symbol: str):
        self.nav.setCurrentRow(3)
        self.evolution.tabs.setCurrentIndex(1)  # Adaptive Migration tab
        self.evolution.adapt_tab.symbol_input.setText(symbol)
        self.evolution.adapt_tab.adapt_requested.emit(symbol)

    # ── 主题切换 ──
    def _toggle_theme(self):
        self._dark = not self._dark
        self._apply_theme(self._dark)

    def _apply_theme(self, dark: bool):
        self._dark = dark
        theme = DARK if dark else LIGHT
        stylesheet = qss(theme)
        self.setStyleSheet(stylesheet)

        # 更新股票盈亏颜色
        up_color = theme['up']
        down_color = theme['down']

        # 更新 overview 盈亏颜色
        QTimer.singleShot(100, self.overview.refresh)

    # ── 调度器 ──
    def _toggle_scheduler(self, running: bool):
        self._scheduler_running = running
        if running and self._scheduler is None:
            self._scheduler = SchedulerThread()
            self._scheduler.log_signal.connect(self._on_scheduler_log)
            self._scheduler.start()
        elif not running and self._scheduler:
            self._scheduler.running = False
            self._scheduler = None

    def _on_scheduler_log(self, msg: str):
        print(f"[调度器] {msg}")  # 控制台输出

    # ── 邮件监听 ──
    def _auto_start_mail_listener(self):
        """自动启动邮件监听（如果已配置邮箱）。"""
        from utils.config import get_config_value
        addr = get_config_value("EMAIL_ADDRESS")
        pwd = get_config_value("EMAIL_PASSWORD")
        if addr and pwd:
            self._toggle_mail_listener(True)
            print("[MailListener] 自动启动邮件监听")

    def _toggle_mail_listener(self, running: bool):
        self._mail_listener_running = running
        if running and self._mail_listener_thread is None:
            from mail.receiver import start_mail_listener
            self._mail_listener_thread = threading.Thread(
                target=start_mail_listener, args=(60,), daemon=True
            )
            self._mail_listener_thread.start()
            print("[MailListener] 已启动")
        elif not running and self._mail_listener_thread is not None:
            # daemon线程无法强制停止，标记为停止状态
            self._mail_listener_thread = None
            print("[MailListener] 已标记停止")

    def _check_mail_now(self):
        """立即检查一次邮件指令（托盘菜单触发）。"""
        from mail.receiver import check_and_execute
        def _run():
            result = check_and_execute()
            n = result.get("processed", 0)
            print(f"[MailListener] 手动检查完成，处理 {n} 封指令邮件")
        t = threading.Thread(target=_run, daemon=True)
        t.start()

    # ── 系统托盘 ──
    def setup_tray(self):
        self._tray_icon = QSystemTrayIcon(self)
        # 创建图标
        pixmap = QPixmap(32, 32)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        gradient = QLinearGradient(0, 0, 32, 32)
        gradient.setColorAt(0, QColor("#7aa2f7"))
        gradient.setColorAt(1, QColor("#2ac3de"))
        painter.setBrush(QBrush(gradient))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(2, 2, 28, 28, 8, 8)
        painter.setPen(QColor("white"))
        painter.setFont(QFont("Arial", 14, QFont.Bold))
        painter.drawText(QRect(2, 2, 28, 28), Qt.AlignCenter, "S")
        painter.end()
        icon = QIcon(pixmap)
        self._tray_icon.setIcon(icon)
        self._tray_icon.setToolTip("StockMind 已运行")

        menu = QMenu()
        show_action = QAction("📊 显示主窗口", self)
        show_action.triggered.connect(self.show_and_raise)
        menu.addAction(show_action)

        refresh_action = QAction("🔄 刷新持仓", self)
        refresh_action.triggered.connect(lambda: self.overview.refresh())
        menu.addAction(refresh_action)

        menu.addSeparator()

        mail_check_action = QAction("📬 立即检查邮件指令", self)
        mail_check_action.triggered.connect(self._check_mail_now)
        menu.addAction(mail_check_action)

        menu.addSeparator()

        quit_action = QAction("❌ 退出", self)
        quit_action.triggered.connect(self.quit_app)
        menu.addAction(quit_action)

        self._tray_icon.setContextMenu(menu)
        self._tray_icon.activated.connect(self._on_tray_activated)
        self._tray_icon.show()

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.DoubleClick:
            self.show_and_raise()

    def copy_to_clipboard(self, text_widget):
        text = text_widget.toPlainText() if hasattr(text_widget, 'toPlainText') else str(text_widget)
        if text.strip():
            QApplication.clipboard().setText(text.strip())
            self._status_bar.showMessage("已复制到剪贴板", 2000)

    def save_analysis_log(self, symbol: str, text: str):
        """将分析输出写入日志文件（剪贴板兜底的第一保障）。"""
        if not text or not text.strip():
            return
        try:
            from datetime import datetime
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{symbol}_{ts}.log"
            filepath = os.path.join(self._analysis_logs_dir, filename)
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(text.strip())
            self._status_bar.showMessage(f"日志已保存: {filename}", 3000)
        except Exception as e:
            self._status_bar.showMessage(f"日志保存失败: {e}", 3000)

    def show_and_raise(self):
        """从托盘恢复窗口，优先使用 Windows API 确保可靠显示。"""
        if sys.platform == 'win32':
            import ctypes
            hwnd = int(self.winId())
            ctypes.windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            ctypes.windll.user32.SetForegroundWindow(hwnd)
        self.show()
        if self.isMinimized():
            self.showNormal()
        self.raise_()
        self.activateWindow()

    def hide_to_tray(self):
        """隐藏到系统托盘。"""
        self.hide()

    def closeEvent(self, event):
        if self._tray_icon and self._tray_icon.isVisible():
            self.hide_to_tray()
            event.ignore()
        else:
            event.accept()

    def quit_app(self):
        if self._tray_icon:
            self._tray_icon.hide()
            self._tray_icon = None
        if self._scheduler:
            self._scheduler.running = False
        self.close()
        QApplication.quit()


# ═══════════════════════════════════════════════════════════════
# 调度器线程
# ═══════════════════════════════════════════════════════════════

class SchedulerThread(QThread):
    log_signal = Signal(str)

    def __init__(self):
        super().__init__()
        self.running = True

    def run(self):
        self.log_signal.emit("调度器已启动")
        from datetime import time as dtime
        schedule_times = [dtime(9, 30), dtime(11, 30), dtime(15, 0)]
        last_run = {}
        import time

        while self.running:
            now = datetime.now()
            is_weekday = now.weekday() < 5

            for t in schedule_times:
                key = f"{t.hour}:{t.minute}"
                if is_weekday and now.hour == t.hour and now.minute == t.minute and last_run.get(key) != now.day:
                    last_run[key] = now.day
                    self.log_signal.emit(f"⏰ 执行定时任务 {t.hour}:{t.minute:02d}")
                    self._run_task()

            for _ in range(30):
                if not self.running:
                    return
                time.sleep(1)

        self.log_signal.emit("调度器已停止")

    def _run_task(self):
        try:
            from portfolio.manager import update_market_values, get_portfolio_summary
            update_market_values()
            ps = get_portfolio_summary()
            self.log_signal.emit(f"  总资产 ¥{ps['total_assets']:,.0f} 盈亏 {ps['total_floating_pnl']:+,.0f}")

            # 发送邮件报告
            from mail.sender import send_daily_report
            self.log_signal.emit("  正在发送邮件报告...")
            ok = send_daily_report()
            if ok:
                self.log_signal.emit("  📧 邮件报告已发送")
            else:
                self.log_signal.emit("  ⚠ 邮件发送失败 (可能未配置QQ邮箱)")
        except Exception as e:
            self.log_signal.emit(f"  任务失败: {e}")


# ═══════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════

def main():
    if not _check_single_instance():
        print("[StockMind] 已有一个实例在运行")
        return

    # 加载 config.json 到环境变量
    from utils.config import load_config
    cfg = load_config()
    for k, v in cfg.items():
        if v and not os.environ.get(k):
            os.environ[k] = str(v)

    app = QApplication(sys.argv)
    app.setApplicationName("StockMind")
    app.setOrganizationName("StockMind")

    # 全局字体
    font = QFont("Microsoft YaHei", 9)
    font.setStyleStrategy(QFont.PreferAntialias)
    app.setFont(font)

    # 主窗口
    window = MainWindow()
    window.setup_tray()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
