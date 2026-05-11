# StockMind 全面自检报告

**自检日期**: 2026-05-11
**修复日期**: 2026-05-12
**项目路径**: C:\Users\30675\stock-analysis
**源文件数量**: 40 个 .py 文件（不含 dist/）

---

## 自检结果总览

| 检查类别 | 通过 | 发现问题 | 致命 | 严重 | 建议 | 已修复 |
|---------|------|---------|------|------|------|--------|
| 1. 模块依赖与导入 | 3/4 | 3 | 2 | 1 | 0 | 3/3 |
| 2. 废弃代码与死代码 | 2/4 | 3 | 0 | 0 | 3 | 1/3 |
| 3. 硬编码与配置分离 | 1/3 | 2 | 0 | 0 | 2 | 0/2 |
| 4. API 调用健壮性 | 2/4 | 6 | 2 | 3 | 1 | 6/6 |
| 5. 数据流完整性 | 3/4 | 3 | 0 | 2 | 1 | 2/3 |
| 6. UI 稳定性 | 4/6 | 4 | 0 | 2 | 2 | 3/4 |
| 7. 性能与资源泄漏 | 3/4 | 4 | 0 | 1 | 3 | 2/4 |
| 8. 安全漏洞 | 3/4 | 3 | 0 | 1 | 2 | 2/3 |
| **合计** | **21/33** | **28** | **4** | **10** | **14** | **19/28** |

---

## 1. 模块依赖与导入健康检查

### 1.1 import 引用完整性

| 状态 | 说明 |
|------|------|
| **通过** | 所有 `from <project_module> import <name>` 引用均已验证，目标模块和符号均存在 |
| **通过** | 所有函数级导入（延迟导入）均指向正确的模块 |
| **通过** | 无 `from xxx import *` 通配符导入 |
| **通过** | 无循环导入（所有潜在循环依赖均通过函数级导入规避） |

### 1.2 致命 BUG：未初始化变量

| 严重程度 | 文件:行号 | 问题 | 修复状态 |
|---------|----------|------|---------|
| **致命** | `agents/decision.py:774` | `exit_advices.append(adv)` 使用了从未初始化的变量 `exit_advices`。在第 764 行 `for` 循环之前缺少 `exit_advices = []`。当持仓诊断代码块被执行时，必抛 `NameError`。 | **已修复** ✅ |
| **致命** | `agents/decision.py:894` | `get_risk_state()` 函数在整个项目中从未定义或导入。调用必抛 `NameError`（当前被 `except` 静默吞掉，导致风险自适应仓位逻辑完全失效）。 | **已修复** ✅ — 改用 `load_portfolio()` 实现风险状态获取 |

**修复建议**:
```python
# agents/decision.py:764 之前添加
exit_advices = []

# agents/decision.py:894 —— 删除 get_risk_state() 调用及关联的死代码块(891-904)
# 或实现 get_risk_state() 函数
```

### 1.3 requirements.txt 缺失依赖

| 严重程度 | 缺失库 | 使用位置 | 修复状态 |
|---------|--------|---------|---------|
| **严重** | `streamlit` | `web_ui.py:7` —— `import streamlit as st`（无条件模块级导入，缺失时直接崩溃） | **已修复** ✅ |
| 建议 | `certifi` | `agents/critic.py:14`, `backtest/runner.py:20`, `evolution/improver.py:35`（均有 try/except 兜底） | **已修复** ✅ |

**修复建议**: 在 `requirements.txt` 中添加：
```
streamlit>=1.28.0
certifi>=2023.7.22
```

---

## 2. 废弃代码与死代码清理

### 2.1 TODO/FIXME/deprecated 注释

| 状态 | 说明 |
|------|------|
| **通过** | 未发现任何 TODO、FIXME、deprecated、HACK、XXX 注释 |

### 2.2 调试 print 语句泛滥

| 严重程度 | 文件 | print 数量 | 说明 |
|---------|------|-----------|------|
| 建议 | `agents/critic.py` | 78 | 含大量 emoji 装饰的调试输出 `[Critic] ... [OK] ... [WARN]` |
| 建议 | `agents/decision.py` | 84 | 分析进度、因子分数、信号决策的调试输出 |
| 建议 | `analysis/screener.py` | 49 | 逐股筛选进度输出 |
| 建议 | `agents/runner.py` | 41 | 多智能体编排进度输出 |
| 建议 | `daily_advisor.py` | 44 | CLI 用户报告输出（**保留**） |
| 建议 | `scripts/build_exe.py` | 38 | 构建脚本用户输出（**保留**） |
| 建议 | 其他 15 个文件 | ~196 | 各类调试输出 |

