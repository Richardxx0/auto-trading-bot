# Auto-Trading Bot — Project Notes

## 项目概述

YSS 信号自动交易机器人。监听 yss-signal.com 信号，在 Binance 合约市场自动开仓/平仓/止盈止损，附带实时 Dashboard 监控面板。

### 核心入口

| 文件 | 职责 |
|---|---|
| `main.py` | 入口，启动信号监听 + PositionMonitor + Cron |
| `proxy.py` | HTTP CONNECT 代理（Binance API），端口 18080 |
| `watchdog.py` | 子进程管理器（自动重启 bot/dashboard） |
| `boot.py` | 部署工具（SSH 同步、Dashboard 管理） |

### src/ — 交易引擎

| 文件 | 职责 |
|---|---|
| `signal_handler.py` | 信号处理流水线 + 两层去重 + symbol_locks |
| `exchange.py` | ExchangeClient，封装 CCXT + Binance API |
| `position_service.py` | **统一持仓查询入口**，标准化 Binance PositionRisk DTO |
| `position_monitor.py` | 10s 定轮询，价格里程碑 + 健康检查(TIMEOUT/TIMEOUT_LOSS + 仓位利用率日志) + sync_positions |
| `risk_manager.py` | 仓位计算（金额/杠杆/止损止盈） |
| `analyzer.py` | 技术分析（EMA/RSI/ATR/ADX → 市场状态分类） |
| `parser.py` | 自然语言信号解析 |
| `trade_constants.py` | 枚举常量（CloseReason） |
| `dedup_service.py` | dedup.json 持久化层（旧格式迁移 + 状态管理） |
| `web_listener.py` | YSS 信号 HTTP 监听 |
| `telegram_listener.py` | Telegram 信号监听 |

### core/ — 异步封装

| 文件 | 职责 |
|---|---|
| `exchange_service.py` | 异步 ExchangeClient 封装（带超时/重试） |

### dashboard/ — Web 监控面板

| 文件 | 职责 |
|---|---|
| `app.py` | Flask + SocketIO Web 服务 |
| `trade_store.py` | 交易记录 JSON 持久化（trades.json） |
| `signal_store.py` | 信号 JSON 持久化（signals.json） |
| `event_log.py` | 事件日志 JSON 持久化（events.json） |
| `asset_history.py` | 资产快照 JSON 持久化（asset_history.json） |
| `templates/dashboard.html` | 前端单页 Web 界面 |

### config/ — 配置

| 文件 | 职责 |
|---|---|
| `settings.py` | 配置加载（.env → Config dataclass） |

### 基础设施

| 文件 | 职责 |
|---|---|
| `Dockerfile` | Linux/Docker 构建 |
| `docker-compose.yml` | bot + dashboard + proxy 三服务 |
| `.dockerignore` | Docker 构建排除规则 |
| `requirements.txt` | Python 依赖 |
| `start_proxy.sh` | Linux 代理启动脚本 |
| `_deploy_fix.py` / `_vps_deploy.py` | VPS 部署脚本 |

---

## 数据流

```
YSS Signal (yss-signal.com)
  ↓
web_listener.on_web_signal()
  ↓
signal_handler._处理解析后的信号()
  ├─ symbol_locks[contract_symbol]  (async with lock)
  ├─ step4: active_signals 内存去重 (symbol, direction, alert_count)
  ├─ step4: dedup.json 持久化去重 (status=OPEN/CLOSED)
  ├─ stale dedup → _清除去重 (自动收敛)
  ├─ position_service.has_open_position()   ← Binance PositionRisk
  ├─ position_service.get_open_positions_count()
  ├─ risk_manager.calculate()
  ├─ exchange.open_long_market()
  ├─ exchange.set_stop_loss_take_profit()
  ├─ trade_store.add_trade()
  └─ event_log.write()
       ↓
Dashboard (Flask + SocketIO)
  ├─ position_service.get_open_positions()  ← 统一 DTO
  ├─ position_service.get_account_info()
  ├─ position_service.get_asset_balances()
  ├─ trade_store / signal_store / event_log
  └─ asset_history  (60s 快照)
       ↓
position_monitor (10s 后台)
  ├─ position_service.get_open_positions()
  ├─ 价格里程碑 (breakeven → TP1 → TP2)
  ├─ 健康检查 (TIMEOUT_LOSS: 4h + -3% / TIMEOUT: 8h)
  └─ position_service.sync_positions()  ← 收敛幽灵仓位 + 联动 dedup→CLOSED
```

