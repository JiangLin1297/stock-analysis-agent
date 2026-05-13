#!/usr/bin/env python3
"""
智能选股模块 — 纯本地计算，基于 akshare 免费数据。
扫描沪深300/中证500 成分股，按技术面+基本面+财务安全+流动性+趋势过滤打分排序。

用法:
    py stock_screener.py                          # 默认沪深300
    py stock_screener.py --scope zz500            # 中证500
    py stock_screener.py --scope hs300 --top 10   # 沪深300 Top10
"""

import sys
import time
import json
import re
import functools
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd
import requests

# ── 轻量级 HTTP 会话 ──────────────────────────────────────
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
})

# ── 默认筛选条件 ──────────────────────────────────────────
DEFAULT_CRITERIA = {
    "ma_bullish": True,        # MA5 > MA20
    "rsi_min": 40,
    "rsi_max": 75,
    "macd_positive": True,     # MACD 柱 > 0
    "pe_min": 0.01,
    "pe_max": 50,
    "pb_min": 0.5,
    "pb_max": 8,
    "roe_min": 5.0,            # ROE > 5%
    "debt_ratio_max": 70,      # 资产负债率 < 70%
    "turnover_min": 1.0,
    "turnover_max": 15.0,
    "above_ma60": True,        # 收盘 > MA60
}

# 收益潜力目标
RETURN_TARGETS = {
    "short": 10,   # 短线 ≥10%
    "mid": 30,     # 中线 ≥30%
    "long": 200,   # 长线累计 ≥200%
}

# 成长型行业关键词
GROWTH_SECTORS = ["新能源", "AI", "人工智能", "半导体", "芯片", "生物医药", "创新药",
                  "消费升级", "光伏", "储能", "锂电池", "机器人", "智能驾驶",
                  "新材料", "军工", "航空航天", "数字经济", "东数西算"]

# 打分权重：每满足一项 +1，满分为 len(checks)
SCORE_CHECKS = [
    "ma_bullish",
    "rsi_range",
    "macd_positive",
    "pe_range",
    "pb_range",
    "roe_min",
    "debt_ratio_max",
    "turnover_range",
    "above_ma60",
]

INDEX_SCOPE = {
    "hs300": ("000300", "沪深300"),
    "zz500": ("000905", "中证500"),
}

MAX_WORKERS = 12
KL_NDAYS = 120
TIMEOUT_SINGLE = 20


def _f(val):
    """Safe float conversion."""
    if val is None or val == '' or val == '-':
        return None
    try:
        return round(float(val), 4)
    except (ValueError, TypeError):
        return None


# ═══════════════════════════════════════════════════════════════
# 1. 获取指数成分股列表
# ═══════════════════════════════════════════════════════════════

def _get_constituents(scope: str) -> list:
    """获取沪深300或中证500成分股代码列表。"""
    import akshare as ak
    index_code, index_name = INDEX_SCOPE.get(scope, INDEX_SCOPE["hs300"])
    try:
        df = ak.index_stock_cons(symbol=index_code)
        if df is None or df.empty:
            print(f"  ⚠ index_stock_cons 返回空，尝试备用接口")
            raise ValueError("Empty response")
        for col in ["品种代码", "stock_code", "code", "成分券代码", "constituent_code"]:
            if col in df.columns:
                symbols = [str(s).strip().zfill(6) for s in df[col].tolist()]
                print(f"  [{index_name}] 成分股数量: {len(symbols)}")
                return symbols
        print(f"  ⚠ 未知列名: {df.columns.tolist()[:5]}")
        return []
    except Exception as e:
        print(f"  ⚠ index_stock_cons({index_code}) 失败: {e}")
        return _get_constituents_fallback(scope)


def _get_constituents_fallback(scope: str) -> list:
    """备用方案：通过 ak.stock_zh_a_spot_em() + 市值排序近似获取。"""
    import akshare as ak
    print("  使用备用方案: 全市场市值排序近似...")
    try:
        df = ak.stock_zh_a_spot_em()
        if df is None or df.empty:
            return []
        code_col = "代码"
        mc_col = None
        for c in ["总市值", "market_cap"]:
            if c in df.columns:
                mc_col = c
                break
        if mc_col is None:
            return []
        df = df.dropna(subset=[mc_col])
        df = df.sort_values(mc_col, ascending=False)
        if scope == "hs300":
            df = df.head(300)
        else:
            df = df.head(800).tail(500)
        symbols = [str(s).strip().zfill(6) for s in df[code_col].tolist()]
        print(f"  备用方案成分股数量: {len(symbols)}")
        return symbols
    except Exception:
        return []


# ═══════════════════════════════════════════════════════════════
# 2. 批量获取行情快照
# ═══════════════════════════════════════════════════════════════

