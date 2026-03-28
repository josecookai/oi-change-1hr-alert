# OI Change Alert — 实现方案

每小时推送加密货币期货市场 OI 异动 Top 5，数据来源 [crypto.vniu.ai](https://crypto.vniu.ai)，通过 Telegram Bot 发送。

---

## 数据来源

| 项目 | 说明 |
|------|------|
| WebSocket | `wss://monitor.vniu.ai/ws` |
| 数据结构 | `{ binance, bybit, hyperliquid, combined, timestamp }` |
| 使用字段 | `combined`（跨交易所汇总） |

### 每个合约包含的字段

| 字段 | 含义 |
|------|------|
| `symbol` | 交易对，如 `BTCUSDT` |
| `oi_usdt` | 持仓量（USDT） |
| `oi_usdt_change_15m` | 15m OI 变化 |
| `oi_usdt_change_1h` | 1h OI 变化 |
| `oi_usdt_change_4h` | 4h OI 变化 |
| `oi_usdt_change_24h` | 24h OI 变化 |
| `price_change_24h` | 24h 价格变化 |
| `funding_rate` | 当前资金费率 |

---

## 消息样式

```
📊 OI Alert — 2026-03-28 14:00 UTC

⏱ 15m Top 5 OI Change
#  Symbol     OI($M)   OI Chg   CoinChg  FundRate
1  BTCUSDT   12,340   +5.2%    +1.3%    0.012%
2  ETHUSDT    5,210   +4.1%    +0.8%    0.008%
3  SOLUSDT    1,820   +3.7%    -0.5%    0.015%
4  DOGEUSDT     430   -3.2%    -1.1%   -0.003%
5  BNBUSDT      610   +2.9%    +0.6%    0.010%

⏱ 1h Top 5 OI Change
...

⏱ 4h Top 5 OI Change
...

⏱ 24h Top 5 OI Change
...
```

> 排序方式：按 OI 变化**正向降序**（只看 OI 增加最多的合约）

---

## 项目结构

```
oi-change-1hr-alert/
├── main.py           # 入口：启动 WebSocket + 调度器
├── ws_client.py      # WebSocket 客户端，维护最新数据快照
├── analyzer.py       # Top 5 筛选逻辑（4 个时间维度）
├── formatter.py      # Telegram 消息格式化
├── telegram_bot.py   # Telegram Bot 发送封装
├── config.py         # 环境变量读取
├── requirements.txt  # 依赖
└── .env.example      # 环境变量模板
```

---

## 核心模块说明

### `ws_client.py` — WebSocket 客户端

- 连接 `wss://monitor.vniu.ai/ws`
- 持续接收数据，维护 `latest_data` 内存快照
- 断线自动重连，指数退避（1s → 30s）
- 后台线程运行，不阻塞调度器

### `analyzer.py` — 数据分析

- 从 `latest_data["combined"]` 取全量合约
- 针对 15m / 1h / 4h / 24h 各取 Top 5
- 按 `oi_usdt_change_{tf}` **正向**降序排列（只选 OI 增加的合约）

### `formatter.py` — 消息格式化

- 统一格式：Symbol / OI(M) / OI Chg% / CoinChg% / FundRate%
- 正值绿色箭头 ▲，负值红色箭头 ▼（Telegram Markdown）
- 时间戳使用 UTC

### `telegram_bot.py` — Telegram 推送

- 使用 `python-telegram-bot` 异步发送
- 支持 Markdown 格式
- 发送失败自动 retry 3 次

### `main.py` — 入口与调度

- 启动时立即建立 WebSocket 连接
- `APScheduler` 每小时整点触发 `send_alert()`
- 推送前检查 `latest_data` 是否有效（非空且 timestamp 在 5 分钟内）

---

## 技术栈

```
Python 3.11+
websockets==12.0
python-telegram-bot==21.5
APScheduler==3.10.4
python-dotenv==1.0.1
```

---

## 环境变量

```env
# .env.example
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_CHAT_ID=your_chat_id_here        # 群组或个人 chat_id
WS_URL=wss://monitor.vniu.ai/ws
TOP_N=5
PUSH_INTERVAL_HOURS=1
```

---

## 部署方式

### 本地 / VPS

```bash
git clone https://github.com/josecookai/oi-change-1hr-alert.git
cd oi-change-1hr-alert
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env 填入 token
python main.py
```

### Docker

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "main.py"]
```

```bash
docker build -t oi-alert .
docker run -d --env-file .env --name oi-alert oi-alert
```

### GitHub Actions（定时触发备选）

若不想维护常驻进程，可用 GitHub Actions `schedule` 每小时触发一次脚本：

```yaml
on:
  schedule:
    - cron: '0 * * * *'
```

---

## 开发顺序

1. `config.py` — 环境变量
2. `ws_client.py` — WebSocket 连接与数据缓存
3. `analyzer.py` — Top 5 逻辑
4. `formatter.py` — 消息格式
5. `telegram_bot.py` — 发送
6. `main.py` — 组装 + 调度
7. 本地测试推送一次，确认格式
8. 部署到 VPS / Docker

---

## 待确认事项

- [ ] Telegram Bot Token 和 Chat ID
- [x] 排序只看正向（OI 增加最多）
- [x] 使用 `combined`（全交易所汇总数据）
- [ ] 是否需要过滤掉小市值合约（如设置 OI > $10M 门槛）