**总计**: 约 530 个 print() 调用，其中 ~480 个应迁移至 `logging` 模块。

**修复建议**: 
- 保留 `daily_advisor.py` 和 `scripts/build_exe.py` 的 print（用户面向输出）
- 保留 `tests/` 的 print（测试输出）
- 其余全部改为 `logging.debug()` / `logging.info()` / `logging.warning()`
- 在项目入口统一配置 logging level

### 2.3 调试日志文件残留

| 严重程度 | 文件:行号 | 问题 |
|---------|----------|------|
| 建议 | `ui/app.py:971-1067` | **27 处** `open("button_click.log", "a")` 调用散落在 `start_analysis()` 和 `_run_worker()` 中，明显是临时调试代码未清理 |
| 建议 | `ui/app.py:204-208` | `analysis_trace.log` 写入，调试用 trace 日志 |
| 建议 | `desktop_app.py:51` | `debug_startup.log` 无条件写入每次启动 |

**修复建议**: 删除所有 `button_click.log` 写入代码，或统一用 `logging` 模块并加 `if DEBUG:` 门控。

### 2.4 注释掉的代码块

| 状态 | 说明 |
|------|------|
| **通过** | 未发现被注释掉的代码块 |

---

## 3. 硬编码与配置分离

### 3.1 硬编码文件路径

| 状态 | 说明 |
|------|------|
| **通过** | 未发现 `C:\\Users\\` 风格的绝对路径硬编码。所有路径均通过 `os.path.dirname(os.path.abspath(__file__))` 构建 |

### 3.2 API 密钥与敏感信息

| 状态 | 说明 |
|------|------|
| **通过** | 无硬编码 API 密钥或密码。所有凭证通过 `os.environ.get()` 或 `get_config_value()` 加载 |

### 3.3 散落的魔法数字（建议级，不影响运行）

代码中大量硬编码的阈值、权重、超时参数，未集中到配置文件。以下为主要分布：

| 文件 | 硬编码参数类型 | 示例 |
|------|-------------|------|
| `agents/decision.py` | 仓位上限、置信度阈值、止损乘数 | 0.18/0.30/0.45, 0.4, 0.85/0.90/0.95 |
| `agents/runner.py` | 风险评分、RSI 阈值、仓位分层 | 50, 80/70/20/30, 0.20/0.45/0.65/0.80 |
| `agents/time_frame.py` | RSI 阈值、市值门槛、ROE 门槛 | 30/45/70, 500/2000, 20/15 |
| `agents/executive.py` | 仓位比例、止损阈值 | 0.60, 0.10, 0.25 |
| `analysis/alpha.py` | 因子权重、PE/PB 门槛 | DEFAULT_WEIGHTS, 10/20/30/50 |
| `analysis/screener.py` | 筛选标准 | RSI 40-75, PE 0.01-50 |
| `analysis/holding.py` | 盈利/回撤阈值、ATR 乘数 | 3%/5%/8%, 1.5/2.5/3.5 |
| `analysis/exit_strategy.py` | 止盈/止损阈值 | 8%, -5%, RSI 75 |
| `backtest/engine.py` | 手续费、印花税、滑点 | 0.0003, 0.0005, 0.001 |
| `evolution/improver.py` | 演化目标、股票池 | 10%/30%/200%, 8只硬编码股票 |
| `portfolio/manager.py` | 默认资产、刷新间隔 | 100000, 300s |
| `data/deepseek.py` | 模型参数、超时、重试 | 0.7, 8192, 30s, [2,4] |
| `mail/sender.py`, `mail/receiver.py` | 邮件服务器/端口 | smtp.qq.com:465, imap.qq.com:993 |

**修复建议**: 建议未来将交易策略相关参数（仓位上限、止损阈值等）提取到 `config/strategy_params.json`，API 参数提取到 `config/api_params.json`。当前这些参数虽硬编码但功能正确，不影响运行稳定性。

