#!/usr/bin/env python3
"""
Alpha 因子计算引擎 — 纯统计因子，不调用 LLM。

所有因子基于 data_pipeline 提供的日线数据和基础财务数据本地计算。
因子标准化后合成综合评分，驱动买卖决策。

用法:
    py alpha_factors.py 600744              # 计算并展示所有因子
    py alpha_factors.py 600744 --timeframe mid    # 计算综合评分
    py alpha_factors.py --rank hs300 --top 10     # 排名选股
"""
import sys
import os
import json
import math
from datetime import datetime
from collections import defaultdict

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FACTOR_WEIGHTS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "factor_weights.json")

# ── 默认因子权重（若 factor_weights.json 不存在） ──
DEFAULT_WEIGHTS = {
    "short": {
        "momentum_20d": 0.18,
        "momentum_60d": 0.09,
        "volatility_20d": -0.09,
        "avg_turnover_20d": 0.09,
        "breakout_20d_high": 0.135,
        "volume_ratio": 0.09,
        "north_flow_days": 0.09,
        "margin_change": 0.045,
        "ma_bull_alignment": 0.09,
        "market_cap_factor": 0.10,
        "threshold": 55,
    },
    "mid": {
        "momentum_60d": 0.12,
        "momentum_120d": 0.08,
        "roe_ttm": 0.15,
        "roe_stability": 0.10,
        "pe_percentile": 0.12,
        "pb_percentile": 0.08,
        "gross_margin_trend": 0.10,
        "ma_bull_alignment": 0.15,
        "breakout_20d_high": 0.05,
        "excess_return_vs_index": 0.05,
        "threshold": 60,
    },
    "long": {
        "momentum_120d": 0.05,
        "roe_ttm": 0.15,
        "roe_stability": 0.15,
        "pe_percentile": 0.12,
        "pb_percentile": 0.08,
        "gross_margin_trend": 0.15,
        "excess_return_vs_index": 0.05,
        "ma_bull_alignment": 0.10,
        "volatility_20d": -0.05,
        "avg_turnover_20d": 0.05,
        "market_cap_factor": 0.05,
        "threshold": 55,
    },
}

# ── Z-score 标准化缓存 ──
_zscore_cache = {}  # {factor_name: {"mean": x, "std": y}}


def _load_weights():
    """加载因子权重配置。若配置文件不存在，返回默认值并写入。"""
    if os.path.exists(FACTOR_WEIGHTS_PATH):
        try:
            with open(FACTOR_WEIGHTS_PATH, 'r', encoding='utf-8') as f:
                w = json.load(f)
            return w
        except Exception:
            pass
    # 写出默认配置
    os.makedirs(os.path.dirname(FACTOR_WEIGHTS_PATH), exist_ok=True)
    with open(FACTOR_WEIGHTS_PATH, 'w', encoding='utf-8') as f:
        json.dump(DEFAULT_WEIGHTS, f, ensure_ascii=False, indent=2)
    return dict(DEFAULT_WEIGHTS)


def _safe_float(v):
    """将值安全转为 float，失败返回 None。"""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    try:
        return round(float(v), 6)
    except (ValueError, TypeError):
        return None


def _zscore(val, factor_name, population=None):
    """
    Z-score 标准化。若提供 population（同批股票的值列表），则基于该群体计算；
    否则使用预设的历史均值和标准差。
    """
    if val is None:
        return None
    if population and len(population) >= 5:
        pop_clean = [v for v in population if v is not None]
        if len(pop_clean) >= 3:
            mu = np.mean(pop_clean)
            sigma = np.std(pop_clean)
            if sigma > 0:
                _zscore_cache[factor_name] = {"mean": float(mu), "std": float(sigma)}
                return round((val - mu) / sigma, 4)
    cached = _zscore_cache.get(factor_name)
    if cached and cached["std"] > 0:
        return round((val - cached["mean"]) / cached["std"], 4)
    # Fallback: min-max normalization to [-3, 3]
    return round(max(-3.0, min(3.0, val / 10.0)), 4)


# ═══════════════════════════════════════════════════════════════
# 1. 基础因子函数
# ═══════════════════════════════════════════════════════════════

def momentum_20d(df: pd.DataFrame) -> float:
    """过去20日累计收益率（%）。"""
    if len(df) < 21:
        return None
    close = df["close"].values
    return _safe_float((close[-1] / close[-21] - 1) * 100)


def momentum_60d(df: pd.DataFrame) -> float:
    """过去60日累计收益率（%）。"""
    if len(df) < 61:
        return None
    close = df["close"].values
    return _safe_float((close[-1] / close[-61] - 1) * 100)


def momentum_120d(df: pd.DataFrame) -> float:
    """过去120日累计收益率（%）。"""
    if len(df) < 121:
        return None
    close = df["close"].values
    return _safe_float((close[-1] / close[-121] - 1) * 100)


def volatility_20d(df: pd.DataFrame) -> float:
    """20日年化波动率（%）。"""
    if len(df) < 21:
        return None
    close = df["close"]
    daily_ret = close.pct_change().dropna()
    if len(daily_ret) < 20:
        return None
    std_daily = float(daily_ret.tail(20).std())
    return _safe_float(std_daily * math.sqrt(252) * 100)


def avg_turnover_20d(df: pd.DataFrame) -> float:
    """20日平均换手率（%）。若无 volume 数据则返回 None。"""
    if "volume" not in df.columns and "turnover" not in df.columns:
        return None
    if "turnover" in df.columns:
        return _safe_float(df["turnover"].tail(20).mean())
    return None


def volume_ratio(df: pd.DataFrame) -> float:
    """当日成交量 / 20日均量。"""
    if "volume" not in df.columns or len(df) < 21:
        return None
    vol = df["volume"]
    today = float(vol.iloc[-1])
    avg = float(vol.tail(21).head(20).mean())
    if avg == 0:
        return None
    return _safe_float(today / avg)


