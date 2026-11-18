"""The Settings page's setup check: each part's result and what to do, with the network and the
database stood in for. Plus the page's button."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from streamlit.testing.v1 import AppTest

import setup_check as sc

NOW = datetime(2026, 11, 18, 10, 45, tzinfo=timezone.utc)
_DASHBOARD = str(Path(__file__).parent.parent / "src" / "dashboard" / "dashboard.py")


class Con:
    """Answers the queries the checks make, by what they ask for."""

    def __init__(self, last_run=None, status="ok", last_payment=None, broken=False):
        self.last_run, self.status, self.last_payment, self.broken = last_run, status, last_payment, broken

    def execute(self, sql, *args):
        if self.broken:
            raise OSError("connection refused")
        if "MAX(started_at)" in sql:
            value = self.last_run
        elif "SELECT status" in sql:
            value = self.status
        elif "transactions_raw" in sql:
            value = self.last_payment
        else:
            value = 1
        return SimpleNamespace(fetchone=lambda: (value,))


# ── the database and the worker ──────────────────────────────────────────────

def test_an_unreachable_database_is_a_problem_and_skips_the_checks_that_need_it(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    results = sc.run_all(Con(broken=True), get=lambda *a: {"ok": True, "result": {"url": "https://x/receive_telegram/"}}, now=NOW)
    assert results[0].name == "Database" and results[0].result == sc.PROBLEM
    assert [r.name for r in results] == ["Database", "Telegram webhook", "Claude key"]


def test_a_worker_on_schedule_is_ok():
    assert sc.check_worker(Con(last_run=NOW - timedelta(minutes=40)), NOW).result == sc.OK


def test_a_worker_that_missed_a_run_is_a_problem():
    check = sc.check_worker(Con(last_run=NOW - timedelta(hours=3)), NOW)
    assert check.result == sc.PROBLEM and "didn't start" in check.detail


def test_a_run_with_a_failed_step_is_a_warning():
    assert sc.check_worker(Con(last_run=NOW - timedelta(minutes=40), status="problems"), NOW).result == sc.WARNING


def test_no_run_yet_is_a_problem():
    assert sc.check_worker(Con(last_run=None), NOW).result == sc.PROBLEM


# ── Monzo payments ───────────────────────────────────────────────────────────

def test_payments_that_have_gone_quiet_are_a_warning():
    assert sc.check_payments(Con(last_payment=NOW - timedelta(days=6)), NOW).result == sc.WARNING


def test_a_recent_payment_is_ok():
    assert sc.check_payments(Con(last_payment=NOW - timedelta(hours=2)), NOW).result == sc.OK


# ── the Telegram webhook ─────────────────────────────────────────────────────

def test_no_bot_token_is_a_problem(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert sc.check_telegram_webhook(lambda url: {}).result == sc.PROBLEM


def test_no_registered_webhook_is_a_problem(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    check = sc.check_telegram_webhook(lambda url: {"ok": True, "result": {"url": ""}})
    assert check.result == sc.PROBLEM and "No webhook" in check.detail


def test_a_webhook_with_a_failed_delivery_is_a_warning(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    body = {"ok": True, "result": {"url": "https://me.duckdns.org/receive_telegram/", "last_error_message": "Connection timed out"}}
    check = sc.check_telegram_webhook(lambda url: body)
    assert check.result == sc.WARNING and "timed out" in check.detail


def test_the_old_webhook_spelling_still_counts(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    body = {"ok": True, "result": {"url": "https://me.duckdns.org/recieve_telegram/"}}
    assert sc.check_telegram_webhook(lambda url: body).result == sc.OK


def test_a_webhook_somewhere_else_is_a_warning(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    body = {"ok": True, "result": {"url": "https://other.example/hook"}}
    assert sc.check_telegram_webhook(lambda url: body).result == sc.WARNING


# ── the Claude key ───────────────────────────────────────────────────────────

def test_a_rejected_claude_key_is_a_problem(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "bad")
    check = sc.check_claude_key(lambda url, headers=None: {"type": "error", "error": {"type": "authentication_error"}})
    assert check.result == sc.PROBLEM


def test_an_accepted_claude_key_is_ok(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "good")
    assert sc.check_claude_key(lambda url, headers=None: {"data": []}).result == sc.OK


def test_no_claude_key_is_a_problem(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert sc.check_claude_key(lambda url, headers=None: {}).result == sc.PROBLEM


# ── the page ─────────────────────────────────────────────────────────────────

def test_the_button_shows_each_result(db, monkeypatch):
    monkeypatch.setattr(sc, "check_telegram_webhook", lambda get: sc.Check("Telegram webhook", sc.OK, "Registered."))
    monkeypatch.setattr(sc, "check_claude_key", lambda get: sc.Check("Claude key", sc.PROBLEM, "Not set."))
    at = AppTest.from_file(_DASHBOARD, default_timeout=30)
    at.switch_page("views/settings.py").run()
    assert not at.exception
    at.button(key="setup_run_checks").click().run()
    assert not at.exception
    assert any("Database" in s.value for s in at.success)
    assert any("Claude key" in e.value for e in at.error)
