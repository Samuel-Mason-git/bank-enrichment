"""Who can reach what. Goes through real HTTP requests (TestClient, without
the app lifespan) so middleware, headers and query strings are exercised the
way an attacker would hit them -- not by calling route functions directly."""
import json
import logging
import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import main
import monzo_oauth

OWNER = 12345  # TELEGRAM_CHAT_ID in conftest


@pytest.fixture
def client(server_con, monkeypatch):
    monkeypatch.setattr(main, "DASHBOARD_USER", "admin")
    monkeypatch.setattr(main, "DASHBOARD_PASSWORD", "pw")
    with patch.object(main, "get_con", return_value=server_con), \
         patch.object(main, "bot", MagicMock()), \
         patch.object(main, "check_rules", return_value=main.NO_MATCH), \
         patch.object(main, "get_quick_categories", return_value=[]):
        yield TestClient(main.app)


def _monzo_payload(txn_id="tx1"):
    return {"type": "transaction.created", "data": {
        "account_id": "acc_1", "amount": -500, "created": "2026-10-20T10:00:00.000Z",
        "currency": "GBP", "description": "TESCO", "id": txn_id,
        "category": "groceries", "is_load": False, "merchant": {"name": "Tesco"},
    }}


def _stored(con, txn_id="tx1"):
    return con.execute("SELECT COUNT(*) FROM transactions_raw WHERE id = %s", [txn_id]).fetchone()[0]


class TestMonzoWebhook:
    def test_rejected_without_the_token(self, client, server_con, monkeypatch):
        monkeypatch.setattr(main, "MONZO_WEBHOOK_SECRET", "s3cret")
        assert client.post("/recieve_monzo/", json=_monzo_payload()).status_code == 403
        assert _stored(server_con) == 0

    def test_rejected_with_the_wrong_token(self, client, server_con, monkeypatch):
        monkeypatch.setattr(main, "MONZO_WEBHOOK_SECRET", "s3cret")
        assert client.post("/recieve_monzo/?token=guess", json=_monzo_payload()).status_code == 403
        assert _stored(server_con) == 0

    def test_accepted_with_the_right_token(self, client, server_con, monkeypatch):
        monkeypatch.setattr(main, "MONZO_WEBHOOK_SECRET", "s3cret")
        assert client.post("/recieve_monzo/?token=s3cret", json=_monzo_payload()).status_code == 200
        assert _stored(server_con) == 1

    def test_unconfigured_install_still_accepts(self, client, server_con, monkeypatch):
        """Deploys happen on merge -- an install that hasn't set the secret
        yet must keep working rather than silently drop every transaction."""
        monkeypatch.setattr(main, "MONZO_WEBHOOK_SECRET", None)
        assert client.post("/recieve_monzo/", json=_monzo_payload()).status_code == 200


def _tap(data, user_id=OWNER):
    return {"update_id": 1, "callback_query": {
        "id": "cbq", "from": {"id": user_id, "first_name": "x", "is_bot": False}, "data": data}}


def _pending_txn(con, txn_id="tx1"):
    con.execute(
        "INSERT INTO transactions_raw (id, payload, received_at) VALUES (%s, %s, NOW())",
        [txn_id, json.dumps(_monzo_payload(txn_id))],
    )


def _skipped(con, txn_id="tx1"):
    return con.execute("SELECT skipped FROM transactions WHERE id = %s", [txn_id]).fetchone()[0]


