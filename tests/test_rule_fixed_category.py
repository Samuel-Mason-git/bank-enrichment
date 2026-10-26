"""Rules that file a match straight into a fixed category, skipping the
classifier -- server side: matching, the webhook, a context you give
overriding it, the rule form and the Rules page's dropdowns, and approved
suggestions."""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

from starlette.requests import Request

import main
from check_rules import RuleMatch, check_rules
from tests.server_helpers import add_rule, label_sources, status, transaction


def _run(coro):
    return asyncio.run(coro)


def _taxonomy(con):
    con.execute("INSERT INTO parent_categories (id, name, created_at) VALUES "
                "(1, 'Bills', '2026-01-01'), (2, 'Food & Drink', '2026-01-01')")
    con.execute("INSERT INTO subcategories (id, name, parent_id, created_at) VALUES "
                "(1, 'Phone', 1, '2026-01-01'), (2, 'Broadband', 1, '2026-01-01'), "
                "(3, 'Groceries', 2, '2026-01-01')")


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


EE = dict(match_type="exact", match_value="EE")


class TestCheckRulesReturnsThePlacement:
    def test_a_rule_with_a_placement_returns_it(self, server_con):
        _taxonomy(server_con)
        add_rule(server_con, subcategory_id=1, **EE)
        assert check_rules(MagicMock(merchant={"name": "EE"})) == RuleMatch(None, False, "Bills", "Phone", 1, 1)

    def test_a_placement_alone_counts_as_a_match(self, server_con):
        _taxonomy(server_con)
        add_rule(server_con, subcategory_id=1, **EE)
        assert check_rules(MagicMock(merchant={"name": "EE"})).matched is True

    def test_an_archived_placement_is_ignored(self, server_con):
        """Filing into a category you've since deleted would bring it back."""
        _taxonomy(server_con)
        add_rule(server_con, auto_context="Phone bill", subcategory_id=1, **EE)
        server_con.execute("UPDATE subcategories SET archived_at = '2026-10-01' WHERE id = 1")
        match = check_rules(MagicMock(merchant={"name": "EE"}))
        assert match.subcategory_id is None
        assert match.context == "Phone bill"

    def test_the_lowest_numbered_matching_rule_wins(self, server_con):
        add_rule(server_con, rule_id=2, auto_context="second", **EE)
        add_rule(server_con, rule_id=1, auto_context="first", **EE)
        assert check_rules(MagicMock(merchant={"name": "EE"})).context == "first"


class TestWebhookFilesTheTransaction:
    def test_labels_it_and_sends_no_card(self, server_con):
        _taxonomy(server_con)
        add_rule(server_con, subcategory_id=1, **EE)
        bot = MagicMock()
        _receive(server_con, _webhook(), bot)
        t = transaction(server_con, "tx1")
        assert (t["user_context"], t["llm_category"], t["llm_subcategory"], t["llm_model"]) == \
            ("Bills - Phone", "Bills", "Phone", "rule")
        assert status(server_con, "tx1") == "classified"
        bot.send_card.assert_not_called()

    def test_the_rules_own_context_wins_over_the_label(self, server_con):
        _taxonomy(server_con)
        add_rule(server_con, auto_context="Monthly phone bill", subcategory_id=1, **EE)
        _receive(server_con, _webhook(), MagicMock())
        assert transaction(server_con, "tx1")["user_context"] == "Monthly phone bill"

    def test_a_context_only_rule_leaves_it_for_the_classifier(self, server_con):
        add_rule(server_con, auto_context="Phone bill", **EE)
        _receive(server_con, _webhook(), MagicMock())
        assert label_sources(server_con, "tx1") == []
        assert status(server_con, "tx1") == "enriched"


class TestYourContextOverridesTheRule:
    """A rule files from the transaction's details alone; once you say what it
    was, the classifier should look again with that."""

    def test_editing_the_context_clears_the_rules_label(self, server_con):
        _taxonomy(server_con)
        add_rule(server_con, subcategory_id=1, **EE)
        _receive(server_con, _webhook(), MagicMock())
        with patch.object(main, "get_con", return_value=server_con):
            _run(main.edit_transaction_context("tx1", _form_req({"context": "Actually a gift"}), MagicMock()))
        t = transaction(server_con, "tx1")
        assert (t["user_context"], t["llm_category"]) == ("Actually a gift", None)
        assert status(server_con, "tx1") == "enriched"


