<p align="center">
  <img src="stock.ico" width="80" alt="StockMind Logo" />
</p>

<h1 align="center">StockMind</h1>

<p align="center">
  <strong>多 Agent 深度股票分析系统</strong><br>
  趋势感知 · 自适应策略 · 全周期回测 · 桌面交互
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.9+-blue.svg" alt="Python" />
  <img src="https://img.shields.io/badge/GUI-PySide6-green.svg" alt="PySide6" />
  <img src="https://img.shields.io/badge/LLM-DeepSeek-orange.svg" alt="DeepSeek" />
  <img src="https://img.shields.io/badge/license-MIT-lightgrey.svg" alt="License" />
</p>

---

## 系统简介

StockMind 是一个基于多 Agent 协作的智能股票分析系统。它模拟专业投资团队的工作方式，由多个 AI 分析师并行工作、交叉辩论，最终融合生成交易决策。系统集成了桌面客户端、邮件指令通道和自进化策略引擎。

### 核心理念

> 不是单一个 AI 看盘，而是一整个投资委员会在为你工作。

---

## 架构总览

```
┌─────────────────────────────────────────────────────┐
│                  StockMind 桌面客户端                  │
│              (PySide6 · 深色/亮色主题)                 │
└─────────────────────┬───────────────────────────────┘
                      │
    ┌─────────────────┼─────────────────┐
    ▼                 ▼                  ▼
┌───────┐  ┌──────────────┐  ┌────────────────┐
│ 数据管道 │  │  多 Agent 引擎  │  │   邮件指令通道    │
│ pipeline │  │              │  │  (IMAP/SMTP)   │
└───────┘  └──────┬───────┘  └────────────────┘
                  │
    ┌─────────────┼─────────────┐
    ▼             ▼             ▼
┌──────┐   ┌──────────┐   ┌──────────┐
│技术分析│   │ 基本面分析  │   │ 新闻情绪  │
│ Agent │   │  Agent   │   │  Agent   │
└──┬───┘   └────┬─────┘   └────┬─────┘
   │            │              │
   └────────────┼──────────────┘
                ▼
       ┌───────────────┐
       │   辩论引擎      │
       │  (多方交叉质证)  │
       └───────┬───────┘
               ▼
       ┌───────────────┐
       │   决策引擎      │
       │  (融合输出信号)  │
       └───────┬───────┘
               ▼
       ┌───────────────┐
       │   Critic 诊断  │
       │  (深度回溯评估) │
       └───────┬───────┘
               ▼
       ┌───────────────┐
       │  自进化引擎     │
       │  (策略基因进化)  │
       └───────────────┘
```

---

## 核心模块

| 模块 | 文件 | 职责 |
|------|------|------|
| 桌面客户端 | `desktop_app.py` | PySide6 桌面应用，系统托盘，实时流输出 |
| 数据管道 | `data_pipeline.py` | 行情拉取、技术指标计算、财务数据获取 |
| Agent 运行器 | `agent_runner.py` | 调度 4 个 LLM 分析 Agent 并行工作 |
| Prompt 库 | `agent_prompts.py` | 所有 Agent 的系统提示词定义 |
| 决策引擎 | `decision_engine.py` | 融合多维度分析，输出最终交易信号 |
| 辩论引擎 | `debate_engine.py` | 多 Agent 交叉质证，消除群体盲点 |
| Critic 诊断 | `critic_agent.py` | 深度回溯评估，生成改进路线图 |
| 自进化引擎 | `auto_improver.py` | 策略基因提取、跨股迁移、参数自适应 |
| 回测引擎 | `backtest_engine.py` | 全周期回测(短/中/长线)，夏普比率计算 |
| 选股器 | `stock_screener.py` | 多因子量化选股，alpha 因子库 |
| 持仓评估 | `holding_evaluator.py` | 实时持仓分析，退出策略判断 |
| 邮件通道 | `mail_receiver.py` / `email_sender.py` | IMAP 指令接收 + SMTP 报告发送 |
| 主题系统 | `ui_theme.py` | 深色/亮色双主题 QSS |

