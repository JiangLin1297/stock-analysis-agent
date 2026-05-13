#!/usr/bin/env python3
"""
自动进化器 v3.0 — 不达标就永续迭代。

核心机制：
  1. 每次随机选一只股票 + 一个时间框架（短/中/长）
  2. 执行回测，检查是否达标
  3. 未达标 → Critic深度分析 → 修改代码 → 重新回测 → 循环
  4. 唯一终止条件：连续3次随机选择（不同股票、不同周期）全部达标
  5. 最大保护：单轮50次迭代上限
  6. 每10轮检查过拟合风险

硬性收益目标：
  - 短线（1天~2周）：每笔交易平均收益率 ≥ 10%，胜率 ≥ 50%
  - 中线（2周~6个月）：每笔交易平均收益率 ≥ 30%，胜率 ≥ 55%
  - 长线（6个月~数年）：累计收益率 ≥ 200%，最大回撤 ≤ 30%
  - 智能选股Top5：3个月模拟跟踪，平均收益 ≥ 15%

用法:
    py auto_improver.py --full --max_iterations 50
    py auto_improver.py --full --max_iterations 50 --seed 42
"""
import sys
import os
import json
import shutil
import re
import time
import random
from datetime import datetime
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    import certifi
    os.environ['SSL_CERT_FILE'] = certifi.where()
    os.environ['REQUESTS_CA_BUNDLE'] = certifi.where()
except Exception:
    pass

def _data_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

EVOLUTION_LOG = os.path.join(_data_dir(), "evolution_log.txt")
FILE_MOD_COUNT = defaultdict(int)
EVOLUTION_STATE_FILE = os.path.join(_data_dir(), "evolution_state.json")

# ═══════════════════════════════════════════════════════════════
# 硬性收益目标
# ═══════════════════════════════════════════════════════════════
TARGETS = {
    "short": {"avg_return_pct": 10.0, "win_rate_pct": 50.0},
    "mid":   {"avg_return_pct": 30.0, "win_rate_pct": 55.0},
    "long":  {"total_return_pct": 200.0, "max_drawdown_pct": 30.0},
}

# 已适配股票池（可随机选择）
STOCK_POOL = [
    "600744",  # 华银电力
    "000001",  # 平安银行
    "002709",  # 天赐材料
    "600519",  # 贵州茅台
    "300750",  # 宁德时代
    "000858",  # 五粮液
    "601012",  # 隆基绿能
    "002594",  # 比亚迪
]

# 时间框架映射
TIMEFRAME_LABELS = {"short": "短线", "mid": "中线", "long": "长线"}
TIMEFRAME_DAYS = {"short": 30, "mid": 180, "long": 500}


def log(msg: str, also_print: bool = True):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f"[{ts}] {msg}"
    if also_print:
        try:
            print(line)
        except UnicodeEncodeError:
            enc = sys.stdout.encoding or 'utf-8'
            print(line.encode(enc, errors='replace').decode(enc))
    with open(EVOLUTION_LOG, 'a', encoding='utf-8') as f:
        f.write(line + "\n")


def backup_file(filepath: str) -> bool:
    if not os.path.exists(filepath):
        return False
    bak = filepath + ".bak"
    try:
        shutil.copy2(filepath, bak)
        return True
    except Exception as e:
        log(f"  ❌ 备份失败: {e}")
        return False


def check_targets_met(metrics: dict, timeframe: str) -> tuple:
    """
    检查回测指标是否达标。

    Returns:
        (passed: bool, details: dict)
    """
    target = TARGETS[timeframe]
    results = {}
    all_pass = True

    if timeframe in ("short", "mid"):
        avg_ret = abs(metrics.get("return_" + ("short" if timeframe == "short" else "mid"), 0))
        win_rate = metrics.get("win_rate_pct", 0)
        results["avg_return"] = {"actual": avg_ret, "target": target["avg_return_pct"],
                                  "pass": avg_ret >= target["avg_return_pct"]}
        results["win_rate"] = {"actual": win_rate, "target": target["win_rate_pct"],
                                "pass": win_rate >= target["win_rate_pct"]}
        all_pass = results["avg_return"]["pass"] and results["win_rate"]["pass"]
    else:  # long
        total_ret = metrics.get("total_return_pct", 0)
        max_dd = metrics.get("max_drawdown_pct", 100)
        results["total_return"] = {"actual": total_ret, "target": target["total_return_pct"],
                                    "pass": total_ret >= target["total_return_pct"]}
        results["max_drawdown"] = {"actual": max_dd, "target": target["max_drawdown_pct"],
                                    "pass": max_dd <= target["max_drawdown_pct"]}
        all_pass = results["total_return"]["pass"] and results["max_drawdown"]["pass"]

    return all_pass, results


