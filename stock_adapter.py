#!/usr/bin/env python3
"""
策略基因提取与全市场自适应引擎。
功能：
  1. extract_features(symbol) — 提取股票特征向量
  2. generate_adapted_dna(symbol, base_params_path) — LLM驱动的参数适配
  3. auto_adapt_and_backtest(symbol, time_frame, days) — 全自动适配+回测

用法:
    py stock_adapter.py 000001 --base 600744_best_params.json
    py stock_adapter.py 002709 --time_frame mid --days 180
"""

import sys
import os
import json
import math
from datetime import datetime, date

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from data_pipeline import download_full_history, normalize_symbol

# ═══════════════════════════════════════════════════════════════
# 1. 特征提取
# ═══════════════════════════════════════════════════════════════

def extract_features(symbol: str) -> dict:
    """
    提取股票特征向量，用于策略适配。

    特征包括:
      - 年化波动率 (基于日收益率标准差 * sqrt(252))
      - 平均换手率 (日换手率的均值)
      - PE中位数 (历史PE中位数，若有)
      - 牛熊市比例 (BEAR天数 / 总天数)
      - 市值级别 (小<200亿 / 中200-1000亿 / 大>1000亿)
      - 行业类型 (基于代码前缀推断)
      - 价格区间 (最低/最高/当前)
      - 日均成交量
      - MA60趋势持续性 (MA60斜率 >0 的天数占比)
    """
    sym, exchange = normalize_symbol(symbol)

    try:
        cache_path = download_full_history(sym, ndays=800)
        df = pd.read_csv(cache_path)
        df['date'] = pd.to_datetime(df['date'])
        df = df.sort_values('date').reset_index(drop=True)
    except Exception as e:
        print(f"  [WARN] 历史数据下载失败: {e}，使用占位特征")
        return _placeholder_features(symbol)

    close = df['close']
    vol = df['volume']
    high = df['high']
    low = df['low']

    # 年化波动率
    daily_returns = close.pct_change().dropna()
    annual_vol = round(float(daily_returns.std() * math.sqrt(252) * 100), 2) if len(daily_returns) > 0 else 30.0

    # 平均换手率 (用成交量/流通盘估算，这里用日收益率波动近似)
    avg_turnover = None  # 历史数据中换手率不可得

    # PE中位数 (历史数据中PE不可得，标记为None)
    pe_median = None

    # 牛熊市比例：计算MA60并判断趋势
    ma60 = close.rolling(60).mean()
    # MA60斜率（10日变化率）
    ma60_slope = ma60.pct_change(10) * 100
    bear_days = int(((close < ma60) & (ma60_slope < 0)).sum())
    total_days = len(close.dropna())
    bear_ratio = round(bear_days / total_days * 100, 1) if total_days > 0 else 0

    bull_days = int(((close > ma60) & (ma60_slope > 0)).sum())
    bull_ratio = round(bull_days / total_days * 100, 1) if total_days > 0 else 0
    sideways_ratio = round(100 - bull_ratio - bear_ratio, 1)

    # 市值级别 (基于价格*固定股数，非常粗略)
    current_price = float(close.iloc[-1])
    # 无法获取精确市值，使用代码前缀判断大中小
    if symbol.startswith('6'):
        market_cap_level = "中大市值(沪市主板)"
    elif symbol.startswith('0') or symbol.startswith('3'):
        market_cap_level = "中小市值(深市)"
    elif symbol.startswith('688'):
        market_cap_level = "科创板"
    elif symbol.startswith('8') or symbol.startswith('4'):
        market_cap_level = "北交所"
    else:
        market_cap_level = "未知"

    # 行业类型
    industry = _guess_industry(symbol)

    # 价格区间
    price_min = round(float(close.min()), 2)
    price_max = round(float(close.max()), 2)
    price_current = round(float(close.iloc[-1]), 2)

    # 日均成交量
    avg_daily_vol = round(float(vol.mean()), 0)

    # MA60趋势持续性
    ma60_up_days = int((ma60_slope > 0).sum())
    ma60_up_ratio = round(ma60_up_days / total_days * 100, 1) if total_days > 0 else 50

    # 极端波动事件（单日涨跌>5%次数）
    extreme_days = int((abs(daily_returns) > 5).sum())
    extreme_ratio = round(extreme_days / len(daily_returns) * 100, 1) if len(daily_returns) > 0 else 0

    # 最大连续下跌天数
    down_streak = 0
    max_down_streak = 0
    for ret in daily_returns:
        if ret < 0:
            down_streak += 1
            max_down_streak = max(max_down_streak, down_streak)
        else:
            down_streak = 0

    features = {
        "symbol": symbol,
        "annual_volatility_pct": annual_vol,
        "avg_turnover_pct": avg_turnover,
        "pe_median": pe_median,
        "market_state": {
            "bull_ratio_pct": bull_ratio,
            "bear_ratio_pct": bear_ratio,
            "sideways_ratio_pct": sideways_ratio,
        },
        "market_cap_level": market_cap_level,
        "industry": industry,
        "price_range": {"min": price_min, "max": price_max, "current": price_current},
        "avg_daily_volume": avg_daily_vol,
        "ma60_up_ratio_pct": ma60_up_ratio,
        "extreme_volatility_ratio_pct": extreme_ratio,
        "max_consecutive_down_days": max_down_streak,
        "data_days": total_days,
    }

    print(f"  [特征提取] {symbol}: 波动率={annual_vol}% 熊市={bear_ratio}% "
          f"极端波动={extreme_ratio}% 行业={industry}")
    return features


