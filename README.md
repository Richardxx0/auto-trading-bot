# YSS Signal Trading Bot

监听 Telegram YSS 信号群，自动在 Binance Futures 执行"首次买入信号"。

## Flow

```mermaid
flowchart TD
    TG[Telegram 新消息] --> P[SignalParser<br/>匹配 "首次买入信号"]
    P -- 未匹配 --> SKIP[忽略]
    P -- 匹配成功 --> TYP[检查 signal_type]
    TYP -- 非 LONG --> SKIP
    TYP -- LONG --> M[映射 coin → 合约名<br/>CLO → CLOUSDT]
    M --> EX[ExchangeClient<br/>检查合约是否存在]
    EX -- 不存在 --> SKIP
    EX -- 存在 --> DEDUP{已处理过?}
    DEDUP -- 是(in-memory) --> SKIP
    DEDUP -- 否 --> POS[ExchangeClient<br/>检查是否已持仓]
    POS -- 已持仓 --> SKIP
    POS -- 未持仓 --> PRICE[获取入场价<br/>消息价格 / 实时市价]
    PRICE --> BAL[获取账户 USDT 余额]
    BAL --> R[RiskManager<br/>计算仓位 & SL/TP]
    R --> OPEN[市价开多]
    OPEN --> SL[设置止损 STOP_MARKET -2%]
    SL --> TP[设置止盈 TAKE_PROFIT_MARKET +4%]
    TP --> DONE[完成 ✓]
```

## Project Structure

```
auto-trading-bot/
├── config/
│   ├── __init__.py
│   └── settings.py          # .env 加载 + Config dataclass
├── src/
│   ├── __init__.py
│   ├── parser.py            # 信号解析: 关键词 + 币种 + 价格提取
│   ├── exchange.py          # Binance USDⓈ-M Futures (ccxt)
│   ├── risk_manager.py      # 固定风险: 2% / 止损 2% / 止盈 4%
│   ├── telegram_listener.py # Telethon 异步监听 + 自动重连
│   ├── signal_handler.py    # 流程编排 + 内存去重
├── main.py                  # 入口
├── .env.example
├── requirements.txt
└── README.md
```

## Quick Start

### 1. Install

```bash
cd auto-trading-bot
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
```

Edit `.env` with your credentials:

| Variable | Description |
|----------|-------------|
| `TELEGRAM_API_ID` | Telegram API ID (https://my.telegram.org/apps) |
| `TELEGRAM_API_HASH` | Telegram API Hash |
| `TELEGRAM_PHONE` | Phone number linked to Telegram account |
| `TELEGRAM_CHANNEL` | Target channel (@username or ID; blank = all dialogs) |
| `BINANCE_API_KEY` | Binance API Key (futures permissions only) |
| `BINANCE_SECRET_KEY` | Binance Secret Key |
| `BINANCE_TESTNET` | `true` = testnet, `false` = live (default: `true`) |
| `RISK_PER_TRADE` | Fraction of balance risked per trade (default `0.02` = 2%) |
| `STOP_LOSS_PCT` | Stop-loss percentage (default `0.02` = 2%) |
| `TAKE_PROFIT_PCT` | Take-profit percentage (default `0.04` = 4%, 1:2 R:R) |
| `LEVERAGE` | Leverage (default `5`) |

### 3. Run

```bash
python main.py
```

First run prompts for a Telegram verification code (entered in terminal).

### 4. Custom Coin Mapping

Edit `config/settings.py` → `Config.coin_mapping` dict, or override at runtime.

## Module Details

| Module | Responsibility |
|--------|---------------|
| `config/settings.py` | Loads `.env` or system env; validates required keys |
| `src/parser.py` | Regex matches "首次买入信号", extracts coin symbol + optional price |
| `src/exchange.py` | Wraps ccxt `binanceusdm`; balance, price, position query, market long, SL/TP |
| `src/risk_manager.py` | Risk = balance × 2%; qty = risk / (entry × 2%); SL = entry × 0.98; TP = entry × 1.04 |
| `src/telegram_listener.py` | Telethon with auto-reconnect (exponential backoff 5s→120s) |
| `src/signal_handler.py` | Orchestrates parse → map → check → calc → execute; in-memory dedup set |

## Safety

- **Always start with `BINANCE_TESTNET=true`** to verify logic against Binance testnet
- API keys should only have **Futures trading** permission (disable withdrawal)
- SL/TP use Binance `STOP_MARKET` / `TAKE_PROFIT_MARKET` (server-side, survive disconnect)
- In-memory dedup prevents re-processing a symbol even if the exchange order fails
- All exceptions are caught and logged; the bot keeps running
