"""Route-level tests for main.py FastAPI endpoints.

Strategy: call async route functions directly with get_con patched to the
in-memory server_con fixture — avoids needing the full lifespan (Telegram,
requester_loop) while still exercising the real SQL and template rendering.
"""
import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.requests import Request
from starlette.responses import RedirectResponse

import main
from main import logout


# ── helpers ───────────────────────────────────────────────────────────────────

def _req(method="GET", path="/dashboard"):
    return Request({
        "type": "http", "method": method, "path": path,
        "headers": [], "query_string": b"", "app": main.app,
    })


def _form_req(data: dict):
    req = MagicMock()
    req.form = AsyncMock(return_value=data)
    return req


_CREDS = MagicMock()  # bypass verify_credentials — not called when invoked directly


def _txn_payload(merchant="TestMerchant", description="Test Purchase", amount=-1000):
    return json.dumps({
        "data": {
            "amount": amount,
            "description": description,
            "merchant": {"name": merchant},
            "counterparty": {},
        }
    })


def _insert_txn(con, id="tx1", status="pending", skipped=False,
                user_context=None, merchant="TestMerchant",
                description="Test Purchase", amount=-1000):
    con.execute(
        "INSERT INTO webhook_queue (id, payload, received_at, status, user_context, skipped) "
        "VALUES (?, ?, NOW(), ?, ?, ?)",
        [id, _txn_payload(merchant, description, amount), status, user_context, skipped],
    )


def _insert_rule(con, id=1, name="Gym Rule", match_field="description",
                 match_type="contains", match_value="gym",
                 auto_context="Gym membership", enabled=True, auto_skip=False,
                 match_field_2=None, match_type_2=None, match_value_2=None):
    con.execute(
        "INSERT INTO rules (id, name, match_field, match_type, match_value, auto_context, "
        "enabled, auto_skip, match_field_2, match_type_2, match_value_2) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [id, name, match_field, match_type, match_value, auto_context,
         enabled, auto_skip, match_field_2, match_type_2, match_value_2],
    )


# ── Existing tests ─────────────────────────────────────────────────────────────

class TestStaticVersion:
    """Regression guard: static assets must be cache-busted so a deploy can't
    silently leave browsers serving a stale style.css against new markup."""

    def test_static_version_is_set(self):
        assert main.templates.env.globals.get("static_version")

    def test_stylesheet_link_includes_version(self):
        html = main.templates.env.get_template("dashboard.html").render(
            total_received=0, total_amount="£0.00", requests_sent=0, total_enriched=0,
            total_processed=0, queue_stats=[], total_queue=0, page=1, total_pages=1,
            queue=[], status_filter="", search="",
        )
        version = main.templates.env.globals["static_version"]
        assert f"/static/style.css?v={version}" in html


class TestLogout:
    """Regression guard: logout must never trigger a 401/WWW-Authenticate
    challenge — that traps the browser in a prompt loop it can never satisfy."""

    def test_returns_200_not_401(self):
        response = asyncio.run(logout(_req()))
        assert response.status_code == 200

    def test_does_not_set_www_authenticate_header(self):
        response = asyncio.run(logout(_req()))
        assert "www-authenticate" not in {k.lower() for k in response.headers.keys()}

    def test_body_explains_the_limitation(self):
        response = asyncio.run(logout(_req()))
        assert b"close this browser tab" in response.body.lower()


# ── Dashboard filter / search ──────────────────────────────────────────────────

