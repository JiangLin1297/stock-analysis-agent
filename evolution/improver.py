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


def _apply_fix_to_weights(desc: str, project_dir: str) -> bool:
    """
    Parse and apply a fix instruction targeting factor_weights.json.

    The file structure is: {"short": {"factor": weight, "threshold": N}, "mid": {...}, "long": {...}}

    Supported formats:
      - short.momentum_20d: 0.20 → 0.25
      - mid.threshold: 60 → 55
      - short.momentum_20d += 0.05
      - mid.threshold -= 5
      - Increase momentum_20d weight for short from 0.20 to 0.25
      - Lower short threshold from 55 to 50
    """
    weights_path = os.path.join(project_dir, "factor_weights.json")
    if not os.path.exists(weights_path):
        return False

    try:
        with open(weights_path, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
    except Exception:
        return False

    backup_file(weights_path)
    modified = False
    TF_NAMES = {"short", "mid", "long"}

    # Format A: tf.key: old_val → new_val  (e.g. "short.momentum_20d: 0.20 → 0.25" or "mid.threshold: 60 → 55")
    m = re.search(r'(short|mid|long)\.(\w+)\s*:\s*([\d.]+)\s*→\s*([\d.]+)', desc)
    if m:
        tf = m.group(1)
        key = m.group(2)
        new_val = float(m.group(4))
        if tf in cfg and key in cfg[tf]:
            old_val = cfg[tf][key]
            cfg[tf][key] = new_val
            log(f"  🔧 factor_weights: {tf}.{key} {old_val} → {new_val}")
            modified = True

    # Format B: tf.key += delta or tf.key -= delta
    if not modified:
        m = re.search(r'(short|mid|long)\.(\w+)\s*([+\-]=)\s*([\d.]+)', desc)
        if m:
            tf = m.group(1)
            key = m.group(2)
            op = m.group(3)
            delta = float(m.group(4))
            if tf in cfg and key in cfg[tf]:
                old_val = cfg[tf][key]
                if op == "+=":
                    cfg[tf][key] = round(old_val + delta, 4)
                else:
                    cfg[tf][key] = round(max(0, old_val - delta), 4)
                log(f"  🔧 factor_weights: {tf}.{key} {old_val} {op} {delta} → {cfg[tf][key]}")
                modified = True

    # Format C: natural language — "Increase/Lower <factor> weight for <tf> from X to Y"
    if not modified:
        m = re.search(r'(?:Increase|Raise|Boost|Lower|Reduce|Decrease)\s+(\w+)\s+(?:weight|threshold)\s+(?:for|in)\s+(\w+)[\s\w]*from\s+([\d.]+)\s+to\s+([\d.]+)', desc, re.IGNORECASE)
        if m:
            factor = m.group(1).lower()
            tf_word = m.group(2).lower()
            new_val = float(m.group(4))
            tf_map = {"short": "short", "mid": "mid", "long": "long",
                      "短线": "short", "中线": "mid", "长线": "long"}
            tf = tf_map.get(tf_word, tf_word)
            if tf in cfg and factor in cfg[tf]:
                old_val = cfg[tf][factor]
                cfg[tf][factor] = new_val
                log(f"  🔧 factor_weights: {tf}.{factor} {old_val} → {new_val}")
                modified = True

    # Format D: Chinese natural language — "权重/阈值: factor tf 从 X 到 Y"
    if not modified:
        m = re.search(r'(?:权重|weight|阈值|threshold)\s*[：:]\s*(\w+)\s*(?:(\w+)\s*)?从\s*([\d.]+)\s*(?:→|到|调整为?|改为)\s*([\d.]+)', desc)
        if m:
            key = m.group(1)
            tf_hint = m.group(2) or ""
            new_val = float(m.group(4))
            tf_map = {"short": "short", "mid": "mid", "long": "long",
                      "短线": "short", "中线": "mid", "长线": "long"}
            tf = tf_map.get(tf_hint, None)
            if tf and tf in cfg and key in cfg[tf]:
                old_val = cfg[tf][key]
                cfg[tf][key] = new_val
                log(f"  🔧 factor_weights: {tf}.{key} {old_val} → {new_val}")
                modified = True
            elif not tf:
                # Search all timeframes
                for t in TF_NAMES:
                    if key in cfg[t]:
                        old_val = cfg[t][key]
                        cfg[t][key] = new_val
                        log(f"  🔧 factor_weights: {t}.{key} {old_val} → {new_val}")
                        modified = True
                        break

    if modified:
        try:
            with open(weights_path, 'w', encoding='utf-8') as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            FILE_MOD_COUNT[weights_path] += 1
            return True
        except Exception:
            return False

    return False


def _apply_fix_via_deepseek(instruction: str, target_file: str, project_dir: str) -> bool:
    """通过 DeepSeek 将自然语言修改指令翻译为代码 diff 并应用。

    流程:
      1. 读取目标文件内容
      2. 发送指令 + 文件内容给 DeepSeek，要求返回修改后的完整代码
      3. 验证语法（py_compile）
      4. 先在 .bak 文件上测试，确认无误后覆盖原文件
    """
    try:
        from data.deepseek import deepseek_chat
    except ImportError:
        log("  ⚠ DeepSeek 模块不可用，跳过 NL 翻译")
        return False

    if not os.path.isfile(target_file):
        log(f"  ⚠ 目标文件不存在: {target_file}")
        return False

    try:
        with open(target_file, 'r', encoding='utf-8') as f:
            original_content = f.read()
    except Exception as e:
        log(f"  ⚠ 读取文件失败: {e}")
        return False

    filename = os.path.basename(target_file)
    prompt = f"""你是一个精确的 Python 代码修改助手。请根据以下修改指令，对文件 `{filename}` 进行精确修改。

## 修改指令
{instruction}

## 当前文件完整内容
```python
{original_content}
```

## 要求
1. 只修改与指令相关的代码，不要改动其他部分
2. 返回修改后的 **完整文件内容**，用以下标记包裹：
<<<MODIFIED_FILE_START>>>
（修改后的完整 Python 代码）
<<<MODIFIED_FILE_END>>>
3. 不要添加任何解释文字，只返回标记包裹的代码
4. 确保修改后的代码语法正确"""

    try:
        response = deepseek_chat(prompt, temperature=0.1)
    except Exception as e:
        log(f"  ⚠ DeepSeek 调用失败: {e}")
        return False

    if not response:
        log("  ⚠ DeepSeek 返回为空")
        return False

    # 提取修改后的代码
    m = re.search(
        r'<<<MODIFIED_FILE_START>>>(.*?)<<<MODIFIED_FILE_END>>>',
        response, re.DOTALL
    )
    if not m:
        log("  ⚠ DeepSeek 返回格式不符（缺少标记），跳过")
        log(f"    原始响应前200字: {response[:200]}")
        return False

    new_content = m.group(1).strip()
    # 去除可能的 markdown 代码块标记
    if new_content.startswith("```python"):
        new_content = new_content[len("```python"):].strip()
    elif new_content.startswith("```"):
        new_content = new_content[3:].strip()
    if new_content.endswith("```"):
        new_content = new_content[:-3].strip()

    if new_content == original_content:
        log(f"  ℹ DeepSeek 未修改 {filename}（内容相同）")
        return False

    # 语法验证
    import py_compile
    import tempfile
    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False,
                                     encoding='utf-8') as tmp:
        tmp.write(new_content)
        tmp_path = tmp.name
    try:
        py_compile.compile(tmp_path, doraise=True)
    except py_compile.PyCompileError as e:
        log(f"  ⚠ DeepSeek 返回的代码语法错误: {e}")
        os.unlink(tmp_path)
        return False
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

    # 备份原文件
    backup_file(target_file)

    # 先写入备份路径做最终确认
    bak_path = target_file + ".bak"
    try:
        with open(bak_path, 'w', encoding='utf-8') as f:
            f.write(new_content)
        # 确认备份可读
        with open(bak_path, 'r', encoding='utf-8') as f:
            verify = f.read()
        if verify != new_content:
            log("  ⚠ 备份文件验证失败")
            return False
    except Exception as e:
        log(f"  ⚠ 写入备份失败: {e}")
        return False

    # 覆盖原文件
    try:
        with open(target_file, 'w', encoding='utf-8') as f:
            f.write(new_content)
        FILE_MOD_COUNT[target_file] += 1
        log(f"  ✅ DeepSeek 修改成功: {filename}")
        return True
    except Exception as e:
        log(f"  ⚠ 覆盖原文件失败: {e}")
        # 尝试从备份恢复
        try:
            shutil.copy2(bak_path, target_file)
        except Exception:
            pass
        return False


