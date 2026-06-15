# YSS 自动交易机器人

监听 **yss-signal.com** 网页信号，自动在 Binance Futures 执行交易。

> 监控网页新出现的币种报警 → 技术分析 → 动态风控 → 自动开仓 → 移动止损

---

## 完整流程

```mermaid
flowchart TD
    WS[WebSignalListener<br/>每30秒轮询 API] --> LOGIN[登录 yss-signal.com<br/>获取 Bearer Token]
    LOGIN --> INIT[首次加载: 记录所有币种的<br/>当前报警次数, 不触发交易]
    INIT --> POLL{轮询对比<br/>alert_count}
    POLL -- 没增长 --> POLL
    POLL -- 增长 --> SH[SignalHandler<br/>第N次报警]

    SH --> TYPE[第2步: 检查信号类型]
    TYPE -- 非 LONG --> SKIP
    TYPE -- LONG --> MAP[第3步: 币种映射<br/>CLO → CLOUSDT]

    MAP --> AUTO{映射表<br/>找不到?}
    AUTO -- 有 --> DEDUP
    AUTO -- 无 --> TRYUSDT[自动尝试 {币种}USDT]
    TRYUSDT -- 合约存在 --> DEDUP
    TRYUSDT -- 不存在 --> SKIP

    DEDUP[第4步: 持久化去重<br/>dedup.json] -- 已处理过 --> SKIP
    DEDUP -- 未处理 --> POS[第5步: 检查合约存在]
    POS -- 不存在 --> SKIP
    POS -- 存在 --> HOLD[第6步: 检查持仓]
    HOLD -- 已持仓 --> SKIP
    HOLD -- 无持仓 --> BAL[第7步: 获取余额]

    BAL --> LIMIT{第3.5步: 最大持仓}
    LIMIT -- 已达上限 --> SKIP
    LIMIT -- 未超限 --> REGIME[第8步A: 市场状态分类<br/>ADX + ATR → TRENDING/RANGING/VOLATILE]

    REGIME --> CONFIRM[第8步A+: 连续2次一致<br/>状态才确认生效]
    CONFIRM -- 未稳定 --> SKIP
    CONFIRM -- 已稳定 --> ANALYSIS[第8步B: 技术分析<br/>EMA20/50 + RSI14 + ATR]

    ANALYSIS --> SLTP[计算动态SL/TP<br/>SL=1.5xATR  TP1/2/3=2x/3x/4.5xATR]
    
    SLTP --> RISKCTRL[风控联动]
    RISKCTRL -- 趋势行情 --> MARKET
    RISKCTRL -- 震荡行情 --> LIMIT_ORDER
    RISKCTRL -- 高波动 --> HALF[仓位减半]

    MARKET[第9步: 市价开多] --> SL_ORDER[挂止损 STOP_MARKET<br/>全仓 1 个单]
    LIMIT_ORDER[第9步: 限价开多] --> MONITOR_LIMIT[后台监控成交]

    SL_ORDER --> TP_ORDERS[分批止盈]
    MONITOR_LIMIT --> TP_ORDERS

    TP_ORDERS --> TP1[TP1: 卖50%<br/>+2x ATR]
    TP1 --> TP2[TP2: 卖30%<br/>+3x ATR]
    TP2 --> TP3[TP3: 卖20%<br/>+4.5x ATR]

    TP3 --> MONITOR[启动后台持仓监控<br/>PositionMonitor 每30秒巡检]

    MONITOR --> TRAIL{浮盈>3%?}
    TRAIL -- 否 --> MONITOR
    TRAIL -- 是 --> TRAIL_ACT[移动止损<br/>SL 上移到最高价-2%]

    TRAIL_ACT --> MONITOR
    SKIP[忽略 ✅]
```

---

## 项目结构

```
auto-trading-bot/
├── config/
│   ├── __init__.py
│   └── settings.py          # .env 加载 + 全部配置项
├── src/
│   ├── __init__.py
│   ├── web_listener.py      # yss-signal.com API 轮询监听器
│   ├── signal_handler.py    # 信号处理流水线（分析→风控→执行）
│   ├── exchange.py          # Binance Futures 交易封装 (ccxt)
│   ├── analyzer.py          # K线技术分析 (EMA/RSI/ATR/ADX)
│   ├── parser.py            # Telegram 信号解析（保留兼容）
│   ├── risk_manager.py      # 仓位计算
│   ├── telegram_listener.py # Telegram 监听（可选保留）
│   ├── position_monitor.py  # 持仓监控 + 移动止损管理
├── dashboard/               # 交易监控面板（Flask + SocketIO）
│   ├── app.py               # 后端
│   ├── trade_store.py       # 交易记录持久化
│   └── templates/
│       └── dashboard.html   # 前端界面
├── main.py                  # 启动入口
├── .env                     # 配置文件（不提交到 Git）
├── .env.example             # 配置示例
├── requirements.txt
├── dedup.json               # 已处理币种记录（自动生成）
└── README.md
```