class TestDashboardFilter:
    """The dashboard route builds a dynamic WHERE clause from status_filter and
    search — these tests verify the right rows come back for each combination."""

    def _run(self, server_con, status_filter="", search="", page=1):
        with patch("main.get_con", return_value=server_con):
            return asyncio.run(
                main.dashboard(_req(), _CREDS, page=page,
                               status_filter=status_filter, search=search)
            )

    def test_no_filter_returns_all_non_processed(self, server_con):
        _insert_txn(server_con, "tx1", status="pending")
        _insert_txn(server_con, "tx2", status="enriched")
        _insert_txn(server_con, "tx3", status="processed")
        resp = self._run(server_con)
        ids = {r["id"] for r in resp.context["queue"]}
        assert "tx1" in ids and "tx2" in ids
        assert "tx3" not in ids  # processed excluded

    def test_filter_skipped_returns_only_skipped(self, server_con):
        _insert_txn(server_con, "tx_skip", status="enriched", skipped=True)
        _insert_txn(server_con, "tx_normal", status="pending", skipped=False)
        resp = self._run(server_con, status_filter="skipped")
        ids = {r["id"] for r in resp.context["queue"]}
        assert "tx_skip" in ids
        assert "tx_normal" not in ids

    def test_filter_pending_returns_only_pending(self, server_con):
        _insert_txn(server_con, "tx_pend", status="pending")
        _insert_txn(server_con, "tx_enr", status="enriched")
        resp = self._run(server_con, status_filter="pending")
        ids = {r["id"] for r in resp.context["queue"]}
        assert "tx_pend" in ids
        assert "tx_enr" not in ids

    def test_filter_enriched_returns_only_enriched(self, server_con):
        _insert_txn(server_con, "tx_pend", status="pending")
        _insert_txn(server_con, "tx_enr", status="enriched")
        resp = self._run(server_con, status_filter="enriched")
        ids = {r["id"] for r in resp.context["queue"]}
        assert "tx_enr" in ids
        assert "tx_pend" not in ids

    def test_search_by_id_substring(self, server_con):
        _insert_txn(server_con, "tx_abc_001", status="pending")
        _insert_txn(server_con, "tx_xyz_002", status="pending")
        resp = self._run(server_con, search="abc")
        ids = {r["id"] for r in resp.context["queue"]}
        assert "tx_abc_001" in ids
        assert "tx_xyz_002" not in ids

    def test_search_by_merchant_name(self, server_con):
        _insert_txn(server_con, "tx1", merchant="PureGym", description="Monthly DD")
        _insert_txn(server_con, "tx2", merchant="Tesco", description="Groceries")
        resp = self._run(server_con, search="PureGym")
        ids = {r["id"] for r in resp.context["queue"]}
        assert "tx1" in ids
        assert "tx2" not in ids

    def test_combined_status_and_search(self, server_con):
        _insert_txn(server_con, "tx_match", status="pending", merchant="Netflix")
        _insert_txn(server_con, "tx_wrong_status", status="enriched", merchant="Netflix")
        _insert_txn(server_con, "tx_wrong_merchant", status="pending", merchant="Spotify")
        resp = self._run(server_con, status_filter="pending", search="Netflix")
        ids = {r["id"] for r in resp.context["queue"]}
        assert "tx_match" in ids
        assert "tx_wrong_status" not in ids
        assert "tx_wrong_merchant" not in ids

    def test_context_includes_filter_params(self, server_con):
        resp = self._run(server_con, status_filter="pending", search="gym")
        assert resp.context["status_filter"] == "pending"
        assert resp.context["search"] == "gym"

    def test_queue_row_includes_merchant_and_description(self, server_con):
        _insert_txn(server_con, "tx1", merchant="PureGym", description="Monthly membership")
        resp = self._run(server_con)
        row = next(r for r in resp.context["queue"] if r["id"] == "tx1")
        assert row["merchant"] == "PureGym"
        assert row["description"] == "Monthly membership"

    def test_pagination_links_preserve_filters(self, server_con):
        # Insert enough rows to force pagination (PAGE_SIZE = 20)
        for i in range(25):
            _insert_txn(server_con, f"tx_{i:03d}", status="pending")
        resp = self._run(server_con, status_filter="pending", page=1)
        assert resp.context["total_pages"] > 1
        # The rendered HTML should pass status filter through pagination links
        body = resp.body.decode()
        assert "status=pending" in body


# ── Edit enrichment context ────────────────────────────────────────────────────

class TestEditContext:
    def test_updates_user_context(self, server_con):
        _insert_txn(server_con, "tx1", status="enriched", user_context="old text")
        req = _form_req({"context": "new context text"})
        with patch("main.get_con", return_value=server_con):
            asyncio.run(main.edit_transaction_context("tx1", req, _CREDS))
        row = server_con.execute(
            "SELECT user_context FROM webhook_queue WHERE id = 'tx1'"
        ).fetchone()
        assert row[0] == "new context text"

    def test_empty_context_not_saved(self, server_con):
        _insert_txn(server_con, "tx1", status="enriched", user_context="original")
        req = _form_req({"context": "   "})
        with patch("main.get_con", return_value=server_con):
            asyncio.run(main.edit_transaction_context("tx1", req, _CREDS))
        row = server_con.execute(
            "SELECT user_context FROM webhook_queue WHERE id = 'tx1'"
        ).fetchone()
        assert row[0] == "original"

    def test_redirects_back_to_transaction(self, server_con):
        _insert_txn(server_con, "tx1")
        req = _form_req({"context": "updated"})
        with patch("main.get_con", return_value=server_con):
            resp = asyncio.run(main.edit_transaction_context("tx1", req, _CREDS))
        assert isinstance(resp, RedirectResponse)
        assert "/dashboard/transaction/tx1" in resp.headers["location"]