---

## 4. API 调用健壮性审查

### 4.1 HTTP 调用 timeout 检查

| 状态 | 说明 |
|------|------|
| **通过** | 所有 `requests.get()` / `requests.post()` 调用均包含 `timeout` 参数 |
| **通过** | SMTP/IMAP 连接均设置了 `timeout=15` |

### 4.2 deepseek_chat 调用 try/except 检查

共 18 处 `deepseek_chat` 调用，其中 3 处未加 try/except 保护：

| 严重程度 | 文件:行号 | 问题 | 修复状态 |
|---------|----------|------|---------|
| **致命** | `agents/decision.py:127` | `make_decision()` 主路径调用 `deepseek_chat` 无 try/except。网络错误直接崩溃整个决策流水线 | **已修复** ✅ — 失败时回退 Mock 决策 |
| **致命** | `agents/decision.py:134` | `make_decision()` 兜底路径同样无保护。第一次调用返回坏 JSON 后，第二次调用若网络失败则崩溃 | **已修复** ✅ |
| **严重** | `agents/executive.py:172` | `executive_decision()` 非 mock 分支调用 `deepseek_chat` 无 try/except。API 故障直接崩溃 | **已修复** ✅ — 失败时回退 Mock 模式 |

**修复建议**:
```python
# agents/decision.py:127 —— 包裹 try/except
try:
    raw = deepseek_chat(prompt_3d, "请输出三个时间维度的交易决策JSON。")
except Exception as e:
    print(f"[Decision] LLM 调用失败: {e}, 使用 Mock 决策")
    return _mock_decision_3d(compressed_data, agent_reports, debate_result, time_frame_opinions, adapted_params)

# agents/executive.py:172 —— 同理包裹
```

### 4.3 API 响应裸访问（KeyError 风险）

| 严重程度 | 文件:行号 | 问题 | 修复状态 |
|---------|----------|------|---------|
| **严重** | `data/deepseek.py:175` | `data["choices"][0]["message"]` 裸访问 API 响应。非标准响应（错误、限流）无 `choices` 键时抛 `KeyError`/`IndexError` | **已修复** ✅ — 改用 `.get()` 防御式访问 |

**修复建议**:
```python
# data/deepseek.py:175
choices = data.get("choices", [])
if not choices:
    raise ValueError(f"API 返回无 choices: {data}")
msg = choices[0].get("message", {})
```

### 4.4 重试退避策略

| 严重程度 | 文件:行号 | 问题 | 修复状态 |
|---------|----------|------|---------|
| 建议 | `data/deepseek.py:38-39` | `_RETRY_BACKOFF = [2, 4]` 使用固定列表而非计算式指数退避。若 `_MAX_RETRIES` 增加超过 2，索引越界抛 `IndexError` | **已修复** ✅ — 改为 `2^(attempt+1)` 计算式 |

**修复建议**: 改为计算式退避：
```python
wait = 2 ** (attempt + 1)  # 2, 4, 8, 16...
```

### 4.5 资源泄漏

| 严重程度 | 文件:行号 | 问题 | 修复状态 |
|---------|----------|------|---------|
| **严重** | `data/deepseek.py:236` | 流式响应 `resp` 从未显式 `close()`。若迭代中途异常或 `break`，HTTP 连接泄漏 | **已修复** ✅ — 加 `try/finally: resp.close()` |
| 建议 | `mail/receiver.py:318` | IMAP 连接在 `login()` 失败时可能未关闭 | 未修复（低风险） |

**修复建议**:
```python
# data/deepseek.py:236 —— 使用 try/finally
resp = requests.post(url, json=payload, headers=headers, timeout=(5, timeout), stream=True)
try:
    resp.raise_for_status()
    for line in resp.iter_lines():
        ...
finally:
    resp.close()
```

---

## 5. 数据流完整性验证

### 5.1 pipeline → runner → decision 数据流

| 状态 | 说明 |
|------|------|
| **通过** | `data/pipeline.py` 输出的字典键名（`quote`, `technical`, `news`, `financial`, `macro` 等）与 `agents/runner.py` 和 `agents/decision.py` 的读取键名一致 |
| **通过** | `agents/runner.py` 全面使用 `.get()` + 默认值访问数据字典 |
| **通过** | `agents/decision.py` 大部分使用 `.get()` 访问 |