---

## 快速开始

### 1. 安装

```bash
cd auto-trading-bot
pip install -r requirements.txt
```

### 2. 配置

```bash
cp .env.example .env
```

编辑 `.env` 文件，填入你的凭据：

| 变量 | 说明 |
|------|------|
| `YSS_EMAIL` | yss-signal.com 登录邮箱 |
| `YSS_PASSWORD` | yss-signal.com 登录密码 |
| `BINANCE_API_KEY` | Binance API Key（合约交易权限） |
| `BINANCE_SECRET_KEY` | Binance Secret Key |
| `BINANCE_TESTNET` | `true` = 测试网（默认） `false` = 实盘 |
| `RISK_PER_TRADE` | 每笔风险比例（默认 `0.02` = 2%） |
| `STOP_LOSS_PCT` | 固定止损（默认 `0.02` = 2%，动态模式下被 ATR 覆盖） |
| `TAKE_PROFIT_PCT` | 固定止盈（默认 `0.04` = 4%，动态模式下被 ATR 覆盖） |
| `LEVERAGE` | 杠杆倍数（默认 `5`） |

### 3. 运行

```bash
python main.py
```

首次启动会自动登录 yss-signal.com，加载当前信号列表，然后开始每 30 秒轮询。

### 4. 查看仪表盘

```bash
python -m dashboard.app
```

访问 http://127.0.0.1:5000/ 查看实时监控面板。

---

## 技术分析说明

### 市场状态分类（第8步A）

使用 ADX + ATR 判断当前行情：

| 状态 | 条件 | 风控行为 |
|------|------|----------|
| **TRENDING**（趋势） | ADX > 25，EMA 发散 | 市价单，正常仓位 |
| **RANGING**（震荡） | ADX ≤ 25 | 限价单，禁止追涨 |
| **VOLATILE**（高波动） | ATR 比值 > 1.5 | 仓位减半 |

状态需要连续 2 次轮询一致才生效，防止频繁跳变。

### 技术指标（第8步B）

| 指标 | 参数 | 用途 |
|------|------|------|
| EMA(20) / EMA(50) | 4h K线 | 判断趋势方向 |
| RSI(14) | 1h K线 | 判断超买/超卖 |
| ATR(14) | 4h K线 | 衡量波动、计算动态SL/TP |

### 动态止损止盈

基于 ATR（平均真实波幅）自动计算，适应不同波动率：

| 等级 | 价格 | 仓位上 |
|------|------|--------|
| **SL** | 入场价 - 1.5 × ATR | 100%（一个止损单） |
| **TP1** | 入场价 + 2.0 × ATR | 50% |
| **TP2** | 入场价 + 3.0 × ATR | 30% |
| **TP3** | 入场价 + 4.5 × ATR | 20% |

> ATR 大时 SL 宽（不容易被震出去），ATR 小时 SL 紧（控制亏损）。止盈分三批，先锁一半利润，再让剩余仓位继续跑。

---

## 移动止损

后台 **PositionMonitor** 每 30 秒检查一次所有持仓：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| 激活阈值 | 浮盈 > 3% | 超过此比例才开始跟踪 |
| 回撤距离 | 最高价 - 2% | 从最高价回落 2% 即止损 |

```
例：开仓价 100 → 涨到 110 → SL 上移到 107.8
    → 涨到 115 → SL 上移到 112.7
    → 跌到 112.5 → 触发止损, 锁定 12.5% 利润
```

---

## 风控规则

| 规则 | 说明 |
|------|------|
| 最大持仓数 | 默认 5 个（`max_open_positions`） |
| 持久化去重 | 已处理的币种写入 `dedup.json`，重启不重复开仓 |
| 内存去重 | 同一次运行中不重复开同一个币 |
| 持仓检查 | 开仓前查询交易所，已有持仓则跳过 |
| 分批止盈 | 50% + 30% + 20% 三批止盈 |
| 移动止损 | 浮盈 3% 后启动，锁定利润 |

---

## 安全建议

- **始终先用测试网**（`BINANCE_TESTNET=true`）验证逻辑
- API Key 只开 **合约交易** 权限，不要开提币
- 止损止盈使用交易所条件单（`STOP_MARKET` / `TAKE_PROFIT_MARKET`），机器人离线也能触发
- 所有异常都会被捕获并记录日志，机器人不会因单个错误停止
