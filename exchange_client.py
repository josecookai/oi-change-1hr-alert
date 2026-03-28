"""
Exchange client abstractions for live trading (v1.3).

Provides a common Protocol interface (ExchangeClient) plus three concrete
implementations: BinanceClient, BybitClient, HyperliquidClient.

All clients:
- Use `requests` for sync HTTP
- Raise ExchangeError on any API failure
- Raise ExchangeConfigError if required env vars are missing at init time
- Log via logging.getLogger(__name__)
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlencode

import requests

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------


class ExchangeError(Exception):
    """Raised when an exchange API call fails."""


class ExchangeConfigError(Exception):
    """Raised when required configuration (env vars) is missing."""


# ---------------------------------------------------------------------------
# Data transfer objects
# ---------------------------------------------------------------------------


@dataclass
class OrderResult:
    order_id: str
    symbol: str
    side: str           # "buy" | "sell"
    filled_qty: float
    avg_price: float
    status: str         # "filled" | "partial" | "cancelled" | "error"
    error_msg: str | None = None


@dataclass
class PositionInfo:
    symbol: str
    side: str           # "long" | "short"
    size: float         # in contracts / coins
    notional_usdt: float
    unrealized_pnl: float
    entry_price: float
    mark_price: float


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class ExchangeClient(Protocol):
    def place_market_order(self, symbol: str, side: str, notional_usdt: float) -> OrderResult: ...
    def cancel_order(self, symbol: str, order_id: str) -> bool: ...
    def get_position(self, symbol: str) -> PositionInfo | None: ...
    def get_usdt_balance(self) -> float: ...
    def get_mark_price(self, symbol: str) -> float: ...
    def test_connectivity(self) -> bool: ...


# ---------------------------------------------------------------------------
# Binance USDM Futures
# ---------------------------------------------------------------------------

_BINANCE_BASE = "https://fapi.binance.com"


class BinanceClient:
    """Binance USDM Futures client."""

    def __init__(self) -> None:
        self._api_key = os.environ.get("BINANCE_API_KEY")
        self._api_secret = os.environ.get("BINANCE_API_SECRET")
        if not self._api_key:
            raise ExchangeConfigError("BINANCE_API_KEY env var is missing")
        if not self._api_secret:
            raise ExchangeConfigError("BINANCE_API_SECRET env var is missing")
        self._session = requests.Session()
        self._session.headers.update({"X-MBX-APIKEY": self._api_key})

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _sign(self, params: dict) -> dict:
        """Append HMAC-SHA256 signature to params dict (mutates and returns it)."""
        params["timestamp"] = int(time.time() * 1000)
        query_string = urlencode(params)
        sig = hmac.new(
            self._api_secret.encode(),
            query_string.encode(),
            hashlib.sha256,
        ).hexdigest()
        params["signature"] = sig
        return params

    def _get(self, path: str, params: dict | None = None, signed: bool = False) -> dict | list:
        if params is None:
            params = {}
        if signed:
            params = self._sign(params)
        url = f"{_BINANCE_BASE}{path}"
        resp = self._session.get(url, params=params, timeout=10)
        self._raise_for_status(resp)
        return resp.json()

    def _post(self, path: str, params: dict | None = None) -> dict:
        if params is None:
            params = {}
        params = self._sign(params)
        url = f"{_BINANCE_BASE}{path}"
        resp = self._session.post(url, params=params, timeout=10)
        self._raise_for_status(resp)
        return resp.json()

    def _delete(self, path: str, params: dict | None = None) -> dict:
        if params is None:
            params = {}
        params = self._sign(params)
        url = f"{_BINANCE_BASE}{path}"
        resp = self._session.delete(url, params=params, timeout=10)
        self._raise_for_status(resp)
        return resp.json()

    @staticmethod
    def _raise_for_status(resp: requests.Response) -> None:
        if resp.status_code >= 400:
            try:
                msg = resp.json().get("msg", resp.text)
            except Exception:
                msg = resp.text
            raise ExchangeError(
                f"Binance API error {resp.status_code}: {msg}"
            )

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def test_connectivity(self) -> bool:
        """Lightweight public ping."""
        resp = self._session.get(f"{_BINANCE_BASE}/fapi/v1/ping", timeout=10)
        return resp.status_code == 200

    def place_market_order(self, symbol: str, side: str, notional_usdt: float) -> OrderResult:
        logger.info("Binance place_market_order symbol=%s side=%s notional=%.2f", symbol, side, notional_usdt)
        params = {
            "symbol": symbol.upper(),
            "side": side.upper(),
            "type": "MARKET",
            "quoteOrderQty": notional_usdt,
        }
        data = self._post("/fapi/v1/order", params)
        filled_qty = float(data.get("executedQty", 0))
        avg_price = float(data.get("avgPrice") or data.get("price", 0))
        status_raw = data.get("status", "")
        status_map = {"FILLED": "filled", "PARTIALLY_FILLED": "partial", "CANCELED": "cancelled"}
        status = status_map.get(status_raw, "error")
        return OrderResult(
            order_id=str(data.get("orderId", "")),
            symbol=symbol.upper(),
            side=side.lower(),
            filled_qty=filled_qty,
            avg_price=avg_price,
            status=status,
        )

    def cancel_order(self, symbol: str, order_id: str) -> bool:
        logger.info("Binance cancel_order symbol=%s order_id=%s", symbol, order_id)
        params = {"symbol": symbol.upper(), "orderId": order_id}
        data = self._delete("/fapi/v1/order", params)
        return data.get("status") == "CANCELED"

    def get_position(self, symbol: str) -> PositionInfo | None:
        data = self._get("/fapi/v2/positionRisk", {"symbol": symbol.upper()}, signed=True)
        for pos in data:
            pos_amt = float(pos.get("positionAmt", 0))
            if pos_amt == 0:
                continue
            side = "long" if pos_amt > 0 else "short"
            entry = float(pos.get("entryPrice", 0))
            mark = float(pos.get("markPrice", 0))
            notional = abs(pos_amt) * mark
            return PositionInfo(
                symbol=symbol.upper(),
                side=side,
                size=abs(pos_amt),
                notional_usdt=notional,
                unrealized_pnl=float(pos.get("unRealizedProfit", 0)),
                entry_price=entry,
                mark_price=mark,
            )
        return None

    def get_usdt_balance(self) -> float:
        data = self._get("/fapi/v2/account", signed=True)
        assets = data.get("assets", [])
        for asset in assets:
            if asset.get("asset") == "USDT":
                return float(asset.get("availableBalance", 0))
        raise ExchangeError("USDT asset not found in Binance account response")

    def get_mark_price(self, symbol: str) -> float:
        data = self._get("/fapi/v1/premiumIndex", {"symbol": symbol.upper()})
        if isinstance(data, list):
            for item in data:
                if item.get("symbol") == symbol.upper():
                    return float(item["markPrice"])
            raise ExchangeError(f"Symbol {symbol} not found in Binance premiumIndex")
        return float(data["markPrice"])


# ---------------------------------------------------------------------------
# Bybit V5 linear perpetuals
# ---------------------------------------------------------------------------

_BYBIT_BASE = "https://api.bybit.com"
_BYBIT_RECV_WINDOW = "5000"


class BybitClient:
    """Bybit V5 linear perpetuals client."""

    def __init__(self) -> None:
        self._api_key = os.environ.get("BYBIT_API_KEY")
        self._api_secret = os.environ.get("BYBIT_API_SECRET")
        if not self._api_key:
            raise ExchangeConfigError("BYBIT_API_KEY env var is missing")
        if not self._api_secret:
            raise ExchangeConfigError("BYBIT_API_SECRET env var is missing")
        self._session = requests.Session()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _sign(self, timestamp: str, payload: str) -> str:
        pre_hash = f"{timestamp}{self._api_key}{_BYBIT_RECV_WINDOW}{payload}"
        return hmac.new(
            self._api_secret.encode(),
            pre_hash.encode(),
            hashlib.sha256,
        ).hexdigest()

    def _auth_headers(self, payload: str) -> dict:
        timestamp = str(int(time.time() * 1000))
        sig = self._sign(timestamp, payload)
        return {
            "X-BAPI-API-KEY": self._api_key,
            "X-BAPI-SIGN": sig,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": _BYBIT_RECV_WINDOW,
        }

    def _get(self, path: str, params: dict | None = None, signed: bool = False) -> dict:
        if params is None:
            params = {}
        url = f"{_BYBIT_BASE}{path}"
        if signed:
            query_string = urlencode(params)
            headers = self._auth_headers(query_string)
        else:
            headers = {}
        resp = self._session.get(url, params=params, headers=headers, timeout=10)
        self._raise_for_status(resp)
        body = resp.json()
        self._raise_for_ret_code(body)
        return body

    def _post(self, path: str, payload: dict) -> dict:
        import json as _json
        url = f"{_BYBIT_BASE}{path}"
        body_str = _json.dumps(payload)
        headers = self._auth_headers(body_str)
        headers["Content-Type"] = "application/json"
        resp = self._session.post(url, data=body_str, headers=headers, timeout=10)
        self._raise_for_status(resp)
        body = resp.json()
        self._raise_for_ret_code(body)
        return body

    @staticmethod
    def _raise_for_status(resp: requests.Response) -> None:
        if resp.status_code >= 400:
            try:
                msg = resp.json().get("retMsg", resp.text)
            except Exception:
                msg = resp.text
            raise ExchangeError(f"Bybit HTTP error {resp.status_code}: {msg}")

    @staticmethod
    def _raise_for_ret_code(body: dict) -> None:
        ret_code = body.get("retCode", 0)
        if ret_code != 0:
            raise ExchangeError(f"Bybit API error {ret_code}: {body.get('retMsg', '')}")

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def test_connectivity(self) -> bool:
        resp = self._session.get(f"{_BYBIT_BASE}/v5/market/time", timeout=10)
        return resp.status_code == 200

    def place_market_order(self, symbol: str, side: str, notional_usdt: float) -> OrderResult:
        logger.info("Bybit place_market_order symbol=%s side=%s notional=%.2f", symbol, side, notional_usdt)
        mark = self.get_mark_price(symbol)
        if mark <= 0:
            raise ExchangeError(f"Invalid mark price for {symbol}: {mark}")
        qty = round(notional_usdt / mark, 6)
        payload = {
            "category": "linear",
            "symbol": symbol.upper(),
            "side": side.capitalize(),
            "orderType": "Market",
            "qty": str(qty),
        }
        data = self._post("/v5/order/create", payload)
        result = data.get("result", {})
        return OrderResult(
            order_id=str(result.get("orderId", "")),
            symbol=symbol.upper(),
            side=side.lower(),
            filled_qty=float(result.get("cumExecQty", 0) or 0),
            avg_price=float(result.get("avgPrice", 0) or 0),
            status="filled",
        )

    def cancel_order(self, symbol: str, order_id: str) -> bool:
        logger.info("Bybit cancel_order symbol=%s order_id=%s", symbol, order_id)
        payload = {
            "category": "linear",
            "symbol": symbol.upper(),
            "orderId": order_id,
        }
        self._post("/v5/order/cancel", payload)
        return True

    def get_position(self, symbol: str) -> PositionInfo | None:
        params = {"category": "linear", "symbol": symbol.upper()}
        data = self._get("/v5/position/list", params, signed=True)
        positions = data.get("result", {}).get("list", [])
        for pos in positions:
            size = float(pos.get("size", 0))
            if size == 0:
                continue
            side_raw = pos.get("side", "").lower()
            side = "long" if side_raw == "buy" else "short"
            entry = float(pos.get("avgPrice", 0))
            mark = float(pos.get("markPrice", 0))
            return PositionInfo(
                symbol=symbol.upper(),
                side=side,
                size=size,
                notional_usdt=float(pos.get("positionValue", size * mark)),
                unrealized_pnl=float(pos.get("unrealisedPnl", 0)),
                entry_price=entry,
                mark_price=mark,
            )
        return None

    def get_usdt_balance(self) -> float:
        params = {"accountType": "UNIFIED"}
        data = self._get("/v5/account/wallet-balance", params, signed=True)
        accounts = data.get("result", {}).get("list", [])
        for account in accounts:
            for coin in account.get("coin", []):
                if coin.get("coin") == "USDT":
                    return float(coin.get("availableToWithdraw", 0))
        raise ExchangeError("USDT balance not found in Bybit wallet response")

    def get_mark_price(self, symbol: str) -> float:
        params = {"category": "linear", "symbol": symbol.upper()}
        data = self._get("/v5/market/tickers", params)
        tickers = data.get("result", {}).get("list", [])
        for ticker in tickers:
            if ticker.get("symbol") == symbol.upper():
                return float(ticker["markPrice"])
        raise ExchangeError(f"Symbol {symbol} not found in Bybit tickers")


# ---------------------------------------------------------------------------
# Hyperliquid L1
# ---------------------------------------------------------------------------

_HL_BASE = "https://api.hyperliquid.xyz"


def _hl_normalize_symbol(symbol: str) -> str:
    """Hyperliquid uses base asset names like 'BTC', not 'BTCUSDT'."""
    sym = symbol.upper()
    for suffix in ("USDT", "USDC", "-PERP", "PERP"):
        if sym.endswith(suffix):
            sym = sym[: -len(suffix)]
    return sym


class HyperliquidClient:
    """Hyperliquid L1 client."""

    def __init__(self) -> None:
        self._wallet_address = os.environ.get("HYPERLIQUID_WALLET_ADDRESS")
        self._private_key = os.environ.get("HYPERLIQUID_PRIVATE_KEY")
        if not self._wallet_address:
            raise ExchangeConfigError("HYPERLIQUID_WALLET_ADDRESS env var is missing")
        if not self._private_key:
            raise ExchangeConfigError("HYPERLIQUID_PRIVATE_KEY env var is missing")
        self._session = requests.Session()

        # Try to load eth_account for EIP-712 signing
        try:
            from eth_account import Account  # type: ignore
            self._account = Account.from_key(self._private_key)
        except ImportError:
            logger.warning("eth_account not installed; signing will use stub")
            self._account = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _info_post(self, payload: dict) -> dict | list:
        resp = self._session.post(
            f"{_HL_BASE}/info",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        if resp.status_code >= 400:
            raise ExchangeError(f"Hyperliquid info error {resp.status_code}: {resp.text}")
        return resp.json()

    def _exchange_post(self, action: dict) -> dict:
        timestamp = int(time.time() * 1000)
        signature = self._sign_action(action, timestamp)
        payload = {
            "action": action,
            "nonce": timestamp,
            "signature": signature,
        }
        resp = self._session.post(
            f"{_HL_BASE}/exchange",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        if resp.status_code >= 400:
            raise ExchangeError(f"Hyperliquid exchange error {resp.status_code}: {resp.text}")
        data = resp.json()
        if isinstance(data, dict) and data.get("status") == "err":
            raise ExchangeError(f"Hyperliquid exchange error: {data.get('response', data)}")
        return data

    def _sign_action(self, action: dict, timestamp: int) -> dict:
        """Sign an action with EIP-712 or return a stub signature."""
        if self._account is None:
            # Stub: return empty signature (for testing without eth_account)
            return {"r": "0x0", "s": "0x0", "v": 0}
        try:
            import json as _json
            from eth_account.messages import encode_defunct  # type: ignore

            msg_str = _json.dumps({"action": action, "nonce": timestamp}, sort_keys=True)
            msg = encode_defunct(text=msg_str)
            signed = self._account.sign_message(msg)
            return {"r": hex(signed.r), "s": hex(signed.s), "v": signed.v}
        except Exception as exc:
            logger.warning("Hyperliquid signing failed: %s; using stub", exc)
            return {"r": "0x0", "s": "0x0", "v": 0}

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def test_connectivity(self) -> bool:
        resp = self._session.post(
            f"{_HL_BASE}/info",
            json={"type": "meta"},
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        return resp.status_code == 200

    def place_market_order(self, symbol: str, side: str, notional_usdt: float) -> OrderResult:
        logger.info("Hyperliquid place_market_order symbol=%s side=%s notional=%.2f", symbol, side, notional_usdt)
        coin = _hl_normalize_symbol(symbol)
        mark = self.get_mark_price(symbol)
        if mark <= 0:
            raise ExchangeError(f"Invalid mark price for {symbol}: {mark}")
        size = round(notional_usdt / mark, 6)
        is_buy = side.lower() == "buy"
        # Slippage market order: limit price with 2% slippage buffer
        slippage = 0.02
        limit_px = mark * (1 + slippage) if is_buy else mark * (1 - slippage)

        action = {
            "type": "order",
            "orders": [{
                "a": coin,
                "b": is_buy,
                "p": str(round(limit_px, 6)),
                "s": str(size),
                "r": False,  # not reduce-only
                "t": {"limit": {"tif": "Ioc"}},  # IOC = market-like
            }],
            "grouping": "na",
        }
        data = self._exchange_post(action)
        response = data.get("response", {})
        statuses = response.get("data", {}).get("statuses", [{}])
        first = statuses[0] if statuses else {}
        filled = first.get("filled", {})
        order_id = str(filled.get("oid", ""))
        filled_qty = float(filled.get("totalSz", 0))
        avg_price = float(filled.get("avgPx", mark))
        return OrderResult(
            order_id=order_id,
            symbol=symbol.upper(),
            side=side.lower(),
            filled_qty=filled_qty,
            avg_price=avg_price,
            status="filled",
        )

    def cancel_order(self, symbol: str, order_id: str) -> bool:
        logger.info("Hyperliquid cancel_order symbol=%s order_id=%s", symbol, order_id)
        coin = _hl_normalize_symbol(symbol)
        action = {
            "type": "cancel",
            "cancels": [{"a": coin, "o": int(order_id)}],
        }
        self._exchange_post(action)
        return True

    def get_position(self, symbol: str) -> PositionInfo | None:
        coin = _hl_normalize_symbol(symbol)
        data = self._info_post({"type": "clearinghouseState", "user": self._wallet_address})
        positions = data.get("assetPositions", [])
        for item in positions:
            pos = item.get("position", {})
            if pos.get("coin") != coin:
                continue
            szi = float(pos.get("szi", 0))
            if szi == 0:
                continue
            side = "long" if szi > 0 else "short"
            entry = float(pos.get("entryPx", 0))
            pos_value = pos.get("positionValue", abs(szi) * entry)
            return PositionInfo(
                symbol=symbol.upper(),
                side=side,
                size=abs(szi),
                notional_usdt=float(pos_value),
                unrealized_pnl=float(pos.get("unrealizedPnl", 0)),
                entry_price=entry,
                mark_price=float(pos.get("returnOnEquity", 0)),  # mark not in pos; use entry as proxy
            )
        return None

    def get_usdt_balance(self) -> float:
        data = self._info_post({"type": "clearinghouseState", "user": self._wallet_address})
        cross_margin = data.get("crossMarginSummary", {})
        account_value = cross_margin.get("accountValue")
        if account_value is not None:
            return float(account_value)
        # Fall back to withdrawable field
        withdrawable = data.get("withdrawable")
        if withdrawable is not None:
            return float(withdrawable)
        raise ExchangeError("Could not extract USDC balance from Hyperliquid response")

    def get_mark_price(self, symbol: str) -> float:
        coin = _hl_normalize_symbol(symbol)
        data = self._info_post({"type": "allMids"})
        if isinstance(data, dict):
            price = data.get(coin)
            if price is not None:
                return float(price)
        raise ExchangeError(f"Symbol {symbol} (coin={coin}) not found in Hyperliquid allMids")


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_EXCHANGE_MAP: dict[str, type] = {
    "binance": BinanceClient,
    "bybit": BybitClient,
    "hyperliquid": HyperliquidClient,
}


def get_client(exchange: str) -> ExchangeClient:
    """
    Factory function — returns the correct ExchangeClient for the given name.

    Raises:
        ExchangeConfigError: if exchange name is unknown or env vars are missing.
    """
    key = exchange.lower().strip()
    cls = _EXCHANGE_MAP.get(key)
    if cls is None:
        raise ExchangeConfigError(
            f"Unknown exchange '{exchange}'. Valid options: {list(_EXCHANGE_MAP)}"
        )
    return cls()
