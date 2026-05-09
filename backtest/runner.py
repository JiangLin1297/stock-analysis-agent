#!/usr/bin/env python3
"""
回测+Critic联动自我进化引擎。
循环: 回测 → Critic评审 → 自动修改 → 重新回测 → ... 直到评分达标或达到最大轮数。

用法:
    py backtest_runner.py --symbol 600744 --time_frame mid --days 120 --max_rounds 3
    py backtest_runner.py --symbol 600519 --start 2024-01-01 --end 2024-12-31 --max_rounds 5
"""

import sys
import os
import json
import time
from datetime import datetime
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    import certifi
    os.environ['SSL_CERT_FILE'] = certifi.where()
    os.environ['REQUESTS_CA_BUNDLE'] = certifi.where()
except Exception:
    pass

from backtest.engine import random_period_test, run_backtest, format_results
from agents.critic import critique_backtest
from evolution.improver import apply_fix, backup_file

EVOLUTION_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_evolution_log.txt")


def log(msg: str, also_print: bool = True):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f"[{ts}] {msg}"
    if also_print:
        print(line)
    with open(EVOLUTION_LOG, 'a', encoding='utf-8') as f:
        f.write(line + "\n")


def run_backtest_with_critic(symbol: str = "600744", time_frame: str = "mid",
                              days: int = 120, max_rounds: int = 3,
                              start_date: str = None, end_date: str = None,
                              initial_capital: float = 100000.0,
                              use_mock: bool = True) -> dict:
    """
    回测 + Critic联动进化循环。

    每轮:
      1. 回测（首轮随机选时段，后续用同时段+同seed确保可比）
      2. Critic分析回测结果
      3. 若评分达标或达到最大轮数 → 停止
      4. 应用 must_fix 修改指令
      5. 重新回测

    Returns:
        {"symbol", "period", "rounds": [...], "final_score", "improvement"}
    """
    print(f"""
╔══════════════════════════════════════════════════════════╗
║     🔬 回测+Critic 联动自我进化引擎                      ║
║     回测 → 评审 → 修改 → 再回测                          ║
║                                                         ║
║     标的: {symbol:<20}              ║
║     维度: {time_frame:<20}              ║
║     最大轮数: {max_rounds}                                       ║
╚══════════════════════════════════════════════════════════╝
""")

    with open(EVOLUTION_LOG, 'a', encoding='utf-8') as f:
        f.write(f"\n{'='*70}\n")
        f.write(f"回测进化启动: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"标的: {symbol} | 维度: {time_frame} | 最大轮数: {max_rounds}\n")
        f.write(f"{'='*70}\n\n")

    project_dir = os.path.dirname(os.path.abspath(__file__))
    round_results = []
    score_history = []

    # 确定回测区间（首轮确定后所有轮使用相同区间）
    if start_date and end_date:
        period = {"symbol": symbol, "start_date": start_date,
                  "end_date": end_date, "days": days,
                  "time_frame": time_frame, "seed": 42}
    else:
        seed = 42
        period = random_period_test(symbol, time_frame, days, seed=seed)
        print(f"\n  随机时段: {period['start_date']} → {period['end_date']} (seed={seed})\n")

    for round_num in range(1, max_rounds + 1):
        print(f"\n{'#'*70}")
        print(f"#  第 {round_num}/{max_rounds} 轮: 回测 → Critic → 改进")
        print(f"{'#'*70}\n")

        # 强制重载模块（可能被上一轮修改过）
        import importlib
        for mod_name in ['backtest_engine', 'critic_agent', 'auto_improver',
                          'time_frame_runner', 'decision_engine', 'agent_prompts']:
            if mod_name in sys.modules:
                try:
                    importlib.reload(sys.modules[mod_name])
                except Exception:
                    pass

        # 重新导入（从重载后的模块获取最新版本）
        from backtest.engine import run_backtest as _run_backtest
        from agents.critic import critique_backtest as _critique_backtest

        # ═══ 步骤1: 回测 ═══
        log(f"--- 第{round_num}轮: 回测 ---")
        print(f"  [1/3] 运行回测...")

        try:
            bt_result = _run_backtest(symbol, period['start_date'],
                                       period['end_date'], initial_capital)
        except Exception as e:
            import traceback
            log(f"❌ 回测失败: {e}")
            log(traceback.format_exc())
            print(f"  ❌ 回测失败: {e}")
            break

        print(format_results(bt_result))
        log(f"回测完成: 收益{bt_result['metrics']['total_return_pct']:+.2f}% "
            f"夏普{bt_result['metrics']['sharpe_ratio']:.2f} "
            f"胜率{bt_result['metrics']['win_rate_pct']:.1f}%")

        # ═══ 步骤2: Critic 分析 ═══
        log(f"--- 第{round_num}轮: Critic评审 ---")
        print(f"  [2/3] Critic分析回测结果...")

        try:
            critic_result = _critique_backtest(bt_result, use_mock=use_mock)
        except Exception as e:
            import traceback
            log(f"❌ Critic分析失败: {e}")
            log(traceback.format_exc())
            print(f"  ❌ Critic分析失败: {e}")
            break

        score = critic_result.get("overall_score", 0)
        score_history.append(score)

        # Deep dive亏损交易（仅中长线、第2轮起）
        deep_dive_fixes = []
        if time_frame in ("mid", "long") and round_num >= 1:
            print(f"  [2b/3] 亏损交易深度剖析 ({time_frame}线)...")
            try:
                from agents.critic import deep_dive_losing_trades
                tf_dive = "mid" if time_frame == "mid" else "long"
                dive_result = deep_dive_losing_trades(
                    bt_result.get("trade_log", []), time_frame=tf_dive, use_mock=use_mock
                )
                deep_dive_fixes = dive_result.get("fix_suggestions", [])
                if deep_dive_fixes:
                    log(f"DeepDive发现{len(deep_dive_fixes)}条修改建议")
            except Exception as e:
                log(f"DeepDive分析失败: {e}")

        # 合并Critic和DeepDive的修改建议
        must_fix_all = critic_result.get("must_fix", []) + deep_dive_fixes

        round_info = {
            "round": round_num,
            "backtest_metrics": bt_result["metrics"],
            "critic_score": score,
            "main_issue": critic_result.get("main_issue", ""),
            "must_fix": must_fix_all,
            "fixes_applied": [],
            "deep_dive_patterns": deep_dive_fixes,
        }
        round_results.append(round_info)

        log(f"Critic评分: {score}/10 | 问题: {critic_result.get('main_issue', '')[:80]}")

        # ═══ 步骤3: 判断是否继续 ═══
        if score >= 8:
            log(f"🎯 回测评分{score}/10已达优秀，进化完成!")
            print(f"\n  🎯 回测评分{score}/10已达优秀标准，无需继续改进。")
            break

        if round_num >= max_rounds:
            log(f"已达最大轮数{max_rounds}，进化结束")
            print(f"\n  ⏰ 已达最大轮数{max_rounds}，进化结束。")
            break

        # ═══ 步骤4: 应用修改 ═══
        must_fix = must_fix_all
        if not must_fix:
            log("无修改指令，进化结束")
            print("\n  无修改指令，进化结束")
            break

        print(f"\n  [3/3] 应用修改指令 ({len(must_fix[:3])}条)...")
        log(f"应用修改指令: {must_fix[:3]}")

        for i, fix in enumerate(must_fix[:3]):
            print(f"    🔧 [{i+1}] {fix[:80]}...")
            success = apply_fix(fix, project_dir)
            if success:
                round_info["fixes_applied"].append(fix)
                log(f"  ✅ 已应用: {fix[:60]}")
            else:
                log(f"  ⚠ 未能应用: {fix[:60]}")

        if not round_info["fixes_applied"]:
            log("本轮无有效修改，进化结束")
            print("\n  ⚠ 本轮无有效修改，进化结束")
            break

        # 等待文件系统同步
        time.sleep(0.5)

    # ═══ 进化摘要 ═══
    print(f"""
╔══════════════════════════════════════════════════════════╗
║           🔬 回测进化摘要报告                             ║
╚══════════════════════════════════════════════════════════╝
""")
    print(f"  标的: {symbol} | 维度: {time_frame}")
    print(f"  区间: {period['start_date']} → {period['end_date']}")
    print(f"  总轮数: {len(round_results)}")
    print()

    for r in round_results:
        m = r["backtest_metrics"]
        print(f"  ┌─ 第{r['round']}轮 ─────────────────────────────────────┐")
        print(f"  │ 回测收益: {m['total_return_pct']:>+8.2f}%  夏普: {m['sharpe_ratio']:>6.2f}  胜率: {m['win_rate_pct']:>5.1f}%  │")
        print(f"  │ Critic评分: {r['critic_score']}/10                             │")
        print(f"  │ 主要问题: {r['main_issue'][:50]:<50} │")
        print(f"  │ 应用修改: {len(r['fixes_applied'])}条                              │")
        print(f"  └──────────────────────────────────────────┘")

    if len(score_history) >= 2:
        improvement = score_history[-1] - score_history[0]
        arrow = "📈" if improvement > 0 else ("📉" if improvement < 0 else "➡️")
        print(f"\n  评分变化: {score_history[0]} → {score_history[-1]} {arrow} ({improvement:+d})")
    else:
        improvement = 0

    print()

    return {
        "symbol": symbol,
        "period": period,
        "rounds": round_results,
        "final_score": score_history[-1] if score_history else 0,
        "score_history": score_history,
        "improvement": improvement if len(score_history) >= 2 else 0,
    }


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8')

    import argparse
    parser = argparse.ArgumentParser(
        description='回测+Critic联动自我进化引擎')
    parser.add_argument('--symbol', default='600744', help='股票代码')
    parser.add_argument('--time_frame', default='mid',
                        choices=['short', 'mid', 'long'],
                        help='时间维度')
    parser.add_argument('--days', type=int, default=120,
                        help='回测天数（随机模式）')
    parser.add_argument('--start', default=None, help='起始日期 YYYY-MM-DD')
    parser.add_argument('--end', default=None, help='结束日期 YYYY-MM-DD')
    parser.add_argument('--max_rounds', type=int, default=3,
                        help='最大进化轮数')
    parser.add_argument('--capital', type=float, default=100000,
                        help='初始资金')
    parser.add_argument('--no-mock', action='store_true',
                        help='使用真实API（非Mock）')
    args = parser.parse_args()

    run_backtest_with_critic(
        symbol=args.symbol,
        time_frame=args.time_frame,
        days=args.days,
        max_rounds=args.max_rounds,
        start_date=args.start,
        end_date=args.end,
        initial_capital=args.capital,
        use_mock=not args.no_mock,
    )