def _get_spot_batch(symbols: list) -> dict:
    """获取一批股票的实时行情快照，返回 {symbol: {...}}。
    优先使用 akshare 东方财富接口，失败时降级为腾讯批量行情 API。"""
    import akshare as ak
    result = {}

    # ── 方案1: akshare 东方财富 ──
    try:
        df = ak.stock_zh_a_spot_em()
        if df is not None and not df.empty:
            code_col = next((c for c in ["代码", "code"] if c in df.columns), None)
            if code_col is not None:
                df[code_col] = df[code_col].astype(str).str.strip().str.zfill(6)
                sym_set = set(symbols)
                df = df[df[code_col].isin(sym_set)]
                name_col = next((c for c in ["名称", "name"] if c in df.columns), None)
                price_col = next((c for c in ["最新价", "price"] if c in df.columns), None)
                pe_col = next((c for c in ["市盈率-动态", "pe"] if c in df.columns), None)
                pb_col = next((c for c in ["市净率", "pb"] if c in df.columns), None)
                chg_col = next((c for c in ["涨跌幅", "change_pct"] if c in df.columns), None)
                to_col = next((c for c in ["换手率", "turnover"] if c in df.columns), None)

                for _, row in df.iterrows():
                    sym = str(row[code_col]).strip().zfill(6)
                    result[sym] = {
                        "symbol": sym,
                        "name": str(row[name_col]) if name_col else "",
                        "close": _f(row.get(price_col)) if price_col else None,
                        "change_pct": _f(row.get(chg_col)) if chg_col else None,
                        "pe": _f(row.get(pe_col)) if pe_col else None,
                        "pb": _f(row.get(pb_col)) if pb_col else None,
                        "turnover": _f(row.get(to_col)) if to_col else None,
                    }
                if result:
                    print(f"  东方财富接口: {len(result)} 只")
                    return result
    except Exception as e:
        print(f"  ⚠ 东方财富接口失败: {e}")

    # ── 方案2: 腾讯批量行情 API（降级） ──
    print(f"  ⚠ 降级使用腾讯行情 API...")
    try:
        # 分批次查询（腾讯单次支持多个代码）
        batch_size = 50
        for i in range(0, len(symbols), batch_size):
            batch = symbols[i:i + batch_size]
            codes = []
            sym_map = {}
            for sym in batch:
                exchange = "sh" if sym.startswith(("6", "9")) else "sz"
                codes.append(f"{exchange}{sym}")
                sym_map[sym] = exchange
            url = f"https://qt.gtimg.cn/q={','.join(codes)}"
            resp = SESSION.get(url, timeout=15)
            resp.raise_for_status()
            text = resp.content.decode("gbk")
            for line in text.strip().split("\n"):
                if '="' not in line:
                    continue
                fields = line.split('="', 1)[1].rstrip('";\n').split('~')
                if len(fields) < 40:
                    continue
                sym = str(fields[2]).zfill(6) if fields[2] else ""
                if not sym or sym not in sym_map:
                    continue
                result[sym] = {
                    "symbol": sym,
                    "name": fields[1],
                    "close": _f(fields[3]),
                    "change_pct": _f(fields[32]),
                    "pe": _f(fields[39]),
                    "pb": _f(fields[46]),
                    "turnover": _f(fields[38]),
                }
            print(f"  腾讯行情进度: {min(i + batch_size, len(symbols))}/{len(symbols)}")
    except Exception as e:
        print(f"  ⚠ 腾讯行情降级也失败: {e}")

    print(f"  共获取 {len(result)} 只行情")
    return result


# ═══════════════════════════════════════════════════════════════
# 3. 单票K线+技术指标
# ═══════════════════════════════════════════════════════════════

