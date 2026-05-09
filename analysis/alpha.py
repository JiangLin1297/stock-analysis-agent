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
        "momentum_20d": 0.20,
        "momentum_60d": 0.10,
        "volatility_20d": -0.10,
        "avg_turnover_20d": 0.10,
        "breakout_20d_high": 0.15,
        "volume_ratio": 0.10,
        "north_flow_days": 0.10,
        "margin_change": 0.05,
        "ma_bull_alignment": 0.10,
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


def composite_score(factors: dict, time_frame: str = "mid") -> dict:
    """
    根据时间框架的因子权重计算综合评分。

    Args:
        factors: calc_all_factors 返回的因子字典
        time_frame: "short" / "mid" / "long"

    Returns:
        {
            "score": 0-100 综合评分,
            "signal": "BUY" / "HOLD" / "SELL",
            "contributions": {factor_name: weighted_contribution, ...},
            "threshold": 使用的阈值,
            "time_frame": 时间框架,
        }
    """
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
            comp = composite_score(factors, time_frame)
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

    print(f"\n{'='*60}")
    print(f"  Alpha 因子报告: {symbol}")
    print(f"{'='*60}")

    # 分组显示
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

    # 综合评分
    for tf in ["short", "mid", "long"]:
        comp = composite_score(factors, tf)
        sig_map = {"BUY": "买入", "CAUTIOUS_BUY": "谨慎买入", "HOLD": "持有",
                   "CAUTIOUS_SELL": "谨慎卖出", "SELL": "卖出"}
        print(f"\n  [{tf}线] 综合评分: {comp['score']:.1f}/100 "
              f"→ {sig_map.get(comp['signal'], comp['signal'])} (阈值={comp['threshold']})")
        top_contrib = sorted(comp["contributions"].items(), key=lambda x: abs(x[1]), reverse=True)[:5]
        contrib_str = " | ".join(f"{k}={v:+.1f}" for k, v in top_contrib if v != 0)
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
            comp = composite_score(factors, args.timeframe)
            print(json.dumps({"factors": factors, "composite": comp}, ensure_ascii=False, indent=2))
        else:
            _print_factors(args.symbol)
    else:
        parser.print_help()
