#!/usr/bin/env python3
"""
四因子 Alpha 模型 — 攻击性选股引擎。

因子体系:
  1. 主力痕迹 (35%) — OBV背离、量价异动、换手异常、量能放大
  2. 热点引擎 (30%) — 板块动量、涨停基因、龙虎榜、事件催化
  3. 未来空间 (20%) — 离高点距离、市值适中、换手活跃、布林张口
  4. 基本质量 (15%) — ST过滤、负债过滤、ROE、营收增长

纯本地计算，不发起任何网络请求。
"""

import numpy as np
import pandas as pd

# ═══════════════════════════════════════════════════════════════
# 因子权重配置
# ═══════════════════════════════════════════════════════════════

FACTOR_CATEGORIES = {
    "主力痕迹": 0.35,
    "热点引擎": 0.30,
    "未来空间": 0.20,
    "基本质量": 0.15,
}

# 子因子权重（每组内部归一化）
SUB_FACTOR_WEIGHTS = {
    "主力痕迹": {
        "obv_divergence": 0.30,     # OBV底部背离
        "vol_price_anomaly": 0.25,  # 量价异动
        "vol_ratio_5d": 0.20,       # 量比(5日/20日)
        "turnover_anomaly": 0.15,   # 换手率异动
        "vol_amplification": 0.10,  # 近5日放量
    },
    "热点引擎": {
        "sector_momentum": 0.35,    # 板块动量
        "limit_up_gene": 0.25,      # 涨停基因
        "dragon_tiger": 0.20,       # 龙虎榜溢价
        "event_catalyst": 0.20,     # 事件催化
    },
    "未来空间": {
        "ath_distance": 0.30,       # 离年内高点距离
        "market_cap_range": 0.25,   # 市值适中
        "turnover_active": 0.25,    # 换手活跃
        "boll_width_expansion": 0.20,  # 布林张口扩大
    },
    "基本质量": {
        "debt_filter": 0.40,        # 负债率
        "roe_score": 0.35,          # ROE
        "revenue_growth": 0.25,     # 营收增长
    },
}


def _safe(val, default=0.0):
    """安全数值转换。"""
    if val is None or val == '' or val == '-':
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


# ═══════════════════════════════════════════════════════════════
# 1. 主力痕迹因子 (35%)
# ═══════════════════════════════════════════════════════════════

def _calc_obv(df: pd.DataFrame) -> pd.Series:
    """计算 OBV (On-Balance Volume)。"""
    close = df["close"].values
    volume = df["volume"].values.astype(float)
    obv = np.zeros(len(close))
    for i in range(1, len(close)):
        if close[i] > close[i - 1]:
            obv[i] = obv[i - 1] + volume[i]
        elif close[i] < close[i - 1]:
            obv[i] = obv[i - 1] - volume[i]
        else:
            obv[i] = obv[i - 1]
    return pd.Series(obv, index=df.index)


def _score_obv_divergence(df: pd.DataFrame) -> float:
    """
    OBV 底部背离检测: 价格创新低但 OBV 未创新低 → 底部吸筹信号。
    返回 0~1，1 = 强背离（底部吸筹）。
    """
    if len(df) < 40:
        return 0.0

    close = df["close"]
    obv = _calc_obv(df)

    # 近20日 vs 前20日
    recent_close = close.tail(20)
    prev_close = close.iloc[-40:-20]
    recent_obv = obv.tail(20)
    prev_obv = obv.iloc[-40:-20]

    if len(prev_close) == 0 or len(prev_obv) == 0:
        return 0.0

    price_new_low = recent_close.min() < prev_close.min() * 0.98
    obv_higher = recent_obv.min() > prev_obv.min()

    if price_new_low and obv_higher:
        return 1.0

    # 次级信号：价格持平但 OBV 上升
    price_flat = abs(recent_close.mean() / prev_close.mean() - 1) < 0.03
    obv_up = recent_obv.mean() > prev_obv.mean() * 1.1
    if price_flat and obv_up:
        return 0.6

    return 0.0