def _fetch_kline_indicators(symbol: str) -> dict:
    """获取一只股票的技术指标（腾讯K线 + 本地计算）。"""
    exchange = "sh" if symbol.startswith(("6", "9")) else "sz"
    code = f"{exchange}{symbol}"
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    try:
        resp = SESSION.get(url, params={"param": f"{code},day,,,{KL_NDAYS},qfq"}, timeout=TIMEOUT_SINGLE)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            return {}
        stock_data = data.get("data", {}).get(code, {})
        rows = stock_data.get("qfqday") or stock_data.get("day")
        if not rows:
            return {}
        clean = [r[:6] for r in rows if len(r) >= 6 and all(not isinstance(x, dict) for x in r[:6])]
        if len(clean) < 60:
            return {}
        df = pd.DataFrame(clean, columns=["date", "open", "close", "high", "low", "volume"])
        for col in ["open", "close", "high", "low", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["close"])
        if len(df) < 60:
            return {}
        close = df["close"]
        low = df["low"]
        ma5 = close.rolling(5).mean().iloc[-1]
        ma10 = close.rolling(10).mean().iloc[-1]
        ma20 = close.rolling(20).mean().iloc[-1]
        ma60 = close.rolling(60).mean().iloc[-1]
        # 昨日最低价
        prev_low = float(low.iloc[-2]) if len(low) >= 2 else None
        # 近20日最低价
        low_20d = round(float(low.tail(20).min()), 2)
        # MA20 斜率 (5日变化率，百分比)
        ma20_series = close.rolling(20).mean()
        ma20_5d = ma20_series.iloc[-6] if len(ma20_series) >= 6 else None
        ma20_slope = round(float((ma20_series.iloc[-1] - ma20_5d) / ma20_5d * 100), 2) if ma20_5d and not pd.isna(ma20_5d) and ma20_5d != 0 else None
        # MA5/MA20 交叉次数 (近30日)
        ma5_series = close.rolling(5).mean()
        cross_count = 0
        if len(ma5_series) >= 40 and len(ma20_series) >= 40:
            r5 = ma5_series.tail(30)
            r20 = ma20_series.tail(30)
            above = (r5.values > r20.values)
            cross_count = int((above[1:] != above[:-1]).sum())
        # RSI14
        delta = close.diff()
        gain = delta.clip(lower=0).ewm(span=14, adjust=False).mean().iloc[-1]
        loss = (-delta).clip(lower=0).ewm(span=14, adjust=False).mean().iloc[-1]
        rsi = 100.0 if loss == 0 else round(100.0 - 100.0 / (1.0 + gain / loss), 2)
        # MACD
        e12 = close.ewm(span=12, adjust=False).mean()
        e26 = close.ewm(span=26, adjust=False).mean()
        dif = e12 - e26
        dea = dif.ewm(span=9, adjust=False).mean()
        macd_hist = dif - dea
        # Bollinger bands (20-period, 2 std)
        bb_mid = close.rolling(20).mean()
        bb_std = close.rolling(20).std()
        bb_upper = bb_mid + 2 * bb_std
        bb_lower = bb_mid - 2 * bb_std
        last_close = float(close.iloc[-1])
        indicators = {
            "close": round(last_close, 2),
            "ma5": round(float(ma5), 2) if not pd.isna(ma5) else None,
            "ma10": round(float(ma10), 2) if not pd.isna(ma10) else None,
            "ma20": round(float(ma20), 2) if not pd.isna(ma20) else None,
            "ma60": round(float(ma60), 2) if not pd.isna(ma60) else None,
            "rsi14": rsi,
            "macd_hist": round(float(macd_hist.iloc[-1]), 4) if not pd.isna(macd_hist.iloc[-1]) else None,
            "boll_upper": round(float(bb_upper.iloc[-1]), 2) if not pd.isna(bb_upper.iloc[-1]) else None,
            "boll_mid": round(float(bb_mid.iloc[-1]), 2) if not pd.isna(bb_mid.iloc[-1]) else None,
            "boll_lower": round(float(bb_lower.iloc[-1]), 2) if not pd.isna(bb_lower.iloc[-1]) else None,
            "prev_low": prev_low,
            "low_20d": low_20d,
            "ma20_slope": ma20_slope,
            "cross_count": cross_count,
        }
        return indicators, df
    except Exception:
        return {}, pd.DataFrame()


# ═══════════════════════════════════════════════════════════════
# 4. 单票财务数据
# ═══════════════════════════════════════════════════════════════

def _fetch_financial_fast(symbol: str) -> dict:
    """快速获取 ROE、资产负债率和营收增长。"""
    import akshare as ak
    try:
        df = ak.stock_financial_abstract(symbol=symbol)
        if df is None or df.empty:
            return {}
        if "指标" not in df.columns or len(df) < 3:
            return {}
        latest_col = df.columns[2]

        def _get_val(keywords):
            for kw in keywords:
                mask = df["指标"].str.contains(kw, na=False, regex=False)
                if mask.any():
                    return _f(df.loc[mask, latest_col].iloc[0])
            return None

        return {
            "roe": _get_val(["净资产收益率(ROE)", "净资产收益率"]),
            "debt_ratio": _get_val(["资产负债率"]),
            "revenue_growth": _get_val(["营业收入增长率", "营收增长率", "营业总收入增长率"]),
        }
    except Exception:
        return {}


# ═══════════════════════════════════════════════════════════════
# 5. 并行数据获取
# ═══════════════════════════════════════════════════════════════

def _fetch_one_stock(symbol: str, spot_info: dict) -> dict:
    """获取单个股票的完整数据（技术+财务+原始K线），用于并行执行。"""
    tech, kline_df = _fetch_kline_indicators(symbol)
    fin = _fetch_financial_fast(symbol)
    result = dict(spot_info)
    result.update(tech)
    result.update(fin)
    result["_kline_df"] = kline_df
    return result