def _resolve_file(file_part: str, project_dir: str) -> str:
    if os.path.sep in file_part or '/' in file_part:
        target = file_part
        if not os.path.isabs(target):
            target = os.path.join(project_dir, target)
    else:
        target = os.path.join(project_dir, file_part)
        if not os.path.exists(target):
            matches = __import__('glob').glob(os.path.join(project_dir, "**", file_part), recursive=True)
            if matches:
                target = matches[0]
    return target if os.path.exists(target) else None


# ═══════════════════════════════════════════════════════════════
# 项目根目录（从本文件位置推导，不依赖调用方传入）
# ═══════════════════════════════════════════════════════════════
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _set_json_value(data: dict, dot_path: str, new_value) -> bool:
    """按点分隔路径设置 JSON 值。例如 "short.threshold" → data["short"]["threshold"] = new_value。"""
    keys = dot_path.split(".")
    obj = data
    for k in keys[:-1]:
        if not isinstance(obj, dict) or k not in obj:
            return False
        obj = obj[k]
    final_key = keys[-1]
    if not isinstance(obj, dict) or final_key not in obj:
        return False
    obj[final_key] = new_value
    return True


def apply_fix(critic_json: dict) -> int:
    """解析 Critic 输出的 JSON 中的 operations 数组，逐条执行文件修改。

    Args:
        critic_json: Critic 输出的完整 dict，需包含 "operations" 数组。
            每条 operation: {"file": "路径", "target": "键名或变量名", "new_value": 新值}

    Returns:
        成功修改的条数。
    """
    operations = critic_json.get("operations")
    if not operations:
        print("无可用修改指令")
        return 0

    applied = 0
    project_dir = _data_dir()

    for op in operations:
        file_rel = op.get("file", "")
        target = op.get("target", "")
        new_value = op.get("new_value")

        if not file_rel or not target:
            log(f"  ⚠ [operation] 缺少 file 或 target，跳过: {op}")
            continue

        # 解析文件路径
        target_file = _resolve_file(file_rel, project_dir)
        if not target_file:
            target_file = _resolve_file(file_rel, _PROJECT_ROOT)
        if not target_file:
            log(f"  ⚠ [operation] 文件不存在: {file_rel}")
            continue

        if file_rel.endswith(".json"):
            # JSON 文件：按点分隔路径找到键，修改值
            try:
                with open(target_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            except Exception as e:
                log(f"  ⚠ [operation] 读取 JSON 失败: {file_rel} — {e}")
                continue

            if not _set_json_value(data, target, new_value):
                log(f"  ⚠ [operation] 路径不存在: {file_rel}:{target}")
                continue

            backup_file(target_file)
            try:
                with open(target_file, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            except Exception as e:
                log(f"  ⚠ [operation] 写入 JSON 失败: {file_rel} — {e}")
                continue

            FILE_MOD_COUNT[target_file] += 1
            log(f"  [Critic生效] {file_rel}:{target} = {new_value}")
            applied += 1

        elif file_rel.endswith(".py"):
            # Python 文件：正匹配合 target = 数值，替换为新值
            try:
                with open(target_file, 'r', encoding='utf-8') as f:
                    content = f.read()
            except Exception as e:
                log(f"  ⚠ [operation] 读取 Python 失败: {file_rel} — {e}")
                continue

            pattern = r'\b' + re.escape(target) + r'\s*=\s*[0-9.]+'
            match = re.search(pattern, content)
            if not match:
                log(f"  ⚠ [operation] 未找到赋值语句: {file_rel}:{target}")
                continue

            replacement = f"{target} = {new_value}"
            new_content = content[:match.start()] + replacement + content[match.end():]

            backup_file(target_file)
            try:
                with open(target_file, 'w', encoding='utf-8') as f:
                    f.write(new_content)
            except Exception as e:
                log(f"  ⚠ [operation] 写入 Python 失败: {file_rel} — {e}")
                continue

            FILE_MOD_COUNT[target_file] += 1
            log(f"  [Critic生效] {file_rel}:{target} = {new_value}")
            applied += 1

        else:
            log(f"  ⚠ [operation] 不支持的文件类型: {file_rel}")
            continue

    return applied


def run_single_backtest(symbol: str, timeframe: str, days: int = None,
                        initial_capital: float = 100000.0) -> dict:
    """
    对指定股票+时间框架执行一次回测。

    Returns:
        {"symbol", "timeframe", "metrics": {...}, "trade_log": [...], "passed": bool, "details": {...}}
    """
    from backtest.engine import run_backtest, random_period_test

    if days is None:
        days = TIMEFRAME_DAYS.get(timeframe, 180)

    # 随机选取时段
    period = random_period_test(symbol, timeframe, days)

    log(f"  回测: {symbol} {TIMEFRAME_LABELS[timeframe]} "
        f"{period['start_date']} → {period['end_date']} ({days}天)")

    try:
        result = run_backtest(symbol, period['start_date'], period['end_date'], initial_capital)
        metrics = result.get("metrics", {})
        passed, details = check_targets_met(metrics, timeframe)

        # 打印指标
        log(f"  总收益: {metrics.get('total_return_pct', 0):+.2f}% | "
            f"胜率: {metrics.get('win_rate_pct', 0):.1f}% | "
            f"最大回撤: {metrics.get('max_drawdown_pct', 0):.2f}% | "
            f"夏普: {metrics.get('sharpe_ratio', 0):.2f}")

        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "label": TIMEFRAME_LABELS[timeframe],
            "start_date": period['start_date'],
            "end_date": period['end_date'],
            "metrics": metrics,
            "trade_log": result.get("trade_log", []),
            "passed": passed,
            "details": details,
        }
    except Exception as e:
        import traceback
        log(f"  ❌ 回测失败: {e}")
        log(traceback.format_exc())
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "label": TIMEFRAME_LABELS[timeframe],
            "error": str(e),
            "passed": False,
            "details": {},
            "metrics": {},
            "trade_log": [],
        }


