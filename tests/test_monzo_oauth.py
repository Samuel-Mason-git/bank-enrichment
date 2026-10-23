"""Tests for the Monzo OAuth token exchange/refresh module.

The actual client secret and every token must never appear in a log line or
a returned value -- these tests assert on return values and file contents,
never on what gets printed."""
import json
import time
from unittest.mock import MagicMock, patch

import pytest

import monzo_oauth


@pytest.fixture(autouse=True)
def _tokens_path(tmp_path, monkeypatch):
    monkeypatch.setattr(monzo_oauth, "TOKENS_PATH", str(tmp_path / "monzo_oauth.json"))
    return tmp_path / "monzo_oauth.json"


def _response(status_code=200, **json_body):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body
    return resp


class TestExchangeCodeForTokens:
    def test_success_saves_tokens_to_disk(self, _tokens_path):
        with patch("monzo_oauth.requests.post", return_value=_response(
                access_token="a", refresh_token="r", expires_in=21600)):
            assert monzo_oauth.exchange_code_for_tokens("some-code") is True
        saved = json.loads(_tokens_path.read_text())
        assert saved["access_token"] == "a"
        assert saved["refresh_token"] == "r"

    def test_failure_does_not_write_a_file(self, _tokens_path):
        with patch("monzo_oauth.requests.post", return_value=_response(status_code=401)):
            assert monzo_oauth.exchange_code_for_tokens("bad-code") is False
        assert not _tokens_path.exists()


class TestRefreshTokens:
    def test_with_no_prior_tokens_returns_false(self, _tokens_path):
        assert monzo_oauth.refresh_tokens() is False

    def test_success_overwrites_with_the_new_refresh_token(self, _tokens_path):
        """Monzo invalidates the previous refresh token on every renewal -- if
        the new one isn't persisted, the next cycle has nothing valid left."""
        _tokens_path.write_text(json.dumps({
            "access_token": "old-access", "refresh_token": "old-refresh",
            "obtained_at": time.time(), "expires_at": time.time() + 100,
        }))
        with patch("monzo_oauth.requests.post", return_value=_response(
                access_token="new-access", refresh_token="new-refresh", expires_in=21600)) as mock_post:
            assert monzo_oauth.refresh_tokens() is True
        assert mock_post.call_args.kwargs["data"]["refresh_token"] == "old-refresh"
        saved = json.loads(_tokens_path.read_text())
        assert saved["refresh_token"] == "new-refresh"
        assert saved["access_token"] == "new-access"

    def test_failure_leaves_the_existing_tokens_in_place(self, _tokens_path):
        _tokens_path.write_text(json.dumps({
            "access_token": "old-access", "refresh_token": "old-refresh",
            "obtained_at": time.time(), "expires_at": time.time() + 100,
        }))
        with patch("monzo_oauth.requests.post", return_value=_response(status_code=401)):
            assert monzo_oauth.refresh_tokens() is False
        saved = json.loads(_tokens_path.read_text())
        assert saved["refresh_token"] == "old-refresh"


class TestPing:
    def test_with_no_tokens_returns_false_without_a_request(self, _tokens_path):
        with patch("monzo_oauth.requests.get") as mock_get:
            assert monzo_oauth.ping() is False
        mock_get.assert_not_called()

    def test_uses_the_stored_access_token_as_a_bearer_header(self, _tokens_path):
        _tokens_path.write_text(json.dumps({
            "access_token": "my-access", "refresh_token": "r",
            "obtained_at": time.time(), "expires_at": time.time() + 100,
        }))
        with patch("monzo_oauth.requests.get", return_value=_response(status_code=200)) as mock_get:
            assert monzo_oauth.ping() is True
        assert mock_get.call_args.kwargs["headers"]["Authorization"] == "Bearer my-access"


class TestRunMonzoRefresh:
    @pytest.fixture(autouse=True)
    def _reset_alert_flag(self):
        monzo_oauth._alerted_for_current_failure = False
        yield
        monzo_oauth._alerted_for_current_failure = False

    def test_no_prior_tokens_sends_no_alert(self, _tokens_path):
        """Before the one-time authorization has ever happened -- expected
        state, not a failure."""
        bot = MagicMock()
        with patch("monzo_oauth.requests.post", return_value=_response(status_code=401)):
            monzo_oauth.run_monzo_refresh(bot)
        bot.send_alert.assert_not_called()

    def test_failure_after_prior_success_sends_one_alert(self, _tokens_path, monkeypatch):
        monkeypatch.setattr(monzo_oauth, "TELEGRAM_CHAT_ID", "12345")
        _tokens_path.write_text(json.dumps({
            "access_token": "a", "refresh_token": "r",
            "obtained_at": time.time(), "expires_at": time.time() + 100,
        }))
        bot = MagicMock()
        with patch("monzo_oauth.requests.post", return_value=_response(status_code=401)):
            monzo_oauth.run_monzo_refresh(bot)
        bot.send_alert.assert_called_once()
        assert bot.send_alert.call_args.args[0] == 12345

    def test_does_not_re_alert_on_a_second_consecutive_failure(self, _tokens_path, monkeypatch):
        monkeypatch.setattr(monzo_oauth, "TELEGRAM_CHAT_ID", "12345")
        _tokens_path.write_text(json.dumps({
            "access_token": "a", "refresh_token": "r",
            "obtained_at": time.time(), "expires_at": time.time() + 100,
        }))
        bot = MagicMock()
        with patch("monzo_oauth.requests.post", return_value=_response(status_code=401)):
            monzo_oauth.run_monzo_refresh(bot)
            monzo_oauth.run_monzo_refresh(bot)
        bot.send_alert.assert_called_once()

    def test_alerts_again_after_a_success_in_between(self, _tokens_path, monkeypatch):
        monkeypatch.setattr(monzo_oauth, "TELEGRAM_CHAT_ID", "12345")
        _tokens_path.write_text(json.dumps({
            "access_token": "a", "refresh_token": "r",
            "obtained_at": time.time(), "expires_at": time.time() + 100,
        }))
        bot = MagicMock()
        with patch("monzo_oauth.requests.post", return_value=_response(status_code=401)):
            monzo_oauth.run_monzo_refresh(bot)
        with patch("monzo_oauth.requests.post", return_value=_response(
                access_token="a2", refresh_token="r2", expires_in=21600)):
            monzo_oauth.run_monzo_refresh(bot)
        with patch("monzo_oauth.requests.post", return_value=_response(status_code=401)):
            monzo_oauth.run_monzo_refresh(bot)
        assert bot.send_alert.call_count == 2

    def test_success_calls_ping_with_the_fresh_token(self, _tokens_path):
        _tokens_path.write_text(json.dumps({
            "access_token": "old", "refresh_token": "old-r",
            "obtained_at": time.time(), "expires_at": time.time() + 100,
        }))
        bot = MagicMock()
        with patch("monzo_oauth.requests.post", return_value=_response(
                access_token="fresh", refresh_token="r2", expires_in=21600)):
            with patch("monzo_oauth.requests.get", return_value=_response(status_code=200)) as mock_get:
                monzo_oauth.run_monzo_refresh(bot)
        assert mock_get.call_args.kwargs["headers"]["Authorization"] == "Bearer fresh"
        bot.send_alert.assert_not_called()