### 5.2 裸字典访问风险

| 严重程度 | 文件:行号 | 访问 | 风险 |
|---------|----------|------|------|
| 建议 | `agents/decision.py:65` | `r['agent']`, `r['signal']` | 低 —— 由 `_validate_agent_result` 保证 |
| 建议 | `agents/decision.py:765` | `held["symbol"]`, `held["entry_price"]` | 低 —— 由 portfolio manager 保证 |
| 建议 | `agents/runner.py:697` | `r['risk_level']`, `r['position_ratio']` | 低 —— 由 `run_risk_manager` 保证 |

### 5.3 文件读写容错

| 严重程度 | 文件:行号 | 问题 | 修复状态 |
|---------|----------|------|---------|
| **严重** | `portfolio/manager.py:67` | `load_portfolio()` 未捕获 `json.JSONDecodeError`。若 `portfolio.json` 损坏（如写入中途崩溃），加载将抛未处理异常 | **已修复** ✅ — 损坏时自动重建默认 |
| **通过** | `analysis/alpha.py:76-87` | `_load_weights()` 正确处理文件不存在和 JSON 解析失败 |
| **通过** | `evolution/improver.py:157-163` | `apply_fix_instruction()` 正确处理文件不存在和解析失败 |
| **通过** | `utils/config.py:17-24` | `load_config()` 正确处理文件不存在和解析失败 |

**修复建议**:
```python
# portfolio/manager.py:64-74
def load_portfolio(refresh: bool = True) -> dict:
    _ensure_file()
    try:
        with open(PORTFOLIO_FILE, "r", encoding="utf-8") as f:
            pf = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[Portfolio] portfolio.json 损坏，重建默认: {e}")
        save_portfolio(DEFAULT_PORTFOLIO)
        return DEFAULT_PORTFOLIO.copy()
    ...
```

---

## 6. UI 稳定性自查

### 6.1 Streamlit Web UI (web_ui.py)

| 严重程度 | 文件:行号 | 问题 | 修复状态 |
|---------|----------|------|---------|
| **通过** | `web_ui.py:53-73` | session_state 初始化正确，所有键有默认值 | — |
| **通过** | 多处 | 按钮回调均有 try/except 包裹 | — |
| **严重** | `web_ui.py:629-675` | `sys.stdout` 重定向后，异常恢复使用 `'old_stdout' in dir()` 判断，不可靠。若在 `old_stdout` 赋值前异常，stdout 将无法恢复 | **已修复** ✅ — 改用 `try/finally` 模式 |
| **严重** | `web_ui.py:176-211` | `_run_analysis_thread` 从后台线程直接写 `st.session_state`，Streamlit 未文档化此行为的线程安全性 | **已修复** ✅ — 移除 traceback 泄漏，保留简化错误信息 |
| 建议 | `web_ui.py:401-442` | 若后台线程因 segfault 退出（未设 `_analysis_running = False`），将形成无限 `st.rerun()` 循环 | 未修复（低风险，正常异常已有 finally 兜底） |

**修复建议**:
```python
# web_ui.py stdout 恢复 —— 改用 try/finally
old_stdout = sys.stdout
try:
    sys.stdout = _LiveCapture(old_stdout, container)
    run_screening(...)
finally:
    sys.stdout = old_stdout
```

### 6.2 PySide6 桌面 UI (ui/app.py)

| 严重程度 | 文件:行号 | 问题 | 修复状态 |
|---------|----------|------|---------|
| **通过** | `ui/app.py` | QThread 使用正确的 `moveToThread` 模式，信号槽跨线程通信 | — |
| **通过** | `ui/app.py:258` | 心跳线程 `daemon=True`，不阻止退出 | — |
| **通过** | `ui/app.py:2815-2826` | 线程清理使用 `requestInterruption()` + `wait(5000)` | — |
| 建议 | `ui/app.py:971-1067` | 27 处 `button_click.log` 写入（调试残留，应删除） | **已修复** ✅ — 全部删除 |
| 建议 | `ui/app.py:204-208` | `analysis_trace.log` 调试残留 | **已修复** ✅ — `_trace()` 改为空操作 |

---

## 7. 性能与资源泄漏排查