---

## 分析 Agent 团队

| Agent | 角色 | 分析维度 |
|-------|------|---------|
| 技术分析师 | 图表技术派 | MA / MACD / RSI / KDJ / 布林带 / 量价关系 |
| 基本面分析师 | 价值投资派 | PE / PB / ROE / 财报 / 行业对比 |
| 新闻情绪师 | 市场情绪派 | 舆情分析、公告解读、板块联动 |
| 量化策略师 | 数据驱动派 | 因子暴露、波动率建模、统计套利 |

---

## 自进化系统

系统具备策略自进化能力，核心流程：

```
基准股票训练 → 提取策略基因(DNA) → 跨股票自适应迁移 → Critic 评估 → 进化报告
```

### 进化能力

- **趋势感知**: 自动识别 BULL / BEAR / SIDEWAYS 市场状态，动态调整信号强度
- **ATR 动态止损**: 根据标的波动率自适应收紧/放宽止损倍数
- **策略基因库**: 每只新股票自动提取特征、生成参数文件，积累形成知识图谱
- **跨股迁移**: 从训练标的中提取策略 DNA，适配到新股票上

---

## 快速开始

### 环境要求

- Python 3.9+
- DeepSeek API Key

### 安装依赖

```bash
pip install -r requirements.txt
```

### 配置

在桌面应用"设置"页面中配置 API Key 和邮箱参数，或设置环境变量：

```bash
set DEEPSEEK_API_KEY=sk-xxxxxxxx
```

### 启动

```bash
# 直接运行桌面应用
python desktop_app.py

# 或使用启动脚本
run.bat
```

### 命令行分析

```bash
# 分析单只股票
python data_pipeline.py 600519 | python agent_runner.py -

# 运行回测
python backtest_runner.py --symbol 000001 --days 180

# 启动邮件指令监听
python mail_receiver.py --listen
```

---

## 项目结构

```
stock-analysis/
├── desktop_app.py          # 桌面主程序 (PySide6)
├── agent_runner.py         # Agent 调度器
├── agent_prompts.py        # LLM Prompt 定义
├── decision_engine.py      # 最终决策引擎
├── debate_engine.py        # 多Agent辩论引擎
├── critic_agent.py         # 深度诊断与评估
├── auto_improver.py        # 自进化引擎
├── backtest_engine.py      # 回测引擎
├── backtest_runner.py      # 回测运行器
├── stock_screener.py       # 量化选股器
├── alpha_factors.py        # Alpha 因子库
├── holding_evaluator.py    # 持仓评估
├── data_pipeline.py        # 数据管道
├── deepseek_client.py      # LLM API 客户端
├── mail_receiver.py        # 邮件指令接收
├── email_sender.py         # 邮件发送
├── config_manager.py       # 配置管理
├── portfolio_manager.py    # 组合管理
├── exit_strategy.py        # 退出策略
├── format_utils.py         # 终端格式化输出
├── ui_theme.py             # UI 主题定义
├── stock_adapter.py        # 股票数据适配器
├── time_frame_runner.py    # 多周期运行器
├── build_exe.py            # PyInstaller 打包脚本
├── run.bat                 # Windows 启动脚本
├── stock.ico / app_icon.ico # 应用图标
└── test_backtest_integration.py  # 回测集成测试
```

---

## 进化报告

查看 [EVOLUTION_REPORT.md](EVOLUTION_REPORT.md) 了解系统从华银电力调试版到全市场自适应策略引擎的完整进化历程。

---

## 免责声明

**StockMind 仅用于学习和研究目的。** 所有分析结果和交易信号均为 AI 模型生成，不构成任何投资建议。股市有风险，投资需谨慎。使用者应自行承担交易风险。

---

<p align="center">
  <sub>Made with ❤️ for quantitative research</sub>
</p>