---

## 设计决策

### position_service — 统一持仓入口

- 所有调用方通过 `PositionService` 获取持仓数据，不再直接访问 `ExchangeClient._exch`
- DTO 标准化：`positionAmt→position_amt`, `unRealizedProfit→unrealized_pnl` 等
- `get_open_positions()` 内部 catch 异常，返回空列表（不崩溃）
- `sync_positions()` 幂等：只关闭本地 OPEN 但交易所不存在的记录
- 字段标准化在 `_normalize_position()` 中完成，保留 `raw` 字段供需要原始数据的场景

### symbol_locks — 每合约锁

- `signal_handler._处理解析后的信号` 中 step6−step7−风控检查 持有 `symbol_locks[contract_symbol]`
- 锁内代码为同步代码块（无 await），不阻塞事件循环
- 分析（step8）+ 下单（step9）在锁外
- per-symbol 粒度，不影响其他币种

### close_trade 兼容性

- `realized_pnl` / `close_price` 为 `Optional[float]`，允许 `None`
- `close_reason` 新增字段（TradeConstants.CloseReason）
- 值为 None 时不覆盖已有字段值
- `status: "CLOSED"` 始终写入

### 持仓真相源

- **Binance PositionRisk** 是唯一真相源
- `trades.json` 仅作为日志和 Dashboard 展示（不再影响交易决策）
- `sync_positions()` 确保两者最终收敛

---

### dedup 两层去重

- **内存层**：`active_signals: set[(symbol, direction, alert_count)]`
- **持久化层**：`dedup.json` 结构化记录（direction/alert_count/status/updated_at）
- **旧格式迁移**：`{"SYMBOL": timestamp}` → `{"SYMBOL": {"direction": "...", "status": "OPEN/CLOSED", ...}}`
- **持仓联动**：`sync_positions()` 关闭幽灵仓位时同步更新 dedup status→CLOSED
- **自动收敛**：step 4 检测到 dedup=OPEN 但交易所无持仓时自动清除过期条目（第一个信号即触发）
- 内存层更新在 `symbol_locks` 内部；持久化写入在锁外


## 三层架构

```
Layer 3: View Layer
  Dashboard (Flask API + SocketIO + ECharts)

Layer 2: Decision Layer
  Signal Handler (locks → checks → analysis → execution)
  Position Monitor (state machine + sync_positions)

Layer 1: Truth Layer
  Binance Exchange (PositionRisk / Account / Balance)
  position_service (统一 DTO 入口)
  trade_store / signal_store / event_log (日志层)
```

---

## 变更日志

### 2026-07-01: P0 — 状态收敛与统一持仓入口

**动机**：消除手动平仓、重启、JSON 损坏导致的重复开仓和幽灵仓位。

| 文件 | 操作 | 说明 |
|---|---|---|
| `src/trade_constants.py` | 新增 | CloseReason 枚举 |
| `src/position_service.py` | 新增 | 统一持仓查询 + DTO + sync_positions |
| `dashboard/trade_store.py` | 修改 | close_trade 签名兼容 Optional + close_reason |
| `src/position_monitor.py` | 修改 | 接入 position_service，sync_positions 替代手动清理 |
| `src/signal_handler.py` | 修改 | 接入 position_service + symbol_locks |
| `dashboard/app.py` | 修改 | 8 处 Binance API 调用替换为 position_service |

**关键变更**：
- 持仓判断从 `trades.json` 迁移到 `PositionRisk`（Binance 真相源）
- `sync_positions()` 每 10s 自动关闭幽灵仓位（close_reason=SYNC）
- `symbol_locks` 防护临界区（防止多信号源并发）
- Dashboard 全部 8 处 `_exchange._exch.fapi*` 替换
- `close_trade()` 参数全兼容
- `signal_handler` 自动创建 `PositionService`（position_service or PositionService）

**验证**：
- All files py_compile ✓
- 启动无 crash ✓
- 信号处理 step1→2→3→4 正常 ✓

### 2026-07-01: Docker / Linux 兼容性