def _score_vol_price_anomaly(df: pd.DataFrame) -> float:
    """
    量价异动: 跌幅>2%但量能>均量1.5倍 → 恐慌洗盘; 涨幅>2%且量能>1.5倍 → 放量突破。
    返回 0~1。
    """
    if len(df) < 5:
        return 0.0

    last = df.iloc[-1]
    close = float(last["close"])
    open_ = float(last["open"])
    volume = float(last["volume"])
    avg_vol = df["volume"].tail(20).mean()

    if avg_vol <= 0:
        return 0.0

    vol_ratio = volume / avg_vol
    pct_change = (close - open_) / open_ * 100

    # 恐慌洗盘（跌+放量）→ 底部吸筹信号
    if pct_change <= -2 and vol_ratio >= 1.5:
        return 0.8

    # 放量突破（涨+放量）→ 趋势启动信号
    if pct_change >= 2 and vol_ratio >= 1.5:
        return 1.0

    # 缩量下跌（温和洗盘）
    if pct_change <= -1 and vol_ratio < 0.7:
        return 0.4

    return 0.0


def _score_vol_ratio_5d(df: pd.DataFrame) -> float:
    """
    5日量比: 近5日平均成交量 / 近20日平均成交量。
    返回 0~1，1.2~2.0区间最优。
    """
    if len(df) < 20:
        return 0.0

    vol_5 = df["volume"].tail(5).mean()
    vol_20 = df["volume"].tail(20).mean()

    if vol_20 <= 0:
        return 0.0

    ratio = vol_5 / vol_20

    if 1.2 <= ratio <= 2.0:
        return 1.0
    elif 1.0 <= ratio < 1.2:
        return 0.5
    elif ratio > 2.0:
        return 0.7  # 过度放量，可能见顶
    return 0.0


def _score_turnover_anomaly(turnover: float) -> float:
    """
    换手率异动: 3%~10% 活跃度最优。
    返回 0~1。
    """
    if turnover is None or turnover <= 0:
        return 0.0
    if 3.0 <= turnover <= 10.0:
        return 1.0
    elif 2.0 <= turnover < 3.0:
        return 0.5
    elif 10.0 < turnover <= 15.0:
        return 0.6
    return 0.0


def _score_vol_amplification(df: pd.DataFrame) -> float:
    """
    近5日成交量 vs 前20日: 连续温和放量 → 主力缓慢建仓。
    返回 0~1。
    """
    if len(df) < 25:
        return 0.0

    vol_5 = df["volume"].tail(5)
    vol_prev = df["volume"].iloc[-25:-5]

    if vol_prev.mean() <= 0:
        return 0.0

    # 5日中每日量>均量的天数
    above_count = (vol_5 > vol_prev.mean()).sum()

    if above_count >= 4:
        return 1.0
    elif above_count >= 3:
        return 0.6
    return 0.0


def compute_mainforce_factors(df: pd.DataFrame, spot: dict) -> dict:
    """计算主力痕迹因子组。"""
    turnover = _safe(spot.get("turnover"))
    return {
        "obv_divergence": _score_obv_divergence(df),
        "vol_price_anomaly": _score_vol_price_anomaly(df),
        "vol_ratio_5d": _score_vol_ratio_5d(df),
        "turnover_anomaly": _score_turnover_anomaly(turnover),
        "vol_amplification": _score_vol_amplification(df),
    }


# ═══════════════════════════════════════════════════════════════
# 2. 热点引擎因子 (30%)
# ═══════════════════════════════════════════════════════════════

# 板块关键词 → 近期热度加分
_HOT_SECTOR_KEYWORDS = {
    "AI": 1.0, "人工智能": 1.0, "大模型": 1.0, "算力": 0.9,
    "机器人": 0.9, "智能驾驶": 0.85, "自动驾驶": 0.85,
    "半导体": 0.85, "芯片": 0.85, "光刻": 0.8,
    "新能源": 0.7, "锂电": 0.7, "储能": 0.75, "光伏": 0.65,
    "军工": 0.8, "航空航天": 0.8, "卫星": 0.75,
    "生物医药": 0.6, "创新药": 0.65, "CRO": 0.6,
    "数据要素": 0.8, "数字经济": 0.7, "东数西算": 0.7,
    "低空经济": 0.85, "量子": 0.7, "脑机": 0.7,
    "固态电池": 0.8, "钙钛矿": 0.7,
}


def _score_sector_momentum(name: str, change_pct: float) -> float:
    """
    板块动量: 基于股票名称关键词匹配热门板块。
    返回 0~1。
    """
    if not name:
        return 0.0
    name = str(name)
    max_heat = 0.0
    for kw, heat in _HOT_SECTOR_KEYWORDS.items():
        if kw in name:
            max_heat = max(max_heat, heat)
    if max_heat <= 0:
        return 0.0
    # 板块热度 × 当日表现加成
    if change_pct is not None and change_pct > 0:
        return min(max_heat * (1 + change_pct / 100), 1.0)
    return max_heat * 0.7  # 在热门板块但当日下跌，降低得分