# ── Bulk actions ───────────────────────────────────────────────────────────────

class TestBulkActions:
    def test_delete_skipped_removes_all_skipped(self, server_con):
        _insert_txn(server_con, "tx_skip1", skipped=True, status="enriched")
        _insert_txn(server_con, "tx_skip2", skipped=True, status="enriched")
        _insert_txn(server_con, "tx_keep", skipped=False, status="pending")
        with patch("main.get_con", return_value=server_con):
            asyncio.run(main.bulk_delete_skipped(_req("POST"), _CREDS))
        remaining = {r[0] for r in server_con.execute(
            "SELECT id FROM webhook_queue"
        ).fetchall()}
        assert "tx_skip1" not in remaining
        assert "tx_skip2" not in remaining
        assert "tx_keep" in remaining

    def test_delete_skipped_leaves_non_skipped_intact(self, server_con):
        _insert_txn(server_con, "tx_pend", status="pending", skipped=False)
        _insert_txn(server_con, "tx_enr", status="enriched", skipped=False)
        with patch("main.get_con", return_value=server_con):
            asyncio.run(main.bulk_delete_skipped(_req("POST"), _CREDS))
        count = server_con.execute(
            "SELECT COUNT(*) FROM webhook_queue"
        ).fetchone()[0]
        assert count == 2

    def test_delete_skipped_redirects_to_dashboard(self, server_con):
        with patch("main.get_con", return_value=server_con):
            resp = asyncio.run(main.bulk_delete_skipped(_req("POST"), _CREDS))
        assert isinstance(resp, RedirectResponse)
        assert resp.headers["location"] == "/dashboard"

    def test_requeue_pending_redirects_to_dashboard(self, server_con):
        with patch("main.get_con", return_value=server_con):
            resp = asyncio.run(main.bulk_requeue_pending(_req("POST"), _CREDS))
        assert isinstance(resp, RedirectResponse)
        assert resp.headers["location"] == "/dashboard"


# ── Logs viewer ────────────────────────────────────────────────────────────────

class TestLogsView:
    def test_returns_200(self, server_con):
        with patch("main.get_con", return_value=server_con):
            resp = asyncio.run(main.logs_view(_req(), _CREDS, lines=100))
        assert resp.status_code == 200

    def test_missing_log_file_shows_message(self, server_con, tmp_path):
        fake_path = str(tmp_path / "nonexistent.log")
        with patch("main.LOG_PATH", fake_path), \
             patch("main.get_con", return_value=server_con):
            resp = asyncio.run(main.logs_view(_req(), _CREDS, lines=100))
        assert any("not found" in str(line).lower() for line in resp.context["log_lines"])

    def test_reads_last_n_lines(self, server_con, tmp_path):
        log_file = tmp_path / "server.log"
        log_file.write_text("\n".join(f"line {i}" for i in range(100)))
        with patch("main.LOG_PATH", str(log_file)), \
             patch("main.get_con", return_value=server_con):
            resp = asyncio.run(main.logs_view(_req(), _CREDS, lines=10))
        assert len(resp.context["log_lines"]) == 10
        assert "line 99" in resp.context["log_lines"][-1]


# ── Rule tester ────────────────────────────────────────────────────────────────