def _guess_industry(symbol: str) -> str:
    """根据股票代码前缀推断行业分类。"""
    prefix = symbol[:3]
    mapping = {
        "600": "主板-综合",
        "601": "主板-大盘蓝筹",
        "603": "主板-制造",
        "605": "主板-消费",
        "688": "科创板-科技",
        "000": "深市主板-综合",
        "001": "深市主板-金融地产",
        "002": "中小板-制造科技",
        "003": "中小板-消费",
        "300": "创业板-成长科技",
        "301": "创业板-新兴",
    }
    for key, val in mapping.items():
        if symbol.startswith(key):
            return val
    return "未知行业"


def _placeholder_features(symbol: str) -> dict:
    return {
        "symbol": symbol,
        "annual_volatility_pct": 30.0,
        "avg_turnover_pct": None,
        "pe_median": None,
        "market_state": {"bull_ratio_pct": 33, "bear_ratio_pct": 33, "sideways_ratio_pct": 34},
        "market_cap_level": "未知",
        "industry": _guess_industry(symbol),
        "price_range": {"min": 0, "max": 0, "current": 0},
        "avg_daily_volume": 0,
        "ma60_up_ratio_pct": 50,
        "extreme_volatility_ratio_pct": 0,
        "max_consecutive_down_days": 0,
        "data_days": 0,
    }


# ═══════════════════════════════════════════════════════════════
# 2. 策略DNA生成
# ═══════════════════════════════════════════════════════════════

ADAPTATION_PROMPT = """你是一位策略移植专家，负责将一只股票的最优策略参数适配到另一只完全不同的股票。

## 源股票（基准）的特征与最优参数
{base_features}

{base_params}

{base_diagnosis}

## 目标股票的特征
{target_features}

## 适配规则（必须遵守）
1. **不得照搬参数** — 每项参数必须根据目标股票特征给出调整理由
2. **波动率适配** — 目标波动率更高 → 止损放宽、仓位降低；波动率更低 → 止损收紧、仓位提高
3. **熊市比例适配** — 目标熊市占比更高 → 更保守的入场阈值、更严格的空仓规则
4. **极端波动适配** — 目标极端波动>5% → ATR止损倍数×1.3-1.5
5. **市值/行业适配** — 大市值→中长线权重更高；创业板→放宽波动容忍

## 输出格式
请输出严格JSON（不要markdown代码块包裹）：
{
  "symbol": "目标代码",
  "adapted_from": "源代码",
  "adaptation_rationale": "一句话总结核心适配逻辑",
  "param_changes": [
    {"param": "参数名", "base_value": "原值", "new_value": "新值", "reason": "调整理由（必须具体到目标股票特征数据）"}
  ],
  "params": {
    "short_term": {
      "entry_threshold_score": 4,
      "position_pct": 19,
      "atr_stop_multiplier": 1.5,
      "confidence_min": 0.45,
      "rsi_oversold_threshold": 30,
      "rsi_overbought_threshold": 70
    },
    "mid_term": {
      "entry_threshold_score": 4,
      "position_pct": 18,
      "atr_stop_multiplier": 2.5,
      "confidence_min": 0.45,
      "ma20_slope_min": 0.0
    },
    "long_term": {
      "entry_threshold_score": 5,
      "position_pct": 25,
      "atr_stop_multiplier": 3.5,
      "confidence_min": 0.40,
      "ma60_stop_discount": 0.95,
      "roe_min": 15,
      "revenue_growth_min": 20
    },
    "trend_filter": {
      "bear_short_block": true,
      "bear_mid_block": true,
      "bear_long_allow": true,
      "sideways_position_factor": 0.7,
      "bull_position_factor": 1.2
    },
    "risk_control": {
      "max_single_position_pct": 30,
      "max_total_position_pct": 80,
      "cash_reserve_pct": 10,
      "cooling_period_days": 0
    }
  }
}"""