def _score_limit_up_gene(df: pd.DataFrame) -> float:
    """
    涨停基因: 近30日有多少天涨幅≥9.5%（接近涨停）。
    返回 0~1。
    """
    if len(df) < 20:
        return 0.0

    close = df["close"]
    pct = close.pct_change() * 100
    recent_pct = pct.tail(30)
    limit_up_count = (recent_pct >= 9.5).sum()

    if limit_up_count >= 3:
        return 1.0
    elif limit_up_count >= 2:
        return 0.8
    elif limit_up_count >= 1:
        return 0.5
    return 0.0


def _score_dragon_tiger(df: pd.DataFrame) -> float:
    """
    龙虎榜溢价（代理指标）: 用极端量价行为近似。
    近10日出现单日涨幅>5%且成交量>3倍均量 → 机构异动。
    返回 0~1。
    """
    if len(df) < 15:
        return 0.0

    close = df["close"]
    volume = df["volume"]
    avg_vol = volume.tail(20).mean()

    if avg_vol <= 0:
        return 0.0

    pct = close.pct_change() * 100
    recent = df.tail(10)
    recent_pct = pct.tail(10)
    recent_vol = volume.tail(10)

    score = 0.0
    for i in range(len(recent)):
        p = recent_pct.iloc[i] if not pd.isna(recent_pct.iloc[i]) else 0
        v = recent_vol.iloc[i] if not pd.isna(recent_vol.iloc[i]) else 0
        if p >= 5 and v >= avg_vol * 3:
            score = max(score, 1.0)
        elif p >= 3 and v >= avg_vol * 2:
            score = max(score, 0.6)

    return score


def _score_event_catalyst(df: pd.DataFrame) -> float:
    """
    事件催化（代理指标）: 近5日累计涨幅>15% + 量能持续放大。
    返回 0~1。
    """
    if len(df) < 10:
        return 0.0

    close = df["close"]
    volume = df["volume"]

    ret_5d = (close.iloc[-1] / close.iloc[-6] - 1) * 100 if len(close) >= 6 else 0
    vol_5 = volume.tail(5).mean()
    vol_20 = volume.tail(20).mean()
    vol_ratio = vol_5 / vol_20 if vol_20 > 0 else 0

    if ret_5d >= 15 and vol_ratio >= 1.5:
        return 1.0
    elif ret_5d >= 10 and vol_ratio >= 1.2:
        return 0.7
    elif ret_5d >= 5 and vol_ratio >= 1.0:
        return 0.4
    return 0.0


def compute_hotspot_factors(df: pd.DataFrame, spot: dict) -> dict:
    """计算热点引擎因子组。"""
    name = spot.get("name", "")
    change_pct = _safe(spot.get("change_pct"))
    return {
        "sector_momentum": _score_sector_momentum(name, change_pct),
        "limit_up_gene": _score_limit_up_gene(df),
        "dragon_tiger": _score_dragon_tiger(df),
        "event_catalyst": _score_event_catalyst(df),
    }


# ═══════════════════════════════════════════════════════════════
# 3. 未来空间因子 (20%)
# ═══════════════════════════════════════════════════════════════

def _score_ath_distance(df: pd.DataFrame) -> float:
    """
    离年内最高价距离: 距离越大，未来空间越大。
    返回 0~1。
    """
    if len(df) < 20:
        return 0.0

    close = df["close"]
    current = float(close.iloc[-1])
    high_250d = float(close.tail(min(250, len(close))).max())

    if high_250d <= 0:
        return 0.0

    distance = (high_250d - current) / high_250d

    if 0.3 <= distance <= 0.6:
        return 1.0
    elif 0.2 <= distance < 0.3:
        return 0.7
    elif 0.1 <= distance < 0.2:
        return 0.4
    elif distance > 0.6:
        return 0.5  # 距离太远可能是趋势下行
    return 0.0


def _score_market_cap_range(mc: float) -> float:
    """
    市值适中: 100~500亿最优（既有流动性又有成长空间）。
    返回 0~1。
    """
    if mc is None or mc <= 0:
        return 0.0

    mc_yi = mc / 1e8  # 转换为亿元

    if 100 <= mc_yi <= 500:
        return 1.0
    elif 50 <= mc_yi < 100:
        return 0.8
    elif 500 < mc_yi <= 1000:
        return 0.6
    elif 30 <= mc_yi < 50:
        return 0.5
    elif mc_yi > 1000:
        return 0.2
    return 0.0


