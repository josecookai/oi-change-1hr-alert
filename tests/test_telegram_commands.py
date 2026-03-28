import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from unittest.mock import patch, MagicMock
import pytest
import telegram_commands


ALLOWED_CHAT = "12345"
OTHER_CHAT = "99999"


def _make_message(text: str, chat_id: str = ALLOWED_CHAT) -> dict:
    return {"chat": {"id": chat_id}, "text": text}


@pytest.fixture(autouse=True)
def patch_config(monkeypatch):
    monkeypatch.setattr(telegram_commands, "_ALLOWED_CHAT", ALLOWED_CHAT)


class TestHandleAuth:
    def test_ignores_unauthorized_chat(self):
        with patch.object(telegram_commands, "_reply") as mock_reply:
            telegram_commands._handle(_make_message("/arb", chat_id=OTHER_CHAT))
        mock_reply.assert_not_called()

    def test_responds_to_authorized_chat(self):
        with patch.object(telegram_commands, "_reply") as mock_reply, \
             patch("telegram_commands.ws_client.get_latest", return_value={}):
            telegram_commands._handle(_make_message("/arb", chat_id=ALLOWED_CHAT))
        mock_reply.assert_called_once()


class TestCommands:
    def test_help_command(self):
        with patch.object(telegram_commands, "_reply") as mock_reply:
            telegram_commands._handle(_make_message("/help"))
        mock_reply.assert_called_once()
        text = mock_reply.call_args[0][1]
        assert "/arb" in text
        assert "/status" in text
        assert "/paper" in text

    def test_start_command(self):
        with patch.object(telegram_commands, "_reply") as mock_reply:
            telegram_commands._handle(_make_message("/start"))
        mock_reply.assert_called_once()

    def test_arb_no_data(self):
        with patch.object(telegram_commands, "_reply") as mock_reply, \
             patch("telegram_commands.ws_client.get_latest", return_value={}):
            telegram_commands._handle(_make_message("/arb"))
        text = mock_reply.call_args[0][1]
        assert "No live data" in text

    def test_arb_with_data(self):
        fake_opp = MagicMock()
        fake_opp.symbol = "BTCUSDT"
        fake_opp.spread_pct = 0.3
        fake_opp.net_per_10k_per_interval = 10.0
        fake_opp.breakeven_periods = 0.5
        fake_opp.long_exchange = "bybit"
        fake_opp.short_exchange = "binance"

        with patch.object(telegram_commands, "_reply") as mock_reply, \
             patch("telegram_commands.ws_client.get_latest", return_value={"binance": []}), \
             patch("telegram_commands.arb_detector.detect", return_value=[fake_opp]), \
             patch("telegram_commands.formatter.build_arb_section", return_value="arb text"):
            telegram_commands._handle(_make_message("/arb"))
        assert mock_reply.call_args[0][1] == "arb text"

    def test_status_no_data(self):
        with patch.object(telegram_commands, "_reply") as mock_reply, \
             patch("telegram_commands.ws_client.get_latest", return_value=None):
            telegram_commands._handle(_make_message("/status"))
        text = mock_reply.call_args[0][1]
        assert "not connected" in text.lower() or "websocket" in text.lower()

    def test_status_with_data(self):
        fake_opp = MagicMock()
        fake_opp.spread_pct = 0.5
        fake_opp.symbol = "ETHUSDT"
        fake_opp.long_exchange = "bybit"
        fake_opp.short_exchange = "binance"

        with patch.object(telegram_commands, "_reply") as mock_reply, \
             patch("telegram_commands.ws_client.get_latest", return_value={"x": 1}), \
             patch("telegram_commands.arb_detector.detect", return_value=[fake_opp]):
            telegram_commands._handle(_make_message("/status"))
        text = mock_reply.call_args[0][1]
        assert "Status" in text

    def test_paper_command(self):
        fake_snap = {
            "open_positions": [],
            "closed_count": 2,
            "total_net_pnl": 15.5,
            "total_fee_paid": 4.0,
            "win_rate": 1.0,
            "avg_hold_hours": 24.0,
        }
        mock_trader = MagicMock()
        mock_trader.snapshot.return_value = fake_snap

        with patch.object(telegram_commands, "_reply") as mock_reply, \
             patch("telegram_commands.get_trader", return_value=mock_trader):
            telegram_commands._handle(_make_message("/paper"))
        mock_reply.assert_called_once()

    def test_unknown_command_gets_help_hint(self):
        with patch.object(telegram_commands, "_reply") as mock_reply:
            telegram_commands._handle(_make_message("/foobar"))
        text = mock_reply.call_args[0][1]
        assert "/help" in text

    def test_empty_message_ignored(self):
        with patch.object(telegram_commands, "_reply") as mock_reply:
            telegram_commands._handle(_make_message(""))
        mock_reply.assert_not_called()

    def test_command_with_bot_suffix(self):
        """'/arb@MyBot' should be parsed correctly."""
        with patch.object(telegram_commands, "_reply") as mock_reply, \
             patch("telegram_commands.ws_client.get_latest", return_value={}):
            telegram_commands._handle(_make_message("/arb@OIMonitorBot"))
        mock_reply.assert_called_once()