def generate_adapted_dna(symbol: str, base_params_path: str = "600744_best_params.json",
                          use_mock: bool = True) -> dict:
    """
    调用 deepseek_chat 生成自适应策略参数。

    Args:
        symbol: 目标股票代码
        base_params_path: 基准策略参数文件路径（来自华银电力最优参数）
        use_mock: True=启发式适配（快速但粗略），False=LLM深度适配

    Returns:
        适配后的完整参数 dict（格式见 ADAPTATION_PROMPT 输出定义）
    """
    project_dir = os.path.dirname(os.path.abspath(__file__))

    # 1. 提取目标股票特征
    print(f"\n  [DNA生成] 提取 {symbol} 特征...")
    target_features = extract_features(symbol)

    # 2. 加载基准参数
    base_params_path = os.path.join(project_dir, os.path.basename(base_params_path))
    if os.path.exists(base_params_path):
        with open(base_params_path, 'r', encoding='utf-8') as f:
            base_data = json.load(f)
    else:
        print(f"  [WARN] 基准参数文件不存在: {base_params_path}，使用默认参数")
        base_data = _default_base_params()

    base_params = base_data.get("params", base_data)

    # 3. 加载基准股票诊断
    diag_path = os.path.join(project_dir, "strategy_diagnosis.md")
    base_diagnosis = ""
    if os.path.exists(diag_path):
        with open(diag_path, 'r', encoding='utf-8') as f:
            base_diagnosis = f.read()[:2000]
    else:
        base_diagnosis = "（无历史诊断报告）"

    # 4. 提取基准股票特征（从 base_data 或重新提取）
    base_symbol = base_data.get("symbol", base_data.get("adapted_from", "600744"))
    if "base_features" in base_data:
        base_features_str = json.dumps(base_data["base_features"], ensure_ascii=False, indent=2)
    else:
        try:
            base_features = extract_features(base_symbol)
            base_features_str = json.dumps(base_features, ensure_ascii=False, indent=2)
        except Exception:
            base_features_str = f"（无法提取 {base_symbol} 特征）"

    if use_mock:
        # ── 启发式适配 ──
        result = _mock_adapt_dna(symbol, target_features, base_params)
    else:
        # ── LLM 深度适配 ──
        from deepseek_client import deepseek_chat
        prompt = ADAPTATION_PROMPT.format(
            base_features=base_features_str,
            base_params=json.dumps(base_params, ensure_ascii=False, indent=2),
            base_diagnosis=base_diagnosis[:2000],
            target_features=json.dumps(target_features, ensure_ascii=False, indent=2),
        )
        try:
            raw = deepseek_chat(prompt, "请输出适配后的完整参数JSON。")
            import re as _re
            clean = raw.strip()
            try:
                result = json.loads(clean)
            except json.JSONDecodeError:
                m = _re.search(r'\{.*\}', clean, re.DOTALL)
                result = json.loads(m.group(0)) if m else _mock_adapt_dna(symbol, target_features, base_params)
        except Exception as e:
            print(f"  [WARN] LLM适配失败: {e}，使用启发式适配")
            result = _mock_adapt_dna(symbol, target_features, base_params)

    # 5. 保存适配参数
    adapted_path = os.path.join(project_dir, f"{symbol}_adapted_params.json")
    result["base_features"] = target_features
    result["adapted_at"] = datetime.now().isoformat()
    with open(adapted_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"  [DNA保存] → {symbol}_adapted_params.json")

    # 打印关键差异
    changes = result.get("param_changes", [])
    if changes:
        print(f"  [参数差异] {len(changes)}项关键调整:")
        for c in changes[:5]:
            print(f"    {c['param']}: {c['base_value']} → {c['new_value']} ({c['reason'][:40]}...)")
    print(f"  [适配理由] {result.get('adaptation_rationale', '')}")

    return result