def breakout_20d_high(df: pd.DataFrame) -> bool:
    """今日收盘是否为20日最高（含今日）。"""
    if len(df) < 20:
        return False
    close = df["close"]
    today = float(close.iloc[-1])
    high_20 = float(close.tail(20).max())
    return today >= high_20 * 0.995


def ma_bull_alignment(df: pd.DataFrame) -> int:
    """
    短期均线多头排列天数：统计最近连续 MA5 > MA10 > MA20 > MA60 的天数。
    返回 0~60 的整数。
    """
    if len(df) < 60:
        return 0
    close = df["close"]
    ma5 = close.rolling(5).mean()
    ma10 = close.rolling(10).mean()
    ma20 = close.rolling(20).mean()
    ma60 = close.rolling(60).mean()
    aligned = (ma5 > ma10) & (ma10 > ma20) & (ma20 > ma60)
    if aligned.empty:
        return 0
    count = 0
    for v in reversed(aligned.values):
        if v and not (isinstance(v, float) and math.isnan(v)):
            count += 1
        else:
            break
    return count


def excess_return_vs_index(df: pd.DataFrame, index_df: pd.DataFrame = None) -> float:
    """
    相对市场平均的超额收益。
    若未提供 index_df，用 20日 A 股平均涨跌幅近似（取 1% 作为基准）。
    返回超额收益（%）。
    """
    if len(df) < 21:
        return None
    stock_ret = momentum_20d(df)
    if index_df is not None and len(index_df) >= 21:
        idx_close = index_df["close"].values
        idx_ret = (idx_close[-1] / idx_close[-21] - 1) * 100
    else:
        # 用 20 日约 1% 作为市场基准（A 股历史均值）
        idx_ret = 1.0
    return _safe_float(stock_ret - idx_ret) if stock_ret is not None else None


# ═══════════════════════════════════════════════════════════════
# 2. 财务因子（需要 financials dict）
# ═══════════════════════════════════════════════════════════════

def roe_ttm(financials: dict) -> float:
    """ROE（直接从 financials 提取，若仅有单季则返回单季值）。"""
    roe = financials.get("roe") if financials else None
    return _safe_float(roe)


def roe_stability(financials: dict) -> float:
    """
    ROE 稳定性：1 - (ROE标准差 / |ROE均值|)，归一化到 [0, 1]。
    1.0 = 极度稳定，0 = 极度不稳定。
    由于仅有最新 ROE，此处用负债率和毛利率的稳定性作为代理。
    """
    if not financials:
        return None
    # 代理指标：debt_ratio < 60 且 gross_margin > 20 → 稳定
    debt = _safe_float(financials.get("debt_ratio"))
    gm = _safe_float(financials.get("gross_margin"))
    if debt is None or gm is None:
        return None
    stability = 1.0
    if debt > 70:
        stability -= 0.4
    elif debt > 50:
        stability -= 0.2
    if gm < 15:
        stability -= 0.3
    elif gm < 25:
        stability -= 0.1
    return round(max(0.1, stability), 4)


def gross_margin_trend(financials: dict) -> float:
    """毛利率趋势（代理：用最新毛利率与行业基准对比，正值=优于均值）。"""
    if not financials:
        return None
    gm = _safe_float(financials.get("gross_margin"))
    if gm is None:
        return None
    # 30% 作为 A 股中位毛利率参考
    return _safe_float(gm - 30.0)


def pe_percentile(quote: dict, technical: dict) -> float:
    """
    PE 分位数代理：当前 PE vs 合理区间。
    返回 0~1，越小表示估值越低。
    若无 PE 数据，用价格/MA60偏离度作为代理。
    """
    pe = quote.get("pe") if quote else None
    if pe is not None:
        pe = _safe_float(pe)
        if pe <= 0:
            return None
        # PE 映射到分位数：PE<10 → 0.1, PE10-20 → 0.3, PE20-30 → 0.5, PE30-50 → 0.7, PE>50 → 0.9
        if pe < 10:
            return 0.1
        elif pe < 20:
            return 0.3
        elif pe < 30:
            return 0.5
        elif pe < 50:
            return 0.7
        else:
            return 0.9
    # 代理：价格/MA60 偏离
    price = _safe_float(technical.get("close")) if technical else None
    ma60 = _safe_float(technical.get("ma60")) if technical else None
    if price and ma60 and ma60 > 0:
        ratio = price / ma60
        return round(min(0.95, max(0.05, ratio - 0.5)), 4)
    return None


def pb_percentile(quote: dict) -> float:
    """PB 分位数代理。若无 PB 数据返回 None。"""
    pb = quote.get("pb") if quote else None
    if pb is None:
        return None
    pb = _safe_float(pb)
    if pb <= 0:
        return None
    if pb < 1:
        return 0.1
    elif pb < 2:
        return 0.3
    elif pb < 4:
        return 0.5
    elif pb < 8:
        return 0.7
    else:
        return 0.9


def market_cap_factor(quote: dict) -> float:
    """市值因子：小市值加分。返回标准化值（越小越好）。"""
    mc = quote.get("market_cap") if quote else None
    if mc is None:
        return None
    mc = _safe_float(mc)
    if mc < 100:
        return 1.0
    elif mc < 300:
        return 0.7
    elif mc < 500:
        return 0.5
    elif mc < 1000:
        return 0.3
    else:
        return 0.1


def north_flow_days(df: pd.DataFrame) -> int:
    """北向资金连续净流入天数（预留接口，暂返回 0）。"""
    return 0


def margin_change(technical: dict) -> float:
    """融资余额近5日变化率（预留接口，暂返回 0）。"""
    return 0.0


