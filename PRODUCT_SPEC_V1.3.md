# Product Spec — v1.3: Live Trade Execution

**Status:** Planning
**Date:** 2026-03-28
**Scope:** Execute real delta-neutral funding rate arbitrage on Binance, Bybit, Hyperliquid

---

## Context

| Version | Purpose |
|---------|---------|
| v1.1 | Cross-exchange funding rate arb detection |
| v1.2 | Paper trading — simulate positions, track P&L |
| **v1.3** | **Live execution — open/close real positions via exchange APIs** |

---

## Objective

Automatically open and close delta-neutral positions on two exchanges when the funding rate spread exceeds a configurable threshold, collect funding payments, and close positions when the spread collapses or a risk limit is hit.

**Strategy recap:**
Long perp on exchange A (pays lowest/negative funding) + Short perp on exchange B (pays highest funding).
Net per interval ≈ spread × notional − round-trip fees.

---

## Key Decisions

### 1. Order Type
- **Market orders only** at v1.3 — slippage already modeled in v1.2
- Limit orders (post-only) deferred to v1.4
- Both legs submitted near-simultaneously; partial fill handled by auto-cancel + retry

### 2. Position Sizing
- Fixed notional per pair: `LIVE_POSITION_SIZE` (env, default $500 USDT)
- Max concurrent live positions: `MAX_LIVE_POSITIONS` (env, default 3)
- Single-side max exposure: `MAX_SINGLE_EXCHANGE_EXPOSURE` (env, default $2000)

### 3. Entry Conditions (all must pass)
1. `spread >= LIVE_MIN_SPREAD` (default 0.20% per 8h — ~9% annualized)
2. `net_per_10k_per_interval > 0` (spread > round-trip fees)
3. `net_per_10k_after_slippage > 0` (slippage-enriched check)
4. `min(long_oi_usdt, short_oi_usdt) >= LIVE_MIN_OI` (default $5M — liquidity check)
5. No existing open position for this symbol
6. `MAX_LIVE_POSITIONS` not reached

### 4. Exit Conditions
| Trigger | Condition |
|---------|-----------|
| Spread collapse | `current_spread < LIVE_CLOSE_SPREAD` (default 0.02%) |
| Max hold | `hold_hours >= LIVE_MAX_HOLD_HOURS` (default 72h) |
| Emergency stop | Manual `/stop <symbol>` Telegram command |
| Circuit breaker | Unrealized loss > `MAX_LOSS_PER_POSITION` (default $30 on $500) |

### 5. Execution Flow
```
detect() → entry_check() → [simultaneous market orders on both legs]
         → on fill: record LivePosition → monitor loop (every 15m)
         → exit_check() → [close both legs] → record closed
```

### 6. Leg Sync Strategy
- Submit both legs within 500ms window
- If one leg fills and the other fails: auto-close the filled leg immediately (hedge protection)
- Alert via Telegram on any partial fill situation

---

## New Components

### `exchange_client.py`
Unified interface over exchange REST APIs:

```python
class ExchangeClient(Protocol):
    def place_market_order(symbol, side, notional) -> OrderResult
    def cancel_order(symbol, order_id) -> None
    def get_position(symbol) -> PositionInfo | None
    def get_balance() -> float  # USDT available
```

Implementations: `BinanceClient`, `BybitClient`, `HyperliquidClient`

### `live_trader.py`
Orchestrates entry/exit, mirrors `paper_trader.py` structure:

```python
class LiveTrader:
    def scan(opportunities) -> list[LivePosition]   # open new positions
    def monitor(opportunities) -> list[LivePosition] # check exit conditions
    def close_position(pos, reason) -> ClosedPosition
    def emergency_stop(symbol) -> None
    def snapshot() -> dict  # stats for dashboard + Telegram
```

Persistence: SQLite (`live_positions.db`) — not JSON, for concurrent-safe reads.

### `live_positions.db` Schema
```sql
CREATE TABLE live_positions (
    id              TEXT PRIMARY KEY,  -- uuid
    symbol          TEXT NOT NULL,
    long_exchange   TEXT NOT NULL,
    short_exchange  TEXT NOT NULL,
    entry_spread    REAL NOT NULL,
    long_order_id   TEXT,
    short_order_id  TEXT,
    long_fill_price REAL,
    short_fill_price REAL,
    notional_usdt   REAL NOT NULL,
    status          TEXT NOT NULL,     -- open | closed | error
    entry_time      INTEGER NOT NULL,  -- unix epoch
    close_time      INTEGER,
    close_reason    TEXT,
    funding_collected REAL DEFAULT 0,
    fee_paid        REAL NOT NULL,
    close_pnl       REAL
);
```

### `risk_manager.py`
Pre-flight checks before any order is submitted:

```python
def pre_entry_check(opp, current_positions, balances) -> CheckResult
def pre_exit_check(pos, current_spread) -> CheckResult
```

