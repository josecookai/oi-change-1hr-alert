"""
Tests for exchange_client.py — all HTTP calls are mocked.
"""
from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from exchange_client import (
    BinanceClient,
    BybitClient,
    ExchangeConfigError,
    ExchangeError,
    HyperliquidClient,
    OrderResult,
    PositionInfo,
    get_client,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_response(status_code: int, body) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = body
    resp.text = json.dumps(body) if not isinstance(body, str) else body
    return resp


def _binance_env(monkeypatch):
    monkeypatch.setenv("BINANCE_API_KEY", "test_key")
    monkeypatch.setenv("BINANCE_API_SECRET", "test_secret")


def _bybit_env(monkeypatch):
    monkeypatch.setenv("BYBIT_API_KEY", "test_key")
    monkeypatch.setenv("BYBIT_API_SECRET", "test_secret")


def _hl_env(monkeypatch):
    monkeypatch.setenv("HYPERLIQUID_WALLET_ADDRESS", "0xdeadbeef")
    monkeypatch.setenv("HYPERLIQUID_PRIVATE_KEY", "0x" + "a" * 64)


# ===========================================================================
# BinanceClient tests
# ===========================================================================


class TestBinanceClientConfig:
    def test_raises_if_api_key_missing(self, monkeypatch):
        monkeypatch.delenv("BINANCE_API_KEY", raising=False)
        monkeypatch.setenv("BINANCE_API_SECRET", "s")
        with pytest.raises(ExchangeConfigError, match="BINANCE_API_KEY"):
            BinanceClient()

    def test_raises_if_api_secret_missing(self, monkeypatch):
        monkeypatch.setenv("BINANCE_API_KEY", "k")
        monkeypatch.delenv("BINANCE_API_SECRET", raising=False)
        with pytest.raises(ExchangeConfigError, match="BINANCE_API_SECRET"):
            BinanceClient()

    def test_initialises_with_valid_env(self, monkeypatch):
        _binance_env(monkeypatch)
        client = BinanceClient()
        assert client is not None


class TestBinancePlaceMarketOrder:
    def test_returns_order_result_on_success(self, monkeypatch):
        _binance_env(monkeypatch)
        client = BinanceClient()
        resp_body = {
            "orderId": 123456,
            "symbol": "BTCUSDT",
            "side": "BUY",
            "executedQty": "0.001",
            "avgPrice": "50000.0",
            "status": "FILLED",
        }
        client._session.post = MagicMock(return_value=_mock_response(200, resp_body))
        result = client.place_market_order("BTCUSDT", "buy", 50.0)
        assert isinstance(result, OrderResult)
        assert result.order_id == "123456"
        assert result.symbol == "BTCUSDT"
        assert result.side == "buy"
        assert result.filled_qty == 0.001
        assert result.avg_price == 50000.0
        assert result.status == "filled"

    def test_raises_exchange_error_on_4xx(self, monkeypatch):
        _binance_env(monkeypatch)
        client = BinanceClient()
        error_body = {"code": -2010, "msg": "Account has insufficient balance"}
        client._session.post = MagicMock(return_value=_mock_response(400, error_body))
        with pytest.raises(ExchangeError, match="Binance API error 400"):
            client.place_market_order("BTCUSDT", "buy", 50.0)

    def test_symbol_normalisation_uppercase(self, monkeypatch):
        """Lower-case symbol should be sent as upper-case."""
        _binance_env(monkeypatch)
        client = BinanceClient()
        resp_body = {
            "orderId": 1,
            "symbol": "ETHUSDT",
            "side": "SELL",
            "executedQty": "0.1",
            "avgPrice": "3000.0",
            "status": "FILLED",
        }
        post_mock = MagicMock(return_value=_mock_response(200, resp_body))
        client._session.post = post_mock
        result = client.place_market_order("ethusdt", "sell", 300.0)
        assert result.symbol == "ETHUSDT"
        # Check that the request contained upper-case symbol
        call_kwargs = post_mock.call_args
        assert "ETHUSDT" in str(call_kwargs)


class TestBinanceCancelOrder:
    def test_returns_true_on_cancelled(self, monkeypatch):
        _binance_env(monkeypatch)
        client = BinanceClient()
        client._session.delete = MagicMock(
            return_value=_mock_response(200, {"status": "CANCELED"})
        )
        assert client.cancel_order("BTCUSDT", "999") is True

    def test_raises_on_4xx(self, monkeypatch):
        _binance_env(monkeypatch)
        client = BinanceClient()
        client._session.delete = MagicMock(
            return_value=_mock_response(400, {"msg": "Unknown order"})
        )
        with pytest.raises(ExchangeError):
            client.cancel_order("BTCUSDT", "999")


class TestBinanceGetPosition:
    def test_returns_position_info_when_open(self, monkeypatch):
        _binance_env(monkeypatch)
        client = BinanceClient()
        pos_data = [
            {
                "symbol": "BTCUSDT",
                "positionAmt": "0.5",
                "entryPrice": "48000.0",
                "markPrice": "50000.0",
                "unRealizedProfit": "1000.0",
            }
        ]
        client._session.get = MagicMock(return_value=_mock_response(200, pos_data))
        pos = client.get_position("BTCUSDT")
        assert pos is not None
        assert isinstance(pos, PositionInfo)
        assert pos.side == "long"
        assert pos.size == 0.5
        assert pos.entry_price == 48000.0

    def test_returns_none_when_no_position(self, monkeypatch):
        _binance_env(monkeypatch)
        client = BinanceClient()
        pos_data = [{"symbol": "BTCUSDT", "positionAmt": "0", "entryPrice": "0", "markPrice": "50000"}]
        client._session.get = MagicMock(return_value=_mock_response(200, pos_data))
        assert client.get_position("BTCUSDT") is None

    def test_returns_none_on_empty_list(self, monkeypatch):
        _binance_env(monkeypatch)
        client = BinanceClient()
        client._session.get = MagicMock(return_value=_mock_response(200, []))
        assert client.get_position("BTCUSDT") is None


class TestBinanceGetUsdtBalance:
    def test_returns_balance(self, monkeypatch):
        _binance_env(monkeypatch)
        client = BinanceClient()
        account_data = {
            "assets": [
                {"asset": "BNB", "availableBalance": "5.0"},
                {"asset": "USDT", "availableBalance": "1234.56"},
            ]
        }
        client._session.get = MagicMock(return_value=_mock_response(200, account_data))
        assert client.get_usdt_balance() == 1234.56

    def test_raises_if_usdt_not_found(self, monkeypatch):
        _binance_env(monkeypatch)
        client = BinanceClient()
        account_data = {"assets": [{"asset": "BNB", "availableBalance": "5.0"}]}
        client._session.get = MagicMock(return_value=_mock_response(200, account_data))
        with pytest.raises(ExchangeError, match="USDT asset not found"):
            client.get_usdt_balance()


class TestBinanceGetMarkPrice:
    def test_returns_price_from_dict(self, monkeypatch):
        _binance_env(monkeypatch)
        client = BinanceClient()
        client._session.get = MagicMock(
            return_value=_mock_response(200, {"symbol": "BTCUSDT", "markPrice": "51000.0"})
        )
        assert client.get_mark_price("BTCUSDT") == 51000.0

    def test_returns_price_from_list(self, monkeypatch):
        _binance_env(monkeypatch)
        client = BinanceClient()
        client._session.get = MagicMock(
            return_value=_mock_response(200, [
                {"symbol": "ETHUSDT", "markPrice": "3100.0"},
                {"symbol": "BTCUSDT", "markPrice": "51000.0"},
            ])
        )
        assert client.get_mark_price("BTCUSDT") == 51000.0


# ===========================================================================
# BybitClient tests
# ===========================================================================


class TestBybitClientConfig:
    def test_raises_if_api_key_missing(self, monkeypatch):
        monkeypatch.delenv("BYBIT_API_KEY", raising=False)
        monkeypatch.setenv("BYBIT_API_SECRET", "s")
        with pytest.raises(ExchangeConfigError, match="BYBIT_API_KEY"):
            BybitClient()

    def test_raises_if_api_secret_missing(self, monkeypatch):
        monkeypatch.setenv("BYBIT_API_KEY", "k")
        monkeypatch.delenv("BYBIT_API_SECRET", raising=False)
        with pytest.raises(ExchangeConfigError, match="BYBIT_API_SECRET"):
            BybitClient()


class TestBybitPlaceMarketOrder:
    def _make_client(self, monkeypatch):
        _bybit_env(monkeypatch)
        return BybitClient()

    def _ticker_response(self, price: str = "50000.0"):
        return {
            "retCode": 0,
            "result": {"list": [{"symbol": "BTCUSDT", "markPrice": price}]},
        }

    def _order_response(self):
        return {
            "retCode": 0,
            "result": {"orderId": "bybit-001", "cumExecQty": "0.001", "avgPrice": "50000"},
        }

    def test_returns_order_result_on_success(self, monkeypatch):
        client = self._make_client(monkeypatch)
        get_mock = MagicMock(return_value=_mock_response(200, self._ticker_response()))
        post_mock = MagicMock(return_value=_mock_response(200, self._order_response()))
        client._session.get = get_mock
        client._session.post = post_mock
        result = client.place_market_order("BTCUSDT", "buy", 50.0)
        assert isinstance(result, OrderResult)
        assert result.order_id == "bybit-001"
        assert result.status == "filled"

    def test_raises_exchange_error_on_non_zero_ret_code(self, monkeypatch):
        client = self._make_client(monkeypatch)
        # get_mark_price succeeds
        get_mock = MagicMock(return_value=_mock_response(200, self._ticker_response()))
        # order fails with retCode != 0
        error_body = {"retCode": 10001, "retMsg": "Insufficient balance"}
        post_mock = MagicMock(return_value=_mock_response(200, error_body))
        client._session.get = get_mock
        client._session.post = post_mock
        with pytest.raises(ExchangeError, match="Bybit API error 10001"):
            client.place_market_order("BTCUSDT", "buy", 50.0)

    def test_raises_exchange_error_on_http_4xx(self, monkeypatch):
        client = self._make_client(monkeypatch)
        get_mock = MagicMock(return_value=_mock_response(200, self._ticker_response()))
        post_mock = MagicMock(return_value=_mock_response(403, {"retMsg": "Forbidden"}))
        client._session.get = get_mock
        client._session.post = post_mock
        with pytest.raises(ExchangeError, match="Bybit HTTP error 403"):
            client.place_market_order("BTCUSDT", "buy", 50.0)


class TestBybitGetPosition:
    def test_returns_none_when_no_position(self, monkeypatch):
        _bybit_env(monkeypatch)
        client = BybitClient()
        data = {
            "retCode": 0,
            "result": {"list": [{"symbol": "BTCUSDT", "size": "0", "side": "Buy"}]},
        }
        client._session.get = MagicMock(return_value=_mock_response(200, data))
        assert client.get_position("BTCUSDT") is None

    def test_returns_position_when_open(self, monkeypatch):
        _bybit_env(monkeypatch)
        client = BybitClient()
        data = {
            "retCode": 0,
            "result": {
                "list": [{
                    "symbol": "BTCUSDT",
                    "size": "0.5",
                    "side": "Buy",
                    "avgPrice": "49000",
                    "markPrice": "50000",
                    "positionValue": "24500",
                    "unrealisedPnl": "500",
                }]
            },
        }
        client._session.get = MagicMock(return_value=_mock_response(200, data))
        pos = client.get_position("BTCUSDT")
        assert pos is not None
        assert pos.side == "long"
        assert pos.size == 0.5


# ===========================================================================
# HyperliquidClient tests
# ===========================================================================


class TestHyperliquidClientConfig:
    def test_raises_if_wallet_address_missing(self, monkeypatch):
        monkeypatch.delenv("HYPERLIQUID_WALLET_ADDRESS", raising=False)
        monkeypatch.setenv("HYPERLIQUID_PRIVATE_KEY", "0x" + "a" * 64)
        with pytest.raises(ExchangeConfigError, match="HYPERLIQUID_WALLET_ADDRESS"):
            HyperliquidClient()

    def test_raises_if_private_key_missing(self, monkeypatch):
        monkeypatch.setenv("HYPERLIQUID_WALLET_ADDRESS", "0xdead")
        monkeypatch.delenv("HYPERLIQUID_PRIVATE_KEY", raising=False)
        with pytest.raises(ExchangeConfigError, match="HYPERLIQUID_PRIVATE_KEY"):
            HyperliquidClient()


class TestHyperliquidGetMarkPrice:
    def test_returns_price(self, monkeypatch):
        _hl_env(monkeypatch)
        client = HyperliquidClient()
        client._session.post = MagicMock(
            return_value=_mock_response(200, {"BTC": "51000.5", "ETH": "3100.0"})
        )
        assert client.get_mark_price("BTCUSDT") == 51000.5

    def test_normalizes_symbol_with_suffix(self, monkeypatch):
        _hl_env(monkeypatch)
        client = HyperliquidClient()
        client._session.post = MagicMock(
            return_value=_mock_response(200, {"ETH": "3100.0"})
        )
        # "ETHUSDT" → "ETH"
        assert client.get_mark_price("ETHUSDT") == 3100.0

    def test_raises_if_symbol_not_found(self, monkeypatch):
        _hl_env(monkeypatch)
        client = HyperliquidClient()
        client._session.post = MagicMock(
            return_value=_mock_response(200, {"ETH": "3100.0"})
        )
        with pytest.raises(ExchangeError, match="not found"):
            client.get_mark_price("SOLUSDT")


class TestHyperliquidGetPosition:
    def test_returns_none_when_no_position(self, monkeypatch):
        _hl_env(monkeypatch)
        client = HyperliquidClient()
        data = {
            "crossMarginSummary": {"accountValue": "1000"},
            "assetPositions": [
                {"position": {"coin": "BTC", "szi": "0", "entryPx": "0", "positionValue": "0", "unrealizedPnl": "0"}}
            ],
        }
        client._session.post = MagicMock(return_value=_mock_response(200, data))
        assert client.get_position("BTCUSDT") is None

    def test_returns_position_when_open(self, monkeypatch):
        _hl_env(monkeypatch)
        client = HyperliquidClient()
        data = {
            "crossMarginSummary": {"accountValue": "2000"},
            "assetPositions": [
                {
                    "position": {
                        "coin": "BTC",
                        "szi": "0.1",
                        "entryPx": "50000",
                        "positionValue": "5000",
                        "unrealizedPnl": "200",
                    }
                }
            ],
        }
        client._session.post = MagicMock(return_value=_mock_response(200, data))
        pos = client.get_position("BTCUSDT")
        assert pos is not None
        assert pos.side == "long"
        assert pos.size == 0.1

    def test_raises_on_http_error(self, monkeypatch):
        _hl_env(monkeypatch)
        client = HyperliquidClient()
        client._session.post = MagicMock(return_value=_mock_response(500, "Internal Error"))
        with pytest.raises(ExchangeError):
            client.get_position("BTCUSDT")


class TestHyperliquidGetUsdtBalance:
    def test_returns_account_value(self, monkeypatch):
        _hl_env(monkeypatch)
        client = HyperliquidClient()
        data = {"crossMarginSummary": {"accountValue": "9876.54"}, "assetPositions": []}
        client._session.post = MagicMock(return_value=_mock_response(200, data))
        assert client.get_usdt_balance() == 9876.54

    def test_raises_if_balance_missing(self, monkeypatch):
        _hl_env(monkeypatch)
        client = HyperliquidClient()
        client._session.post = MagicMock(return_value=_mock_response(200, {"assetPositions": []}))
        with pytest.raises(ExchangeError, match="USDC balance"):
            client.get_usdt_balance()


# ===========================================================================
# Factory tests
# ===========================================================================


class TestGetClientFactory:
    def test_returns_binance_client(self, monkeypatch):
        _binance_env(monkeypatch)
        client = get_client("binance")
        assert isinstance(client, BinanceClient)

    def test_returns_bybit_client(self, monkeypatch):
        _bybit_env(monkeypatch)
        client = get_client("bybit")
        assert isinstance(client, BybitClient)

    def test_returns_hyperliquid_client(self, monkeypatch):
        _hl_env(monkeypatch)
        client = get_client("hyperliquid")
        assert isinstance(client, HyperliquidClient)

    def test_case_insensitive(self, monkeypatch):
        _binance_env(monkeypatch)
        client = get_client("Binance")
        assert isinstance(client, BinanceClient)

    def test_raises_for_unknown_exchange(self, monkeypatch):
        with pytest.raises(ExchangeConfigError, match="Unknown exchange"):
            get_client("kraken")
