"""/health: the message the owner gets, and the command being recognised."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import bot_health


@pytest.mark.parametrize("text, is_cmd", [
    ("/health", True), ("/health@MyMoneyBot", True), ("  /HEALTH  ", True),
    ("/healthy eating", False), ("how is my health", False), ("", False), (None, False),
])
def test_health_is_recognised(text, is_cmd):
    assert bot_health.is_health(text) is is_cmd


class FakeCon:
    """Answers the three queries report() makes, by what they ask for."""

    def __init__(self, last_payment=None, run=None, fail=False):
        self.last_payment, self.run, self.fail = last_payment, run, fail

    def execute(self, sql, *args):
        if self.fail:
            raise RuntimeError("connection refused")
        if "transactions_raw" in sql:
            value = self.last_payment
        elif "FROM runs" in sql:
            value = self.run
        else:
            value = 1
        return SimpleNamespace(fetchone=lambda: (value,) if "transactions_raw" in sql else value)


def _now(hours_ago=0):
    return datetime.now(timezone.utc) - timedelta(hours=hours_ago)


def _report(con, key="sk-test"):
    with patch.object(bot_health.app_config, "anthropic_key", return_value=key):
        return bot_health.report(lambda: con)


def test_a_healthy_app_says_so():
    text = _report(FakeCon(last_payment=_now(0.1), run=(_now(0.5), "ok")))
    assert "✅ Server: answering" in text
    assert "✅ Database: reachable" in text
    assert "Last Monzo payment received: 6 min ago" in text
    assert "✅ Worker: last run 30 min ago, all fine" in text
    assert "✅ Claude key: set" in text


def test_an_unreachable_database_is_reported_not_raised():
    def broken():
        raise RuntimeError("no database")
    with patch.object(bot_health.app_config, "anthropic_key", return_value="k"):
        text = bot_health.report(broken)
    assert "✅ Server: answering" in text
    assert "❌ Database: not reachable" in text


def test_a_worker_that_has_stopped_is_flagged():
    text = _report(FakeCon(last_payment=_now(1), run=(_now(30), "ok")))
    assert "⚠️ Worker: last run 30 h ago, so it may have stopped" in text


def test_a_run_with_a_failed_step_is_flagged():
    text = _report(FakeCon(last_payment=_now(1), run=(_now(2), "problems")))
    assert "⚠️ Worker: last run 2 h ago had a step fail" in text


def test_no_run_yet_and_no_payment_yet():
    text = _report(FakeCon(last_payment=None, run=None))
    assert "Last Monzo payment received: never" in text
    assert "⚠️ Worker: no run recorded yet" in text


def test_a_missing_claude_key_is_flagged():
    text = _report(FakeCon(last_payment=_now(1), run=(_now(1), "ok")), key=None)
    assert "❌ Claude key: not set" in text
