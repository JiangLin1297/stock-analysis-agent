#!/usr/bin/env python3
"""
A股数据获取管道 - 腾讯行情 + stockstats技术指标 + akshare辅助数据。
输出压缩 JSON，不发送原始K线数据给 LLM。
每个数据字段均附带来源 URL，支持数据溯源。

用法:
    py data_pipeline.py 600519           # 贵州茅台
    py data_pipeline.py 000001 --market A
"""

import sys
import os
import json
import time
import argparse
import functools
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import requests

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from stockstats import StockDataFrame
import akshare as ak

# ── 配置 ──────────────────────────────────────────────────
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Referer": "https://finance.qq.com/",
})
TIMEOUT = 15


def retry(max_attempts=3, base_delay=1.5):
    def deco(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_err = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_err = e
                    if attempt < max_attempts:
                        time.sleep(base_delay ** attempt)
            raise last_err
        return wrapper
    return deco


def run_with_timeout(func, timeout_sec=10, *args, **kwargs):
    """在线程池中运行 func，超时则返回 None 并打印警告。"""
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(func, *args, **kwargs)
        try:
            return future.result(timeout=timeout_sec)
        except FuturesTimeoutError:
            print(f"  ⚠ {func.__name__} 超时 ({timeout_sec}s)，跳过此数据源")
            return None
        except Exception as e:
            print(f"  ⚠ {func.__name__} 异常: {e}")
            return None


def normalize_symbol(symbol):
    symbol = str(symbol).strip()
    if '.' in symbol:
        symbol = symbol.split('.')[0]
    symbol = symbol.zfill(6)
    exchange = 'sh' if symbol.startswith(('6', '9')) else 'sz'
    return symbol, exchange


def _f(val):
    if val is None or val == '' or val == '-':
        return None
    try:
        return round(float(val), 4)
    except (ValueError, TypeError):
        return None


# ── 1. 实时行情 (腾讯 API) ─────────────────────────────────
@retry(max_attempts=3)
def fetch_spot(symbol, exchange):
    code = f"{exchange}{symbol}"
    url = f"https://qt.gtimg.cn/q={code}"
    resp = SESSION.get(url, timeout=TIMEOUT)
    resp.raise_for_status()
    text = resp.content.decode('gbk')

    if '="' not in text:
        return {"error": "腾讯行情格式异常"}

    fields = text.split('="', 1)[1].rstrip('";\n').split('~')
    if len(fields) < 40:
        return {"error": f"腾讯行情字段不足，仅{len(fields)}个"}

    return {
        "_source": f"腾讯行情 {url}",
        "_source_url": url,
        "name": fields[1],
        "price": _f(fields[3]),
        "prev_close": _f(fields[4]),
        "open": _f(fields[5]),
        "volume_lots": _f(fields[6]),
        "high": _f(fields[33]),
        "low": _f(fields[34]),
        "change_pct": _f(fields[32]),
        "change_amt": _f(fields[31]),
        "turnover": _f(fields[38]),
        "pe": _f(fields[39]),
        "pb": _f(fields[46]),
        "amount_wan": _f(fields[37]),
        "amplitude": _f(fields[43]),
        "market_cap": _f(fields[45]),
        "high_limit": _f(fields[47]),
        "low_limit": _f(fields[48]),
    }


# ── 2. 历史K线 + 技术指标 ─────────────────────────────────
@retry(max_attempts=3)
def fetch_kline_indicators(symbol, exchange, ndays=120):
    code = f"{exchange}{symbol}"
    kline_url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,{ndays},qfq"
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    params = {"param": f"{code},day,,,{ndays},qfq"}
    resp = SESSION.get(url, params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()

    if data.get("code") != 0:
        return {"error": f"腾讯K线API返回错误: {data.get('msg')}"}

    stock_data = data.get("data", {}).get(code, {})
    rows = stock_data.get("qfqday") or stock_data.get("day")
    if not rows:
        return {"error": "未获取到K线数据"}

    clean_rows = [
        r[:6] for r in rows
        if len(r) >= 6 and all(not isinstance(x, dict) for x in r[:6])
    ]
    df = pd.DataFrame(clean_rows, columns=["date", "open", "close", "high", "low", "volume"])
    for col in ["open", "close", "high", "low", "volume"]:
        df[col] = pd.to_numeric(df[col], errors='coerce')
    df['date'] = pd.to_datetime(df['date'])
    df = df.sort_values('date').reset_index(drop=True)
    df = df.dropna(subset=["close"])
    if len(df) < 5:
        return {"error": f"K线数据不足（仅{len(df)}条）"}

    df_ss = df.set_index('date')
    sdf = StockDataFrame(df_ss)

    def _ss(col):
        if col not in sdf.columns:
            return None
        val = sdf[col].iloc[-1]
        return round(float(val), 4) if not pd.isna(val) else None

    close = df['close']
    def _ma(s, w):
        v = s.rolling(w).mean().iloc[-1]
        return round(float(v), 2) if not pd.isna(v) else None
    def _rsi(s, p=14):
        d = s.diff()
        g = d.clip(lower=0).ewm(span=p, adjust=False).mean().iloc[-1]
        l = (-d).clip(lower=0).ewm(span=p, adjust=False).mean().iloc[-1]
        return 100.0 if l == 0 else round(100.0 - 100.0 / (1.0 + g / l), 2)
    def _boll(s, w=20):
        m = s.rolling(w).mean().iloc[-1]
        std = s.rolling(w).std().iloc[-1]
        if pd.isna(m): return None, None, None
        return (round(float(m + 2 * std), 2), round(float(m), 2), round(float(m - 2 * std), 2))
    def _macd(s):
        e12 = s.ewm(span=12, adjust=False).mean()
        e26 = s.ewm(span=26, adjust=False).mean()
        d = e12 - e26
        dea = d.ewm(span=9, adjust=False).mean()
        h = d - dea
        return (round(float(d.iloc[-1]), 4), round(float(dea.iloc[-1]), 4), round(float(h.iloc[-1]), 4))

    def _atr(s, w=14):
        h = df['high']
        l = df['low']
        prev_c = df['close'].shift(1)
        tr = pd.concat([
            h - l,
            (h - prev_c).abs(),
            (l - prev_c).abs()
        ], axis=1).max(axis=1)
        atr_val = tr.ewm(span=w, adjust=False).mean().iloc[-1]
        return round(float(atr_val), 2) if not pd.isna(atr_val) else None

    ub, mid, lb = _boll(close)
    dif, dea, hist = _macd(close)

    # 新增指标：MA10、昨日最低、20日最低、MA20斜率、MA5/MA20交叉次数、ATR
    ma10_val = _ma(close, 10)
    prev_low = float(df['low'].iloc[-2]) if len(df) >= 2 else None
    low_20d = round(float(df['low'].tail(20).min()), 2)
    ma20_series = close.rolling(20).mean()
    ma20_5d_ago = ma20_series.iloc[-6] if len(ma20_series) >= 6 else None
    ma20_slope = round(float((ma20_series.iloc[-1] - ma20_5d_ago) / ma20_5d_ago * 100), 2) if ma20_5d_ago and not pd.isna(ma20_5d_ago) and ma20_5d_ago != 0 else None
    # MA60斜率
    ma60_series = close.rolling(60).mean()
    ma60_now = ma60_series.iloc[-1] if len(ma60_series) >= 1 else None
    ma60_10d_ago = ma60_series.iloc[-11] if len(ma60_series) >= 11 else None
    ma60_slope = round(float((ma60_now - ma60_10d_ago) / ma60_10d_ago * 100), 2) if ma60_now and ma60_10d_ago and not pd.isna(ma60_10d_ago) and ma60_10d_ago != 0 else None
    ma5_series = close.rolling(5).mean()
    cross_count = 0
    if len(ma5_series) >= 40 and len(ma20_series) >= 40:
        recent_ma5 = ma5_series.tail(30)
        recent_ma20 = ma20_series.tail(30)
        above = (recent_ma5.values > recent_ma20.values)
        cross_count = int((above[1:] != above[:-1]).sum())

    # 成交量与布林带宽（用于入场信号 breakout_signals）
    vol = df['volume']
    avg_vol_20d = round(float(vol.rolling(20).mean().iloc[-1]), 2) if len(vol) >= 20 else None
    today_vol = round(float(vol.iloc[-1]), 2)
    boll_w = round(float((ub - lb) / mid), 4) if ub and lb and mid and mid > 0 else None
    boll_width_percentile = None
    if len(close) >= 60 and boll_w is not None:
        bw_list = []
        for i in range(max(0, len(close) - 60), len(close)):
            sub_c = close.iloc[max(0, i - 20 + 1):i + 1]
            if len(sub_c) >= 20:
                sm = sub_c.rolling(20).mean().iloc[-1]
                ss = sub_c.rolling(20).std().iloc[-1]
                if sm and ss and not pd.isna(sm) and not pd.isna(ss) and sm > 0:
                    bw_list.append((sm + 2 * ss - (sm - 2 * ss)) / sm)
        if bw_list:
            boll_width_percentile = round(float(sum(1 for b in bw_list if b <= boll_w) / len(bw_list) * 100), 1)

    return {
        "_source": f"腾讯前复权K线 {kline_url} + stockstats技术指标计算",
        "_kline_url": kline_url,
        "date": str(df['date'].iloc[-1].date()),
        "close": round(float(close.iloc[-1]), 2),
        "ma5": _ss('close_5_sma') or _ma(close, 5),
        "ma10": ma10_val,
        "ma20": _ss('close_20_sma') or _ma(close, 20),
        "ma60": _ss('close_60_sma') or _ma(close, 60),
        "rsi14": _ss('rsi_14') or _rsi(close, 14),
        "macd": _ss('macd') or dif,
        "macd_signal": _ss('macds') or dea,
        "macd_histogram": _ss('macdh') or hist,
        "boll_upper": _ss('boll_ub') or ub,
        "boll_mid": _ss('boll') or mid,
        "boll_lower": _ss('boll_lb') or lb,
        "kdj_k": _ss('kdjk'),
        "kdj_d": _ss('kdjd'),
        "kdj_j": _ss('kdjj'),
        "prev_low": prev_low,
        "low_20d": low_20d,
        "ma20_slope": ma20_slope,
        "ma60_slope": ma60_slope,
        "cross_count": cross_count,
        "avg_vol_20d": avg_vol_20d,
        "today_vol": today_vol,
        "boll_width": boll_w,
        "boll_width_percentile": boll_width_percentile,
        "atr14": _atr(close, 14),
        # 动量因子
        "momentum_20d": round(float((close.iloc[-1] - close.iloc[-21]) / close.iloc[-21] * 100), 2) if len(close) >= 21 and close.iloc[-21] != 0 else None,
        "momentum_60d": round(float((close.iloc[-1] - close.iloc[-61]) / close.iloc[-61] * 100), 2) if len(close) >= 61 and close.iloc[-61] != 0 else None,
        "is_52w_high": bool(close.iloc[-1] >= close.tail(250).max()) if len(close) >= 250 else None,
        "high_120d": round(float(close.tail(120).max()), 2) if len(close) >= 120 else None,
    }


# ── 3. 新闻 (含URL溯源) ───────────────────────────────────
@retry(max_attempts=2)
def fetch_news(symbol):
    """获取近5条新闻，每项包含 title + url + source。"""
    items = []

    # 来源1: akshare 东方财富个股新闻 (含URL)
    try:
        df = ak.stock_news_em(symbol=symbol)
        if df is not None and not df.empty:
            for _, row in df.head(10).iterrows():
                title, news_url = _extract_news_row(row, source="东方财富个股新闻")
                if title and len(items) < 5:
                    items.append({
                        "title": title[:80],
                        "url": news_url or f"https://so.eastmoney.com/news/s?keyword={symbol}",
                        "source": "东方财富",
                    })
    except Exception:
        pass

    if len(items) >= 5:
        return items

    # 来源2: akshare 财联社电报
    try:
        df = ak.stock_info_global_news()
        if df is not None and not df.empty:
            for _, row in df.head(10).iterrows():
                title, news_url = _extract_news_row(row, source="财联社电报")
                if title and len(items) < 5:
                    items.append({
                        "title": title[:80],
                        "url": news_url or "https://www.cls.cn/searchPage?keyword=" + symbol,
                        "source": "财联社",
                    })
    except Exception:
        pass

    return items


def _extract_news_row(row, source=""):
    """从 DataFrame 行提取标题和URL。"""
    title = None
    for col in ['标题', 'title', 'title_ch', 'content', '新闻标题', 'name']:
        if col in row.index:
            t = str(row[col])
            if t and t not in ('nan', 'None', ''):
                title = t[:80]
                break

    news_url = None
    for col in ['url', 'URL', '链接', '新闻链接', 'art_url', 'share_url']:
        if col in row.index:
            u = str(row[col])
            if u and u not in ('nan', 'None', '') and u.startswith('http'):
                news_url = u
                break

    return title, news_url


# ── 4. 财务数据 ───────────────────────────────────────────
@retry(max_attempts=2)
def fetch_financial(symbol):
    """
    从东方财富获取最新财务指标。
    使用 stock_financial_abstract（akshare 中比 analysis_indicator 更稳定）。
    """
    result = {
        "_source": f"东方财富财报 https://emweb.securities.eastmoney.com/pc_hsf10/pages/index.html?type=web&code=SH{symbol}&color=r#/cwfx",
    }
    try:
        df = ak.stock_financial_abstract(symbol=symbol)
        if df is None or df.empty:
            print(f"  ⚠ stock_financial_abstract 返回空，尝试 stock_financial_analysis_indicator 兜底")
            df = ak.stock_financial_analysis_indicator(symbol=symbol)

        if df is not None and not df.empty:
            # stock_financial_abstract 格式: 列='指标' + 各报告期，行=各项指标
            if '指标' in df.columns and len(df) > 3:
                # 最新报告期是第3列 (跳过 '选项' 和 '指标')
                latest_col = df.columns[2]
                def _get_val(keywords):
                    for kw in keywords:
                        mask = df['指标'].str.contains(kw, na=False, regex=False)
                        if mask.any():
                            val = df.loc[mask, latest_col].iloc[0]
                            return _f(val)
                    return None
                result["roe"] = _get_val(['净资产收益率(ROE)', '净资产收益率'])
                result["net_profit_growth"] = _get_val(['归属母公司净利润增长率', '净利润增长率'])
                result["revenue_growth"] = _get_val(['营业总收入增长率', '营业收入增长率', '营收增长率'])
                result["gross_margin"] = _get_val(['毛利率', '销售毛利率'])
                result["net_margin"] = _get_val(['净利率', '销售净利率'])
                result["debt_ratio"] = _get_val(['资产负债率'])
            else:
                # stock_financial_analysis_indicator 格式: 直接取最后一行
                latest = df.iloc[-1]
                result["roe"] = _f(latest.get('净资产收益率(%)', latest.get('净资产收益率', None)))
                result["net_profit_growth"] = _f(latest.get('净利润增长率(%)', latest.get('净利润增长率', None)))
                result["revenue_growth"] = _f(latest.get('营业收入增长率(%)', latest.get('营业收入增长率', None)))
                result["gross_margin"] = _f(latest.get('销售毛利率(%)', latest.get('销售毛利率', None)))
                result["net_margin"] = _f(latest.get('销售净利率(%)', latest.get('销售净利率', None)))
                result["debt_ratio"] = _f(latest.get('资产负债率(%)', latest.get('资产负债率', None)))
    except Exception as e:
        print(f"  ⚠ 财务数据获取异常: {e}")
    return result


# ── 5. 宏观占位 ───────────────────────────────────────────
def fetch_macro():
    return {
        "_source": "宏观数据占位符，后续接入 Tushare(https://tushare.pro) 或 Wind 终端",
        "shibor_overnight": None,
        "shibor_1w": None,
        "cpi_yoy": None,
        "pmi_manufacturing": None,
        "gdp_growth": None,
        "note": "宏观指标占位符，后续接入 Tushare/Wind 数据源补充"
    }


# ── 6. 最优入场价计算（纯本地）──────────────────────────────
def suggest_entry_price(symbol: str, market: str = "A") -> dict:
    """
    基于实时技术指标计算最优入场价格，纯本地计算，不调 LLM。

    三种市况判断：
      - 强势股（价格 > MA20 且 MA20 斜率向上）：追涨入场
      - 弱势股（价格 < MA20）：博反弹入场
      - 震荡市（MA5/MA20 反复交叉）：等待突破确认

    止损 = min(入场价*0.95, 布林下轨*0.98, 近20日最低价)
    """
    sym, exchange = normalize_symbol(symbol)

    spot = fetch_spot(sym, exchange)
    tech = fetch_kline_indicators(sym, exchange)

    close = spot.get("price") or tech.get("close")
    change_pct = spot.get("change_pct")
    ma5 = tech.get("ma5")
    ma10 = tech.get("ma10")
    ma20 = tech.get("ma20")
    ma60 = tech.get("ma60")
    rsi = tech.get("rsi14")
    boll_lower = tech.get("boll_lower")
    boll_mid = tech.get("boll_mid")
    boll_upper = tech.get("boll_upper")
    prev_low = tech.get("prev_low")
    low_20d = tech.get("low_20d")
    ma20_slope = tech.get("ma20_slope")
    cross_count = tech.get("cross_count", 0)

    if not close or not boll_lower or not boll_mid or not boll_upper:
        return {"error": "技术指标数据不足，无法计算入场价"}

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
            basis = f"强势股(RSI={rsi}>65)，激进追涨，入场=MA5={entry_price}"
        elif ma10 is not None and prev_low is not None:
            entry_price = round(max(ma10, prev_low), 2)
            entry_type = "ma10_chase"
            basis = f"强势股(价格>MA20,MA20斜率+{ma20_slope}%)，入场=max(MA10={ma10}, 昨低={prev_low})={entry_price}"
        elif ma10 is not None:
            entry_price = round(ma10, 2)
            entry_type = "ma10_chase"
            basis = f"强势股，入场=MA10={entry_price}"
        else:
            entry_price = round(close, 2)
            entry_type = "current_price"
            basis = "强势股但缺MA10/昨低数据，兜底=现价"

    elif is_weak:
        if close < boll_lower:
            entry_price = round(close, 2)
            entry_type = "bollinger_reversal"
            basis = f"弱势股(价格<MA20)，价格<布林下轨({boll_lower})，超卖反弹机会，入场=现价={entry_price}"
        else:
            entry_price = round(boll_lower * 1.02, 2)
            entry_type = "bollinger_reversal"
            basis = f"弱势股(价格<MA20)，入场=布林下轨*1.02={entry_price}"

    elif is_choppy:
        entry_price = round(boll_mid, 2)
        entry_type = "boll_mid_wait"
        basis = f"震荡市(MA5/MA20交叉{cross_count}次)，入场=布林中轨={entry_price}，等待突破确认"

    else:
        # 兜底：布林下轨支撑区域
        boll_support = round(boll_lower + (boll_mid - boll_lower) * 0.1, 2)
        if ma20 is not None and close > ma20:
            entry_price = round(ma20, 2)
            entry_type = "ma20_support"
            basis = f"价格>MA20，入场=MA20支撑={entry_price}"
        elif ma60 is not None and close > ma60:
            entry_price = round(ma60, 2)
            entry_type = "ma60_support"
            basis = f"价格在MA20-MA60间，入场=MA60={entry_price}"
        else:
            entry_price = boll_support
            entry_type = "boll_support"
            basis = f"兜底策略，入场=布林下轨支撑={entry_price}"

    # ── 入场区间 ──
    entry_range = [round(entry_price * 0.99, 2), round(entry_price * 1.02, 2)]

    # ── 止损 = min(入场价*0.95, 布林下轨*0.98, 近20日最低价) ──
    stop_candidates = [round(entry_price * 0.95, 2), round(boll_lower * 0.98, 2)]
    if low_20d is not None:
        stop_candidates.append(round(low_20d, 2))
    stop_loss = min(stop_candidates)

    return {
        "entry_price": entry_price,
        "entry_range": entry_range,
        "stop_loss": stop_loss,
        "entry_type": entry_type,
        "basis": basis,
        "warning": warning,
        "current_price": round(close, 2),
        "boll_lower": boll_lower,
        "boll_mid": boll_mid,
        "boll_upper": boll_upper,
        "ma5": ma5,
        "ma10": ma10,
        "ma20": ma20,
        "ma60": ma60,
        "prev_low": prev_low,
        "low_20d": low_20d,
    }


# ── 7. 突破信号计算 ───────────────────────────────────────
def _compute_breakout_signals(data: dict) -> dict:
    """从已获取的 quote + technical 数据中计算主动入场信号。"""
    q = data.get("quote", {})
    t = data.get("technical", {})

    price = q.get("price") or t.get("close")
    boll_upper = t.get("boll_upper")
    boll_lower = t.get("boll_lower")
    boll_mid = t.get("boll_mid")
    today_vol = t.get("today_vol")
    avg_vol_20d = t.get("avg_vol_20d")
    boll_width_pct = t.get("boll_width_percentile")
    change_pct = q.get("change_pct")
    amount_wan = q.get("amount_wan")

    signals = {
        "boll_breakout": False,
        "volume_ratio": None,
        "vcp": False,
        "surge_confirm": False,
        "breakout_score": 0,
    }

    # volume_ratio: 当日量 / 20日均量
    if today_vol and avg_vol_20d and avg_vol_20d > 0:
        vol_ratio = round(float(today_vol) / float(avg_vol_20d), 2)
        signals["volume_ratio"] = vol_ratio
    else:
        vol_ratio = None

    # boll_breakout: 价格放量突破布林上轨 且 成交量>1.5倍均量
    if (price and boll_upper and price > boll_upper
            and vol_ratio is not None and vol_ratio > 1.5):
        signals["boll_breakout"] = True
        signals["breakout_score"] += 3

    # VCP 波动收缩：布林带宽在近60日最窄20% 且 价格在支撑位上方
    if (boll_width_pct is not None and boll_width_pct <= 20
            and price and boll_lower and boll_mid
            and price > boll_lower and price < boll_upper):
        signals["vcp"] = True
        signals["breakout_score"] += 2

    # 放量上攻确认：当日涨幅>3% 且 量比>1.5
    if (change_pct is not None and change_pct > 3
            and vol_ratio is not None and vol_ratio > 1.5):
        signals["surge_confirm"] = True
        signals["breakout_score"] += 4

    # 动量因子评分
    mom_20d = t.get("momentum_20d")
    mom_60d = t.get("momentum_60d")
    is_52w_high = t.get("is_52w_high")
    signals["momentum_score"] = 0
    signals["momentum_rank"] = None
    signals["is_52w_high"] = is_52w_high

    if mom_20d is not None:
        signals["momentum_20d"] = mom_20d
        if mom_20d > 5:
            signals["momentum_score"] += 2
            signals["breakout_score"] += 1
    if mom_60d is not None:
        signals["momentum_60d"] = mom_60d
        if mom_60d > 30:
            signals["momentum_score"] += 3
            signals["breakout_score"] += 2
    if is_52w_high:
        signals["momentum_score"] += 2
        signals["breakout_score"] += 1

    return signals


def _compute_trend_state(quote: dict, technical: dict) -> dict:
    """
    计算当前市场趋势状态。
    BULL: 价格 > MA60 且 MA60 斜率 > 0
    BEAR: 价格 < MA60 且 MA60 斜率 < 0
    SIDEWAYS: 其他所有情况
    """
    price = quote.get("price") or technical.get("close")
    ma60 = technical.get("ma60")
    ma60_slope = technical.get("ma60_slope")

    if price is None or ma60 is None:
        return {"trend_state": "SIDEWAYS", "ma60_slope": None, "reason": "数据不足"}

    slope = ma60_slope if ma60_slope is not None else 0

    if price > ma60 and slope > 0:
        state = "BULL"
        reason = f"价格{price}>MA60({ma60})+MA60斜率{slope}%>0"
    elif price < ma60 and slope < 0:
        state = "BEAR"
        reason = f"价格{price}<MA60({ma60})+MA60斜率{slope}%<0"
    else:
        state = "SIDEWAYS"
        if abs(slope) < 0.1:
            reason = f"MA60斜率{slope}%≈0，横盘震荡"
        elif price > ma60 and slope < 0:
            reason = f"价格>MA60但斜率{slope}%<0，趋势转弱"
        else:
            reason = f"价格<MA60但斜率{slope}%>0，趋势转强"

    return {"trend_state": state, "ma60_slope": slope, "reason": reason}


def _compute_weekly_trend(df: pd.DataFrame) -> dict:
    """
    从日线DataFrame聚合周线，计算周线MA10/MA20，判断周线趋势。

    UP: 周线MA10 > 周线MA20 且 周线收盘 > 周线MA10
    其他: DOWN/SIDEWAYS
    """
    if len(df) < 100:
        return {"weekly_trend": "UNKNOWN", "weekly_ma10": None, "weekly_ma20": None,
                "weekly_close": None, "reason": "数据不足(需>=100日)"}

    ohlc = df.set_index('date').resample('W').agg({
        'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'
    }).dropna()

    if len(ohlc) < 30:
        return {"weekly_trend": "UNKNOWN", "weekly_ma10": None, "weekly_ma20": None,
                "weekly_close": None, "reason": "周线数据不足(需>=30周)"}

    w_close = ohlc['close']
    w_ma10 = round(float(w_close.rolling(10).mean().iloc[-1]), 2)
    w_ma20 = round(float(w_close.rolling(20).mean().iloc[-1]), 2)
    w_last_close = round(float(w_close.iloc[-1]), 2)

    if pd.isna(w_ma10) or pd.isna(w_ma20):
        return {"weekly_trend": "UNKNOWN", "weekly_ma10": w_ma10, "weekly_ma20": w_ma20,
                "weekly_close": w_last_close, "reason": "周线MA计算失败"}

    if w_ma10 > w_ma20 and w_last_close > w_ma10:
        trend = "UP"
        reason = f"周线MA10({w_ma10})>MA20({w_ma20})+收盘({w_last_close})>MA10"
    elif w_ma10 < w_ma20 and w_last_close < w_ma10:
        trend = "DOWN"
        reason = f"周线MA10({w_ma10})<MA20({w_ma20})+收盘({w_last_close})<MA10"
    else:
        trend = "SIDEWAYS"
        reason = f"周线MA10({w_ma10})vsMA20({w_ma20})交叉或收盘({w_last_close})在MA附近"

    return {"weekly_trend": trend, "weekly_ma10": w_ma10, "weekly_ma20": w_ma20,
            "weekly_close": w_last_close, "reason": reason}


# ── 8. 公开入口 ───────────────────────────────────────────
def get_compressed_data(symbol: str, market: str = "A") -> dict:
    """
    公开 API：输入股票代码，返回压缩数据字典（含数据溯源）。
    供 agent_runner / debate_engine / decision_engine 调用。
    """
    symbol, exchange = normalize_symbol(symbol)
    t = datetime.now().isoformat()

    result = {
        "symbol": symbol,
        "exchange": exchange,
        "market": market,
        "timestamp": t,
        "_meta": {
            "generated_at": t,
            "data_sources": {
                "real_time_quote": "腾讯证券行情 https://qt.gtimg.cn/",
                "historical_kline": "腾讯前复权日K线 https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
                "technical_indicators": "stockstats库 + pandas手动兜底",
                "news": "东方财富个股新闻 / 财联社电报 (via akshare)",
                "financial": "东方财富财报分析 (via akshare)",
                "macro": "占位符，待接入Tushare/Wind",
            },
            "disclaimer": "数据仅供研究参考，不构成投资建议。数据可能有延迟。",
        },
        "_momentum": {
            "note": "动量因子已整合至breakout_signals和technical中",
        },
    }

    # 各数据源独立获取，单个失败不影响整体
    for name, fn, args, timeout_sec in [
        ("quote", fetch_spot, (symbol, exchange), 10),
        ("technical", fetch_kline_indicators, (symbol, exchange), 15),
        ("news", fetch_news, (symbol,), 15),
        ("financial", fetch_financial, (symbol,), 15),
        ("macro", fetch_macro, (), 5),
    ]:
        try:
            result[name] = run_with_timeout(fn, timeout_sec, *args) or {}
        except Exception as e:
            print(f"  ⚠ {name} 获取失败: {e}")
            result[name] = {"error": str(e)}

    # 计算突破入场信号
    result["breakout_signals"] = _compute_breakout_signals(result)

    # 计算趋势状态
    result["trend_state"] = _compute_trend_state(result.get("quote", {}), result.get("technical", {}))

    # 计算周线趋势（从缓存历史数据）
    try:
        cache_path = download_full_history(symbol, ndays=800)
        df_hist = pd.read_csv(cache_path)
        df_hist['date'] = pd.to_datetime(df_hist['date'])
        result["weekly_trend"] = _compute_weekly_trend(df_hist)
    except Exception:
        result["weekly_trend"] = {"weekly_trend": "UNKNOWN", "reason": "历史数据不可用"}

    return result


# ── 9. 历史数据缓存与快照 ──────────────────────────────────
HISTORY_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "history_cache")


def _ensure_cache_dir():
    os.makedirs(HISTORY_CACHE_DIR, exist_ok=True)


def download_full_history(symbol: str, ndays: int = 800) -> str:
    """
    下载全量历史日线数据并缓存到本地 CSV。
    返回缓存文件路径。若已存在有效缓存则跳过下载。
    """
    _ensure_cache_dir()
    sym, exchange = normalize_symbol(symbol)
    cache_path = os.path.join(HISTORY_CACHE_DIR, f"{sym}.csv")

    # 检查缓存是否有效（24小时内）
    if os.path.exists(cache_path):
        mtime = os.path.getmtime(cache_path)
        if time.time() - mtime < 86400:
            try:
                df = pd.read_csv(cache_path)
                if len(df) >= 50:
                    return cache_path
            except Exception:
                pass

    print(f"  [历史数据] 下载 {sym} 近{ndays}天数据...")
    code = f"{exchange}{sym}"
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    params = {"param": f"{code},day,,,{ndays},qfq"}
    resp = SESSION.get(url, params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()

    if data.get("code") != 0:
        raise RuntimeError(f"K线API错误: {data.get('msg')}")

    stock_data = data.get("data", {}).get(code, {})
    rows = stock_data.get("qfqday") or stock_data.get("day")
    if not rows:
        raise RuntimeError("未获取到K线数据")

    clean_rows = [
        r[:6] for r in rows
        if len(r) >= 6 and all(not isinstance(x, dict) for x in r[:6])
    ]
    df = pd.DataFrame(clean_rows, columns=["date", "open", "close", "high", "low", "volume"])
    for col in ["open", "close", "high", "low", "volume"]:
        df[col] = pd.to_numeric(df[col], errors='coerce')
    df['date'] = pd.to_datetime(df['date'])
    df = df.sort_values('date').reset_index(drop=True)
    df = df.dropna(subset=["close"])

    df.to_csv(cache_path, index=False)
    print(f"  [历史数据] 已缓存 {len(df)} 条日线 → {cache_path}")
    return cache_path


def _compute_indicators_from_df(df: pd.DataFrame) -> dict:
    """从 DataFrame 计算与 fetch_kline_indicators 相同格式的技术指标字典。"""
    close = df['close']
    vol = df['volume']

    def _ma(s, w):
        v = s.rolling(w).mean().iloc[-1]
        return round(float(v), 2) if not pd.isna(v) else None

    def _rsi(s, p=14):
        d = s.diff()
        g = d.clip(lower=0).ewm(span=p, adjust=False).mean().iloc[-1]
        l = (-d).clip(lower=0).ewm(span=p, adjust=False).mean().iloc[-1]
        if l == 0 or pd.isna(l):
            return 100.0
        return round(100.0 - 100.0 / (1.0 + g / l), 2)

    def _boll(s, w=20):
        m = s.rolling(w).mean().iloc[-1]
        std = s.rolling(w).std().iloc[-1]
        if pd.isna(m):
            return None, None, None
        return (round(float(m + 2 * std), 2), round(float(m), 2), round(float(m - 2 * std), 2))

    def _macd(s):
        e12 = s.ewm(span=12, adjust=False).mean()
        e26 = s.ewm(span=26, adjust=False).mean()
        d = e12 - e26
        dea = d.ewm(span=9, adjust=False).mean()
        h = d - dea
        return (round(float(d.iloc[-1]), 4), round(float(dea.iloc[-1]), 4), round(float(h.iloc[-1]), 4))

    def _atr(s, w=14):
        h = df['high']
        l = df['low']
        prev_c = df['close'].shift(1)
        tr = pd.concat([
            h - l,
            (h - prev_c).abs(),
            (l - prev_c).abs()
        ], axis=1).max(axis=1)
        atr_val = tr.ewm(span=w, adjust=False).mean().iloc[-1]
        return round(float(atr_val), 2) if not pd.isna(atr_val) else None

    ub, mid, lb = _boll(close)
    dif, dea, hist = _macd(close)

    ma10_val = _ma(close, 10)
    prev_low = float(df['low'].iloc[-2]) if len(df) >= 2 else None
    low_20d = round(float(df['low'].tail(20).min()), 2)
    ma20_series = close.rolling(20).mean()
    ma20_5d_ago = ma20_series.iloc[-6] if len(ma20_series) >= 6 else None
    ma20_slope = round(float((ma20_series.iloc[-1] - ma20_5d_ago) / ma20_5d_ago * 100), 2) if ma20_5d_ago and not pd.isna(ma20_5d_ago) and ma20_5d_ago != 0 else None
    # MA60斜率
    ma60_series = close.rolling(60).mean()
    ma60_now = ma60_series.iloc[-1] if len(ma60_series) >= 1 else None
    ma60_10d_ago = ma60_series.iloc[-11] if len(ma60_series) >= 11 else None
    ma60_slope = round(float((ma60_now - ma60_10d_ago) / ma60_10d_ago * 100), 2) if ma60_now and ma60_10d_ago and not pd.isna(ma60_10d_ago) and ma60_10d_ago != 0 else None
    ma5_series = close.rolling(5).mean()
    cross_count = 0
    if len(ma5_series) >= 40 and len(ma20_series) >= 40:
        recent_ma5 = ma5_series.tail(30)
        recent_ma20 = ma20_series.tail(30)
        above = (recent_ma5.values > recent_ma20.values)
        cross_count = int((above[1:] != above[:-1]).sum())

    avg_vol_20d = round(float(vol.rolling(20).mean().iloc[-1]), 2) if len(vol) >= 20 else None
    today_vol = round(float(vol.iloc[-1]), 2)
    boll_w = round(float((ub - lb) / mid), 4) if ub and lb and mid and mid > 0 else None
    boll_width_percentile = None
    if len(close) >= 60 and boll_w is not None:
        bw_list = []
        for i in range(max(0, len(close) - 60), len(close)):
            sub_c = close.iloc[max(0, i - 20 + 1):i + 1]
            if len(sub_c) >= 20:
                sm = sub_c.rolling(20).mean().iloc[-1]
                ss = sub_c.rolling(20).std().iloc[-1]
                if sm and ss and not pd.isna(sm) and not pd.isna(ss) and sm > 0:
                    bw_list.append((sm + 2 * ss - (sm - 2 * ss)) / sm)
        if bw_list:
            boll_width_percentile = round(float(sum(1 for b in bw_list if b <= boll_w) / len(bw_list) * 100), 1)

    close_val = round(float(close.iloc[-1]), 2)
    open_val = round(float(df['open'].iloc[-1]), 2)
    high_val = round(float(df['high'].iloc[-1]), 2)
    low_val = round(float(df['low'].iloc[-1]), 2)

    prev_close = round(float(close.iloc[-2]), 2) if len(close) >= 2 else close_val
    change_pct = round((close_val - prev_close) / prev_close * 100, 2) if prev_close != 0 else 0

    return {
        "_source": f"历史K线快照 (从缓存计算)",
        "date": str(df['date'].iloc[-1].date()),
        "close": close_val,
        "open": open_val,
        "high": high_val,
        "low": low_val,
        "volume": today_vol,
        "ma5": _ma(close, 5),
        "ma10": ma10_val,
        "ma20": _ma(close, 20),
        "ma60": _ma(close, 60),
        "rsi14": _rsi(close, 14),
        "macd": dif,
        "macd_signal": dea,
        "macd_histogram": hist,
        "boll_upper": ub,
        "boll_mid": mid,
        "boll_lower": lb,
        "kdj_k": None,
        "kdj_d": None,
        "kdj_j": None,
        "prev_low": prev_low,
        "low_20d": low_20d,
        "ma20_slope": ma20_slope,
        "ma60_slope": ma60_slope,
        "cross_count": cross_count,
        "avg_vol_20d": avg_vol_20d,
        "today_vol": today_vol,
        "boll_width": boll_w,
        "boll_width_percentile": boll_width_percentile,
        "atr14": _atr(close, 14),
        # 动量因子
        "momentum_20d": round(float((close.iloc[-1] - close.iloc[-21]) / close.iloc[-21] * 100), 2) if len(close) >= 21 and close.iloc[-21] != 0 else None,
        "momentum_60d": round(float((close.iloc[-1] - close.iloc[-61]) / close.iloc[-61] * 100), 2) if len(close) >= 61 and close.iloc[-61] != 0 else None,
        "is_52w_high": bool(close.iloc[-1] >= close.tail(250).max()) if len(close) >= 250 else None,
        "high_120d": round(float(close.tail(120).max()), 2) if len(close) >= 120 else None,
    }


def get_historical_snapshot(symbol: str, target_date, market: str = "A") -> dict:
    """
    获取指定日期的历史数据快照，格式与 get_compressed_data 兼容。
    所有技术指标仅使用 target_date 及之前的数据计算（无未来信息）。

    Args:
        symbol: 股票代码
        target_date: 目标日期，str('2024-06-15') 或 datetime/date 对象
        market: 市场类型
    Returns:
        与 get_compressed_data 相同格式的压缩数据字典
    """
    from datetime import date as _date, datetime as _dt

    if isinstance(target_date, str):
        target_date = _dt.strptime(target_date, "%Y-%m-%d").date()
    elif isinstance(target_date, _dt):
        target_date = target_date.date()
    elif isinstance(target_date, pd.Timestamp):
        target_date = target_date.date()

    # 确保历史数据已缓存
    cache_path = download_full_history(symbol, ndays=800)
    df = pd.read_csv(cache_path)
    df['date'] = pd.to_datetime(df['date'])

    # 只取 target_date 及之前的数据
    mask = df['date'] <= pd.Timestamp(target_date)
    if not mask.any():
        raise ValueError(f"缓存中无 {target_date} 及之前的数据（最早: {df['date'].iloc[0].date()}）")
    df_hist = df[mask].copy()
    if len(df_hist) < 30:
        raise ValueError(f"{target_date} 之前的数据不足（仅{len(df_hist)}条），至少需要30条计算指标")

    df_hist = df_hist.reset_index(drop=True)
    target_dt = pd.Timestamp(target_date)

    # 计算技术指标（仅基于截至 target_date 的数据）
    sym, exchange = normalize_symbol(symbol)
    tech = _compute_indicators_from_df(df_hist)

    close_val = float(df_hist['close'].iloc[-1])
    open_val = float(df_hist['open'].iloc[-1])
    high_val = float(df_hist['high'].iloc[-1])
    low_val = float(df_hist['low'].iloc[-1])
    prev_close = float(df_hist['close'].iloc[-2]) if len(df_hist) >= 2 else close_val
    change_pct = round((close_val - prev_close) / prev_close * 100, 2) if prev_close != 0 else 0
    today_vol = float(df_hist['volume'].iloc[-1])

    result = {
        "symbol": symbol,
        "exchange": exchange,
        "market": market,
        "timestamp": str(target_date),
        "_meta": {
            "generated_at": str(target_date),
            "historical_mode": True,
            "data_sources": {
                "real_time_quote": f"历史K线快照(截至{target_date})",
                "historical_kline": cache_path,
                "technical_indicators": "pandas手动计算(无未来信息)",
                "news": "历史回测模式-新闻不可用",
                "financial": "历史回测模式-财务数据不可用",
                "macro": "历史回测模式-宏观数据不可用",
            },
            "disclaimer": "历史回测数据仅供研究参考，不构成投资建议。",
        },
        "quote": {
            "_source": f"历史快照 {target_date}",
            "name": symbol,
            "price": close_val,
            "prev_close": prev_close,
            "open": open_val,
            "volume_lots": today_vol,
            "high": high_val,
            "low": low_val,
            "change_pct": change_pct,
            "change_amt": round(close_val - prev_close, 4),
            "turnover": None,
            "pe": None,
            "pb": None,
            "amount_wan": None,
            "amplitude": round((high_val - low_val) / prev_close * 100, 2) if prev_close > 0 else None,
            "market_cap": None,
            "high_limit": None,
            "low_limit": None,
        },
        "technical": tech,
        "news": [{"title": "历史回测模式-新闻不可用", "url": "", "source": "historical"}],
        "financial": {"_source": "历史回测模式-财务不可用", "note": "回测时不使用财务数据"},
        "macro": {"_source": "历史回测模式-宏观不可用", "note": "回测时不使用宏观数据"},
    }

    result["breakout_signals"] = _compute_breakout_signals(result)

    # 计算趋势状态
    result["trend_state"] = _compute_trend_state(result.get("quote", {}), result.get("technical", {}))

    # 计算周线趋势
    result["weekly_trend"] = _compute_weekly_trend(df_hist)

    return result


# ── CLI 入口 ──────────────────────────────────────────────
def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8')

    parser = argparse.ArgumentParser(description='A股数据获取管道')
    parser.add_argument('symbol', help='股票代码，如 600519（贵州茅台）')
    parser.add_argument('--market', default='A', help='市场类型，默认 A')
    parser.add_argument('--pretty', action='store_true', help='格式化输出')
    args = parser.parse_args()

    data = get_compressed_data(args.symbol, args.market)

    if args.pretty:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(data, ensure_ascii=False, separators=(',', ':')))


if __name__ == '__main__':
    main()