def _score_turnover_active(turnover: float) -> float:
    """
    换手率活跃: 3%~8%最优。
    返回 0~1。
    """
    if turnover is None or turnover <= 0:
        return 0.0
    if 3.0 <= turnover <= 8.0:
        return 1.0
    elif 2.0 <= turnover < 3.0:
        return 0.5
    elif 8.0 < turnover <= 12.0:
        return 0.6
    return 0.0


def _score_boll_width_expansion(df: pd.DataFrame) -> float:
    """
    布林带宽度扩大: 当前宽度 > 均值*1.2 → 波动率在扩张。
    返回 0~1。
    """
    if len(df) < 40:
        return 0.0

    close = df["close"]
    bb_mid = close.rolling(20).mean()
    bb_std = close.rolling(20).std()
    bb_width = (bb_std * 4 / bb_mid).dropna()

    if len(bb_width) < 20:
        return 0.0

    current_width = float(bb_width.iloc[-1])
    mean_width = float(bb_width.tail(20).mean())

    if mean_width <= 0:
        return 0.0

    ratio = current_width / mean_width

    if ratio >= 1.2:
        return 1.0
    elif ratio >= 1.0:
        return 0.5
    return 0.0


def compute_upside_factors(df: pd.DataFrame, spot: dict) -> dict:
    """计算未来空间因子组。"""
    market_cap = _safe(spot.get("总市值", spot.get("market_cap")))
    turnover = _safe(spot.get("turnover"))
    return {
        "ath_distance": _score_ath_distance(df),
        "market_cap_range": _score_market_cap_range(market_cap),
        "turnover_active": _score_turnover_active(turnover),
        "boll_width_expansion": _score_boll_width_expansion(df),
    }


# ═══════════════════════════════════════════════════════════════
# 4. 基本质量因子 (15%)
# ═══════════════════════════════════════════════════════════════

def _score_debt_filter(debt_ratio: float) -> float:
    """负债率得分: <50%最优，>90%直接归零。"""
    if debt_ratio is None:
        return 0.5  # 未知数据给中性分
    if debt_ratio > 90:
        return 0.0
    if debt_ratio < 50:
        return 1.0
    elif debt_ratio < 65:
        return 0.7
    elif debt_ratio < 80:
        return 0.4
    return 0.1


def _score_roe(roe: float) -> float:
    """ROE 得分: ≥15%最优。"""
    if roe is None:
        return 0.0
    if roe >= 20:
        return 1.0
    elif roe >= 15:
        return 0.8
    elif roe >= 10:
        return 0.5
    elif roe >= 5:
        return 0.2
    return 0.0


def _score_revenue_growth(revenue_growth: float) -> float:
    """营收增长得分: ≥20%最优。"""
    if revenue_growth is None:
        return 0.3  # 未知给中性分
    if revenue_growth >= 30:
        return 1.0
    elif revenue_growth >= 20:
        return 0.8
    elif revenue_growth >= 10:
        return 0.5
    elif revenue_growth >= 0:
        return 0.2
    return 0.0


def compute_quality_factors(spot: dict, fin: dict) -> dict:
    """计算基本质量因子组。"""
    return {
        "debt_filter": _score_debt_filter(_safe(fin.get("debt_ratio"))),
        "roe_score": _score_roe(_safe(fin.get("roe"))),
        "revenue_growth": _score_revenue_growth(_safe(fin.get("revenue_growth"))),
    }


# ═══════════════════════════════════════════════════════════════
# 主入口: 计算所有因子并返回结构化结果
# ═══════════════════════════════════════════════════════════════

