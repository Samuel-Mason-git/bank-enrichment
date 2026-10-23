"""Rules that file a match straight into a fixed category, skipping the
classifier -- server side: matching, the webhook, the export, the rule
form, the taxonomy sync behind its dropdowns, and approved suggestions."""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

from starlette.requests import Request

import main
from check_rules import RuleMatch, check_rules


def _run(coro):
    return asyncio.run(coro)


async def _ok_api_key(api_key):
    return True


def _add_rule(con, id=1, match_field="merchant_name", match_type="exact", match_value="EE",
              auto_context="", auto_skip=False, category=None, subcategory=None):
    con.execute(
        "INSERT INTO rules (id, name, match_field, match_type, match_value, auto_context, auto_skip, category, subcategory) "
        "VALUES (?, 'r', ?, ?, ?, ?, ?, ?, ?)",
        [id, match_field, match_type, match_value, auto_context, auto_skip, category, subcategory],
    )


def _webhook(txn_id="tx1", merchant="EE", amount=-3000):
    payload = {"type": "transaction.created", "data": {
        "account_id": "acc_1", "amount": amount, "created": "2026-10-20T10:00:00.000Z",
        "currency": "GBP", "description": "EE LIMITED", "id": txn_id,
        "category": "bills", "is_load": False, "merchant": {"name": merchant},
    }}
    req = MagicMock()

    async def _body():
        return json.dumps(payload).encode()

    req.body = _body
    return req


def _receive(server_con, req, bot):
    with patch.object(main, "get_con", return_value=server_con), \
         patch.object(main, "bot", bot), \
         patch.object(main, "get_quick_categories", return_value=[]):
        return _run(main.recieve_monzo(req))


def _form_req(data: dict):
    req = MagicMock()
    req.form = AsyncMock(return_value=data)
    return req


class TestCheckRulesReturnsThePlacement:
    def test_a_rule_with_both_returns_them(self, server_con):
        _add_rule(server_con, category="Bills", subcategory="Phone")
        assert check_rules(MagicMock(merchant={"name": "EE"})) == RuleMatch(None, False, "Bills", "Phone")

    def test_half_a_pair_is_not_a_placement(self, server_con):
        _add_rule(server_con, auto_context="Phone bill", category="Bills")
        match = check_rules(MagicMock(merchant={"name": "EE"}))
        assert match.category is None and match.subcategory is None
        assert match.context == "Phone bill"

    def test_a_placement_alone_counts_as_a_match(self, server_con):
        _add_rule(server_con, category="Bills", subcategory="Phone")
        assert check_rules(MagicMock(merchant={"name": "EE"})).matched is True


class TestWebhookTagsTheTransaction:
    def test_stores_the_placement_and_sends_no_card(self, server_con):
        _add_rule(server_con, category="Bills", subcategory="Phone")
        bot = MagicMock()
        _receive(server_con, _webhook(), bot)
        row = server_con.execute(
            "SELECT status, user_context, rule_category, rule_subcategory FROM webhook_queue WHERE id = 'tx1'"
        ).fetchone()
        assert row == ("enriched", "Bills - Phone", "Bills", "Phone")
        bot.send_card.assert_not_called()

    def test_the_rules_own_context_wins_over_the_label(self, server_con):
        _add_rule(server_con, auto_context="Monthly phone bill", category="Bills", subcategory="Phone")
        _receive(server_con, _webhook(), MagicMock())
        assert server_con.execute(
            "SELECT user_context FROM webhook_queue WHERE id = 'tx1'").fetchone()[0] == "Monthly phone bill"

    def test_a_context_only_rule_leaves_no_placement(self, server_con):
        _add_rule(server_con, auto_context="Phone bill")
        _receive(server_con, _webhook(), MagicMock())
        assert server_con.execute(
            "SELECT rule_category, rule_subcategory FROM webhook_queue WHERE id = 'tx1'").fetchone() == (None, None)

    def test_export_carries_the_placement_down(self, server_con):
        _add_rule(server_con, category="Bills", subcategory="Phone")
        _receive(server_con, _webhook(), MagicMock())
        with patch.object(main, "get_con", return_value=server_con), \
             patch.object(main, "verify_api_key", new=_ok_api_key):
            exported = _run(main.export(api_key="k"))
        assert exported[0]["rule_category"] == "Bills"
        assert exported[0]["rule_subcategory"] == "Phone"


class TestManualContextClearsThePlacement:
    """Otherwise a stale rule tag would still win locally over what you just
    typed or reset by hand."""

    def _tagged(self, con):
        con.execute(
            "INSERT INTO webhook_queue (id, payload, received_at, status, user_context, rule_category, rule_subcategory) "
            "VALUES ('tx1', ?, NOW(), 'enriched', 'Bills - Phone', 'Bills', 'Phone')",
            [json.dumps({"data": {"id": "tx1", "amount": -100}})],
        )

    def _placement(self, con):
        return con.execute("SELECT rule_category, rule_subcategory FROM webhook_queue WHERE id = 'tx1'").fetchone()

    def test_editing_the_context_clears_it(self, server_con):
        self._tagged(server_con)
        with patch.object(main, "get_con", return_value=server_con):
            _run(main.edit_transaction_context("tx1", _form_req({"context": "Actually a gift"}), MagicMock()))
        assert self._placement(server_con) == (None, None)

    def test_resetting_clears_it(self, server_con):
        self._tagged(server_con)
        with patch.object(main, "get_con", return_value=server_con):
            _run(main.reset_transaction("tx1", MagicMock(), MagicMock()))
        assert self._placement(server_con) == (None, None)