def _mock_adapt_dna(symbol: str, features: dict, base_params: dict) -> dict:
    """启发式参数适配（无需API调用），基于特征差异调整参数。"""
    annual_vol = features.get("annual_volatility_pct", 30)
    bear_ratio = features.get("market_state", {}).get("bear_ratio_pct", 33)
    extreme_ratio = features.get("extreme_volatility_ratio_pct", 5)
    max_down = features.get("max_consecutive_down_days", 5)

    # 基准参数（600744 默认值）
    sp = base_params.get("short_term", {})
    mp = base_params.get("mid_term", {})
    lp = base_params.get("long_term", {})
    tf = base_params.get("trend_filter", {})
    rc = base_params.get("risk_control", {})

    changes = []

    # 波动率适配
    vol_factor = annual_vol / 30.0  # 相对于30%基准波动率
    if annual_vol > 45:
        new_short_atr = round(1.5 * 1.4, 1)
        new_mid_atr = round(2.5 * 1.3, 1)
        new_long_atr = round(3.5 * 1.2, 1)
        changes.append({"param": "short_term.atr_stop_multiplier", "base_value": "1.5", "new_value": str(new_short_atr),
                       "reason": f"波动率{annual_vol}%>45%，ATR止损放宽以容纳极端波动"})
    elif annual_vol < 20:
        new_short_atr = round(1.5 * 0.8, 1)
        new_mid_atr = round(2.5 * 0.85, 1)
        new_long_atr = round(3.5 * 0.9, 1)
        changes.append({"param": "short_term.atr_stop_multiplier", "base_value": "1.5", "new_value": str(new_short_atr),
                       "reason": f"波动率{annual_vol}%<20%，收紧止损提高效率"})
    else:
        vol_adj = max(0.7, min(1.3, vol_factor))
        new_short_atr = round(1.5 * vol_adj, 1)
        new_mid_atr = round(2.5 * vol_adj, 1)
        new_long_atr = round(3.5 * vol_adj, 1)

    # 熊市比例适配
    if bear_ratio > 50:
        new_entry_short = max(3, sp.get("entry_threshold_score", 4) + 2)
        new_entry_mid = max(3, mp.get("entry_threshold_score", 4) + 2)
        changes.append({"param": "short_term.entry_threshold_score", "base_value": str(sp.get("entry_threshold_score", 4)),
                       "new_value": str(new_entry_short),
                       "reason": f"熊市占比{bear_ratio}%>50%，提高入场门槛减少接飞刀"})
        new_short_pos = max(8, sp.get("position_pct", 19) - 8)
        new_mid_pos = max(10, mp.get("position_pct", 18) - 6)
        changes.append({"param": "short_term.position_pct", "base_value": str(sp.get("position_pct", 19)),
                       "new_value": str(new_short_pos),
                       "reason": f"高熊市占比，降低仓位控制回撤"})
    elif bear_ratio < 20:
        new_entry_short = max(3, sp.get("entry_threshold_score", 4) - 1)
        new_entry_mid = max(3, mp.get("entry_threshold_score", 4) - 1)
        changes.append({"param": "short_term.entry_threshold_score", "base_value": str(sp.get("entry_threshold_score", 4)),
                       "new_value": str(new_entry_short),
                       "reason": f"熊市占比仅{bear_ratio}%<20%，牛市环境可适当降低入场门槛"})
        new_short_pos = min(30, sp.get("position_pct", 19) + 5)
        new_mid_pos = min(40, mp.get("position_pct", 18) + 8)
        changes.append({"param": "short_term.position_pct", "base_value": str(sp.get("position_pct", 19)),
                       "new_value": str(new_short_pos),
                       "reason": f"低熊市占比，提高仓位捕捉牛市收益"})
    else:
        new_entry_short = sp.get("entry_threshold_score", 4)
        new_entry_mid = mp.get("entry_threshold_score", 4)
        new_short_pos = sp.get("position_pct", 19)
        new_mid_pos = mp.get("position_pct", 18)

    new_entry_long = lp.get("entry_threshold_score", 5)
    new_long_pos = lp.get("position_pct", 25)

    # 极端波动适配
    if extreme_ratio > 10:
        changes.append({"param": "risk_control.cooling_period_days", "base_value": "0", "new_value": "2",
                       "reason": f"极端波动{extreme_ratio}%>10%，增加交易冷却期"})
        cooling_days = 2
    else:
        cooling_days = 0

    # 最大连续下跌适配
    if max_down > 8:
        changes.append({"param": "mid_term.atr_stop_multiplier", "base_value": "2.5", "new_value": str(new_mid_atr),
                       "reason": f"最大连续下跌{max_down}天，放宽中线止损避免被震出"})

    params = {
        "short_term": {
            "entry_threshold_score": new_entry_short,
            "position_pct": new_short_pos,
            "atr_stop_multiplier": new_short_atr,
            "confidence_min": sp.get("confidence_min", 0.45),
            "rsi_oversold_threshold": sp.get("rsi_oversold_threshold", 30),
            "rsi_overbought_threshold": sp.get("rsi_overbought_threshold", 70),
        },
        "mid_term": {
            "entry_threshold_score": new_entry_mid,
            "position_pct": new_mid_pos,
            "atr_stop_multiplier": new_mid_atr,
            "confidence_min": mp.get("confidence_min", 0.45),
            "ma20_slope_min": mp.get("ma20_slope_min", 0.0),
        },
        "long_term": {
            "entry_threshold_score": new_entry_long,
            "position_pct": new_long_pos,
            "atr_stop_multiplier": new_long_atr,
            "confidence_min": lp.get("confidence_min", 0.4),
            "ma60_stop_discount": lp.get("ma60_stop_discount", 0.95),
            "roe_min": lp.get("roe_min", 15),
            "revenue_growth_min": lp.get("revenue_growth_min", 20),
        },
        "trend_filter": {
            "bear_short_block": tf.get("bear_short_block", True),
            "bear_mid_block": tf.get("bear_mid_block", True),
            "bear_long_allow": tf.get("bear_long_allow", True),
            "sideways_position_factor": tf.get("sideways_position_factor", 0.7),
            "bull_position_factor": tf.get("bull_position_factor", 1.2),
        },
        "risk_control": {
            "max_single_position_pct": rc.get("max_single_position_pct", 30),
            "max_total_position_pct": rc.get("max_total_position_pct", 80),
            "cash_reserve_pct": rc.get("cash_reserve_pct", 10),
            "cooling_period_days": cooling_days,
        },
    }

    return {
        "symbol": symbol,
        "adapted_from": base_params.get("symbol", "600744"),
        "adaptation_rationale": f"基于波动率{features.get('annual_volatility_pct', 30)}%和熊市占比{bear_ratio}%的启发式适配",
        "param_changes": changes,
        "params": params,
    }