def compute_all_factors(df: pd.DataFrame, spot: dict, fin: dict) -> dict:
    """
    计算四因子模型所有因子，返回结构化结果。

    Args:
        df: K线 DataFrame，需含 date/open/close/high/low/volume 列（至少60行）
        spot: 行情快照 dict，含 name/close/change_pct/pe/pb/turnover/总市值 等
        fin: 财务数据 dict，含 roe/debt_ratio/revenue_growth 等

    Returns:
        dict: {
            "category_scores": {"主力痕迹": float, "热点引擎": float, ...},
            "sub_factors": {"主力痕迹": {...}, "热点引擎": {...}, ...},
            "composite_score": float (0~100),
            "signal_tags": ["暗仓", "板块", ...],
        }
    """
    # 硬性过滤检查
    name = str(spot.get("name", ""))
    if "ST" in name or "*ST" in name:
        return _empty_result("ST股票")

    debt = _safe(fin.get("debt_ratio"))
    if debt > 90:
        return _empty_result("高负债")

    # 计算四组因子
    mainforce = compute_mainforce_factors(df, spot)
    hotspot = compute_hotspot_factors(df, spot)
    upside = compute_upside_factors(df, spot)
    quality = compute_quality_factors(spot, fin)

    # 计算各组加权得分（0~1）
    cat_scores = {
        "主力痕迹": _weighted_avg(mainforce, SUB_FACTOR_WEIGHTS["主力痕迹"]),
        "热点引擎": _weighted_avg(hotspot, SUB_FACTOR_WEIGHTS["热点引擎"]),
        "未来空间": _weighted_avg(upside, SUB_FACTOR_WEIGHTS["未来空间"]),
        "基本质量": _weighted_avg(quality, SUB_FACTOR_WEIGHTS["基本质量"]),
    }

    # 综合得分 (0~100)
    composite = sum(
        cat_scores[cat] * FACTOR_CATEGORIES[cat]
        for cat in FACTOR_CATEGORIES
    ) * 100

    # 信号标签
    signal_tags = _generate_signal_tags(mainforce, hotspot, upside, quality, spot)

    return {
        "category_scores": cat_scores,
        "sub_factors": {
            "主力痕迹": mainforce,
            "热点引擎": hotspot,
            "未来空间": upside,
            "基本质量": quality,
        },
        "composite_score": round(composite, 2),
        "signal_tags": signal_tags,
    }


def _weighted_avg(scores: dict, weights: dict) -> float:
    """加权平均，权重归一化。"""
    total = 0.0
    w_sum = 0.0
    for k, w in weights.items():
        s = scores.get(k, 0.0)
        total += w * s
        w_sum += w
    return total / w_sum if w_sum > 0 else 0.0


def _empty_result(reason: str) -> dict:
    """被硬性过滤时返回的结果。"""
    return {
        "category_scores": {"主力痕迹": 0, "热点引擎": 0, "未来空间": 0, "基本质量": 0},
        "sub_factors": {},
        "composite_score": 0.0,
        "signal_tags": [f"过滤:{reason}"],
        "filtered": True,
        "filter_reason": reason,
    }


def _generate_signal_tags(mainforce: dict, hotspot: dict, upside: dict,
                          quality: dict, spot: dict) -> list:
    """根据因子得分生成信号标签。"""
    tags = []

    # ── 主力痕迹标签 ──
    if mainforce.get("obv_divergence", 0) >= 0.8:
        tags.append("暗仓吸筹")
    if mainforce.get("vol_price_anomaly", 0) >= 0.8:
        tags.append("量价异动")
    if mainforce.get("vol_amplification", 0) >= 0.8:
        tags.append("持续放量")
    if mainforce.get("turnover_anomaly", 0) >= 0.8:
        tags.append("换手活跃")
    if mainforce.get("vol_ratio_5d", 0) >= 0.8:
        tags.append("量比突破")

    # 综合主力信号
    mf_score = sum(mainforce.values()) / max(len(mainforce), 1)
    if mf_score >= 0.6 and "暗仓吸筹" not in tags:
        tags.append("主力痕迹")

    # ── 热点引擎标签 ──
    if hotspot.get("sector_momentum", 0) >= 0.7:
        tags.append("热门板块")
    if hotspot.get("limit_up_gene", 0) >= 0.5:
        tags.append("涨停基因")
    if hotspot.get("dragon_tiger", 0) >= 0.6:
        tags.append("机构异动")
    if hotspot.get("event_catalyst", 0) >= 0.7:
        tags.append("事件催化")

    # ── 未来空间标签 ──
    if upside.get("ath_distance", 0) >= 0.7:
        tags.append("低位蓄势")
    if upside.get("boll_width_expansion", 0) >= 0.8:
        tags.append("波动扩张")

    # ── 基本质量标签 ──
    roe = _safe(spot.get("roe"))
    if roe >= 20:
        tags.append("高ROE")

    # 如果没有任何标签，给一个默认
    if not tags:
        tags.append("无显著信号")

    return tags