class TestTelegramWebhook:
    HEADER = "X-Telegram-Bot-Api-Secret-Token"

    def test_rejected_without_the_secret_header(self, client, server_con, monkeypatch):
        monkeypatch.setattr(main, "TELEGRAM_WEBHOOK_SECRET", "s3cret")
        _pending_txn(server_con)
        assert client.post("/recieve_telegram/", json=_tap("skip_do:tx1")).status_code == 403
        assert _skipped(server_con) is False

    def test_rejected_with_the_wrong_secret_header(self, client, server_con, monkeypatch):
        monkeypatch.setattr(main, "TELEGRAM_WEBHOOK_SECRET", "s3cret")
        _pending_txn(server_con)
        resp = client.post("/recieve_telegram/", json=_tap("skip_do:tx1"), headers={self.HEADER: "guess"})
        assert resp.status_code == 403
        assert _skipped(server_con) is False

    def test_owner_with_the_right_secret_is_acted_on(self, client, server_con, monkeypatch):
        monkeypatch.setattr(main, "TELEGRAM_WEBHOOK_SECRET", "s3cret")
        _pending_txn(server_con)
        resp = client.post("/recieve_telegram/", json=_tap("skip_do:tx1"), headers={self.HEADER: "s3cret"})
        assert resp.status_code == 200
        assert _skipped(server_con) is True

    def test_someone_else_is_ignored_even_with_the_right_secret(self, client, server_con, monkeypatch):
        """Covers a real Telegram user who found the bot, and a forged update
        claiming any user id -- the secret alone doesn't make it you."""
        monkeypatch.setattr(main, "TELEGRAM_WEBHOOK_SECRET", "s3cret")
        _pending_txn(server_con)
        resp = client.post("/recieve_telegram/", json=_tap("skip_do:tx1", user_id=999),
                           headers={self.HEADER: "s3cret"})
        assert resp.status_code == 200
        assert _skipped(server_con) is False

    def test_someone_else_is_ignored_even_on_an_unconfigured_install(self, client, server_con, monkeypatch):
        monkeypatch.setattr(main, "TELEGRAM_WEBHOOK_SECRET", None)
        server_con.execute(
            """INSERT INTO rule_suggestions (id, match_field, match_value, category, subcategory,
               txn_count, auto_context, status, proposed_at)
               VALUES (1, 'merchant_name', 'EE', 'Bills', 'Phone', 6, '', 'pending', NOW())""")
        client.post("/recieve_telegram/", json=_tap("rulesug:approve:1", user_id=999))
        assert server_con.execute("SELECT COUNT(*) FROM rules").fetchone()[0] == 0

    def test_a_text_message_from_someone_else_is_ignored(self, client, server_con, monkeypatch):
        monkeypatch.setattr(main, "TELEGRAM_WEBHOOK_SECRET", None)
        _pending_txn(server_con)
        main._sessions[999] = {"transaction_id": "tx1", "timeout_task": None}
        try:
            client.post("/recieve_telegram/", json={"update_id": 2, "message": {
                "message_id": 1, "date": 0, "text": "forged context",
                "chat": {"id": 999, "type": "private"}}})
        finally:
            main._sessions.pop(999, None)
        assert server_con.execute(
            "SELECT user_context FROM transactions WHERE id = 'tx1'").fetchone()[0] is None


_RULE_FORM = {"name": "Skip everything", "match_field": "description", "match_type": "regex",
              "match_value": ".*", "auto_skip": "on"}


def _rule_count(con):
    return con.execute("SELECT COUNT(*) FROM rules").fetchone()[0]


class TestDashboardCrossSitePosts:
    """The attack: a page you visit while logged in auto-submits a form to
    your dashboard -- the browser attaches your Basic-auth login for it."""

    def _post(self, client, **headers):
        return client.post("/dashboard/rules/add", data=_RULE_FORM, auth=("admin", "pw"),
                           headers=headers, follow_redirects=False)

    def test_a_form_from_another_site_is_blocked(self, client, server_con):
        assert self._post(client, Origin="https://evil.example").status_code == 403
        assert _rule_count(server_con) == 0

    def test_a_null_origin_is_blocked(self, client, server_con):
        """Sandboxed iframes and data: URLs send Origin: null."""
        assert self._post(client, Origin="null").status_code == 403
        assert _rule_count(server_con) == 0

    def test_a_cross_site_referer_is_blocked_when_there_is_no_origin(self, client, server_con):
        assert self._post(client, Referer="https://evil.example/page").status_code == 403
        assert _rule_count(server_con) == 0

    def test_your_own_dashboard_still_works(self, client, server_con):
        assert self._post(client, Origin="http://testserver").status_code == 303
        assert _rule_count(server_con) == 1

    def test_a_non_browser_client_still_works(self, client, server_con):
        assert self._post(client).status_code == 303
        assert _rule_count(server_con) == 1

    def test_the_check_runs_before_auth(self, client, server_con):
        """Blocked even with valid credentials -- the credentials are exactly
        what a cross-site form gets for free."""
        resp = client.post("/dashboard/rules/1/delete", auth=("admin", "pw"),
                           headers={"Origin": "https://evil.example"}, follow_redirects=False)
        assert resp.status_code == 403