Checks: balance sufficiency, exposure limits, exchange API key scopes, circuit breaker state.

---

## Config (new env vars)

```env
# Live trading — off by default
LIVE_TRADING_ENABLED=false          # must be explicitly set to true

# Sizing
LIVE_POSITION_SIZE=500              # USDT per position
MAX_LIVE_POSITIONS=3
MAX_SINGLE_EXCHANGE_EXPOSURE=2000   # USDT total long or short on one exchange

# Entry thresholds
LIVE_MIN_SPREAD=0.002               # 0.20% per interval
LIVE_MIN_OI=5000000                 # $5M minimum OI on each leg

# Exit thresholds
LIVE_CLOSE_SPREAD=0.0002            # 0.02% — collapse
LIVE_MAX_HOLD_HOURS=72
MAX_LOSS_PER_POSITION=30            # USDT unrealized loss circuit breaker

# Exchange API keys (per exchange)
BINANCE_API_KEY=
BINANCE_API_SECRET=
BYBIT_API_KEY=
BYBIT_API_SECRET=
HYPERLIQUID_WALLET_ADDRESS=
HYPERLIQUID_PRIVATE_KEY=
```

---

## Safety Architecture

### Hard Guards
1. `LIVE_TRADING_ENABLED=false` by default — must opt in explicitly
2. All orders logged to audit table before submission (`live_order_log`)
3. Paper trading continues in parallel — live and paper run independently
4. Emergency stop via Telegram `/stop <symbol>` closes both legs immediately
5. No leverage — positions are 1x (delta-neutral, not leveraged carry)

### Circuit Breakers
- If unrealized loss on any position exceeds `MAX_LOSS_PER_POSITION` → close immediately
- If exchange API returns error 3 times in a row → disable live trading, alert
- If funding rate sign flips (long exchange now paying more than short) → flag for review, do not auto-close unless loss threshold hit

### Audit Log
Every order submitted → `live_order_log` table:
```
ts, symbol, exchange, side, order_type, notional, order_id, status, fill_price, error_msg
```

---

## Telegram Commands (v1.3 additions)

| Command | Description |
|---------|-------------|
| `/live` | Show open live positions + unrealized P&L |
| `/live history` | Last 10 closed positions |
| `/stop <SYMBOL>` | Emergency close both legs for symbol |
| `/exposure` | Current exchange exposure summary |
| `/enable` | Enable live trading (requires confirmation) |
| `/disable` | Disable live trading (no open positions affected) |

---

## Dashboard (v1.3 additions)

- New "Live Positions" tab alongside Paper Trade tab
- Per-position: symbol, direction, entry spread, current spread, unrealized P&L, hold time
- Exchange exposure bar chart (long/short USDT per exchange vs limit)
- Live trading toggle (enable/disable without restarting)
- Order audit log table (last 50 orders)

---

## Development Phases

### Phase 1 — Exchange Clients (Week 1)
- [ ] `exchange_client.py` — Protocol + 3 implementations
- [ ] Unit tests with mocked HTTP responses
- [ ] Manual test: place + cancel a 0-value limit order on testnet

### Phase 2 — Live Trader Core (Week 1-2)
- [ ] `live_trader.py` — scan, monitor, close_position
- [ ] SQLite persistence (`live_positions.db`)
- [ ] `risk_manager.py` — pre-flight checks
- [ ] Integration tests (testnet)

### Phase 3 — Safety + Alerts (Week 2)
- [ ] Circuit breaker implementation
- [ ] Telegram commands: `/live`, `/stop`, `/exposure`, `/enable`, `/disable`
- [ ] Emergency stop flow (leg already filled → immediate hedge close)
- [ ] Audit log

### Phase 4 — Dashboard + Hardening (Week 2-3)
- [ ] Dashboard Live Positions tab
- [ ] Exposure chart
- [ ] End-to-end test on testnet with real API keys
- [ ] Code review + security review

### Phase 5 — Production (Week 3)
- [ ] Set `LIVE_TRADING_ENABLED=true` with small size ($100/position)
- [ ] Monitor for 48h, verify funding credits match expected
- [ ] Increase position size to target

---

## Risks & Mitigations

| Risk | Mitigation |
|------|-----------|
| One leg fills, other fails | Auto-close filled leg within 500ms |
| Exchange API downtime | Fallback: alert only, no new entries; existing positions monitored via REST polling |
| Funding rate sign flip mid-hold | Circuit breaker on unrealized loss; flag for review |
| API key compromise | Withdraw-only permission not needed; trade-only keys |
| Slippage worse than modeled | Use real-time orderbook check at entry; reject if slippage > 0.5x spread |
| Over-exposure on one exchange | `MAX_SINGLE_EXCHANGE_EXPOSURE` hard cap checked before every entry |

---

## Out of Scope (v1.3)

- Limit / post-only orders (v1.4)
- Automatic position rebalancing
- Cross-collateral / portfolio margin optimization
- Tax reporting
- Multi-account support
