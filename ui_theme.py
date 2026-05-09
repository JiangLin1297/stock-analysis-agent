"""
StockMind UI Theme — Dark/Light color palettes and QSS stylesheets.
"""
from PySide6.QtGui import QColor

# ═══════════════════════════════════════════════════════════════
# Color Palettes
# ═══════════════════════════════════════════════════════════════

DARK = {
    "bg_primary":     "#1a1b26",
    "bg_secondary":   "#24283b",
    "bg_card":        "#1f2335",
    "bg_surface":     "#2a2e44",
    "bg_hover":       "#363b54",
    "border":         "#33467c",
    "text_primary":   "#c0caf5",
    "text_secondary": "#7982a9",
    "text_muted":     "#565f89",
    "accent":         "#7aa2f7",
    "accent_hover":   "#89b4fa",
    "accent_dim":     "#5d8fe8",
    "success":        "#9ece6a",
    "danger":         "#f7768e",
    "warning":        "#e0af68",
    "info":           "#2ac3de",
    "up":             "#9ece6a",
    "down":           "#f7768e",
    "nav_active_bg":  "#2f3a6b",
    "nav_active_fg":  "#7aa2f7",
    "scrollbar_bg":   "#1f2335",
    "scrollbar_fg":   "#33467c",
    "input_bg":       "#1a1b26",
    "input_border":   "#33467c",
    "progress_bg":    "#24283b",
    "progress_fg":    "#7aa2f7",
}

LIGHT = {
    "bg_primary":     "#f5f5f7",
    "bg_secondary":   "#ffffff",
    "bg_card":        "#ffffff",
    "bg_surface":     "#eeeeee",
    "bg_hover":       "#e5e5ea",
    "border":         "#d1d1d6",
    "text_primary":   "#1d1d1f",
    "text_secondary": "#6e6e73",
    "text_muted":     "#aeaeb2",
    "accent":         "#5b4bd4",
    "accent_hover":   "#6d5ee0",
    "accent_dim":     "#4a3db5",
    "success":        "#34a853",
    "danger":         "#e74c3c",
    "warning":        "#f59e0b",
    "info":           "#0ea5e9",
    "up":             "#34a853",
    "down":           "#e74c3c",
    "nav_active_bg":  "#e8e5f9",
    "nav_active_fg":  "#5b4bd4",
    "scrollbar_bg":   "#f0f0f0",
    "scrollbar_fg":   "#c7c7cc",
    "input_bg":       "#ffffff",
    "input_border":   "#d1d1d6",
    "progress_bg":    "#e5e5ea",
    "progress_fg":    "#5b4bd4",
}