class TestWebhookTokenRedaction:
    def _record(self, path):
        return logging.LogRecord(
            name="uvicorn.access", level=logging.INFO, pathname="", lineno=0,
            msg='%s - "%s %s HTTP/%s" %d', args=("1.2.3.4", "POST", path, "1.1", 200), exc_info=None)

    def test_the_token_value_is_redacted(self):
        record = self._record("/recieve_monzo/?token=s3cret")
        main._RedactWebhookTokens().filter(record)
        assert "s3cret" not in record.getMessage()
        assert "token=[redacted]" in record.getMessage()

    def test_the_line_is_kept(self):
        record = self._record("/recieve_monzo/?token=s3cret")
        assert main._RedactWebhookTokens().filter(record) is True

    def test_other_lines_are_untouched(self):
        record = self._record("/dashboard/rules")
        main._RedactWebhookTokens().filter(record)
        assert "/dashboard/rules" in record.getMessage()


class TestUnsetSecretsAreReported:
    def test_lists_whichever_are_missing(self, monkeypatch):
        monkeypatch.setattr(main, "TELEGRAM_WEBHOOK_SECRET", None)
        monkeypatch.setattr(main, "MONZO_WEBHOOK_SECRET", "x")
        assert main._unset_webhook_secrets() == ["TELEGRAM_WEBHOOK_SECRET"]

    def test_nothing_when_both_are_set(self, monkeypatch):
        monkeypatch.setattr(main, "TELEGRAM_WEBHOOK_SECRET", "x")
        monkeypatch.setattr(main, "MONZO_WEBHOOK_SECRET", "y")
        assert main._unset_webhook_secrets() == []


@pytest.fixture
def _clean_oauth_state():
    monzo_oauth._pending_authorizations.clear()
    yield
    monzo_oauth._pending_authorizations.clear()


@pytest.mark.usefixtures("_clean_oauth_state")
class TestOauthState:
    REDIRECT = "https://my-domain.example/oauth/callback"

    def _state_from(self, url):
        from urllib.parse import parse_qs, urlsplit
        return parse_qs(urlsplit(url).query)["state"][0]

    def test_a_started_flow_completes_with_the_same_redirect_uri(self):
        state = self._state_from(monzo_oauth.begin_authorization(self.REDIRECT))
        with patch("monzo_oauth.exchange_code_for_tokens", return_value=True) as exchange:
            assert monzo_oauth.complete_authorization("code", state) is True
        exchange.assert_called_once_with("code", self.REDIRECT)

    def test_an_unknown_state_is_never_exchanged(self):
        with patch("monzo_oauth.exchange_code_for_tokens") as exchange:
            assert monzo_oauth.complete_authorization("code", "made-up") is False
        exchange.assert_not_called()

    def test_a_state_only_works_once(self):
        state = self._state_from(monzo_oauth.begin_authorization(self.REDIRECT))
        with patch("monzo_oauth.exchange_code_for_tokens", return_value=True) as exchange:
            monzo_oauth.complete_authorization("code", state)
            assert monzo_oauth.complete_authorization("code", state) is False
        assert exchange.call_count == 1

    def test_an_expired_state_is_rejected(self):
        state = self._state_from(monzo_oauth.begin_authorization(self.REDIRECT))
        uri, started = monzo_oauth._pending_authorizations[state]
        monzo_oauth._pending_authorizations[state] = (uri, started - monzo_oauth.STATE_TTL - 1)
        with patch("monzo_oauth.exchange_code_for_tokens") as exchange:
            assert monzo_oauth.complete_authorization("code", state) is False
        exchange.assert_not_called()

    def test_start_requires_the_dashboard_login(self, client):
        assert client.get("/oauth/start", follow_redirects=False).status_code == 401

    def test_start_redirects_to_monzo_with_this_servers_callback(self, client, monkeypatch):
        monkeypatch.setattr(main, "MONZO_CLIENT_ID", "oauth2client_test")
        monkeypatch.setattr(monzo_oauth, "MONZO_CLIENT_ID", "oauth2client_test")
        resp = client.get("/oauth/start", auth=("admin", "pw"), follow_redirects=False,
                          headers={"Host": "my-domain.example", "X-Forwarded-Proto": "https"})
        assert resp.status_code == 303
        location = resp.headers["location"]
        assert location.startswith("https://auth.monzo.com/")
        assert "redirect_uri=https%3A%2F%2Fmy-domain.example%2Foauth%2Fcallback" in location