# ═══════════════════════════════════════════════════════════════
# 1b. 攻击性因子 — 主力痕迹 / 热点引擎 / 未来空间 / 质量底线
# ═══════════════════════════════════════════════════════════════

def obv_divergence(df: pd.DataFrame) -> float:
    """
    OBV 底部背离: 比较近3日股价涨跌与OBV涨跌方向。
    价格下跌但OBV上升 → 底部吸筹信号，返回 +5。
    价格持平但OBV上升 → 次级信号，返回 +3。
    返回 0~10。
    """
    if df is None or len(df) < 20:
        return 0.0
    close = df["close"].values
    volume = df["volume"].values.astype(float) if "volume" in df.columns else None
    if volume is None or len(volume) < 20:
        return 0.0

    # 计算OBV
    obv = np.zeros(len(close))
    for i in range(1, len(close)):
        if close[i] > close[i - 1]:
            obv[i] = obv[i - 1] + volume[i]
        elif close[i] < close[i - 1]:
            obv[i] = obv[i - 1] - volume[i]
        else:
            obv[i] = obv[i - 1]

    # 近3日: 股价方向 vs OBV方向
    price_dir = close[-1] - close[-4]
    obv_dir = obv[-1] - obv[-4]

    # 20日窗口比较
    recent_close = close[-20:]
    prev_close = close[-40:-20] if len(close) >= 40 else close[:20]
    recent_obv = obv[-20:]
    prev_obv = obv[-40:-20] if len(obv) >= 40 else obv[:20]

    score = 0.0
    # 近3日背离
    if price_dir < 0 and obv_dir > 0:
        score = max(score, 5.0)
    # 20日价格新低但OBV未新低
    if len(prev_close) > 0:
        if min(recent_close) < min(prev_close) * 0.98 and min(recent_obv) > min(prev_obv):
            score = max(score, 5.0)
        elif abs(np.mean(recent_close) / np.mean(prev_close) - 1) < 0.03 and \
             np.mean(recent_obv) > np.mean(prev_obv) * 1.1:
            score = max(score, 3.0)

    return score


def volume_price_divergence(df: pd.DataFrame) -> float:
    """
    量价背离: 放量滞涨（5日均量>20日均量1.8倍且涨幅<3%）→ 主力吸筹。
    放量突破（5日均量>20日均量1.5倍且涨幅>5%）→ 趋势启动。
    返回 0~10。
    """
    if df is None or len(df) < 20:
        return 0.0
    if "volume" not in df.columns:
        return 0.0

    close = df["close"]
    volume = df["volume"]
    vol_5 = float(volume.tail(5).mean())
    vol_20 = float(volume.tail(20).mean())
    if vol_20 <= 0:
        return 0.0

    vol_ratio = vol_5 / vol_20
    price_change_5d = (float(close.iloc[-1]) / float(close.iloc[-6]) - 1) * 100 if len(close) >= 6 else 0

    if vol_ratio >= 1.8 and abs(price_change_5d) < 3:
        return 4.0  # 放量滞涨 → 吸筹
    if vol_ratio >= 1.5 and price_change_5d >= 5:
        return 4.0  # 放量突破
    if vol_ratio >= 1.3 and price_change_5d < 0:
        return 2.0  # 温和放量下跌
    return 0.0


def sector_momentum_rank(quote: dict) -> float:
    """
    板块动量排名（代理指标）: 基于股票名称关键词匹配热门板块。
    匹配到高热度板块返回 +5，中热度返回 +3。
    返回 0~10。
    """
    name = str(quote.get("name", "")) if quote else ""
    if not name:
        return 0.0

    hot_keywords = {
        "AI": 5, "人工智能": 5, "大模型": 5, "算力": 5,
        "机器人": 5, "智能驾驶": 5, "自动驾驶": 5,
        "半导体": 5, "芯片": 5, "光刻": 4,
        "低空经济": 5, "量子": 4, "脑机": 4,
        "固态电池": 4, "卫星": 4,
        "军工": 4, "航空航天": 4,
        "新能源": 3, "锂电": 3, "储能": 3, "光伏": 3,
        "数据要素": 4, "数字经济": 3,
        "生物医药": 3, "创新药": 3, "CRO": 3,
    }

    for kw, score in hot_keywords.items():
        if kw in name:
            return float(score)
    return 0.0


def limit_up_gene(df: pd.DataFrame) -> float:
    """
    涨停基因: 近1月有过涨停(+3)，有过连板(+5)。
    返回 0~10。
    """
    if df is None or len(df) < 20:
        return 0.0
    close = df["close"]
    pct = close.pct_change() * 100
    recent_30 = pct.tail(30)

    limit_up_count = int((recent_30 >= 9.5).sum())

    if limit_up_count >= 3:
        return 10.0
    elif limit_up_count >= 2:
        return 7.0
    elif limit_up_count >= 1:
        return 5.0
    return 0.0


def drawdown_recovery_potential(df: pd.DataFrame) -> float:
    """
    超跌恢复潜力: 距历史最高价跌幅>30% → 超跌反弹空间大。
    返回 0~10。
    """
    if df is None or len(df) < 20:
        return 0.0
    close = df["close"]
    current = float(close.iloc[-1])
    high_all = float(close.max())
    if high_all <= 0:
        return 0.0

    drawdown = (high_all - current) / high_all

    if drawdown >= 0.5:
        return 4.0  # 跌幅过大，可能是趋势下行，给中等分
    elif drawdown >= 0.3:
        return 4.0
    elif drawdown >= 0.2:
        return 2.0
    return 0.0


