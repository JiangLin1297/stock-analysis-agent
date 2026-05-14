"""
实盘交易记录器 — 记录每笔真实交易，同步持仓数据。
供 Web UI、调度器、实盘 Critic 调用。
"""

import json
import os
import sys
from datetime import datetime
from typing import Optional


def _data_dir() -> str:
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


TRADES_FILE = os.path.join(_data_dir(), "portfolio", "real_trades.json")
POSITIONS_FILE = os.path.join(_data_dir(), "portfolio", "positions.json")


def _ensure_trades_file():
    if not os.path.exists(TRADES_FILE):
        with open(TRADES_FILE, 'w', encoding='utf-8') as f:
            json.dump([], f)


def _load_trades() -> list:
    _ensure_trades_file()
    try:
        with open(TRADES_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, Exception):
        return []


def _save_trades(trades: list):
    os.makedirs(os.path.dirname(TRADES_FILE), exist_ok=True)
    with open(TRADES_FILE, 'w', encoding='utf-8') as f:
        json.dump(trades, f, ensure_ascii=False, indent=2)


def _load_positions() -> dict:
    if not os.path.exists(POSITIONS_FILE):
        return {"holdings": [], "total_cash": 100000.0, "last_updated": ""}
    try:
        with open(POSITIONS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, Exception):
        return {"holdings": [], "total_cash": 100000.0, "last_updated": ""}


def _save_positions(positions: dict):
    os.makedirs(os.path.dirname(POSITIONS_FILE), exist_ok=True)
    positions["last_updated"] = datetime.now().isoformat()
    with open(POSITIONS_FILE, 'w', encoding='utf-8') as f:
        json.dump(positions, f, ensure_ascii=False, indent=2)


def record_trade(trade: dict) -> dict:
    """
    记录一笔实盘交易，并同步更新持仓。

    Args:
        trade: {
            "date": "2026-05-14",
            "symbol": "600744",
            "action": "BUY" / "SELL",
            "price": 8.90,
            "quantity": 800,
            "reason": "短线BUY信号",
            "pnl": 0.0,           # 卖出时计算
            "time_frame": "short" / "mid" / "long"
        }

    Returns:
        写入的完整交易记录（含 id、timestamp）
    """
    required = ["symbol", "action", "price", "quantity"]
    for k in required:
        if k not in trade:
            raise ValueError(f"trade 缺少必填字段: {k}")

    now = datetime.now()
    trade.setdefault("date", now.strftime("%Y-%m-%d"))
    trade.setdefault("reason", "")
    trade.setdefault("pnl", 0.0)
    trade.setdefault("time_frame", "short")

    trades = _load_trades()
    trade_record = {
        "id": len(trades) + 1,
        "timestamp": now.isoformat(),
        **trade,
    }
    trades.append(trade_record)
    _save_trades(trades)

    _sync_positions(trade)

    return trade_record


def _sync_positions(trade: dict):
    """根据交易记录同步 positions.json 持仓数据。"""
    positions = _load_positions()
    holdings = positions.get("holdings", [])
    symbol = trade["symbol"]
    action = trade["action"].upper()
    price = float(trade["price"])
    quantity = int(trade["quantity"])

    existing = None
    for h in holdings:
        if h["symbol"] == symbol:
            existing = h
            break

    if action == "BUY":
        cost = price * quantity
        if existing:
            old_qty = int(existing["quantity"])
            old_cost = float(existing["entry_price"]) * old_qty
            new_qty = old_qty + quantity
            existing["entry_price"] = round((old_cost + cost) / new_qty, 4)
            existing["quantity"] = new_qty
        else:
            holdings.append({
                "symbol": symbol,
                "name": trade.get("name", symbol),
                "entry_price": round(price, 4),
                "quantity": quantity,
                "date": trade["date"],
            })
        positions["total_cash"] = round(float(positions.get("total_cash", 0)) - cost, 2)

    elif action == "SELL":
        if existing:
            existing["quantity"] = int(existing["quantity"]) - quantity
            if existing["quantity"] <= 0:
                holdings.remove(existing)
        positions["total_cash"] = round(float(positions.get("total_cash", 0)) + price * quantity, 2)

    positions["holdings"] = holdings
    _save_positions(positions)


def get_recent_trades(days: int = 30) -> list:
    """获取最近 N 天的交易记录。"""
    trades = _load_trades()
    if not trades:
        return []
    cutoff = datetime.now().strftime("%Y-%m-%d")
    from datetime import timedelta
    cutoff_dt = datetime.now() - timedelta(days=days)
    cutoff_str = cutoff_dt.strftime("%Y-%m-%d")
    return [t for t in trades if t.get("date", "") >= cutoff_str]


def get_all_trades() -> list:
    """获取所有交易记录。"""
    return _load_trades()


def has_new_trades_since(since_timestamp: str) -> bool:
    """检查是否有比指定时间更新的交易记录。"""
    trades = _load_trades()
    if not trades:
        return False
    latest = trades[-1].get("timestamp", "")
    return latest > since_timestamp


def get_trades_summary() -> dict:
    """返回交易统计摘要。"""
    trades = _load_trades()
    if not trades:
        return {"total_trades": 0, "buy_count": 0, "sell_count": 0,
                "total_pnl": 0.0, "win_count": 0, "loss_count": 0}

    buy_count = sum(1 for t in trades if t.get("action", "").upper() == "BUY")
    sell_trades = [t for t in trades if t.get("action", "").upper() == "SELL"]
    sell_count = len(sell_trades)
    total_pnl = sum(float(t.get("pnl", 0)) for t in sell_trades)
    win_count = sum(1 for t in sell_trades if float(t.get("pnl", 0)) > 0)
    loss_count = sell_count - win_count

    return {
        "total_trades": len(trades),
        "buy_count": buy_count,
        "sell_count": sell_count,
        "total_pnl": round(total_pnl, 2),
        "win_count": win_count,
        "loss_count": loss_count,
    }
