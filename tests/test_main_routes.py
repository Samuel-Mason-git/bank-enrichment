"""Route-level tests for main.py's remaining pages: the Monzo OAuth callback
and the access-log filter. (The web dashboard's routes are gone: the
Streamlit dashboard does all of that now, and has its own tests.)"""
import asyncio
import logging
from unittest.mock import patch

import main


class TestOauthCallback:
    """The route must never render the authorization code or any token back
    to the browser -- it only ever reports success/failure."""

    def test_error_param_returns_failure_without_exchanging(self):
        with patch("main.complete_authorization") as mock_complete:
            resp = asyncio.run(main.oauth_callback(error="access_denied"))
        mock_complete.assert_not_called()
        assert "failed" in resp.lower()

    def test_no_code_returns_failure_without_exchanging(self):
        with patch("main.complete_authorization") as mock_complete:
            resp = asyncio.run(main.oauth_callback())
        mock_complete.assert_not_called()
        assert "no authorization code" in resp.lower()

    def test_a_code_without_a_state_is_never_exchanged(self):
        with patch("main.complete_authorization") as mock_complete:
            asyncio.run(main.oauth_callback(code="a-secret-code"))
        mock_complete.assert_not_called()

    def test_successful_exchange_reports_success(self):
        with patch("main.complete_authorization", return_value=True):
            resp = asyncio.run(main.oauth_callback(code="a-secret-code", state="s"))
        assert "complete" in resp.lower()
        assert "a-secret-code" not in resp

    def test_failed_exchange_reports_failure(self):
        with patch("main.complete_authorization", return_value=False):
            resp = asyncio.run(main.oauth_callback(code="a-secret-code", state="s"))
        assert "/oauth/start" in resp
        assert "a-secret-code" not in resp


class TestDropOauthCallbackAccessLogs:
    def _record(self, args):
        return logging.LogRecord(
            name="uvicorn.access", level=logging.INFO, pathname="", lineno=0,
            msg='%s - "%s %s HTTP/%s" %d', args=args, exc_info=None,
        )

    def test_drops_the_oauth_callback_line_query_string_and_all(self):
        f = main._DropOauthCallbackAccessLogs()
        record = self._record((
            "127.0.0.1", "GET",
            "/oauth/callback?code=super-secret-code&state=abc", "1.1", 200,
        ))
        assert f.filter(record) is False

    def test_keeps_every_other_route(self):
        f = main._DropOauthCallbackAccessLogs()
        record = self._record(("127.0.0.1", "GET", "/dashboard", "1.1", 200))
        assert f.filter(record) is True