def float_market_cap_score(quote: dict) -> float:
    """
    流通市值评分: 50~300亿最优（流动性好+成长空间大）。
    返回 0~10。
    """
    if not quote:
        return 0.0
    mc = quote.get("market_cap")
    if mc is None:
        return 0.0
    try:
        mc = float(mc)
    except (ValueError, TypeError):
        return 0.0

    mc_yi = mc / 1e8  # 转亿
    if 50 <= mc_yi <= 300:
        return 4.0
    elif 30 <= mc_yi < 50 or 300 < mc_yi <= 500:
        return 3.0
    elif 1000 < mc_yi:
        return 1.0
    return 0.0


def bollinger_expansion(df: pd.DataFrame) -> float:
    """
    布林带扩张: 布林带宽度较20日前扩大>20% → 波动率扩张，变盘在即。
    返回 0~10。
    """
    if df is None or len(df) < 40:
        return 0.0
    close = df["close"]
    bb_mid = close.rolling(20).mean()
    bb_std = close.rolling(20).std()
    bb_width = (bb_std * 4 / bb_mid).dropna()

    if len(bb_width) < 20:
        return 0.0

    current_width = float(bb_width.iloc[-1])
    past_width = float(bb_width.iloc[-20])
    if past_width <= 0:
        return 0.0

    expansion_ratio = current_width / past_width
    if expansion_ratio >= 1.5:
        return 3.0
    elif expansion_ratio >= 1.2:
        return 3.0
    elif expansion_ratio >= 1.0:
        return 1.0
    return 0.0


def quality_baseline_filter(df: pd.DataFrame, financials: dict,
                            quote: dict = None) -> float:
    """
    质量底线过滤: ST/*ST/负债率>90%直接返回0分并标记剔除。
    非ST且负债率<90%时按ROE和营收增长评分。
    返回 0~10。
    """
    # ST 检查
    name = str(quote.get("name", "")) if quote else ""
    if "ST" in name or "*ST" in name:
        return 0.0

    if not financials:
        return 3.0  # 无财务数据给中性分

    debt = _safe_float(financials.get("debt_ratio"))
    if debt is not None and debt > 90:
        return 0.0

    score = 3.0  # 基础分
    roe = _safe_float(financials.get("roe"))
    if roe is not None:
        if roe >= 20:
            score += 4.0
        elif roe >= 15:
            score += 3.0
        elif roe >= 10:
            score += 2.0
        elif roe >= 5:
            score += 1.0

    rev_growth = _safe_float(financials.get("revenue_growth") or
                              financials.get("net_profit_growth"))
    if rev_growth is not None:
        if rev_growth >= 30:
            score += 3.0
        elif rev_growth >= 20:
            score += 2.0
        elif rev_growth >= 10:
            score += 1.0

    return min(score, 10.0)


# ═══════════════════════════════════════════════════════════════
# 3. 因子合成
# ═══════════════════════════════════════════════════════════════

# 因子函数映射表: factor_name -> (func, kwargs_keys, description)
FACTOR_REGISTRY = {
    "momentum_20d":       (momentum_20d,       ["df"],            "20日动量"),
    "momentum_60d":       (momentum_60d,       ["df"],            "60日动量"),
    "momentum_120d":      (momentum_120d,      ["df"],            "120日动量"),
    "volatility_20d":     (volatility_20d,     ["df"],            "20日波动率"),
    "avg_turnover_20d":   (avg_turnover_20d,   ["df"],            "20日平均换手率"),
    "volume_ratio":       (volume_ratio,       ["df"],            "量比"),
    "breakout_20d_high":  (breakout_20d_high,  ["df"],            "20日新高"),
    "ma_bull_alignment":  (ma_bull_alignment,  ["df"],            "均线多头排列天数"),
    "excess_return_vs_index": (excess_return_vs_index, ["df", "index_df"], "超额收益"),
    "roe_ttm":            (roe_ttm,            ["financials"],    "ROE(TTM)"),
    "roe_stability":      (roe_stability,      ["financials"],    "ROE稳定性"),
    "gross_margin_trend": (gross_margin_trend, ["financials"],    "毛利率趋势"),
    "pe_percentile":      (pe_percentile,      ["quote", "technical"], "PE分位数"),
    "pb_percentile":      (pb_percentile,      ["quote"],         "PB分位数"),
    "market_cap_factor":  (market_cap_factor,  ["quote"],         "市值因子"),
    "north_flow_days":    (north_flow_days,    ["df"],            "北向资金流入天数"),
    "margin_change":      (margin_change,      ["technical"],     "融资余额变化"),
    # ── 攻击性因子 ──
    "obv_divergence":          (obv_divergence,          ["df"],                    "OBV底部背离"),
    "volume_price_divergence": (volume_price_divergence, ["df"],                    "量价背离"),
    "sector_momentum_rank":    (sector_momentum_rank,    ["quote"],                 "板块动量排名"),
    "limit_up_gene":           (limit_up_gene,           ["df"],                    "涨停基因"),
    "drawdown_recovery_potential": (drawdown_recovery_potential, ["df"],            "超跌恢复潜力"),
    "float_market_cap_score":  (float_market_cap_score,  ["quote"],                 "流通市值评分"),
    "bollinger_expansion":     (bollinger_expansion,     ["df"],                    "布林带扩张"),
    "quality_baseline_filter": (quality_baseline_filter, ["df", "financials", "quote"], "质量底线过滤"),
}


def _load_history_df(symbol: str) -> pd.DataFrame:
    """加载历史日线 DataFrame。"""
    from data.pipeline import download_full_history
    cache_path = download_full_history(symbol, ndays=800)
    df = pd.read_csv(cache_path)
    df['date'] = pd.to_datetime(df['date'])
    df = df.sort_values('date').reset_index(drop=True)
    return df