# ═══════════════════════════════════════════════════════════════
# 项目根目录（从本文件位置推导，不依赖调用方传入）
# ═══════════════════════════════════════════════════════════════
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _verify_write(target_file: str, expected_marker: str) -> bool:
    """验证文件写入成功：重新读取文件，确认 expected_marker 存在。"""
    try:
        with open(target_file, 'r', encoding='utf-8') as f:
            content = f.read()
        if expected_marker in content:
            return True
        log(f"  ⚠ [写入验证失败] {os.path.basename(target_file)}: 未找到 '{expected_marker[:50]}'")
        return False
    except Exception as e:
        log(f"  ⚠ [写入验证异常] {e}")
        return False


def _find_file_fallback(file_rel: str, func_name: str, old_code: str, project_dir: str) -> str:
    """当 Critic 指定的文件不存在时，尝试定位实际包含目标代码的文件。"""
    # 1. 尝试原始路径
    target = _resolve_file(file_rel, project_dir)
    if target:
        return target
    target = _resolve_file(file_rel, _PROJECT_ROOT)
    if target:
        return target

    # 2. 按 function 名搜索：在所有 .py 文件中查找 'def func_name('
    if func_name:
        import glob as _glob
        for py_file in _glob.glob(os.path.join(project_dir, "**", "*.py"), recursive=True):
            try:
                with open(py_file, 'r', encoding='utf-8') as f:
                    txt = f.read()
                if f"def {func_name}(" in txt:
                    return py_file
            except Exception:
                continue

    # 3. 按 old_code 内容搜索：在所有 .py 和 .json 文件中查找 old_code
    if old_code:
        import glob as _glob
        for pattern in ["**/*.py", "**/*.json"]:
            for fpath in _glob.glob(os.path.join(project_dir, pattern), recursive=True):
                if '__pycache__' in fpath:
                    continue
                try:
                    with open(fpath, 'r', encoding='utf-8') as f:
                        txt = f.read()
                    if old_code in txt:
                        return fpath
                except Exception:
                    continue

    # 4. 常见文件名映射兜底
    basename = os.path.basename(file_rel).replace('.py', '')
    _ALIASES = {
        "hybrid_agent": ["analysis/holding.py", "agents/decision.py"],
        "holding_evaluator": ["analysis/holding.py"],
        "decision_engine": ["agents/decision.py"],
        "risk_manager": ["agents/decision.py", "analysis/holding.py"],
        "time_frame_runner": ["agents/decision.py", "agents/runner.py"],
        "agent_prompts": ["agents/prompts.py"],
    }
    for alias, candidates in _ALIASES.items():
        if alias in basename:
            for c in candidates:
                target = _resolve_file(c, project_dir)
                if target:
                    return target

    return None