def _default_base_params() -> dict:
    """默认基准参数（600744 调试版最优参数）。"""
    return {
        "symbol": "600744",
        "params": {
            "short_term": {
                "entry_threshold_score": 4,
                "position_pct": 19,
                "atr_stop_multiplier": 1.5,
                "confidence_min": 0.45,
                "rsi_oversold_threshold": 30,
                "rsi_overbought_threshold": 70,
            },
            "mid_term": {
                "entry_threshold_score": 4,
                "position_pct": 18,
                "atr_stop_multiplier": 2.5,
                "confidence_min": 0.45,
                "ma20_slope_min": 0.0,
            },
            "long_term": {
                "entry_threshold_score": 5,
                "position_pct": 25,
                "atr_stop_multiplier": 3.5,
                "confidence_min": 0.4,
                "ma60_stop_discount": 0.95,
                "roe_min": 15,
                "revenue_growth_min": 20,
            },
            "trend_filter": {
                "bear_short_block": True,
                "bear_mid_block": True,
                "bear_long_allow": True,
                "sideways_position_factor": 0.7,
                "bull_position_factor": 1.2,
            },
            "risk_control": {
                "max_single_position_pct": 30,
                "max_total_position_pct": 80,
                "cash_reserve_pct": 10,
                "cooling_period_days": 0,
            },
        },
    }


# ═══════════════════════════════════════════════════════════════
# 3. 自动适配+回测
# ═══════════════════════════════════════════════════════════════