def calc_all_factors(symbol: str, market: str = "A",
                     financials: dict = None, quote: dict = None,
                     technical: dict = None) -> dict:
    """
    计算一支股票的所有因子。

    Args:
        symbol: 股票代码
        market: 市场类型
        financials: 可选，已有的财务数据（避免重复获取）
        quote: 可选，已有的行情数据
        technical: 可选，已有的技术指标

    Returns:
        {"factor_name": raw_value, ..., "_meta": {...}}
    """
    from data.pipeline import normalize_symbol

    sym, exchange = normalize_symbol(symbol)

    # 获取历史日线
    try:
        df = _load_history_df(sym)
    except Exception:
        df = None

    # 若未提供，获取实时数据
    if quote is None or technical is None:
        try:
            from data.pipeline import fetch_spot, fetch_kline_indicators
            if quote is None:
                quote = fetch_spot(sym, exchange)
            if technical is None:
                technical = fetch_kline_indicators(sym, exchange)
        except Exception:
            quote = quote or {}
            technical = technical or {}

    if financials is None:
        try:
            from data.pipeline import fetch_financial
            financials = fetch_financial(sym)
        except Exception:
            financials = {}

    # 构建参数上下文
    ctx = {
        "df": df,
        "index_df": None,
        "financials": financials,
        "quote": quote,
        "technical": technical,
    }

    factors = {}
    errors = []
    for fname, (func, arg_keys, desc) in FACTOR_REGISTRY.items():
        try:
            kwargs = {k: ctx.get(k) for k in arg_keys}
            raw = func(**kwargs)
            if isinstance(raw, bool):
                raw = 1.0 if raw else 0.0
            elif isinstance(raw, (int, float)):
                raw = _safe_float(raw)
            factors[fname] = raw
        except Exception as e:
            factors[fname] = None
            errors.append(f"{fname}: {e}")

    factors["_meta"] = {
        "symbol": symbol,
        "computed_at": datetime.now().isoformat(),
        "errors": errors,
    }
    return factors


def composite_score(factors: dict, time_frame: str = "mid",
                    weight_profile: str = "default", df: pd.DataFrame = None) -> dict:
    """
    根据时间框架的因子权重计算综合评分。

    Args:
        factors: calc_all_factors 返回的因子字典
        time_frame: "short" / "mid" / "long"
        weight_profile: "default"=原始因子权重 / "aggressive"=攻击性四因子权重

    Returns:
        {
            "score": 0-100 综合评分,
            "signal": "BUY" / "HOLD" / "SELL",
            "contributions": {factor_name: weighted_contribution, ...},
            "threshold": 使用的阈值,
            "time_frame": 时间框架,
        }
    """
    if weight_profile == "aggressive":
        return _composite_score_aggressive(factors, time_frame, df=df)

    weights_cfg = _load_weights()
    tf_weights = weights_cfg.get(time_frame, DEFAULT_WEIGHTS.get(time_frame, {}))
    threshold = tf_weights.get("threshold", 55)

    total_score = 50.0  # 基准分 50
    contributions = {}
    used_weights = 0

    for fname, weight in tf_weights.items():
        if fname == "threshold":
            continue
        raw = factors.get(fname)
        if raw is None:
            contributions[fname] = 0
            continue
        # 波动率自适应：用映射后的分数替代原始值参与Z-score
        if fname == "volatility_20d" and df is not None:
            raw = _map_traditional_factor(fname, raw, df=df)
        # Z-score 标准化
        z = _zscore(raw, fname)
        if z is None:
            z = 0
        # 加权贡献（z-score 映射到 0-100 区间的影响）
        contrib = z * 10 * abs(weight)  # 1std = 10 points
        if weight < 0:
            # 负权重：值越大扣分越多
            contrib = -z * 10 * abs(weight)
        contributions[fname] = round(contrib, 2)
        total_score += contrib
        used_weights += abs(weight)

    # 归一化总分到 0-100
    if used_weights > 0:
        total_score = 50 + (total_score - 50) / used_weights
    total_score = round(max(0.0, min(100.0, total_score)), 1)

    # 信号判定
    if total_score >= threshold + 10:
        signal = "BUY"
    elif total_score >= threshold:
        signal = "CAUTIOUS_BUY"
    elif total_score <= threshold - 20:
        signal = "SELL"
    elif total_score <= threshold - 10:
        signal = "CAUTIOUS_SELL"
    else:
        signal = "HOLD"

    return {
        "score": total_score,
        "signal": signal,
        "contributions": contributions,
        "threshold": threshold,
        "time_frame": time_frame,
    }


# ── 攻击性四因子权重体系 ──
AGGRESSIVE_CATEGORY_WEIGHTS = {
    "主力痕迹": 0.30,
    "热点引擎": 0.25,
    "未来空间": 0.20,
    "基本质量": 0.25,
}

AGGRESSIVE_FACTOR_CATEGORIES = {
    "obv_divergence":          ("主力痕迹", 0.25),
    "volume_price_divergence": ("主力痕迹", 0.25),
    "volume_ratio":            ("主力痕迹", 0.20),
    "avg_turnover_20d":        ("主力痕迹", 0.15),
    "margin_change":           ("主力痕迹", 0.15),
    "sector_momentum_rank":    ("热点引擎", 0.35),
    "limit_up_gene":           ("热点引擎", 0.30),
    "north_flow_days":         ("热点引擎", 0.20),
    "excess_return_vs_index":  ("热点引擎", 0.15),
    "drawdown_recovery_potential": ("未来空间", 0.30),
    "float_market_cap_score":  ("未来空间", 0.25),
    "avg_turnover_20d_dup":   ("未来空间", 0.25),  # 共享因子，见下方处理
    "bollinger_expansion":     ("未来空间", 0.20),
    "quality_baseline_filter": ("基本质量", 0.35),
    "roe_ttm":                 ("基本质量", 0.30),
    "roe_stability":           ("基本质量", 0.20),
    "gross_margin_trend":      ("基本质量", 0.15),
}

