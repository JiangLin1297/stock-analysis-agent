"""
持仓与总资产管理模块 — JSON 文件持久化。
纯 Python 计算，不调用 LLM。
支持 PyInstaller 打包后的路径解析。
"""

import json
import os
import sys
import time
from datetime import datetime
from typing import Optional


def _data_dir() -> str:
    """返回持久化数据目录（存 portfolio.json）。
    打包后用 exe 所在目录，开发模式用项目根目录。"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


PORTFOLIO_FILE = os.path.join(_data_dir(), "portfolio.json")

DEFAULT_PORTFOLIO = {
    "total_assets": 100000.0,
    "cash": 100000.0,
    "realized_pnl": 0.0,
    "positions": [],
    "last_updated": datetime.now().isoformat(),
}

# 自动刷新间隔（秒），避免每次 load 都刷新
_AUTO_REFRESH_INTERVAL = 300  # 5分钟


def _needs_refresh(pf: dict) -> bool:
    """检查是否超过刷新间隔。"""
    last = pf.get("last_updated", "")
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
        return (datetime.now() - last_dt).total_seconds() > _AUTO_REFRESH_INTERVAL
    except (ValueError, TypeError):
        return True


def _ensure_file():
    if not os.path.exists(PORTFOLIO_FILE):
        # PyInstaller 打包时：从临时目录复制初始文件到 exe 目录
        if getattr(sys, 'frozen', False):
            try:
                import shutil
                frozen_src = os.path.join(sys._MEIPASS, "portfolio.json")
                if os.path.exists(frozen_src):
                    shutil.copy2(frozen_src, PORTFOLIO_FILE)
                    return
            except Exception:
                pass
        save_portfolio(DEFAULT_PORTFOLIO)


def load_portfolio(refresh: bool = True) -> dict:
    """从 portfolio.json 加载持仓和总资产。refresh=True 时自动更新市值（有5分钟冷却）。"""
    _ensure_file()
    with open(PORTFOLIO_FILE, "r", encoding="utf-8") as f:
        pf = json.load(f)
    if refresh and pf.get("positions") and _needs_refresh(pf):
        try:
            pf = update_market_values(pf)
        except Exception:
            pass  # 刷新失败不影响加载
    return pf


def save_portfolio(data: dict):
    """保存持仓数据到 portfolio.json。"""
    data["last_updated"] = datetime.now().isoformat()
    with open(PORTFOLIO_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_position(symbol: str) -> Optional[dict]:
    """获取指定股票的持仓信息。"""
    pf = load_portfolio()
    for pos in pf.get("positions", []):
        if pos.get("symbol") == symbol:
            return pos
    return None


def add_position(symbol: str, entry_price: float, quantity: int, name: str = ""):
    """
    新增或追加持仓。如果已有同 symbol 持仓，则合并（调整均价）。
    """
    pf = load_portfolio()
    cost = entry_price * quantity
    if cost > pf["cash"]:
        raise ValueError(f"现金不足: 需要 {cost:.2f}, 可用 {pf['cash']:.2f}")

    existing = None
    for pos in pf["positions"]:
        if pos["symbol"] == symbol:
            existing = pos
            break

    if existing:
        total_qty = existing["quantity"] + quantity
        total_cost = existing["entry_price"] * existing["quantity"] + cost
        existing["quantity"] = total_qty
        existing["entry_price"] = round(total_cost / total_qty, 4)
    else:
        pf["positions"].append({
            "symbol": symbol,
            "name": name,
            "entry_price": entry_price,
            "quantity": quantity,
        })

    pf["cash"] -= cost
    pf["total_assets"] = pf["cash"] + sum(
        p["entry_price"] * p["quantity"] for p in pf["positions"]
    )
    save_portfolio(pf)


def remove_position(symbol: str, sell_price: float, quantity: int) -> dict:
    """
    卖出部分或全部持仓。
    返回已实现盈亏详情。
    """
    pf = load_portfolio()
    for pos in pf["positions"]:
        if pos["symbol"] == symbol:
            if quantity > pos["quantity"]:
                raise ValueError(f"持仓不足: 需要卖出 {quantity} 股, 持有 {pos['quantity']} 股")

            entry_cost = pos["entry_price"] * quantity
            sell_revenue = sell_price * quantity
            pnl = sell_revenue - entry_cost

            pos["quantity"] -= quantity
            if pos["quantity"] <= 0:
                pf["positions"].remove(pos)

            pf["cash"] += sell_revenue
            pf["realized_pnl"] = pf.get("realized_pnl", 0) + pnl
            pf["total_assets"] = pf["cash"] + sum(
                p["entry_price"] * p["quantity"] for p in pf["positions"]
            )
            save_portfolio(pf)

            return {"symbol": symbol, "sell_price": sell_price, "quantity": quantity,
                    "realized_pnl": round(pnl, 2), "cash_after": pf["cash"]}

    raise ValueError(f"未找到 {symbol} 的持仓")


def get_portfolio_summary(current_prices: dict = None) -> dict:
    """
    返回总资产、总市值、现金、总盈亏比例等摘要。
    current_prices: {symbol: current_price} 用于计算持仓市值和浮动盈亏。
    若不传则用 position 中保存的 current_price。
    """
    pf = load_portfolio(refresh=False)
    cash = pf["cash"]
    positions = pf.get("positions", [])

    market_value = 0.0
    total_cost = 0.0
    position_details = []

    if current_prices is None:
        current_prices = {}

    for pos in positions:
        sym = pos["symbol"]
        qty = pos["quantity"]
        entry = pos["entry_price"]
        # 优先用参数传入的实时价，其次用 position 已保存的现价，最后用成本价
        saved_price = pos.get("current_price")
        cur_price = current_prices.get(sym) or saved_price or entry
        mv = cur_price * qty
        cost = entry * qty
        floating_pnl = mv - cost
        floating_pnl_pct = (cur_price / entry - 1) * 100 if entry > 0 else 0

        market_value += mv
        total_cost += cost
        position_details.append({
            "symbol": sym,
            "name": pos.get("name", ""),
            "entry_price": entry,
            "current_price": cur_price,
            "quantity": qty,
            "market_value": round(mv, 2),
            "cost_basis": round(cost, 2),
            "floating_pnl": round(floating_pnl, 2),
            "floating_pnl_pct": round(floating_pnl_pct, 2),
        })

    total_floating_pnl = market_value - total_cost
    total_assets = cash + market_value
    total_pnl_pct = (total_floating_pnl / total_cost * 100) if total_cost > 0 else 0

    return {
        "cash": round(cash, 2),
        "market_value": round(market_value, 2),
        "total_assets": round(total_assets, 2),
        "total_cost": round(total_cost, 2),
        "total_floating_pnl": round(total_floating_pnl, 2),
        "total_pnl_pct": round(total_pnl_pct, 2),
        "realized_pnl": round(pf.get("realized_pnl", 0), 2),
        "position_count": len(positions),
        "positions": position_details,
        "last_updated": pf.get("last_updated", ""),
    }


def update_market_values(pf: dict = None) -> dict:
    """
    遍历所有持仓，获取最新价并更新市值/浮动盈亏。

    对每只持仓股票调用 data_pipeline.get_compressed_data 获取最新价，
    然后重新计算总市值、总资产。结果自动保存到 portfolio.json。

    Args:
        pf: 可选，已加载的持仓字典。若为 None 则自动加载。

    Returns:
        更新后的持仓字典。
    """
    if pf is None:
        pf = load_portfolio(refresh=False)

    positions = pf.get("positions", [])
    if not positions:
        pf["last_updated"] = datetime.now().isoformat()
        save_portfolio(pf)
        return pf

    from data.pipeline import get_compressed_data

    updated_positions = []
    total_market_value = 0.0

    for pos in positions:
        sym = pos.get("symbol", "")
        entry_price = float(pos.get("entry_price", 0))
        qty = int(pos.get("quantity", 0))

        try:
            data = get_compressed_data(sym)
            price = data.get("quote", {}).get("price") or data.get("technical", {}).get("close")
            # 如 compressed_data 拿不到收盘价，直接调用 K-line 接口
            if price is None:
                try:
                    from data.pipeline import fetch_kline_indicators, normalize_symbol
                    _sym, _ex = normalize_symbol(sym)
                    tech = fetch_kline_indicators(_sym, _ex, ndays=10)
                    price = tech.get("close")
                except Exception:
                    pass
            # 仍为 None 则用上次保存的现价
            if price is None:
                price = pos.get("current_price") or entry_price
            cur_price = round(float(price), 2)
        except Exception:
            cur_price = pos.get("current_price") or entry_price

        mv = round(cur_price * qty, 2)
        unrealized_pnl = round(mv - entry_price * qty, 2)
        total_market_value += mv

        updated_positions.append({
            **pos,
            "current_price": cur_price,
            "market_value": mv,
            "unrealized_pnl": unrealized_pnl,
        })

    cash = pf.get("cash", 0)
    total_assets = round(cash + total_market_value, 2)
    pf["positions"] = updated_positions
    pf["market_value"] = round(total_market_value, 2)
    pf["total_assets"] = total_assets
    pf["last_updated"] = datetime.now().isoformat()
    save_portfolio(pf)

    return pf