def qss(theme: dict) -> str:
    """Generate full QSS stylesheet from a theme dict."""
    t = theme
    return f"""
    /* ── Global ── */
    QWidget {{
        background-color: {t['bg_primary']};
        color: {t['text_primary']};
        font-family: "Microsoft YaHei", "PingFang SC", "Segoe UI", sans-serif;
        font-size: 13px;
    }}

    QMainWindow, QFrame#centralFrame {{
        background: {t['bg_primary']};
    }}

    /* ── Title Bar ── */
    QFrame#titleBar {{
        background: {t['bg_secondary']};
        border-bottom: 1px solid {t['border']};
    }}
    QLabel#titleLabel {{
        color: {t['text_primary']};
        font-size: 14px;
        font-weight: 600;
    }}
    QPushButton#tbBtn {{
        background: transparent;
        border: none;
        border-radius: 6px;
        padding: 4px 10px;
        font-size: 14px;
        color: {t['text_secondary']};
        min-width: 32px;
        min-height: 24px;
    }}
    QPushButton#tbBtn:hover {{
        background: {t['bg_hover']};
        color: {t['text_primary']};
    }}
    QPushButton#tbClose:hover {{
        background: {t['danger']};
        color: white;
    }}

    /* ── Navigation ── */
    QFrame#navBar {{
        background: {t['bg_secondary']};
        border-right: 1px solid {t['border']};
    }}
    QListWidget#navList {{
        background: transparent;
        border: none;
        outline: none;
        padding: 8px 0;
    }}
    QListWidget#navList::item {{
        background: transparent;
        border: none;
        border-radius: 10px;
        padding: 10px 16px;
        margin: 2px 8px;
        color: {t['text_secondary']};
        font-size: 13px;
    }}
    QListWidget#navList::item:hover {{
        background: {t['bg_hover']};
        color: {t['text_primary']};
    }}
    QListWidget#navList::item:selected {{
        background: {t['nav_active_bg']};
        color: {t['nav_active_fg']};
        font-weight: 600;
    }}

    /* ── Cards ── */
    QFrame#card {{
        background: {t['bg_card']};
        border: 1px solid {t['border']};
        border-radius: 12px;
        padding: 16px;
    }}
    QFrame#card:hover {{
        border-color: {t['accent_dim']};
    }}
    QLabel#cardTitle {{
        color: {t['text_muted']};
        font-size: 12px;
        font-weight: 500;
    }}
    QLabel#cardValue {{
        color: {t['text_primary']};
        font-size: 22px;
        font-weight: 700;
    }}

    /* ── Buttons ── */
    QPushButton {{
        background: {t['bg_surface']};
        color: {t['text_primary']};
        border: 1px solid {t['border']};
        border-radius: 8px;
        padding: 8px 18px;
        font-size: 13px;
        font-weight: 500;
        min-height: 20px;
    }}
    QPushButton:hover {{
        background: {t['bg_hover']};
        border-color: {t['accent_dim']};
    }}
    QPushButton:pressed {{
        background: {t['accent_dim']};
        color: white;
    }}
    QPushButton:disabled {{
        background: {t['bg_secondary']};
        color: {t['text_muted']};
        border-color: {t['border']};
    }}
    QPushButton#btnPrimary {{
        background: {t['accent']};
        color: white;
        border: none;
        font-weight: 600;
    }}
    QPushButton#btnPrimary:hover {{
        background: {t['accent_hover']};
    }}
    QPushButton#btnPrimary:pressed {{
        background: {t['accent_dim']};
    }}
    QPushButton#btnPrimary:disabled {{
        background: {t['text_muted']};
        color: {t['text_secondary']};
    }}
    QPushButton#btnSuccess {{
        background: {t['success']};
        color: {t['bg_primary']};
        border: none;
    }}
    QPushButton#btnDanger {{
        background: {t['danger']};
        color: white;
        border: none;
    }}

    /* ── Input ── */
    QLineEdit {{
        background: {t['input_bg']};
        color: {t['text_primary']};
        border: 1px solid {t['input_border']};
        border-radius: 8px;
        padding: 8px 12px;
        font-size: 14px;
        selection-background-color: {t['accent']};
        selection-color: white;
    }}
    QLineEdit:focus {{
        border-color: {t['accent']};
    }}
    QLineEdit::placeholder {{
        color: {t['text_muted']};
    }}

    /* ── ComboBox ── */
    QComboBox {{
        background: {t['input_bg']};
        color: {t['text_primary']};
        border: 1px solid {t['input_border']};
        border-radius: 8px;
        padding: 8px 12px;
        font-size: 13px;
        min-height: 20px;
    }}
    QComboBox:hover {{
        border-color: {t['accent_dim']};
    }}
    QComboBox::drop-down {{
        border: none;
        width: 28px;
    }}
    QComboBox::down-arrow {{
        image: none;
        border: solid {t['text_secondary']};
        border-width: 0 2px 2px 0;
        padding: 3px;
        transform: rotate(45deg);
    }}
    QComboBox QAbstractItemView {{
        background: {t['bg_secondary']};
        border: 1px solid {t['border']};
        border-radius: 8px;
        selection-background-color: {t['bg_hover']};
        selection-color: {t['accent']};
        padding: 4px;
        outline: none;
    }}

    /* ── SpinBox / Slider ── */
    QSpinBox {{
        background: {t['input_bg']};
        color: {t['text_primary']};
        border: 1px solid {t['input_border']};
        border-radius: 8px;
        padding: 6px 10px;
        font-size: 13px;
    }}
    QSlider::groove:horizontal {{
        background: {t['progress_bg']};
        height: 6px;
        border-radius: 3px;
    }}
    QSlider::handle:horizontal {{
        background: {t['accent']};
        width: 18px;
        height: 18px;
        margin: -6px 0;
        border-radius: 9px;
    }}
    QSlider::sub-page:horizontal {{
        background: {t['accent']};
        border-radius: 3px;
    }}

    /* ── Table ── */
    QTableWidget {{
        background: {t['bg_card']};
        alternate-background-color: {t['bg_primary']};
        border: 1px solid {t['border']};
        border-radius: 10px;
        gridline-color: {t['border']};
        font-size: 12px;
        selection-background-color: {t['nav_active_bg']};
        selection-color: {t['accent']};
    }}
    QHeaderView::section {{
        background: {t['bg_secondary']};
        color: {t['accent']};
        padding: 10px 6px;
        border: none;
        border-bottom: 2px solid {t['border']};
        font-weight: 600;
        font-size: 12px;
    }}
    QHeaderView::section:hover {{
        background: {t['bg_hover']};
    }}
    QTableWidget::item {{
        padding: 6px;
        border-bottom: 1px solid {t['border']};
    }}

    /* ── Progress Bar ── */
    QProgressBar {{
        background: {t['progress_bg']};
        border: none;
        border-radius: 6px;
        text-align: center;
        font-size: 11px;
        color: {t['text_secondary']};
        min-height: 12px;
    }}
    QProgressBar::chunk {{
        background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
            stop:0 {t['accent']}, stop:1 {t['info']});
        border-radius: 6px;
    }}

    /* ── ScrollBar ── */
    QScrollBar:vertical {{
        background: {t['scrollbar_bg']};
        width: 8px;
        border: none;
        border-radius: 4px;
    }}
    QScrollBar::handle:vertical {{
        background: {t['scrollbar_fg']};
        border-radius: 4px;
        min-height: 30px;
    }}
    QScrollBar::handle:vertical:hover {{
        background: {t['accent_dim']};
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
        height: 0;
    }}
    QScrollBar:horizontal {{
        background: {t['scrollbar_bg']};
        height: 8px;
        border: none;
        border-radius: 4px;
    }}
    QScrollBar::handle:horizontal {{
        background: {t['scrollbar_fg']};
        border-radius: 4px;
        min-width: 30px;
    }}
    QScrollBar::handle:horizontal:hover {{
        background: {t['accent_dim']};
    }}
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
        width: 0;
    }}

    /* ── CheckBox ── */
    QCheckBox {{
        color: {t['text_primary']};
        spacing: 8px;
    }}
    QCheckBox::indicator {{
        width: 18px;
        height: 18px;
        border-radius: 4px;
        border: 2px solid {t['text_muted']};
    }}
    QCheckBox::indicator:checked {{
        background: {t['accent']};
        border-color: {t['accent']};
    }}

    /* ── GroupBox ── */
    QGroupBox {{
        background: {t['bg_card']};
        border: 1px solid {t['border']};
        border-radius: 12px;
        margin-top: 16px;
        padding: 20px 16px 16px;
        font-weight: 600;
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        left: 16px;
        padding: 0 8px;
        color: {t['text_secondary']};
        font-size: 12px;
        font-weight: 500;
    }}

    /* ── Tooltip ── */
    QToolTip {{
        background: {t['bg_secondary']};
        color: {t['text_primary']};
        border: 1px solid {t['border']};
        border-radius: 6px;
        padding: 6px 10px;
        font-size: 12px;
    }}

    /* ── Status Label ── */
    QLabel#statusOk {{
        color: {t['success']};
        font-weight: 600;
    }}
    QLabel#statusWarn {{
        color: {t['warning']};
        font-weight: 600;
    }}
    QLabel#statusError {{
        color: {t['danger']};
        font-weight: 600;
    }}

    /* ── Result Card ── */
    QFrame#resultCard {{
        background: {t['bg_card']};
        border: 1px solid {t['accent_dim']};
        border-radius: 12px;
        padding: 20px;
    }}
    QFrame#resultCardBuy {{
        border: 2px solid {t['up']};
    }}
    QFrame#resultCardSell {{
        border: 2px solid {t['down']};
    }}
    QFrame#resultCardHold {{
        border: 1px solid {t['text_muted']};
    }}
    """