# 因子→类别映射（每个因子只归属一个主类别，用于贡献归类）
_FACTOR_TO_CATEGORY = {
    "obv_divergence":          "主力痕迹",
    "volume_price_divergence": "主力痕迹",
    "volume_ratio":            "主力痕迹",
    "avg_turnover_20d":        "主力痕迹",
    "margin_change":           "主力痕迹",
    "sector_momentum_rank":    "热点引擎",
    "limit_up_gene":           "热点引擎",
    "north_flow_days":         "热点引擎",
    "excess_return_vs_index":  "热点引擎",
    "drawdown_recovery_potential": "未来空间",
    "float_market_cap_score":  "未来空间",
    "bollinger_expansion":     "未来空间",
    "quality_baseline_filter": "基本质量",
    "roe_ttm":                 "基本质量",
    "roe_stability":           "基本质量",
    "gross_margin_trend":      "基本质量",
}

# 每个类别内的子因子权重（归一化）
_CATEGORY_SUB_WEIGHTS = {
    "主力痕迹": {
        "obv_divergence": 0.25,
        "volume_price_divergence": 0.25,
        "volume_ratio": 0.20,
        "avg_turnover_20d": 0.15,
        "margin_change": 0.15,
    },
    "热点引擎": {
        "sector_momentum_rank": 0.35,
        "limit_up_gene": 0.30,
        "north_flow_days": 0.20,
        "excess_return_vs_index": 0.15,
    },
    "未来空间": {
        "drawdown_recovery_potential": 0.30,
        "float_market_cap_score": 0.25,
        "bollinger_expansion": 0.25,
        "momentum_20d": 0.20,  # 复用已有因子
    },
    "基本质量": {
        "quality_baseline_filter": 0.35,
        "roe_ttm": 0.30,
        "roe_stability": 0.20,
        "gross_margin_trend": 0.15,
    },
}


def _composite_score_aggressive(factors: dict, time_frame: str, df: pd.DataFrame = None) -> dict:
    """
    攻击性四因子综合评分:
    主力痕迹(30%) + 热点引擎(25%) + 未来空间(20%) + 基本质量(25%)

    子因子评分 0~10，加权后映射到 0~100。
    """
    # 从 factor_weights.json 读取阈值（Critic 可修改）
    weights_cfg = _load_weights()
    tf_weights = weights_cfg.get(time_frame, {})
    threshold = tf_weights.get("threshold", {"short": 45, "mid": 50, "long": 55}.get(time_frame, 50))

    contributions = {}
    category_scores = {}

    for cat_name, cat_weight in AGGRESSIVE_CATEGORY_WEIGHTS.items():
        sub_weights = _CATEGORY_SUB_WEIGHTS.get(cat_name, {})
        cat_score = 0.0
        used_w = 0.0

        for fname, sub_w in sub_weights.items():
            raw = factors.get(fname)
            if raw is None:
                contributions[fname] = 0
                continue
            # 因子原始值已经是 0~10 区间（攻击性因子）或需要映射（传统因子）
            if fname in _FACTOR_TO_CATEGORY:
                # 攻击性因子：直接使用 0~10
                factor_score = min(10.0, max(0.0, float(raw)))
            else:
                # 传统因子：映射到 0~10
                factor_score = _map_traditional_factor(fname, raw, df=df)

            weighted = factor_score * sub_w
            cat_score += weighted
            used_w += sub_w
            contributions[fname] = round(weighted * cat_weight, 2)

        if used_w > 0:
            cat_score = cat_score / used_w  # 归一化到 0~10
        category_scores[cat_name] = round(cat_score, 2)

    # 综合评分: 类别分数(0~10) × 类别权重 → 映射到 0~100
    total_score = sum(
        category_scores[cat] * AGGRESSIVE_CATEGORY_WEIGHTS[cat]
        for cat in AGGRESSIVE_CATEGORY_WEIGHTS
    ) * 10  # 0~10 * 权重 * 10 = 0~100
    total_score = round(max(0.0, min(100.0, total_score)), 1)

    # 信号判定
    if total_score >= threshold + 15:
        signal = "BUY"
    elif total_score >= threshold:
        signal = "CAUTIOUS_BUY"
    elif total_score <= threshold - 20:
        signal = "SELL"
    elif total_score <= threshold - 10:
        signal = "CAUTIOUS_SELL"
    else:
        signal = "HOLD"

    # 数据不足保护: 超半数因子缺失时，不下 SELL 判定（给 MA+RSI 机会）
    total_factors = sum(len(_CATEGORY_SUB_WEIGHTS.get(c, {})) for c in AGGRESSIVE_CATEGORY_WEIGHTS)
    available_factors = sum(1 for fname in contributions if contributions[fname] != 0 and not fname.startswith("_"))
    if total_factors > 0 and available_factors < total_factors * 0.5:
        if signal in ("SELL", "CAUTIOUS_SELL"):
            signal = "HOLD"
            contributions["_data_sparse"] = -1

    # 质量底线硬约束: 基本质量=0分时强制不买
    if category_scores.get("基本质量", 0) <= 0:
        if signal in ("BUY", "CAUTIOUS_BUY"):
            signal = "HOLD"
            contributions["_quality_veto"] = -999

    return {
        "score": total_score,
        "signal": signal,
        "contributions": contributions,
        "threshold": threshold,
        "time_frame": time_frame,
        "category_scores": category_scores,
        "weight_profile": "aggressive",
    }