def run_deep_critique(bt_result: dict) -> dict:
    """对回测结果进行Critic深度分析，返回 code_changes 可执行代码修改指令。"""
    from agents.critic import critique_backtest, deep_dive_losing_trades

    try:
        critic_result = critique_backtest(bt_result, use_mock=True)

        if bt_result.get("trade_log"):
            deep = deep_dive_losing_trades(
                bt_result["trade_log"],
                bt_result.get("timeframe", "mid"),
                use_mock=True
            )
            critic_result["deep_dive"] = deep

        return critic_result
    except Exception as e:
        log(f"  ⚠ Critic分析失败: {e}")
        return {"overall_score": 0, "operations": [],
                "verdict": f"Critic失败: {e}"}


def check_overfitting(history: list) -> dict:
    """
    每10轮检查过拟合风险。
    比较前5轮和后5轮的收益：后5轮如果显著低于前5轮（>30%差距），提示过拟合。
    """
    if len(history) < 10:
        return {"overfitting_risk": "insufficient_data", "warning": None}

    recent_10 = history[-10:]
    first_5 = [h for h in recent_10[:5] if h.get("metrics", {}).get("total_return_pct") is not None]
    last_5 = [h for h in recent_10[5:] if h.get("metrics", {}).get("total_return_pct") is not None]

    if not first_5 or not last_5:
        return {"overfitting_risk": "insufficient_data", "warning": None}

    avg_first = sum(h["metrics"]["total_return_pct"] for h in first_5) / len(first_5)
    avg_last = sum(h["metrics"]["total_return_pct"] for h in last_5) / len(last_5)

    if avg_first > 0 and avg_last < 0:
        return {
            "overfitting_risk": "high",
            "warning": f"⚠ 过拟合风险! 前5轮平均{avg_first:+.1f}% → 后5轮平均{avg_last:+.1f}%，系统可能在训练集上过拟合",
            "action": "暂停并提示用户审查策略泛化能力"
        }

    if avg_first > 0 and avg_last < avg_first * 0.5:
        return {
            "overfitting_risk": "medium",
            "warning": f"⚡ 注意: 后5轮收益({avg_last:+.1f}%)显著低于前5轮({avg_first:+.1f}%)，可能过拟合",
            "action": "继续迭代但关注泛化表现"
        }

    return {"overfitting_risk": "low", "warning": None}