class TestRuleForm:
    def _add(self, con, **fields):
        form = {"name": "EE", "match_field": "merchant_name", "match_type": "exact", "match_value": "EE", **fields}
        with patch.object(main, "get_con", return_value=con):
            _run(main.add_rule(_form_req(form), MagicMock()))
        return con.execute("SELECT auto_context, category, subcategory FROM rules").fetchall()

    def test_a_placement_alone_is_enough_to_save_a_rule(self, server_con):
        assert self._add(server_con, category="Bills", subcategory="Phone") == [("", "Bills", "Phone")]

    def test_half_a_placement_and_nothing_else_is_rejected(self, server_con):
        assert self._add(server_con, category="Bills") == []

    def test_half_a_placement_with_a_context_saves_without_the_half(self, server_con):
        assert self._add(server_con, auto_context="Phone", category="Bills") == [("Phone", None, None)]

    def test_editing_sets_and_clears_the_placement(self, server_con):
        _add_rule(server_con, auto_context="Phone")
        form = {"name": "EE", "match_field": "merchant_name", "match_type": "exact", "match_value": "EE",
                "auto_context": "Phone", "category": "Bills", "subcategory": "Phone"}
        with patch.object(main, "get_con", return_value=server_con):
            _run(main.edit_rule(1, _form_req(form), MagicMock()))
            assert server_con.execute("SELECT category, subcategory FROM rules").fetchone() == ("Bills", "Phone")
            form.update(category="", subcategory="")
            _run(main.edit_rule(1, _form_req(form), MagicMock()))
        assert server_con.execute("SELECT category, subcategory FROM rules").fetchone() == (None, None)


class TestTaxonomySync:
    def _sync(self, con, entries):
        body = main.SyncTaxonomyRequest(entries=[main.TaxonomyEntry(category=c, subcategory=s) for c, s in entries])
        with patch.object(main, "get_con", return_value=con), \
             patch.object(main, "verify_api_key", new=_ok_api_key):
            return _run(main.sync_taxonomy(body, api_key="k"))

    def test_groups_subcategories_under_their_category(self, server_con):
        self._sync(server_con, [("Bills", "Phone"), ("Bills", "Broadband"), ("Food & Drink", "Groceries")])
        assert main._get_taxonomy(server_con) == {
            "Bills": ["Broadband", "Phone"], "Food & Drink": ["Groceries"]}

    def test_each_sync_replaces_the_last(self, server_con):
        """A category deleted or renamed locally stops being offered."""
        self._sync(server_con, [("Bills", "Phone")])
        self._sync(server_con, [("Bills", "Mobile")])
        assert main._get_taxonomy(server_con) == {"Bills": ["Mobile"]}

    def test_duplicate_pairs_are_collapsed(self, server_con):
        assert self._sync(server_con, [("Bills", "Phone"), ("Bills", "Phone")]) == {"synced": 1}

    def test_rules_page_offers_the_synced_names(self, server_con):
        self._sync(server_con, [("Bills", "Phone")])
        request = Request({"type": "http", "method": "GET", "path": "/dashboard/rules",
                           "headers": [], "query_string": b"", "app": main.app})
        with patch.object(main, "get_con", return_value=server_con):
            resp = _run(main.rules_view(request, MagicMock()))
        assert resp.context["taxonomy"] == {"Bills": ["Phone"]}
        assert '<option value="Bills">Bills</option>' in resp.body.decode()


class TestApprovedSuggestionFixesThePlacement:
    def test_approval_creates_a_rule_that_skips_the_classifier(self, server_con):
        server_con.execute(
            """INSERT INTO rule_suggestions
               (id, match_field, match_value, category, subcategory, txn_count, auto_context, status, proposed_at)
               VALUES (1, 'merchant_name', 'EE', 'Bills', 'Phone', 6, 'Phone bill', 'pending', NOW())"""
        )
        cq = MagicMock(data="rulesug:approve:1", id="cbq")
        cq.from_user.id = 12345
        update = MagicMock(callback_query=cq, message=None)
        req = MagicMock()

        async def _body():
            return b"{}"

        req.body = _body
        with patch.object(main, "get_con", return_value=server_con), \
             patch.object(main, "bot", MagicMock()), \
             patch.object(main.TelegramUpdate, "model_validate_json", return_value=update):
            _run(main.recieve_telegram(req))
        assert server_con.execute("SELECT category, subcategory FROM rules").fetchone() == ("Bills", "Phone")
        assert check_rules(MagicMock(merchant={"name": "EE"})).category == "Bills"
