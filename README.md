# YSS 信号自动交易机器人

监听 **yss-signal.com** 网页信号，自动在 Binance Futures 执行交易。

> 监控网页新出现的币种报警 → 技术分析 → 动态风控 → 自动开仓 → 阶梯移动止盈止损

---

## 快速开始

### 1. 环境要求
- Python 3.12+
- Git

### 2. 安装

```bash
git clone https://github.com/Richardxx0/auto-trading-bot.git
cd auto-trading-bot
python -m venv venv
venv\Scripts\activate  # Windows
pip install -r requirements.txt
```

### 3. 配置

```bash
cp .env.example .env
```

编辑 .env 文件，填入凭据。重要配置项：

| 变量 | 说明 |
|------|------|
| YSS_EMAIL | yss-signal.com 登录邮箱 |
| YSS_PASSWORD | yss-signal.com 登录密码 |
| BINANCE_API_KEY | Binance API Key |
| BINANCE_SECRET_KEY | Binance Secret Key |
| BINANCE_TESTNET | true=测试网（默认），false=实盘/Demo |
| LEVERAGE | 杠杆倍数（默认 10） |
| MAX_OPEN_POSITIONS | 最大同时持仓数（默认 9） |
| TRAILING_STOP_ACTIVATION_PCT | 移动止损触发阈值（默认 0.012 = 1.2%） |
| TRAILING_STOP_DISTANCE_PCT | 移动止损距离（默认 0.002 = 0.2%） |
| REGIME_MIN_CONFIRM_BARS | 状态确认次数（测试=1，生产=2+） |

### 4. 运行

```bash
python main.py
```

### 5. 仪表盘 / Dashboard

实时监控面板，采用 **Tailwind CSS + ECharts + SocketIO** 构建，支持深色/浅色主题切换。

**当前版本特性：**
- Glassmorphism 毛玻璃设计
- 一键深色/浅色主题切换
- 实时持仓列表（方向颜色标识、浮动盈亏、平仓操作按钮）
- **等宽字体数字列**（Roboto Mono），小数点纵向对齐，专业交易软件质感
- **保证金占用 + 浮动盈亏** 真实计算（API 字段 + 计算兜底双重保障）
- **近一个月资产变化趋势图**（每 5 分钟自动快照记录，equity = totalWalletBalance + totalUnrealizedProfit）
- ECharts 持仓结构饼图（基于持仓数据实时计算）
- 开仓信号记录表（时间、币种、方向、评分、状态）
- 核心信号深度追踪——当日所有报警币种按符号聚合，显示首次推送价、当前标记价、报警次数
- SocketIO 实时日志流 + Binance 延迟心跳
- 每 5 秒自动刷新全盘数据
- 一键全额清仓按钮及逐行平仓操作

```bash
# 启动仪表盘
python -m dashboard.app
```

访问 http://127.0.0.1:5000/

后台启动：
```cmd
start /B python dashboard\app.py
```

## 流程说明

WebSignalListener 每 15 分钟轮询 yss-signal.com API，对比报警次数，
有增长则触发 SignalHandler 处理流水线：

```
第1步: 收到信号 → 第2步: 检查信号类型(仅LONG)
  → 第3步: 币种映射(CLO→CLOUSDT)
  → 第5步: 合约存在检查 → 第6步: 持仓检查(runner 仓位不影响新开)
  → 第7步: 获取余额 → 风控: 最大持仓数(runner 不计入)
  → 第8步A: 市场状态分类(ADX+ATR)
  → 第8步A+: 状态确认(需confirm≥min_confirm)
  → 第8步B: 技术分析(EMA+RSI+ATR → 入场评级)
  → 第9步: 决策执行(市价/限价开仓)
  → 挂止损 + 分批止盈(ATR自适应 / 固定2%/4%)
```

### 逐级加严

预警次数越高，入场门槛越严格：