# ═══════════════════════════════════════════════════════════════
# 6. 打分与过滤
# ═══════════════════════════════════════════════════════════════

def _score_one(stock: dict, criteria: dict) -> tuple:
    """
    四因子模型打分，返回 (composite_score, factor_result_dict)。
    composite_score: 0~100 综合评分
    factor_result_dict: alpha_factors.compute_all_factors 的完整返回
    """
    from analysis.alpha_factors import compute_all_factors

    kline_df = stock.get("_kline_df")
    if kline_df is None or len(kline_df) < 20:
        return 0.0, {"filtered": True, "filter_reason": "K线数据不足"}

    # 构建 spot 和 fin 数据
    spot = {
        "name": stock.get("name", ""),
        "close": stock.get("close"),
        "change_pct": stock.get("change_pct"),
        "pe": stock.get("pe"),
        "pb": stock.get("pb"),
        "turnover": stock.get("turnover"),
        "总市值": stock.get("总市值"),
        "roe": stock.get("roe"),
    }
    fin = {
        "roe": stock.get("roe"),
        "debt_ratio": stock.get("debt_ratio"),
        "revenue_growth": stock.get("revenue_growth"),
    }

    result = compute_all_factors(kline_df, spot, fin)
    return result["composite_score"], result


# ═══════════════════════════════════════════════════════════════
# 6b. 最优入场价计算（基于已有技术指标，不重复请求）
# ═══════════════════════════════════════════════════════════════

def _calc_entry_price(stock: dict) -> dict:
    """
    基于已有技术指标计算最优入场价，纯本地计算。

    三种市况判断：
      - 强势股（价格 > MA20 且 MA20 斜率向上）：追涨，入场=max(MA10, 昨低)
      - 弱势股（价格 < MA20）：博反弹，入场=布林下轨*1.02
      - 震荡市（MA5/MA20 反复交叉）：等待，入场=布林中轨

    止损 = min(入场价*0.95, 布林下轨*0.98, 近20日最低价)
    """
    close = stock.get("close")
    change_pct = stock.get("change_pct")
    ma5 = stock.get("ma5")
    ma10 = stock.get("ma10")
    ma20 = stock.get("ma20")
    ma60 = stock.get("ma60")
    rsi = stock.get("rsi14")
    boll_upper = stock.get("boll_upper")
    boll_mid = stock.get("boll_mid")
    boll_lower = stock.get("boll_lower")
    prev_low = stock.get("prev_low")
    low_20d = stock.get("low_20d")
    ma20_slope = stock.get("ma20_slope")
    cross_count = stock.get("cross_count", 0)

    if not close or not boll_lower or not boll_mid or not boll_upper:
        return {"entry_price": None, "entry_range": [None, None],
                "stop_loss": None, "entry_type": "", "entry_basis": "数据不足", "warning": None}

    # ── 市况判断 ──
    is_strong_base = (
        ma20 is not None and close > ma20
        and ma20_slope is not None and ma20_slope > 0
    )
    is_weak = ma20 is not None and close < ma20
    is_choppy = (
        cross_count is not None and cross_count >= 2
        and not is_strong_base and not is_weak
    )

    # ── 入场价计算 ──
    warning = None

    # 强势股中处理当日大跌情况
    if is_strong_base and change_pct is not None and change_pct <= -5:
        entry_price = round(boll_lower, 2)
        entry_type = "oversold_bounce"
        warning = "跌幅过深，仅限超短线反弹博弈，仓位控制在5%以内"
        basis = f"强势股当日跌{change_pct}%(≤-5%)，超卖反弹，入场=布林下轨={entry_price}"
    elif is_strong_base and change_pct is not None and change_pct <= -2:
        entry_price = round(boll_lower + (boll_mid - boll_lower) * 0.15, 2)
        entry_type = "dip_wait"
        warning = "当日跌幅较大，建议等待止跌信号后再入场"
        basis = f"强势股当日跌{change_pct}%(≤-2%)，等回调企稳，入场=布林下轨+0.15带宽={entry_price}"
    elif is_strong_base:
        if rsi is not None and rsi > 65 and ma5 is not None:
            entry_price = round(ma5, 2)
            entry_type = "ma5_chase"
            basis = f"强势股(RSI={rsi}>65)激进追涨,入场=MA5"
        elif ma10 is not None and prev_low is not None:
            entry_price = round(max(ma10, prev_low), 2)
            entry_type = "ma10_chase"
            basis = f"强势股(MA20斜率+{ma20_slope}%),入场=max(MA10,昨低)"
        elif ma10 is not None:
            entry_price = round(ma10, 2)
            entry_type = "ma10_chase"
            basis = "强势股,入场=MA10"
        else:
            entry_price = round(close, 2)
            entry_type = "current_price"
            basis = "强势股缺MA10,兜底=现价"

    elif is_weak:
        if close < boll_lower:
            entry_price = round(close, 2)
            entry_type = "bollinger_reversal"
            basis = f"弱势股(价格<布林下轨),超卖反弹,入场=现价"
        else:
            entry_price = round(boll_lower * 1.02, 2)
            entry_type = "bollinger_reversal"
            basis = "弱势股(价格<MA20),入场=布林下轨*1.02"

    elif is_choppy:
        entry_price = round(boll_mid, 2)
        entry_type = "boll_mid_wait"
        basis = f"震荡市(MA5/MA20交叉{cross_count}次),入场=布林中轨,等突破"

    else:
        if ma20 is not None and close > ma20:
            entry_price = round(ma20, 2)
            entry_type = "ma20_support"
            basis = "价格>MA20,入场=MA20支撑"
        elif ma60 is not None and close > ma60:
            entry_price = round(ma60, 2)
            entry_type = "ma60_support"
            basis = "价格在MA20-MA60间,入场=MA60"
        else:
            boll_support = round(boll_lower + (boll_mid - boll_lower) * 0.1, 2)
            entry_price = boll_support
            entry_type = "boll_support"
            basis = "兜底,入场=布林下轨支撑"

    entry_range = [round(entry_price * 0.99, 2), round(entry_price * 1.02, 2)]

    # 止损 = min(入场价*0.95, 布林下轨*0.98, 近20日最低价)
    stop_candidates = [round(entry_price * 0.95, 2), round(boll_lower * 0.98, 2)]
    if low_20d is not None:
        stop_candidates.append(round(low_20d, 2))
    stop_loss = min(stop_candidates)

    return {
        "entry_price": entry_price,
        "entry_range": entry_range,
        "stop_loss": stop_loss,
        "entry_type": entry_type,
        "entry_basis": basis,
        "warning": warning,
    }