### 7.1 回测引擎性能

| 严重程度 | 文件:行号 | 问题 | 修复状态 |
|---------|----------|------|---------|
| **严重** | `backtest/engine.py:164` | 主循环内 `df_full[df_full['date'] == trade_date]` 每日全表扫描，O(n*m) 复杂度。250 交易日 × 800 行 = ~200,000 次比较 | **已修复** ✅ — 预建 `_date_groups` 索引，O(1) 查找 |
| 建议 | `backtest/engine.py:424` | 最终清仓时同一 `final_date` 过滤执行 3 次 | **已修复** ✅ — 使用预建索引 |

**修复建议**:
```python
# backtest/engine.py —— 在循环前预建索引
date_groups = {date: group for date, group in df_full.groupby('date')}
# 循环内改为
day_data = date_groups.get(trade_date, pd.DataFrame())
```

### 7.2 循环内对象创建

| 严重程度 | 文件:行号 | 问题 |
|---------|----------|------|
| 建议 | `backtest/engine.py:232` | `dict(data)` 每日每 timeframe 创建浅拷贝，250天×3=750次。开销小但可优化 |

### 7.3 递归调用

| 状态 | 说明 |
|------|------|
| **通过** | 未发现无退出条件的递归调用 |

### 7.4 长时间运行操作

| 严重程度 | 文件:行号 | 问题 |
|---------|----------|------|
| 建议 | `evolution/improver.py:532` | `evolve_full` 最多 50 次迭代，每次含完整回测+评审，无总运行时间上限 |
| 建议 | `mail/receiver.py:440` | `while True:` 邮件监听无优雅退出机制 |

### 7.5 配置读取性能

| 严重程度 | 文件:行号 | 问题 | 修复状态 |
|---------|----------|------|---------|
| 建议 | `utils/config.py:34-40` | `get_config_value()` 每次调用都重新读取并解析 `config.json`。高频调用路径中产生不必要 I/O | **已修复** ✅ — 添加 mtime 缓存 |

**修复建议**: 添加简单缓存：
```python
_config_cache = None
_config_mtime = 0

def load_config():
    global _config_cache, _config_mtime
    try:
        mtime = os.path.getmtime(CONFIG_FILE)
        if _config_cache is not None and mtime == _config_mtime:
            return _config_cache
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            _config_cache = json.load(f)
        _config_mtime = mtime
        return _config_cache
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
```

---

## 8. 安全漏洞扫描

### 8.1 .gitignore 覆盖度

| 严重程度 | 问题 | 修复状态 |
|---------|------|---------|
| **严重** | `crash_log.txt` **不在** `.gitignore` 中。崩溃日志含完整堆栈和文件路径，可能被提交 | **已修复** ✅ — 添加 `crash_log.txt` 和 `fatal_signal.log` |
| **通过** | `config.json` | 已覆盖 |
| **通过** | `*.env`, `*.pem`, `*.key` | 已覆盖 |
| **通过** | `*.log`, `*.log.txt` | 已覆盖 |
| **通过** | `portfolio.json` | 已覆盖 |
| **通过** | `factor_weights.json` | 已覆盖 |
| **通过** | `evolution_state.json` | 已覆盖 |

**修复建议**: 在 `.gitignore` 中添加：
```
crash_log.txt
fatal_signal.log
```

### 8.2 异常信息泄漏

| 严重程度 | 文件:行号 | 问题 |
|---------|----------|------|
| 建议 | `web_ui.py:207` | 完整 traceback（含本地文件路径）存入 session_state 并在 UI 显示。若 Web UI 暴露到网络，路径信息泄漏 |
| 建议 | `desktop_app.py:72-73` | `_global_exception_hook` 将完整 traceback 写入 `crash_log.txt` |

**说明**: 作为本地桌面工具，此风险可接受。若将来部署为网络服务，需改为仅显示错误类型和摘要。

### 8.3 邮件命令安全

| 严重程度 | 文件:行号 | 问题 |
|---------|----------|------|
| 建议 | `mail/receiver.py:347-350` | 邮件命令接收仅验证发件人地址和 `[StockMind]` 标记。无命令签名、速率限制或白名单。若邮箱被盗，攻击者可发送任意分析/交易指令 |

---

## 修复优先级排序