| 次数 | 等级 | 效果 |
|------|------|------|
| 第1次 | 0 | 正常 |
| 第2-5次 | 1 | confirm≥3, 成交量×1.5 |
| 第6-9次 | 2 | confirm≥4, 成交量×2.0, 拒绝poor |
| 第10次+ | 3 | confirm≥5, 成交量×2.5, 拒绝poor |

## 阶梯移动止盈（Position Monitor）

后台每 10 秒巡检持仓，按利润阶梯锁定止损。持仓浮盈达标后自动执行：

| 浮盈触发点 | 止损上移到 | 备注 |
|---|---|---|
| +6% | 保本 +0.2%（entry × 1.002） | 确保不亏 |
| +10% | +4%（entry × 1.04） | 锁定部分利润 |
| +15% | +10%，**取消所有止盈单** | 进入追涨模式 |
| +20% | +20%，标记 `runner=True` | "彩票"模式：不计数、不阻止同币加仓 |

**Runner / 彩票模式**：浮盈达 +20% 后，该仓位不计入 `max_open_positions` 上限，也不阻止同一个币再开新仓。可以同时持有"原始仓位 + 彩票 runner 仓位"。

## 仓位清理保护机制

系统有两个自动清理逻辑，但均受仓位价值保护：

| 触发条件 | 行为 | 保护条件 |
|---|---|---|
| 交易所查不到该持仓 | `_pre_signal_cleanup()` 斩杀 | **仓位价值 ≥ 0.1U 则跳过** |
| 入场价偏差 > 0.5% | PositionMonitor 双重防线清理 | **仓位价值 ≥ 0.1U 则跳过** |
| 交易所无此持仓 | PositionMonitor 双重防线清理 | **仓位价值 ≥ 0.1U 则跳过** |

价值 ≥ 0.1 美元的仓位不会被任何清理机制误杀。

## 风控规则

| 规则 | 说明 |
|------|------|
| 最大持仓 | 默认 9 个（奔跑模式仓位不计入） |
| 保证金上限 | 每笔最多占用可用余额的 10% |
| 止损 | 入场价下方 2%（ATR 自适应） |
| 止盈 | 入场价上方 4%（ATR 自适应封顶） |
| 成交量过滤 | 当前量 < MA20 则跳过 |
| 同币持仓检查 | 已有普通持仓则跳过（runner 仓位不影响） |
| 连续亏损熔断 | 连亏 3 次暂停 4 小时 |
| 每日亏损上限 | 超 3% 停止当日交易 |

## 市场状态分类

| 状态 | 条件 | 风控 |
|------|------|------|
| TRENDING | ADX > 25 | 允许市价单 |
| RANGING | ADX ≤ 25 | 强制限价单（下方 0.5%） |
| VOLATILE | ATR 比值 > 1.5 | 仓位减半（×0.5） |

## 技术指标

| 指标 | 周期 | 用途 |
|------|------|------|
| EMA 20/50 | 4h | 趋势方向判断 |
| RSI 14 | 1h | 超买超卖判断（超卖<35，超买>55） |
| ATR 14 | 4h | 波动衡量 + 动态 SL/TP |
| ADX | 4h | 趋势强度确认（阈值 25） |

## 资产历史（日志回看）

系统自动记录账户余额快照到 `asset_history.json`：

- **记录频率**：每 5 分钟
- **保留上限**：18,000 条（约 60 天）
- **仪表盘图表**：在 "近一个月资产变化" 中展示
- **字段**：`time`, `balance`, `available`, `upnl`, `equity`

## Binance Demo Trading

1. 访问 https://demo.binance.com/zh-CN/futures
2. 注册 demo 账户，创建 API Key
3. .env 配置：`BINANCE_TESTNET=false`

## 安全建议

- 先用测试网验证逻辑
- API Key 只开合约交易权限
- 止损止盈使用交易所条件单（STOP_MARKET / TAKE_PROFIT_MARKET）

---

GitHub: https://github.com/Richardxx0/auto-trading-bot