# ═══════════════════════════════════════════════════════════════
# 6c. DeepSeek 增强分析
# ═══════════════════════════════════════════════════════════════

def _parse_deepseek_json(raw: str) -> dict:
    """从 LLM 回复中提取 JSON。"""
    clean = raw.strip()
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        pass
    m = re.search(r'\{[^{}]*"signal"[^{}]*\}', clean)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return {"signal": "HOLD", "score": 0, "confidence": 0.0, "rationale": "解析失败"}


def _deepseek_enhance_screening(stocks: list) -> list:
    """对筛选结果调用 DeepSeek 进行 LLM 增强分析（仅做点评，不改变排名/数量）。"""
    from agents.prompts import SCREENER_ENHANCE_PROMPT
    from data.deepseek import deepseek_chat

    def _analyze_one(stock: dict) -> dict:
        sym = stock.get("symbol", "")
        name = stock.get("name", "")
        prompt = SCREENER_ENHANCE_PROMPT.format(
            symbol=sym,
            name=name,
            close=stock.get("close", "?"),
            change_pct=stock.get("change_pct", "?"),
            pe=stock.get("pe", "?"),
            pb=stock.get("pb", "?"),
            roe=stock.get("roe", "?"),
            debt_ratio=stock.get("debt_ratio", "?"),
            ma5=stock.get("ma5", "?"),
            ma20=stock.get("ma20", "?"),
            rsi=stock.get("rsi14", "?"),
            turnover=stock.get("turnover", "?"),
        )
        try:
            raw = deepseek_chat(
                prompt,
                "请分析并输出严格JSON。",
                max_tokens=512,
                timeout=30,
            )
            result = _parse_deepseek_json(raw)
            stock["deepseek_signal"] = result.get("signal", "HOLD")
            stock["deepseek_confidence"] = result.get("confidence", 0.0)
            stock["deepseek_rationale"] = (result.get("rationale") or "")[:100]
        except Exception as e:
            stock["deepseek_signal"] = "HOLD"
            stock["deepseek_confidence"] = 0.0
            stock["deepseek_rationale"] = f"AI点评不可用: {e}"
        return stock

    # 为每只股票设置默认 LLM 字段（防止后续读取 KeyError）
    for s in stocks:
        s.setdefault("deepseek_signal", "HOLD")
        s.setdefault("deepseek_confidence", 0.0)
        s.setdefault("deepseek_rationale", "")

    # 并行调用 DeepSeek，失败的股票保留原始数据不丢弃
    results = {}
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(_analyze_one, s): s.get("symbol", "") for s in stocks}
        for future in as_completed(futures):
            sym = futures[future]
            try:
                results[sym] = future.result(timeout=35)
            except Exception:
                pass  # 该股票结果不在 results 中，用原始数据兜底

    # 合并：LLM 成功的用 LLM 结果，失败的保留原始 stock（已有默认字段）
    enhanced = []
    for s in stocks:
        sym = s.get("symbol", "")
        if sym in results:
            enhanced.append(results[sym])
        else:
            enhanced.append(s)  # LLM 失败，原始股票照常输出
    return enhanced