| 文件 | 操作 |
|---|---|
| `Dockerfile` | 新增 |
| `docker-compose.yml` | 新增 |
| `.dockerignore` | 新增 |
| `start_proxy.sh` | 新增 |
| `_deploy_fix.py` | 修改（硬编码路径 → 动态路径） |
| `_vps_deploy.py` | 修改（同上） |
| `.gitignore` | 修改（添加 asset_history.json） |

### 2026-07-01: Dashboard 重构

`dashboard/app.py`: 内部事件队列 + mtime 优化 + 资产快照线程 + 移除 YSS scraper 注释代码。

### 2026-07-01: trade_store 标准化

### 2026-07-01: P2 — 仓位健康检查（TIMEOUT / TIMEOUT_LOSS）

**动机**：仓位占满后新信号无法入场——僵尸仓位（超时/亏损）需要自动释放。

| 文件 | 操作 | 说明 |
|---|---|---|
| `src/position_monitor.py` | 修改 | 健康检查块 + 仓位利用率日志（变化时打印）+ `el.write()` Dashboard 通知 + 仓位数量变化追踪 |
| `config/settings.py` | 修改 | Config 类新增 3 个健康检查配置项 |
| `src/trade_constants.py` | 修改 | 新增 `TIMEOUT` / `TIMEOUT_LOSS` |

**规则**：
- TIMEOUT_LOSS：持仓 ≥ 4h 且 浮亏 ≤ -3% → 超时亏损平仓（close_reason=TIMEOUT_LOSS）
- TIMEOUT：持仓 ≥ 8h → 绝对超时平仓（close_reason=TIMEOUT）

**安全设计**：
- API 平仓成功 → 关本地 + 清 dedup + Dashboard 通知
- API 平仓失败 → 本地不动，10 秒后下一轮重试
- 交易所查不到持仓 → 跳过，交 sync_positions 处理
- 残仓（notional < 1 USDT）不参与健康检查

**验证**：All 8 files py_compile ✓

### 2026-07-01: P1 — dedup 两层重构

**动机**：解决 dedup 只加不减导致已平仓 symbol 永久被跳过的问题，将去重系统从静态集合升级为可恢复状态系统。

| 文件 | 操作 | 说明 |
|---|---|---|
| `src/dedup_service.py` | 新增 | dedup.json 持久化层（旧格式迁移、读写、状态管理） |
| `src/signal_handler.py` | 修改 | 两层去重（active_signals 内存 + dedup.json 持久化）；移除 _dedup_lock/_dedup_file；新增 _清除去重()；step 4 改用双层检查 |
| `src/position_service.py` | 修改 | sync_positions 关闭幽灵仓位时联动更新 dedup status→CLOSED |

**关键变更**：
- 内存层 `active_signals: set[tuple[str, str, int]]` 防止同一 (symbol, direction, alert_count) 组合被重复处理
- 持久化层 dedup.json 从 `{"SYMBOL": timestamp}` 迁移到结构化格式（带 direction/alert_count/status/updated_at）
- `_清除去重()` 在平仓或检测到过期条目时更新 status→CLOSED
- step 4 检测到 dedup=OPEN 但交易所无持仓时自动清除过期条目（第一个信号即收敛触发器）
- sync_positions 关闭幽灵仓位时同时更新 dedup（持平常联动，10s 间隔）

**验证**：
- All 7 files py_compile ✓
- 启动无 crash ✓
- `_标记去重` 调用点已全部更新（含 direction/alert_count 参数）✓
- step 4 块在 Telegram 和 Web 入口均已插入 ✓


---

## 路线图

| 阶段 | 内容 | 状态 |
|---|---|---|
| P0 | 统一持仓入口 + sync_positions + symbol_locks | ✅ 已完成 |
| P1 | dedup 两层重构（内存+持久化，联动持仓状态） | ✅ 已完成 |
| P2 | 仓位健康检查（TIMEOUT/TIMEOUT_LOSS）+ 残仓清理 + Dashboard 通知 | ✅ 已完成 |
| P3 | 显式状态机 + SQLite + 多信号源 | 长期 |

---

## 已知问题

| 问题 | 影响 | 计划 |
|---|---|---|
| Binance testnet 451 地理限制 | 持仓查询/开仓无法进行 | 用 VPN 或 proxy |
| Telegram 入口缺少 symbol_locks | 只有 Web 入口有锁（低风险） | 后续补齐 |