def _map_traditional_factor(fname: str, raw: float, df: pd.DataFrame = None) -> float:
    """将传统因子的原始值映射到 0~10 区间。"""
    if raw is None:
        return 0.0
    raw = float(raw)
    if fname == "momentum_20d":
        return max(0, min(10, 5 + raw / 2))  # -10%~+10% → 0~10
    elif fname == "momentum_60d":
        return max(0, min(10, 5 + raw / 4))
    elif fname == "momentum_120d":
        return max(0, min(10, 5 + raw / 8))
    elif fname == "roe_ttm":
        return max(0, min(10, raw / 2))  # 0%~20% → 0~10
    elif fname == "roe_stability":
        return max(0, min(10, raw * 10))  # 0~1 → 0~10
    elif fname == "gross_margin_trend":
        return max(0, min(10, 5 + raw / 4))
    elif fname == "volatility_20d":
        return _adaptive_volatility_score(raw, df)
    elif fname == "pe_percentile":
        return max(0, min(10, 10 - raw * 10))  # 低PE高分
    elif fname == "pb_percentile":
        return max(0, min(10, 10 - raw * 10))
    elif fname == "ma_bull_alignment":
        return max(0, min(10, raw / 6))  # 0~60天 → 0~10
    elif fname == "breakout_20d_high":
        return 10.0 if raw else 0.0
    elif fname == "market_cap_factor":
        return max(0, min(10, raw * 10))
    else:
        return max(0, min(10, 5 + float(raw)))


def _adaptive_volatility_score(current_vol: float, df: pd.DataFrame = None) -> float:
    """波动率自适应评分：根据股票历史波动率特征分档处理。

    - 高波动标的（历史年化>30%）：当前波动不高于历史1.3倍不扣分
    - 低波动标的（历史年化<15%）：当前波动突然放大>25%加重扣分
    - 其他情况：使用标准线性映射
    """
    if df is None or len(df) < 60:
        # 无历史数据，使用标准映射
        return max(0, min(10, 10 - current_vol / 10))

    # 计算历史年化波动率分布（60日滚动窗口）
    close = df["close"]
    daily_ret = close.pct_change().dropna()
    if len(daily_ret) < 60:
        return max(0, min(10, 10 - current_vol / 10))

    hist_vol_series = daily_ret.rolling(60).std() * math.sqrt(252) * 100
    hist_vol_series = hist_vol_series.dropna()
    if len(hist_vol_series) < 10:
        return max(0, min(10, 10 - current_vol / 10))

    hist_mean = float(hist_vol_series.mean())

    # 高波动标的：历史年化>30%，天然高波动
    if hist_mean > 30:
        if current_vol <= hist_mean * 1.3:
            return 7.0  # 正常波动，不扣分
        else:
            # 超出历史均值1.3倍，按超出比例扣分
            excess = (current_vol - hist_mean * 1.3) / 10
            return max(0, min(10, 7 - excess))

    # 低波动标的：历史年化<15%
    if hist_mean < 15:
        if current_vol > 25:
            return 1.0  # 低波动股突然剧烈波动，重扣
        else:
            return max(0, min(10, 10 - current_vol / 8))

    # 中等波动标的：标准映射
    return max(0, min(10, 10 - current_vol / 10))


def rank_stocks(universe: list = None, top_n: int = 50,
                time_frame: str = "mid", use_mock: bool = True) -> list:
    """
    根据综合评分对股票池排序。

    Args:
        universe: 股票代码列表，None 则使用沪深300
        top_n: 返回前 N 只
        time_frame: 时间框架
        use_mock: True=只计算可获得的因子，不调用 LLM

    Returns:
        [{"symbol", "score", "signal", "factors": {...}, "contributions": {...}}, ...]
    """
    if universe is None:
        try:
            from analysis.screener import _get_constituents
            universe = _get_constituents("hs300")
        except Exception:
            # Fallback to a small default set
            universe = ["600519", "000858", "600744", "300750", "601012",
                       "002594", "000001", "002709"]

    print(f"\n  Alpha因子排名 | 时间框架: {time_frame} | 股票池: {len(universe)}只")

    # Phase 1: 快速行情过滤（减少财务数据获取量）
    try:
        from analysis.screener import _get_spot_batch
        spots = _get_spot_batch(universe[:200])  # Limit initial batch
    except Exception:
        spots = {}

    results = []
    completed = 0
    total = min(len(universe), 100)

    for sym in universe[:total]:
        completed += 1
        try:
            factors = calc_all_factors(sym)
            try:
                df = _load_history_df(sym)
            except Exception:
                df = None
            comp = composite_score(factors, time_frame, df=df)
            results.append({
                "symbol": sym,
                "name": spots.get(sym, {}).get("name", ""),
                "score": comp["score"],
                "signal": comp["signal"],
                "contributions": comp["contributions"],
            })
        except Exception as e:
            pass

        if completed % 20 == 0:
            print(f"  进度: {completed}/{total}")

    results.sort(key=lambda x: x["score"], reverse=True)
    top = results[:top_n]

    print(f"  完成: {len(results)}只有效评分，Top{top_n} 已就绪")
    return top


# ═══════════════════════════════════════════════════════════════
# 4. CLI 入口
# ═══════════════════════════════════════════════════════════════

