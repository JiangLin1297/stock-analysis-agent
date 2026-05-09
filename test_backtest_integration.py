#!/usr/bin/env python3
"""Integration test: backtest + Critic + improvement loop."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

from backtest_runner import run_backtest_with_critic

result = run_backtest_with_critic(
    symbol="600744",
    time_frame="mid",
    days=120,
    max_rounds=3,
    initial_capital=100000.0,
    use_mock=True,
)

print("\n" + "=" * 70)
print("  INTEGRATION TEST COMPLETE")
print(f"  Final Score: {result['final_score']}/10")
print(f"  Score History: {result['score_history']}")
print(f"  Improvement: {result['improvement']:+d}")
print(f"  Total Rounds: {len(result['rounds'])}")
for r in result['rounds']:
    m = r['backtest_metrics']
    print(f"  Round {r['round']}: Return={m['total_return_pct']:+.2f}% Sharpe={m['sharpe_ratio']:.2f} WinRate={m['win_rate_pct']:.1f}% CriticScore={r['critic_score']}/10")
print("=" * 70)