class TestRuleForm:
    def _add(self, con, **fields):
        form = {"name": "EE", "match_field": "merchant_name", "match_type": "exact", "match_value": "EE", **fields}
        with patch.object(main, "get_con", return_value=con):
            _run(main.add_rule(_form_req(form), MagicMock()))
        return con.execute("SELECT auto_context, subcategory_id FROM rules").fetchall()

    def test_a_placement_alone_is_enough_to_save_a_rule(self, server_con):
        _taxonomy(server_con)
        assert self._add(server_con, category="Bills", subcategory="Phone") == [("", 1)]

    def test_half_a_placement_and_nothing_else_is_rejected(self, server_con):
        _taxonomy(server_con)
        assert self._add(server_con, category="Bills") == []

    def test_half_a_placement_with_a_context_saves_without_the_half(self, server_con):
        _taxonomy(server_con)
        assert self._add(server_con, auto_context="Phone", category="Bills") == [("Phone", None)]

    def test_a_placement_that_doesnt_exist_isnt_saved_as_one(self, server_con):
        _taxonomy(server_con)
        assert self._add(server_con, auto_context="Phone", category="Bills", subcategory="Nope") == [("Phone", None)]

    def test_editing_sets_and_clears_the_placement(self, server_con):
        _taxonomy(server_con)
        add_rule(server_con, auto_context="Phone", **EE)
        form = {"name": "EE", "match_field": "merchant_name", "match_type": "exact", "match_value": "EE",
                "auto_context": "Phone", "category": "Bills", "subcategory": "Phone"}
        with patch.object(main, "get_con", return_value=server_con):
            _run(main.edit_rule(1, _form_req(form), MagicMock()))
            assert server_con.execute("SELECT subcategory_id FROM rules").fetchone() == (1,)
            form.update(category="", subcategory="")
            _run(main.edit_rule(1, _form_req(form), MagicMock()))
        assert server_con.execute("SELECT subcategory_id FROM rules").fetchone() == (None,)


class TestRulesPageDropdowns:
    def test_group_live_subcategories_under_their_category(self, server_con):
        _taxonomy(server_con)
        server_con.execute("UPDATE subcategories SET archived_at = '2026-10-01' WHERE id = 2")
        assert main._get_taxonomy(server_con) == {"Bills": ["Phone"], "Food & Drink": ["Groceries"]}

    def test_rules_page_offers_them(self, server_con):
        _taxonomy(server_con)
        request = Request({"type": "http", "method": "GET", "path": "/dashboard/rules",
                           "headers": [], "query_string": b"", "app": main.app})
        with patch.object(main, "get_con", return_value=server_con):
            resp = _run(main.rules_view(request, MagicMock()))
        assert resp.context["taxonomy"]["Bills"] == ["Broadband", "Phone"]
        assert '<option value="Bills">Bills</option>' in resp.body.decode()


class TestApprovedSuggestionFixesThePlacement:
    def _approve(self, server_con, bot=None):
        cq = MagicMock(data="rulesug:approve:1", id="cbq")
        cq.from_user.id = 12345
        update = MagicMock(callback_query=cq, message=None)
        req = MagicMock()

        async def _body():
            return b"{}"

        req.body = _body
        with patch.object(main, "get_con", return_value=server_con), \
             patch.object(main, "bot", bot or MagicMock()), \
             patch.object(main.TelegramUpdate, "model_validate_json", return_value=update):
            _run(main.recieve_telegram(req))

    def _suggest(self, con, subcategory="Phone"):
        con.execute(
            """INSERT INTO rule_suggestions
               (id, match_field, match_value, category, subcategory, txn_count, auto_context, status, proposed_at)
               VALUES (1, 'merchant_name', 'EE', 'Bills', %s, 6, 'Phone bill', 'pending', NOW())""",
            [subcategory],
        )

    def test_approval_creates_a_rule_that_skips_the_classifier(self, server_con):
        _taxonomy(server_con)
        self._suggest(server_con)
        self._approve(server_con)
        assert server_con.execute("SELECT subcategory_id FROM rules").fetchone() == (1,)
        assert check_rules(MagicMock(merchant={"name": "EE"})).category == "Bills"

    def test_a_suggestion_for_a_category_since_deleted_makes_no_rule(self, server_con):
        _taxonomy(server_con)
        self._suggest(server_con, subcategory="Gone")
        bot = MagicMock()
        self._approve(server_con, bot)
        assert server_con.execute("SELECT COUNT(*) FROM rules").fetchone()[0] == 0
        assert "doesn't exist any more" in bot.send_message.call_args[0][1]
