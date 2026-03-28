# OI Monitor v1.2 — Cross-Exchange Arbitrage Paper Trading

## Overview

基于 v1.1 的套利信号，模拟开仓/平仓、追踪累计收益，评估策略可行性。

---

## v1.1 功能（前置依赖）

### 套利机会检测

**数据来源**：WebSocket `wss://monitor.vniu.ai/ws`，字段：
- `funding_rate` — 当前资金费率（小数形式，0.001 = 0.1%）
- `mark_price` — 标记价格
- `funding_interval_hours` — 资金费率结算周期（通常 8h）
- `next_funding_time` — 下次结算时间

**套利逻辑**：

| 场景 | 条件 | 操作 |
|------|------|------|
| 资金费率套利 | 两交易所同一合约 funding_rate 差值 > 阈值 | 在高 funding 交易所做空，低 funding 交易所做多 |
| 价格套利 | mark_price 差值 > 阈值 | 在高价交易所做空，低价交易所做多 |

**每次结算收益**（delta-neutral）：

```
profit_per_funding = (rate_short_exchange - rate_long_exchange) * position_size
```

**消息格式**（Telegram 推送）：

```
🔀 Arb Alert — 2026-03-28 14:00 UTC

Symbol  Long@     Short@    Spread   Est 8h   Annual
AXSUSDT HL(-0.02%) BYBIT(-0.32%) 0.29%  +$29  +127%
EIGENUSDT HL(-0.01%) BYBIT(-0.15%) 0.13%  +$13   +57%
...
```

---

## v1.2 功能

### Paper Trading 模块

#### 开仓条件
- funding rate spread ≥ `MIN_ARB_SPREAD`（默认 0.05% / 8h）
- 两个交易所均有该合约
- 合约未已持仓

#### 平仓条件
- spread 收窄至 < `CLOSE_ARB_SPREAD`（默认 0.01% / 8h）
- 持仓超过 `MAX_HOLD_HOURS`（默认 72h）
- 手动平仓指令

#### 费率模型

| 交易所 | Maker 费率 | Taker 费率 |
|--------|-----------|-----------|
| Binance | 0.02% | 0.05% |
| Bybit | 0.02% | 0.055% |
| Hyperliquid | -0.01%（返佣） | 0.035% |

开仓成本（双边各一次，假设 maker）：

```
entry_fee = (maker_fee_long + maker_fee_short) * position_size
exit_fee  = (maker_fee_long + maker_fee_short) * position_size
total_fee = entry_fee + exit_fee
```

#### 收益计算

```
funding_received = spread_per_8h * n_funding_periods * position_size
net_pnl = funding_received - total_fee
annualized_roi = net_pnl / position_size * (8760 / hold_hours)
```

#### 数据结构

```python
@dataclass
class PaperPosition:
    symbol: str
    long_exchange: str
    short_exchange: str
    entry_spread: float       # funding rate spread at open
    entry_time: datetime
    position_size_usdt: float
    funding_collected: float  # cumulative funding received
    fee_paid: float
    status: str               # "open" | "closed"
    close_reason: str | None
    close_time: datetime | None
```

#### 持仓快照（JSON）

```json
{
  "positions": [...],
  "total_pnl": 1234.56,
  "total_fee": 45.67,
  "win_rate": 0.73,
  "avg_hold_hours": 18.5
}
```

---

## Telegram 推送格式

### 套利机会推送（v1.1，每小时）

```
🔀 *Arb Opportunities* — 2026-03-28 14:00 UTC

`Symbol       Long@Ex  Short@Ex  Spread  $/10k/8h  Annual`
`AXSUSDT      HL       BYBIT     0.29%   +$29      +319%`
`EIGENUSDT    HL       BYBIT     0.13%   +$13      +143%`
`PIXELUSDT    BINANCE  BYBIT     0.09%    +$9       +99%`
```

### 持仓快照推送（v1.2，每 8h）

```
📒 *Paper Trade Update* — 2026-03-28 16:00 UTC

开仓中 (3)：
AXSUSDT  HL↗/BYBIT↘  持仓 4h  已收 0.14%  净盈 +$12.3
EIGENUSDT HL↗/BYBIT↘ 持仓 2h  已收 0.07%  净盈  +$5.1
...

今日汇总：开仓 5 次 | 平仓 2 次 | 累计净盈 +$34.7
```

---

## 配置参数

```env
# v1.1
MIN_ARB_SPREAD=0.0005       # 最小套利 spread（0.05%/8h）
ARB_TOP_N=5                 # 推送 Top N 套利机会

# v1.2
PAPER_POSITION_SIZE=10000   # 模拟仓位大小（USDT）
CLOSE_ARB_SPREAD=0.0001     # 平仓 spread 阈值
MAX_HOLD_HOURS=72           # 最大持仓时间
PAPER_TRADE_FILE=paper_positions.json
```

---

## 项目结构（v1.2 完成后）

```
oi-change-1hr-alert/
├── main.py
├── ws_client.py
├── analyzer.py
├── arb_detector.py       # [v1.1] 套利机会检测
├── paper_trader.py       # [v1.2] 模拟开平仓 + 收益追踪
├── formatter.py          # 更新：增加套利 + 持仓格式
├── telegram_bot.py
├── config.py
├── paper_positions.json  # [v1.2] 持仓状态（运行时生成）
└── requirements.txt
```

---

## 开发顺序

### v1.1
1. `arb_detector.py` — 找共同合约，计算 funding spread，排序
2. `formatter.py` — 新增 `build_arb_section()`
3. `main.py` — 在每小时推送中加入套利部分
4. 测试推送

### v1.2
1. `config.py` — 新增 paper trade 配置
2. `paper_trader.py` — 开仓/更新/平仓逻辑
3. `formatter.py` — 新增 `build_paper_snapshot()`
4. `main.py` — 每 8h 触发 paper trade 更新推送
5. 测试持仓追踪

---

## 风险说明（Paper Trading 局限性）

- 不考虑滑点和流动性深度
- 不考虑爆仓风险（delta-neutral 理论上风险极低）
- funding rate 可能在结算前改变
- mark price 差异可能导致强平