class TestRuleTester:
    def _run(self, server_con, form_data: dict):
        req = _form_req(form_data)
        with patch("main.get_con", return_value=server_con):
            return asyncio.run(main.test_rules(req, _CREDS))

    def test_matching_rule_marked_as_matched(self, server_con):
        _insert_rule(server_con, match_field="description", match_type="contains",
                     match_value="gym", auto_context="Gym membership")
        resp = self._run(server_con, {"description": "PureGym monthly", "amount": "9.99"})
        matched = [r for r in resp.context["test_results"] if r["matched"]]
        assert len(matched) == 1
        assert matched[0]["auto_context"] == "Gym membership"

    def test_non_matching_rule_not_marked(self, server_con):
        _insert_rule(server_con, match_field="description", match_type="contains",
                     match_value="gym")
        resp = self._run(server_con, {"description": "Tesco groceries", "amount": "25.00"})
        assert not any(r["matched"] for r in resp.context["test_results"])

    def test_disabled_rule_appears_in_results_but_not_matched(self, server_con):
        _insert_rule(server_con, match_value="gym", enabled=False)
        resp = self._run(server_con, {"description": "PureGym"})
        results = resp.context["test_results"]
        assert len(results) == 1
        assert results[0]["enabled"] is False
        assert results[0]["matched"] is False

    def test_auto_skip_rule_shows_skip_flag(self, server_con):
        _insert_rule(server_con, match_value="refund", auto_skip=True, auto_context="")
        resp = self._run(server_con, {"description": "Monzo refund"})
        matched = [r for r in resp.context["test_results"] if r["matched"]]
        assert matched[0]["auto_skip"] is True

    def test_merchant_name_field_matches(self, server_con):
        _insert_rule(server_con, match_field="merchant_name", match_type="exact",
                     match_value="Netflix", auto_context="Streaming")
        resp = self._run(server_con, {"merchant_name": "Netflix", "description": ""})
        matched = [r for r in resp.context["test_results"] if r["matched"]]
        assert len(matched) == 1

    def test_amount_range_field_matches(self, server_con):
        _insert_rule(server_con, match_field="amount", match_type="amount_range",
                     match_value="490-510", auto_context="Rent")
        # £500 → 50000 pence; route converts float("500") * 100 → 50000
        resp = self._run(server_con, {"amount": "500"})
        matched = [r for r in resp.context["test_results"] if r["matched"]]
        assert len(matched) == 1

    def test_second_condition_must_also_match(self, server_con):
        _insert_rule(server_con, match_field="description", match_value="gym",
                     match_field_2="merchant_name", match_type_2="contains",
                     match_value_2="pure", auto_context="PureGym")
        # Description matches but merchant doesn't
        resp = self._run(server_con, {"description": "gym payment", "merchant_name": "Unknown"})
        assert not any(r["matched"] for r in resp.context["test_results"])
        # Both match
        resp2 = self._run(server_con, {"description": "gym payment", "merchant_name": "PureGym"})
        assert any(r["matched"] for r in resp2.context["test_results"])


# ── Rule edit ──────────────────────────────────────────────────────────────────

class TestRuleEdit:
    def _run(self, server_con, rule_id: int, form_data: dict):
        req = _form_req(form_data)
        with patch("main.get_con", return_value=server_con):
            return asyncio.run(main.edit_rule(rule_id, req, _CREDS))

    def test_updates_name_and_value(self, server_con):
        _insert_rule(server_con, id=1, name="Old name", match_value="old")
        self._run(server_con, 1, {
            "name": "New name", "match_field": "description",
            "match_type": "contains", "match_value": "new",
            "auto_context": "Updated context",
        })
        row = server_con.execute("SELECT name, match_value FROM rules WHERE id = 1").fetchone()
        assert row[0] == "New name"
        assert row[1] == "new"

    def test_updates_auto_skip(self, server_con):
        _insert_rule(server_con, id=1, auto_skip=False)
        self._run(server_con, 1, {
            "name": "Rule", "match_field": "description",
            "match_type": "contains", "match_value": "x",
            "auto_context": "", "auto_skip": "on",
        })
        row = server_con.execute("SELECT auto_skip FROM rules WHERE id = 1").fetchone()
        assert row[0] is True

    def test_missing_required_fields_does_not_update(self, server_con):
        _insert_rule(server_con, id=1, name="Original", match_value="original")
        self._run(server_con, 1, {
            "name": "", "match_field": "description",
            "match_type": "contains", "match_value": "",
            "auto_context": "",
        })
        row = server_con.execute("SELECT name FROM rules WHERE id = 1").fetchone()
        assert row[0] == "Original"

    def test_redirects_to_rules_page(self, server_con):
        _insert_rule(server_con, id=1)
        resp = self._run(server_con, 1, {
            "name": "Rule", "match_field": "description",
            "match_type": "contains", "match_value": "gym",
            "auto_context": "Gym",
        })
        assert isinstance(resp, RedirectResponse)
        assert resp.headers["location"] == "/dashboard/rules"
