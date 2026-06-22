# YSS 信号自动交易机器人

监听 **yss-signal.com** 网页信号，自动在 Binance Futures 执行交易。

> 监控网页新出现的币种报警 -> 技术分析 -> 动态风控 -> 自动开仓 -> 移动止损

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
| LEVERAGE | 杠杆倍数（默认 5） |
| REGIME_MIN_CONFIRM_BARS | 状态确认次数（测试=1，生产=2+） |

### 4. 运行

```bash
python main.py
```

### 5. 仪表盘

Windows：
```cmd
start /B python dashboard\app.py
```

Linux/Mac：
```bash
nohup python dashboard/app.py &
```

访问 http://127.0.0.1:5000/

## 流程说明

WebSignalListener 每15分钟轮询 yss-signal.com API，对比报警次数，
有增长则触发 SignalHandler 处理流水线：

```
第1步: 收到信号 -> 第2步: 检查信号类型(仅LONG)
  -> 第3步: 币种映射(CLO->CLOUSDT)
  -> 第5步: 合约存在检查 -> 第6步: 持仓检查
  -> 第7步: 获取余额 -> 风控: 最大持仓数
  -> 第8步A: 市场状态分类(ADX+ATR)
  -> 第8步A+: 状态确认(需confirm>=min_confirm)
  -> 第8步B: 技术分析(EMA+RSI+ATR)
  -> 第9步: 决策执行(市价/限价开仓)
  -> 挂止损+分批止盈(50%@2xATR,30%@3xATR,20%@4.5xATR)
```

后台持仓监控每30秒巡检，浮盈>3%启动移动止损。

## Binance Demo Trading

1. 访问 https://demo.binance.com/zh-CN/futures
2. 注册demo账户，创建API Key
3. .env 配置：BINANCE_TESTNET=false

## 逐级加严

预警次数越高，入场门槛越严格：

| 次数 | 等级 | 效果 |
|------|------|------|
| 第1次 | 0 | 正常 |
| 第2-5次 | 1 | confirm>=3, 成交量x1.5 |
| 第6-9次 | 2 | confirm>=4, 成交量x2.0, 拒绝poor |
| 第10次+ | 3 | confirm>=5, 成交量x2.5, 拒绝poor |

## 风控规则

| 规则 | 说明 |
|------|------|
| 最大持仓 | 默认 5 个 |
| 成交量过滤 | 当前量 < MA20 则跳过 |
| 持仓检查 | 已有持仓则跳过 |
| 分批止盈 | 50%+30%+20% |
| 移动止损 | 浮盈3%后启动 |
| 连续亏损熔断 | 连亏3次暂停4小时 |
| 每日亏损上限 | 超3%停止当日交易 |

## 市场状态分类

| 状态 | 条件 | 风控 |
|------|------|------|
| TRENDING | ADX>25 | 市价单 |
| RANGING | ADX<=25 | 限价单 |
| VOLATILE | ATR比值>1.5 | 仓位减半 |

## 技术指标

| 指标 | 周期 | 用途 |
|------|------|------|
| EMA20/50 | 4h | 趋势方向 |
| RSI14 | 1h | 超买超卖 |
| ATR14 | 4h | 波动衡量+SL/TP |

## 安全建议

- 先用测试网验证逻辑
- API Key 只开合约交易权限
- 止损止盈使用交易所条件单

---

GitHub: https://github.com/Richardxx0/auto-trading-bot