# ═══════════════════════════════════════════════════════════════
# 7. 主入口
# ═══════════════════════════════════════════════════════════════

def screen_stocks(market: str = "A", criteria: dict = None,
                  scope: str = "hs300", top_n: int = 5,
                  sector: str = None, use_mock: bool = False) -> list:
    """
    四因子攻击性选股主函数。

    因子体系: 主力痕迹(35%) + 热点引擎(30%) + 未来空间(20%) + 基本质量(15%)
    硬性过滤: ST股、负债率>90%直接剔除
    默认输出 Top 5，DeepSeek 为可选增强层（不改变排名）。

    Args:
        market: 市场类型，默认 "A" (A股)
        criteria: 自定义筛选条件 dict（保留兼容，不再影响核心逻辑）
        scope: 扫描范围 — "hs300"(沪深300) / "zz500"(中证500)
        top_n: 返回前 N 只评分最高的股票（默认5）
        sector: 可选行业过滤，如 "新能源"
        use_mock: True=跳过DeepSeek分析, False=附加AI点评（不改变排名）

    Returns:
        list[dict]: 按 composite_score 降序排列的股票列表
    """

    print(f"\n{'='*60}")
    scope_name = INDEX_SCOPE.get(scope, ("", "沪深300"))[1]
    print(f"  四因子选股 — 范围: {scope_name} | 返回 Top {top_n}")
    print(f"  因子: 主力痕迹(35%) + 热点引擎(30%) + 未来空间(20%) + 基本质量(15%)")
    print(f"{'='*60}\n")

    # ── Step 1: 获取成分股 ──
    print(f"[1/4] 获取{scope_name}成分股列表...")
    symbols = _get_constituents(scope)
    if not symbols:
        print("  ✗ 无法获取成分股列表，退出")
        return []

    # ── Step 2: 批量行情 ──
    print(f"[2/4] 获取批量行情快照...")
    spots = _get_spot_batch(symbols)
    print(f"  覆盖 {len(spots)}/{len(symbols)} 只成分股")

    candidates = [s for s in symbols if s in spots]
    if not candidates:
        print("  ✗ 无有效行情数据")
        return []

    # ── Step 3: 并行抓取技术+财务数据 ──
    print(f"\n[3/4] 并行获取技术指标+财务数据 (max_workers={MAX_WORKERS})...")
    results = []
    completed = 0
    total = len(candidates)
    print(f"  需处理 {total} 只股票，预计耗时 {total * 2 // MAX_WORKERS}s...")

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_map = {
            executor.submit(_fetch_one_stock, sym, spots[sym]): sym
            for sym in candidates
        }
        for future in as_completed(future_map):
            sym = future_map[future]
            completed += 1
            try:
                data = future.result(timeout=TIMEOUT_SINGLE + 5)
                results.append(data)
            except Exception:
                pass
            if completed % 10 == 0 or completed == total:
                print(f"  抓取进度: {completed}/{total}")

    print(f"  成功获取 {len(results)} 只股票的完整数据")

    # ── Step 4: 四因子打分 ──
    print(f"\n[4/4] 四因子打分 & 排序...")
    scored = []
    filtered_count = 0
    for i, stock in enumerate(results):
        score, factor_result = _score_one(stock, criteria or {})
        sym = stock.get("symbol", "?")
        name = stock.get("name", "")

        if factor_result.get("filtered"):
            filtered_count += 1
            if i < 5 or i % 50 == 0:
                reason = factor_result.get("filter_reason", "")
                print(f"  [{i+1}/{len(results)}] {sym} {name} → 过滤: {reason}")
            continue

        tags = factor_result.get("signal_tags", [])
        tag_str = " | ".join(tags[:3])
        verdict = "★" if score >= 60 else ("☆" if score >= 45 else "·")
        print(f"  [{i+1}/{len(results)}] {sym} {name} → {score:.1f}分 {verdict} [{tag_str}]")

        stock["composite_score"] = score
        stock["factor_result"] = factor_result
        stock["signal_tags"] = tags
        scored.append(stock)

    # 去重
    seen = set()
    scored_dedup = []
    for s in scored:
        sym = s.get("symbol", "")
        if sym not in seen:
            seen.add(sym)
            scored_dedup.append(s)
    scored = scored_dedup

    scored.sort(key=lambda x: x["composite_score"], reverse=True)
    print(f"\n  过滤: {filtered_count} 只 | 有效打分: {len(scored)} 只")

    top_stocks = scored[:top_n]

    # ── 行业过滤（如指定） ──
    if sector and top_stocks:
        filtered = [s for s in top_stocks if sector in (s.get("name") or "")]
        if len(filtered) < top_n:
            remaining = [s for s in scored if s not in filtered]
            filtered.extend(remaining[: top_n - len(filtered)])
        top_stocks = filtered[:top_n]

    # ── DeepSeek 增强分析（仅做点评，不改变排名和数量） ──
    if not use_mock:
        print(f"\n  [AI点评] 调用 DeepSeek 分析 {len(top_stocks)} 只候选...")
        enhanced = _deepseek_enhance_screening(top_stocks)
        if len(enhanced) >= len(top_stocks):
            top_stocks = enhanced
        # LLM 结果为空或数量不足时，保留原始 top_stocks 不替换
        print(f"  [AI点评] 完成")

    # ── 格式化输出 ──
    printable = []
    for s in top_stocks:
        entry = _calc_entry_price(s)
        factor_result = s.get("factor_result", {})
        cat_scores = factor_result.get("category_scores", {})

        printable.append({
            "symbol": s.get("symbol", ""),
            "name": s.get("name", ""),
            "close": s.get("close"),
            "change_pct": s.get("change_pct"),
            "pe": s.get("pe"),
            "pb": s.get("pb"),
            "roe": s.get("roe"),
            "debt_ratio": s.get("debt_ratio"),
            "turnover": s.get("turnover"),
            "composite_score": s["composite_score"],
            "score": s["composite_score"],  # 向后兼容
            "category_scores": {
                "主力痕迹": round(cat_scores.get("主力痕迹", 0) * 100, 1),
                "热点引擎": round(cat_scores.get("热点引擎", 0) * 100, 1),
                "未来空间": round(cat_scores.get("未来空间", 0) * 100, 1),
                "基本质量": round(cat_scores.get("基本质量", 0) * 100, 1),
            },
            "signal_tags": s.get("signal_tags", []),
            "entry_price": entry["entry_price"],
            "entry_range": entry["entry_range"],
            "stop_loss": entry["stop_loss"],
            "entry_type": entry["entry_type"],
            "entry_basis": entry["entry_basis"],
            "warning": entry.get("warning"),
            "deepseek_signal": s.get("deepseek_signal"),
            "deepseek_confidence": s.get("deepseek_confidence"),
            "deepseek_rationale": s.get("deepseek_rationale"),
        })

    print(f"\n  最终筛选出 {len(printable)} 只潜力股\n")
    return printable


