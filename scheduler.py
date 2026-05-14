"""
StockMind 定时调度器 — 每日收盘后自动触发实盘 Critic 优化。
独立进程运行: python scheduler.py
也可被 web_ui.py 导入，在后台线程中运行。
"""

import os
import sys
import json
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ═══════════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════════
CRON_HOUR = 15
CRON_MINUTE = 30
CHECK_INTERVAL = 60  # 每 60 秒检查一次

AUTO_EVOLUTION_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "portfolio", "auto_evolution.json"
)


def _log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}")


def is_auto_evolution_enabled() -> bool:
    """读取自动进化开关状态。"""
    if not os.path.exists(AUTO_EVOLUTION_FILE):
        return True  # 默认开启
    try:
        with open(AUTO_EVOLUTION_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data.get("enabled", True)
    except Exception:
        return True


def set_auto_evolution(enabled: bool):
    """设置自动进化开关。"""
    os.makedirs(os.path.dirname(AUTO_EVOLUTION_FILE), exist_ok=True)
    data = {"enabled": enabled, "updated": datetime.now().isoformat()}
    with open(AUTO_EVOLUTION_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _has_new_trades_today() -> bool:
    """检查今日是否有新交易记录。"""
    trades_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "portfolio", "real_trades.json"
    )
    if not os.path.exists(trades_path):
        return False
    try:
        with open(trades_path, 'r', encoding='utf-8') as f:
            trades = json.load(f)
        today = datetime.now().strftime("%Y-%m-%d")
        return any(t.get("date") == today for t in trades)
    except Exception:
        return False


def run_daily_critic():
    """执行每日实盘 Critic 优化。"""
    if not is_auto_evolution_enabled():
        _log("自动进化已关闭，跳过")
        return

    if not _has_new_trades_today():
        _log("今日无新交易记录，跳过 Critic 优化")
        return

    _log("触发每日实盘 Critic 优化...")
    try:
        from agents.real_critic import trigger_real_critic
        result = trigger_real_critic()
        score = result.get("critic_result", {}).get("overall_score", "?")
        ops = result.get("operations_applied", 0)
        _log(f"每日 Critic 完成: 评分={score} 应用{ops}条修改")
    except Exception as e:
        _log(f"每日 Critic 执行失败: {e}")


def run_scheduler():
    """主调度循环。每日 CRON_HOUR:CRON_MINUTE 触发一次。"""
    _log(f"调度器启动 — 每日 {CRON_HOUR}:{CRON_MINUTE:02d} 触发实盘 Critic")
    _log(f"自动进化: {'开启' if is_auto_evolution_enabled() else '关闭'}")

    today_triggered = False

    while True:
        now = datetime.now()
        current_date = now.strftime("%Y-%m-%d")

        # 每天重置触发标志
        if now.hour == 0 and now.minute == 0:
            today_triggered = False

        # 到达触发时间且今日未触发
        if (now.hour == CRON_HOUR and now.minute >= CRON_MINUTE
                and not today_triggered):
            _log(f"到达 {CRON_HOUR}:{CRON_MINUTE:02d}，执行每日优化")
            run_daily_critic()
            today_triggered = True

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    run_scheduler()