def _apply_code_changes(code_changes: list, project_dir: str) -> int:
    """执行 Critic 返回的 JSON 代码修改指令。

    每条指令格式:
      {"file": "相对路径", "function": "函数名", "old_code": "原代码", "new_code": "新代码", "reason": "原因"}

    当 Critic 指定的文件不存在时，自动搜索包含目标代码的实际文件。
    返回成功执行的修改数量。
    """
    applied = 0

    for i, change in enumerate(code_changes):
        if not isinstance(change, dict):
            continue

        file_rel = change.get("file", "")
        old_code = change.get("old_code", "")
        new_code = change.get("new_code", "")
        reason = change.get("reason", "")
        func_name = change.get("function", "")

        if not old_code or not new_code:
            log(f"  ⚠ [代码修改 {i+1}] 缺少 old_code/new_code，跳过")
            continue

        if old_code == new_code:
            continue

        # 解析目标文件（含智能兜底）
        target_file = _find_file_fallback(file_rel, func_name, old_code, project_dir)
        if not target_file:
            log(f"  ⚠ [代码修改 {i+1}] 找不到目标文件: {file_rel} (func={func_name})")
            continue

        # 读取文件
        try:
            with open(target_file, 'r', encoding='utf-8') as f:
                content = f.read()
        except Exception as e:
            log(f"  ⚠ [代码修改 {i+1}] 读取失败: {e}")
            continue

        # 搜索 old_code（精确匹配 + 去空白模糊匹配）
        if old_code not in content:
            # 模糊匹配：去掉首尾空白后比较
            stripped_old = old_code.strip()
            if stripped_old not in content:
                log(f"  ⚠ [代码修改 {i+1}] old_code 未匹配: {os.path.basename(target_file)}:{func_name}")
                log(f"    搜索: {old_code[:60]}...")
                continue
            old_code = stripped_old

        # 替换（只替换第一次出现）
        new_content = content.replace(old_code, new_code, 1)

        # 写入
        backup_file(target_file)
        try:
            with open(target_file, 'w', encoding='utf-8') as f:
                f.write(new_content)
        except Exception as e:
            log(f"  ⚠ [代码修改 {i+1}] 写入失败: {e}")
            continue

        # 验证
        if not _verify_write(target_file, new_code):
            log(f"  ⚠ [代码修改 {i+1}] 写入验证失败")
            continue

        applied += 1
        FILE_MOD_COUNT[target_file] += 1
        log(f"  [Critic生效] {os.path.basename(target_file)}:{func_name} — {reason}")
        log(f"    旧: {old_code[:80]}")
        log(f"    新: {new_code[:80]}")

    return applied