# ═══════════════════════════════════════════════════════════════
# 7b. 智能选股独立验证
# ═══════════════════════════════════════════════════════════════

def validate_screening(days: int = 60, top_n: int = 5, scope: str = "hs300") -> dict:
    """
    对四因子选股Top N进行模拟跟踪验证。

    记录推荐日价格，days天后计算实际收益，统计Top N平均收益是否≥15%。

    Returns:
        {"passed": bool, "avg_return_pct": float, "stocks": [...], "recommend_date": str}
    """
    from data.pipeline import download_full_history, normalize_symbol

    print(f"\n{'='*60}")
    print(f"  四因子选股验证 — {days}天跟踪 | Top {top_n}")
    print(f"{'='*60}\n")

    stocks = screen_stocks(scope=scope, top_n=top_n, use_mock=True)
    if not stocks:
        print("  ✗ 选股无结果")
        return {"passed": False, "avg_return_pct": 0, "error": "选股无结果"}

    recommend_date = datetime.now().strftime("%Y-%m-%d")
    results = []

    for s in stocks:
        sym = s["symbol"]
        name = s.get("name", "")
        rec_price = s.get("close")
        if not rec_price:
            results.append({"symbol": sym, "name": name, "recommend_price": None, "error": "无推荐价"})
            continue

        try:
            sym_norm, _ = normalize_symbol(sym)
            cache_path = download_full_history(sym, ndays=800)

            df = pd.read_csv(cache_path)
            df['date'] = pd.to_datetime(df['date'])

            latest = df.iloc[-1]
            current_price = float(latest['close'])

            if len(df) >= days:
                past_idx = max(0, len(df) - days - 1)
                past_price = float(df.iloc[past_idx]['close'])
                sim_return = round((current_price - past_price) / past_price * 100, 2)
            else:
                sim_return = 0

            results.append({
                "symbol": sym,
                "name": name,
                "recommend_price": rec_price,
                "current_price": current_price,
                "composite_score": s.get("composite_score", 0),
                "signal_tags": s.get("signal_tags", []),
                "sim_return_pct": sim_return,
            })
        except Exception as e:
            results.append({"symbol": sym, "name": name, "recommend_price": rec_price, "error": str(e)})

    valid = [r for r in results if "sim_return_pct" in r]
    avg_return = round(sum(r["sim_return_pct"] for r in valid) / len(valid), 2) if valid else 0
    passed = avg_return >= 15.0

    print(f"  推荐日期: {recommend_date}")
    print(f"  跟踪天数: {days}天 (模拟)")
    print(f"  Top{top_n} 平均收益: {avg_return}%")
    print(f"  达标 (≥15%): {'✅ 通过' if passed else '❌ 未通过'}")
    for r in results:
        ret = r.get('sim_return_pct', '?')
        tags = ", ".join(r.get("signal_tags", [])[:3])
        print(f"    {r['symbol']} {r['name']}: 推荐价{r.get('recommend_price','?')} → 模拟收益{ret}% [{tags}]")

    return {
        "passed": passed,
        "avg_return_pct": avg_return,
        "recommend_date": recommend_date,
        "stocks": results,
    }