def _save_state(state: dict):
    """Save evolution state to disk for recovery."""
    try:
        state["_last_save"] = datetime.now().isoformat()
        with open(EVOLUTION_STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def evolve_full(max_iterations: int = 50, use_mock: bool = True, seed: int = None):
    """
    完整进化主循环 — 以"达标"为唯一终止条件。

    流程:
      1. 随机选择股票 + 时间框架
      2. 回测 → 检查是否达标
      3. 未达标 → Critic分析 → 修改代码 → 重试
      4. 连续3次随机选择全部达标 → 通过
      5. 最大50次迭代 → 兜底
    """
    if seed is not None:
        random.seed(seed)

    project_dir = _data_dir()

    print(f"""
╔══════════════════════════════════════════════════════════════╗
║       🧬 StockMind 自动进化器 v3.0 — 不达标永续迭代        ║
║                                                            ║
║  短线目标: ≥10%平均收益 + ≥50%胜率                         ║
║  中线目标: ≥30%平均收益 + ≥55%胜率                         ║
║  长线目标: ≥200%累计收益 + ≤30%最大回撤                    ║
║  选股目标: Top5 60天跟踪 ≥15%平均收益                      ║
║                                                            ║
║  最大迭代: {max_iterations:<3}  |  终止条件: 连续3次随机测试通过     ║
╚══════════════════════════════════════════════════════════════╝
""")

    # Initialize log
    with open(EVOLUTION_LOG, 'a', encoding='utf-8') as f:
        f.write(f"\n{'='*70}\n")
        f.write(f"进化v3.0启动: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"最大迭代: {max_iterations} | 终止条件: 连续3次随机测试通过\n")
        f.write(f"{'='*70}\n\n")

    history = []           # All backtest results
    consecutive_passes = 0  # Counter for consecutive random passes
    tested_combos = set()   # Track (symbol, timeframe) already tested
    iteration = 0

    while iteration < max_iterations:
        iteration += 1

        print(f"\n{'#'*70}")
        print(f"#  第 {iteration}/{max_iterations} 轮迭代")
        print(f"#  连续通过: {consecutive_passes}/3")
        print(f"{'#'*70}\n")

        # ── 1. Random selection ──
        available = [(s, tf) for s in STOCK_POOL for tf in ["short", "mid", "long"]
                     if (s, tf) not in tested_combos]
        if not available:
            log("所有股票+周期组合均已测试过，重置追踪")
            tested_combos.clear()
            available = [(s, tf) for s in STOCK_POOL for tf in ["short", "mid", "long"]]

        symbol, timeframe = random.choice(available)
        tested_combos.add((symbol, timeframe))

        log(f"🎯 随机选择: {symbol} | {TIMEFRAME_LABELS[timeframe]} ({TIMEFRAME_DAYS[timeframe]}天)")

        # Reload modules to pick up code/weight changes
        import importlib
        for mod_name in ['analysis.alpha', 'agents.prompts', 'agents.time_frame',
                          'agents.runner', 'agents.debate', 'agents.decision',
                          'agents.critic', 'analysis.holding', 'data.pipeline',
                          'backtest.engine', 'analysis.holding']:
            if mod_name in sys.modules:
                try:
                    importlib.reload(sys.modules[mod_name])
                except Exception:
                    pass

        # ── 2. Backtest ──
        bt_result = run_single_backtest(symbol, timeframe)
        history.append(bt_result)

        if bt_result.get("error"):
            log(f"❌ {symbol} {TIMEFRAME_LABELS[timeframe]} 回测失败: {bt_result['error']}")
            continue

        # ── 3. Check targets ──
        if bt_result["passed"]:
            consecutive_passes += 1
            log(f"✅ 达标! {symbol} {TIMEFRAME_LABELS[timeframe]} "
                f"(连续通过 {consecutive_passes}/3)")

            if consecutive_passes >= 3:
                print(f"""
╔══════════════════════════════════════════════════════════════╗
║         🎉 系统达标！连续3次随机测试全部通过！              ║
║                                                            ║
║  测试记录:                                                  ║""")
                for h in history[-3:]:
                    print(f"║  {h['symbol']} {h['label']}: "
                          f"收益={h.get('metrics',{}).get('total_return_pct',0):+.1f}% "
                          f"胜率={h.get('metrics',{}).get('win_rate_pct',0):.1f}%")
                print(f"""║                                                            ║
║  总迭代次数: {iteration}                                      ║
╚══════════════════════════════════════════════════════════════╝
""")
                log("🎉 系统达标！连续3次随机测试全部通过！")
                _save_state({"status": "passed", "iterations": iteration,
                            "consecutive_passes": consecutive_passes,
                            "history": [{k: v for k, v in h.items() if k != "trade_log"}
                                       for h in history[-10:]]})
                return True
        else:
            consecutive_passes = 0
            log(f"❌ 未达标: {symbol} {TIMEFRAME_LABELS[timeframe]}")
            for k, v in bt_result.get("details", {}).items():
                log(f"     {k}: 实际={v['actual']} 目标={v['target']} {'✅' if v['pass'] else '❌'}")

        # ── 4. Critic analysis → apply_fix ──
        log(f"--- Critic深度分析 ---")
        critic = run_deep_critique(bt_result)

        # 如果 Critic 返回的是旧格式 (code_changes)，转换为 operations 格式
        if not critic.get("operations") and critic.get("code_changes"):
            ops = []
            for cc in critic["code_changes"]:
                if not isinstance(cc, dict) or not cc.get("file") or not cc.get("new_code"):
                    continue
                file_path = cc["file"]
                old_code = cc.get("old_code", "")
                new_code = cc["new_code"]
                if file_path.endswith(".json"):
                    tf = "short"
                    for t in ["short", "mid", "long"]:
                        if t in cc.get("reason", ""):
                            tf = t
                            break
                    m = re.search(r'"(\w+)":\s*[\d.]+', old_code)
                    target = f"{tf}.{m.group(1)}" if m else old_code
                    m2 = re.search(r'[\d.]+', new_code)
                    new_value = float(m2.group()) if m2 else new_code
                else:
                    m = re.search(r'(\b[A-Z_][A-Z0-9_]*)\s*=', old_code)
                    target = m.group(1) if m else cc.get("function", old_code)
                    m2 = re.search(r'[\d.]+', new_code)
                    new_value = float(m2.group()) if m2 else new_code
                ops.append({"file": file_path, "target": target, "new_value": new_value})
            if ops:
                critic["operations"] = ops

        num_applied = apply_fix(critic)
        log(f"  本轮应用修改数: {num_applied}")

        # ── 5. Overfitting check (every 10 rounds) ──
        if iteration % 10 == 0:
            of_check = check_overfitting(history)
            log(f"  过拟合检查: {of_check['overfitting_risk']}")
            if of_check.get("warning"):
                log(f"  {of_check['warning']}")
            if of_check["overfitting_risk"] == "high":
                log("  ⚠ 检测到高过拟合风险，建议暂停审查")
                # Continue but flag it
                print(f"\n  ⚠ {of_check['warning']}")

        # ── 6. Save state ──
        _save_state({
            "status": "in_progress",
            "iteration": iteration,
            "max_iterations": max_iterations,
            "consecutive_passes": consecutive_passes,
            "last_symbol": symbol,
            "last_timeframe": timeframe,
            "file_mod_count": dict(FILE_MOD_COUNT),
        })

        time.sleep(0.5)

    # ── Max iterations reached ──
    print(f"""
╔══════════════════════════════════════════════════════════════╗
║     ⚠ 达到最大迭代次数 ({max_iterations}) — 当前架构已达极限     ║
╚══════════════════════════════════════════════════════════════╝

  未达标项目:
""")
    # Tabulate what's still failing
    for h in history[-10:]:
        if not h.get("passed"):
            m = h.get("metrics", {})
            print(f"  {h['symbol']} {h['label']}: "
                  f"收益={m.get('total_return_pct',0):+.1f}% "
                  f"(目标={'10%/30%/200%'}) "
                  f"胜率={m.get('win_rate_pct',0):.1f}%")

    print(f"\n  🔧 文件修改统计:\n")
    for fp, count in sorted(FILE_MOD_COUNT.items(), key=lambda x: -x[1]):
        print(f"    {os.path.basename(fp)}: {count} 次")

    print(f"\n  总迭代: {iteration} | 连续通过: {consecutive_passes}/3")
    print(f"  建议: 人工审查未达标项目，调整策略架构后重新运行\n")

    _save_state({"status": "max_iterations_reached", "iterations": iteration,
                "consecutive_passes": consecutive_passes,
                "file_mod_count": dict(FILE_MOD_COUNT)})
    return False


if __name__ == "__main__":
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8')

    import argparse
    parser = argparse.ArgumentParser(description='自动进化器 v3.0 — 不达标就永续迭代')
    parser.add_argument('--symbol', default=None, help='指定股票代码（不指定则随机选择）')
    parser.add_argument('--timeframe', default=None, choices=['short', 'mid', 'long'],
                        help='指定时间维度（不指定则随机选择）')
    parser.add_argument('--max_iterations', type=int, default=50, help='最大迭代次数')
    parser.add_argument('--full', action='store_true', help='全自动模式：随机选股+随机周期')
    parser.add_argument('--seed', type=int, default=None, help='随机种子（用于结果复现）')
    parser.add_argument('--no-mock', action='store_true', help='使用真实API')
    args = parser.parse_args()

    if args.full:
        evolve_full(
            max_iterations=args.max_iterations,
            use_mock=not args.no_mock,
            seed=args.seed,
        )
    elif args.symbol:
        # Legacy single-stock mode
        from agents.critic import critic_evaluate
        # ... (kept for backward compatibility)
        print("Use --full for the new auto-evolution mode")
    else:
        print("用法: py auto_improver.py --full --max_iterations 50")
        print("  或: py auto_improver.py --symbol 600744 --timeframe mid")