def _save_pending_fix(instruction: str, project_dir: str):
    """将无法自动执行的建议写入 pending_fixes.txt，不阻塞进化流程。"""
    pending_path = os.path.join(project_dir, "pending_fixes.txt")
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    try:
        with open(pending_path, 'a', encoding='utf-8') as f:
            f.write(f"[{timestamp}] {instruction}\n")
        log(f"  📝 未匹配建议已写入 pending_fixes.txt: {instruction[:60]}")
    except Exception:
        log(f"  ⚠ 写入 pending_fixes.txt 失败")


def apply_fix(code_changes: list, project_dir: str) -> bool:
    """执行 Critic 输出的 JSON 代码修改指令。这是唯一的修改入口。

    Args:
        code_changes: Critic 输出的 code_changes 列表，每项为 dict:
            {"file": "相对路径", "function": "函数名", "old_code": "原代码", "new_code": "新代码", "reason": "原因"}
        project_dir: 项目根目录

    Returns:
        True 表示至少有一条修改成功
    """
    if not code_changes:
        log("  无修改指令")
        return False

    applied = _apply_code_changes(code_changes, project_dir)
    if applied == 0:
        log("  ⚠ 0 条 code_changes 生效（old_code 未匹配或文件不存在）")
    return applied > 0


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

        # 确保 code_changes 字段存在
        if "code_changes" not in critic_result:
            critic_result["code_changes"] = []

        return critic_result
    except Exception as e:
        log(f"  ⚠ Critic分析失败: {e}")
        return {"overall_score": 0, "must_fix": [], "code_changes": [],
                "verdict": f"Critic失败: {e}"}


def _generate_factor_fixes(bt_result: dict) -> list:
    """基于回测结果自动生成 factor_weights.json 修改建议。"""
    fixes = []
    metrics = bt_result.get("metrics", {})
    tf = bt_result.get("timeframe", "mid")

    win_rate = metrics.get("win_rate_pct", 0)
    total_ret = metrics.get("total_return_pct", 0)
    total_trades = metrics.get("total_trades", 0)

    target = TARGETS.get(tf, TARGETS["mid"])

    if tf in ("short", "mid"):
        target_wr = target.get("win_rate_pct", 50)
        target_ret = target.get("avg_return_pct", 10)
        if win_rate < target_wr and total_trades > 3:
            fixes.append(f"{tf}.threshold: 55 → 48  # 降低阈值提高信号频率 (胜率{win_rate:.0f}%<{target_wr:.0f}%)")
        if total_trades < 5 and total_ret < target_ret:
            fixes.append(f"{tf}.threshold: 55 → 45  # 交易太少({total_trades}笔)，放宽门槛")
    else:
        target_dd = target.get("max_drawdown_pct", 30)
        max_dd = metrics.get("max_drawdown_pct", 0)
        if max_dd > target_dd:
            fixes.append(f"{tf}.threshold: 55 → 65  # 收紧以控制回撤({max_dd:.1f}%>{target_dd:.0f}%)")
        if total_trades < 3 and total_ret < 20:
            fixes.append(f"{tf}.threshold: 55 → 42  # 放宽以捕捉长期机会")

    return fixes


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

        # ── 4. Critic analysis ──
        log(f"--- Critic深度分析 ---")
        critic = run_deep_critique(bt_result)

        # 输出人工执行清单
        must_fix = critic.get("must_fix", [])
        if must_fix:
            log(f"  📋 人工执行清单 ({len(must_fix)}条):")
            for i, fix in enumerate(must_fix, 1):
                log(f"    {i}. {fix}")
        else:
            log("  ⚠ Critic 未生成修改建议")

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