def _print_factors(symbol: str):
    """打印单只股票的所有因子详情。"""
    factors = calc_all_factors(symbol)
    meta = factors.pop("_meta", {})
    try:
        from data.pipeline import normalize_symbol
        sym, _ = normalize_symbol(symbol)
        df = _load_history_df(sym)
    except Exception:
        df = None

    print(f"\n{'='*60}")
    print(f"  Alpha 因子报告: {symbol}")
    print(f"{'='*60}")

    # 分组显示 — 传统因子
    momentum_group = ["momentum_20d", "momentum_60d", "momentum_120d"]
    quality_group = ["roe_ttm", "roe_stability", "gross_margin_trend"]
    value_group = ["pe_percentile", "pb_percentile", "market_cap_factor"]
    tech_group = ["volatility_20d", "avg_turnover_20d", "volume_ratio",
                  "breakout_20d_high", "ma_bull_alignment", "excess_return_vs_index"]
    flow_group = ["north_flow_days", "margin_change"]

    groups = [
        ("动量因子", momentum_group),
        ("质量因子", quality_group),
        ("估值因子", value_group),
        ("技术因子", tech_group),
        ("资金流因子", flow_group),
    ]

    for gname, gkeys in groups:
        print(f"\n  [{gname}]")
        for k in gkeys:
            v = factors.get(k)
            desc = FACTOR_REGISTRY.get(k, (None, None, "?"))[2]
            if v is not None:
                print(f"    {k:<24s} = {v:>10.4f}  ({desc})")
            else:
                print(f"    {k:<24s} = {'N/A':>10}  ({desc})")

    # 分组显示 — 攻击性因子
    aggressive_groups = [
        ("主力痕迹因子", ["obv_divergence", "volume_price_divergence", "volume_ratio", "avg_turnover_20d"]),
        ("热点引擎因子", ["sector_momentum_rank", "limit_up_gene", "north_flow_days", "excess_return_vs_index"]),
        ("未来空间因子", ["drawdown_recovery_potential", "float_market_cap_score", "bollinger_expansion", "momentum_20d"]),
        ("质量底线因子", ["quality_baseline_filter", "roe_ttm", "roe_stability", "gross_margin_trend"]),
    ]

    for gname, gkeys in aggressive_groups:
        print(f"\n  [{gname}]")
        for k in gkeys:
            v = factors.get(k)
            desc = FACTOR_REGISTRY.get(k, (None, None, "?"))[2]
            if v is not None:
                print(f"    {k:<24s} = {v:>10.4f}  ({desc})")
            else:
                print(f"    {k:<24s} = {'N/A':>10}  ({desc})")

    # 综合评分 — 传统权重
    sig_map = {"BUY": "买入", "CAUTIOUS_BUY": "谨慎买入", "HOLD": "持有",
               "CAUTIOUS_SELL": "谨慎卖出", "SELL": "卖出"}
    print(f"\n  ── 传统因子权重 ──")
    for tf in ["short", "mid", "long"]:
        comp = composite_score(factors, tf, weight_profile="default", df=df)
        print(f"  [{tf}线] 综合评分: {comp['score']:.1f}/100 "
              f"→ {sig_map.get(comp['signal'], comp['signal'])} (阈值={comp['threshold']})")
        top_contrib = sorted(comp["contributions"].items(), key=lambda x: abs(x[1]), reverse=True)[:5]
        contrib_str = " | ".join(f"{k}={v:+.1f}" for k, v in top_contrib if v != 0)
        print(f"    贡献: {contrib_str}")

    # 综合评分 — 攻击性权重
    print(f"\n  ── 攻击性四因子权重 (主力30%/热点25%/空间20%/质量25%) ──")
    for tf in ["short", "mid", "long"]:
        comp = composite_score(factors, tf, weight_profile="aggressive", df=df)
        cats = comp.get("category_scores", {})
        cat_str = " | ".join(f"{k}={v:.1f}" for k, v in cats.items())
        print(f"  [{tf}线] 综合评分: {comp['score']:.1f}/100 "
              f"→ {sig_map.get(comp['signal'], comp['signal'])} (阈值={comp['threshold']})")
        print(f"    分类: {cat_str}")
        top_contrib = sorted(comp["contributions"].items(), key=lambda x: abs(x[1]), reverse=True)[:5]
        contrib_str = " | ".join(f"{k}={v:+.1f}" for k, v in top_contrib if v != 0 and not k.startswith("_"))
        print(f"    贡献: {contrib_str}")

    if meta.get("errors"):
        print(f"\n  [计算错误]")
        for e in meta["errors"]:
            print(f"    - {e}")

    print(f"\n{'='*60}\n")


if __name__ == "__main__":
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')

    import argparse
    parser = argparse.ArgumentParser(description='Alpha因子计算引擎')
    parser.add_argument('symbol', nargs='?', default=None, help='股票代码')
    parser.add_argument('--timeframe', default='mid', choices=['short', 'mid', 'long'],
                        help='时间框架')
    parser.add_argument('--rank', default=None, help='股票池 (hs300/zz500)')
    parser.add_argument('--top', type=int, default=10, help='排名返回数量')
    parser.add_argument('--json', action='store_true', help='JSON格式输出')
    args = parser.parse_args()

    if args.rank:
        pool = None
        if args.rank.lower() in ("hs300", "zz500"):
            from analysis.screener import _get_constituents
            pool = _get_constituents(args.rank.lower())
        ranked = rank_stocks(pool, top_n=args.top, time_frame=args.timeframe)
        if args.json:
            print(json.dumps(ranked, ensure_ascii=False, indent=2))
        else:
            print(f"\n  {'代码':<8} {'评分':>6} {'信号':<12}")
            print(f"  {'-'*30}")
            for r in ranked:
                print(f"  {r['symbol']:<8} {r['score']:>6.1f} {r['signal']:<12}")
            print()
    elif args.symbol:
        if args.json:
            factors = calc_all_factors(args.symbol)
            factors.pop("_meta", None)
            try:
                from data.pipeline import normalize_symbol as _ns
                _s, _ = _ns(args.symbol)
                _df = _load_history_df(_s)
            except Exception:
                _df = None
            comp = composite_score(factors, args.timeframe, df=_df)
            print(json.dumps({"factors": factors, "composite": comp}, ensure_ascii=False, indent=2))
        else:
            _print_factors(args.symbol)
    else:
        parser.print_help()