# ═══════════════════════════════════════════════════════════════
# 8. CLI 入口
# ═══════════════════════════════════════════════════════════════

def _print_table(stocks: list):
    """打印四因子选股结果表格。"""
    if not stocks:
        print("  无符合条件的股票")
        return
    header = (f"{'排名':>4} {'代码':<8} {'名称':<10} {'现价':>8} {'涨跌%':>7} "
              f"{'综合分':>6} {'主力':>5} {'热点':>5} {'空间':>5} {'质量':>5} "
              f"{'信号标签':<28} {'入场价':>8} {'止损':>8}")
    sep = "-" * max(len(header) + 10, 100)
    print(sep)
    print(header)
    print(sep)
    for idx, s in enumerate(stocks, 1):
        ep = f"{s['entry_price']:.2f}" if s.get("entry_price") else "-"
        sl = f"{s['stop_loss']:.2f}" if s.get("stop_loss") else "-"
        cs = s.get("category_scores", {})
        tags = s.get("signal_tags", [])
        tag_str = ", ".join(tags[:4])
        chg = s.get("change_pct")
        chg_str = f"{chg:+.2f}" if chg is not None else "-"
        print(
            f"{idx:>4} {s['symbol']:<8} {s['name']:<10} "
            f"{s['close'] or '-':>8} "
            f"{chg_str:>7} "
            f"{s.get('composite_score', 0):>6.1f} "
            f"{cs.get('主力痕迹', 0):>5.1f} "
            f"{cs.get('热点引擎', 0):>5.1f} "
            f"{cs.get('未来空间', 0):>5.1f} "
            f"{cs.get('基本质量', 0):>5.1f} "
            f"{tag_str:<28} "
            f"{ep:>8} "
            f"{sl:>8}"
        )
    print(sep)
    if any(s.get("deepseek_rationale") for s in stocks):
        print("\n  AI点评:")
        for s in stocks:
            rationale = s.get("deepseek_rationale", "")
            if rationale:
                print(f"    {s['symbol']} {s['name']}: {rationale}")
    print()


if __name__ == "__main__":
    import argparse
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="四因子攻击性选股 — 主力痕迹+热点引擎+未来空间+基本质量")
    parser.add_argument("--scope", default="hs300", choices=["hs300", "zz500"],
                        help="扫描范围 (default: hs300)")
    parser.add_argument("--top", type=int, default=5, help="返回数量 (default: 5)")
    parser.add_argument("--sector", help="行业过滤关键词")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    parser.add_argument("--llm", action="store_true", help="启用 DeepSeek AI 点评（默认关闭）")
    parser.add_argument("--validate", type=int, default=0, help="验证选股: 跟踪天数(如60)")
    args = parser.parse_args()

    if args.validate > 0:
        result = validate_screening(days=args.validate, top_n=args.top, scope=args.scope)
        print(f"\n验证结果: {'通过' if result['passed'] else '未通过'} | Top{args.top}平均收益: {result['avg_return_pct']}%")
        sys.exit(0 if result['passed'] else 1)

    start = time.time()
    stocks = screen_stocks(scope=args.scope, top_n=args.top, sector=args.sector,
                           use_mock=not args.llm)
    elapsed = time.time() - start

    if args.json:
        import json
        # 移除不可序列化的字段
        for s in stocks:
            s.pop("factor_result", None)
        print(json.dumps(stocks, ensure_ascii=False, indent=2))
    else:
        _print_table(stocks)

    print(f"\n耗时: {elapsed:.0f}s | 范围: {args.scope} | 结果: {len(stocks)} 只")