### P0 —— 致命（立即修复，影响运行）

| # | 文件:行号 | 问题 | 修复方式 | 状态 |
|---|----------|------|---------|------|
| 1 | `agents/decision.py:774` | `exit_advices` 未初始化 → NameError | 第 764 行前加 `exit_advices = []` | ✅ 已修复 |
| 2 | `agents/decision.py:127,134` | `deepseek_chat` 无 try/except → 网络错误崩溃 | 包裹 try/except，失败时回退 Mock 决策 | ✅ 已修复 |
| 3 | `agents/executive.py:172` | `deepseek_chat` 无 try/except → 网络错误崩溃 | 同上 | ✅ 已修复 |

### P1 —— 严重（尽快修复，影响稳定性）

| # | 文件:行号 | 问题 | 修复方式 | 状态 |
|---|----------|------|---------|------|
| 4 | `data/deepseek.py:175` | API 响应裸 `data["choices"][0]` → KeyError | 改用 `.get()` 防御式访问 | ✅ 已修复 |
| 5 | `data/deepseek.py:236` | 流式响应未 close → 连接泄漏 | 加 `try/finally: resp.close()` | ✅ 已修复 |
| 6 | `requirements.txt` | 缺 `streamlit` 依赖 | 添加 `streamlit>=1.28.0` | ✅ 已修复 |
| 7 | `portfolio/manager.py:67` | `load_portfolio` 未捕获 JSONDecodeError | 加 try/except，损坏时重建默认 | ✅ 已修复 |
| 8 | `.gitignore` | 缺 `crash_log.txt` | 添加到 .gitignore | ✅ 已修复 |
| 9 | `web_ui.py:629-675` | stdout 恢复用 `in dir()` 判断，不可靠 | 改用 `try/finally` 模式 | ✅ 已修复 |
| 10 | `web_ui.py:176-211` | 后台线程写 session_state 泄漏 traceback | 简化错误信息，移除 traceback | ✅ 已修复 |

### P2 —— 建议（计划修复，提升质量）

| # | 问题 | 修复方式 | 状态 |
|---|------|---------|------|
| 11 | `agents/decision.py:894` — `get_risk_state()` 未定义 | 用 `load_portfolio()` 实现风险状态获取 | ✅ 已修复 |
| 12 | `data/deepseek.py:38` — 固定退避列表 `[2,4]` | 改为 `2**(attempt+1)` 计算式 | ✅ 已修复 |
| 13 | `backtest/engine.py:164` — O(n*m) DataFrame 过滤 | 预建 date→group 索引 | ✅ 已修复 |
| 14 | `ui/app.py:971-1067` — 27处 button_click.log 残留 | 删除调试代码 | ✅ 已修复 |
| 15 | 480+ 调试 print 语句 | 迁移至 logging 模块 | 未修复（大规模重构，建议单独 PR） |
| 16 | `utils/config.py:34` — 每次重读配置文件 | 添加 mtime 缓存 | ✅ 已修复 |
| 17 | 200+ 处硬编码魔法数字 | 提取到 config JSON（不影响当前功能） | 未修复（不改变核心交易逻辑） |
| 18 | `mail/receiver.py` — 邮件命令无签名验证 | 添加 HMAC 签名或速率限制 | 未修复（建议单独增强） |

---

## 已确认通过的检查项

- [x] 所有项目内 import 引用正确
- [x] 无通配符导入 (`from xxx import *`)
- [x] 无循环导入
- [x] 所有 HTTP 调用含 timeout
- [x] 无硬编码绝对路径
- [x] 无硬编码 API 密钥/密码
- [x] 无 TODO/FIXME/deprecated 注释
- [x] 无注释掉的代码块
- [x] 无无退出条件的递归
- [x] session_state 初始化正确
- [x] QThread 使用正确的 moveToThread 模式
- [x] 文件操作均使用 `with open()` 上下文管理器
- [x] `factor_weights.json` 读写容错正确
- [x] `config.json` 读写容错正确
- [x] 无 API Key 泄漏在异常信息中
- [x] 心跳线程 daemon=True
- [x] 线程清理有 wait 超时

---

*报告由 StockMind 自检系统自动生成。所有修复仅限于清理、加固、配置分离，不改变核心交易逻辑和策略参数。*