def auto_adapt_and_backtest(symbol: str, time_frame: str = "mid",
                             days: int = 180, max_rounds: int = 5,
                             initial_capital: float = 100000.0,
                             use_mock: bool = True) -> dict:
    """
    全自动策略适配+回测验证。

    流程:
      1. 提取目标股票特征
      2. 生成适配DNA参数（对比华银电力基准）
      3. 运行回测
      4. 若夏普<0.5或亏损，启动 deep_critique 迭代改进（最多3轮）
      5. 返回最终结果

    Returns:
        与 run_backtest_with_critic 兼容的结果字典
    """
    print(f"""
╔══════════════════════════════════════════════════════════╗
║     🧬 全市场自适应策略引擎                              ║
║     输入代码 → 自动基因匹配 → 策略自进化                 ║
║                                                        ║
║     目标: {symbol:<20}              ║
║     维度: {time_frame:<20}              ║
╚══════════════════════════════════════════════════════════╝
""")

    # 1. 提取特征并生成适配DNA
    dna = generate_adapted_dna(symbol, base_params_path="600744_best_params.json",
                                use_mock=use_mock)

    # 2. 运行回测
    from backtest_runner import run_backtest_with_critic

    print(f"\n  [回测] 启动 {symbol} {time_frame}维度 {days}天回测...")
    result = run_backtest_with_critic(
        symbol=symbol,
        time_frame=time_frame,
        days=days,
        max_rounds=max_rounds,
        initial_capital=initial_capital,
        use_mock=use_mock,
    )

    metrics = result.get("rounds", [{}])[-1].get("backtest_metrics", {}) if result.get("rounds") else {}
    sharpe = metrics.get("sharpe_ratio", 0)
    total_return = metrics.get("total_return_pct", 0)

    # 3. 若不达标，启动 deep_critique 迭代
    if (sharpe < 0.5 or total_return < 0) and max_rounds > 0:
        print(f"\n  [⚠] 回测不达标(夏普{sharpe:.2f}, 收益{total_return:+.2f}%)，启动深度诊断迭代...")

        from critic_agent import deep_critique
        diag = deep_critique(result, use_mock=use_mock, save_report=True)

        # 尝试应用诊断建议（最多3轮额外迭代）
        extra_rounds = min(3, max_rounds)
        if extra_rounds > 0:
            print(f"\n  [迭代] 基于诊断结果追加 {extra_rounds} 轮改进...")
            from auto_improver import apply_fix
            project_dir = os.path.dirname(os.path.abspath(__file__))
            plan = diag.get("improvement_plan", {})
            short_fixes = plan.get("short_term", [])
            for fix in short_fixes[:2]:
                try:
                    apply_fix(fix, project_dir)
                except Exception:
                    pass

            # 重新回测
            from backtest_runner import run_backtest_with_critic as _rerun
            result = _rerun(
                symbol=symbol, time_frame=time_frame, days=days,
                max_rounds=extra_rounds, initial_capital=initial_capital,
                use_mock=use_mock,
            )

    return result


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════
if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8')

    import argparse
    parser = argparse.ArgumentParser(description='策略基因提取与全市场自适应引擎')
    parser.add_argument('symbol', help='目标股票代码')
    parser.add_argument('--base', default='600744_best_params.json',
                        help='基准策略参数文件')
    parser.add_argument('--time_frame', default='mid',
                        choices=['short', 'mid', 'long'],
                        help='回测时间维度')
    parser.add_argument('--days', type=int, default=180,
                        help='回测天数')
    parser.add_argument('--max_rounds', type=int, default=5,
                        help='最大进化轮数')
    parser.add_argument('--capital', type=float, default=100000,
                        help='初始资金')
    parser.add_argument('--features-only', action='store_true',
                        help='仅提取特征，不执行回测')
    parser.add_argument('--no-mock', action='store_true',
                        help='使用真实API（非Mock）')
    args = parser.parse_args()

    if args.features_only:
        features = extract_features(args.symbol)
        print(json.dumps(features, ensure_ascii=False, indent=2))
    else:
        result = auto_adapt_and_backtest(
            symbol=args.symbol,
            time_frame=args.time_frame,
            days=args.days,
            max_rounds=args.max_rounds,
            initial_capital=args.capital,
            use_mock=not args.no_mock,
        )
        print(f"\n{'='*70}")
        print(f"  ADAPTATION COMPLETE: {args.symbol}")
        print(f"  Final Score: {result.get('final_score', '?')}/10")
        print(f"  Score History: {result.get('score_history', [])}")
        print(f"{'='*70}")
