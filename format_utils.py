"""
控制台输出格式化工具 — ASCII 卡片/表格风格。
纯 ASCII 字符，兼容 Windows GBK 终端。
"""

# ── 宽度常量 ──
CARD_W = 76
TABLE_W = 76
HALF_W = 36


def card_header(title: str, width: int = CARD_W) -> str:
    """卡片标题行。"""
    inner = width - 2
    title_line = f"  {title}  "
    if len(title_line) > inner:
        title_line = title_line[:inner]
    return f"+{title_line:=^{inner}}+"


def card_line(text: str, width: int = CARD_W) -> str:
    """卡片内容行。"""
    inner = width - 2
    return f"| {text:<{inner}} |"


def card_empty(width: int = CARD_W) -> str:
    """卡片空行。"""
    return f"|{' ' * (width - 2)}|"


def card_bottom(width: int = CARD_W) -> str:
    """卡片底部。"""
    return f"+{'=' * (width - 2)}+"


def card_field(label: str, value, width: int = CARD_W, label_w: int = 12) -> str:
    """卡片字段行: 标签+值。"""
    inner = width - 2
    val_str = str(value) if value is not None else "N/A"
    available = inner - label_w - 2  # ": " after label
    if len(val_str) > available:
        val_str = val_str[:available - 1] + "..."
    return f"| {label:<{label_w}}: {val_str:<{available}} |"


def card_dual(label1, val1, label2, val2, width: int = CARD_W, w1: int = 12, w2: int = 12) -> str:
    """卡片双字段行：左字段 + 间隔 + 右字段（靠右对齐）。"""
    inner = width - 2
    v1 = str(val1) if val1 is not None else "N/A"
    v2 = str(val2) if val2 is not None else "N/A"
    left = f"{label1:<{w1}}: {v1}"
    right = f"{label2:<{w2}}: {v2}"
    gap = inner - len(left) - len(right)
    if gap < 1:
        gap = 1
    return f"| {left}{' ' * gap}{right} |"


def section_div(title: str, width: int = CARD_W) -> str:
    """分隔线带标题。"""
    inner = width - 2
    return f"+{title:=^{inner}}+"


def thin_sep(width: int = CARD_W) -> str:
    """薄分隔线。"""
    return f"+{'-' * (width - 2)}+"


def table_header(cols: list[tuple[str, int]], width: int = TABLE_W) -> str:
    """表格头。cols = [(name, width), ...]"""
    parts = []
    for name, w in cols:
        parts.append(f" {name:<{w}}")
    return "|" + "|".join(parts) + "|"


def table_row(vals: list, cols: list[tuple[str, int]], width: int = TABLE_W) -> str:
    """表格行。vals 与 cols 一一对应。"""
    parts = []
    for val, (_, w) in zip(vals, cols):
        s = str(val) if val is not None else ""
        if len(s) > w:
            s = s[:w - 1] + "."
        parts.append(f" {s:<{w}}")
    return "|" + "|".join(parts) + "|"


def table_sep(cols: list[tuple[str, int]], width: int = TABLE_W, char: str = "-") -> str:
    """表格分隔线。"""
    parts = []
    for _, w in cols:
        parts.append(char * (w + 1))
    return "+" + "+".join(parts) + "+"


def format_signal(signal: str) -> str:
    """格式化信号为带标签的字符串。"""
    tags = {
        "BUY": "[多] BUY",
        "SELL": "[空] SELL",
        "HOLD": "[观] HOLD",
        "CAUTIOUS_BUY": "[!] CAUTIOUS_BUY",
        "CAUTIOUS_SELL": "[!] CAUTIOUS_SELL",
    }
    return tags.get(signal, f"[?] {signal}")


def format_pct(val, default: str = "N/A") -> str:
    """格式化百分比。"""
    if val is None:
        return default
    try:
        return f"{float(val):.1f}%"
    except (ValueError, TypeError):
        return str(val)


def format_price(val, default: str = "N/A") -> str:
    """格式化价格。"""
    if val is None:
        return default
    try:
        return f"{float(val):.2f}"
    except (ValueError, TypeError):
        return str(val)
