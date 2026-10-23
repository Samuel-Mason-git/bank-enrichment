"""End-to-end tests for the rule-suggestion round trip (server side).

Approving one is simpler than a category proposal: Rules already live in
this database, so approval inserts directly into `rules` -- no local
taxonomy round trip needed."""
import asyncio
from unittest.mock import MagicMock, patch

import main
from check_rules import _matches


def _run(coro):
    return asyncio.run(coro)


async def _ok_api_key(api_key):
    return True


def _candidate(match_field="merchant_name", match_value="Tesco", category="Food & Drink",
                subcategory="Groceries", txn_count=5, auto_context="Weekly shop"):
    return main.RuleSuggestionCandidate(
        match_field=match_field, match_value=match_value, category=category,
        subcategory=subcategory, txn_count=txn_count, auto_context=auto_context,
    )


def _insert_rule(con, id=1, match_field="description", match_type="contains",
                  match_value="gym", enabled=True, match_field_2=None):
    con.execute(
        "INSERT INTO rules (id, name, match_field, match_type, match_value, enabled, match_field_2) "
        "VALUES (?, 'r', ?, ?, ?, ?, ?)",
        [id, match_field, match_type, match_value, enabled, match_field_2],
    )


def _callback(data: str):
    cq = MagicMock()
    cq.data = data
    cq.id = "cbq_1"
    cq.from_user.id = 12345
    update = MagicMock()
    update.callback_query = cq
    update.message = None
    req = MagicMock()

    async def _body():
        return b"{}"

    req.body = _body
    return update, req


class TestAlreadyCovered:
    def test_matching_exact_rule_covers_it(self, server_con):
        _insert_rule(server_con, match_field="merchant_name", match_type="exact", match_value="Tesco")
        assert main._rule_suggestion_already_covered(server_con, "merchant_name", "Tesco") is True

    def test_non_matching_rule_does_not_cover_it(self, server_con):
        _insert_rule(server_con, match_field="merchant_name", match_type="exact", match_value="Asda")
        assert main._rule_suggestion_already_covered(server_con, "merchant_name", "Tesco") is False

    def test_disabled_rule_does_not_cover_it(self, server_con):
        _insert_rule(server_con, match_field="merchant_name", match_type="exact", match_value="Tesco", enabled=False)
        assert main._rule_suggestion_already_covered(server_con, "merchant_name", "Tesco") is False

    def test_a_rule_with_a_second_condition_is_not_treated_as_covering(self, server_con):
        """Can't verify a second condition without a full transaction -- erring
        toward still suggesting rather than silently missing a useful rule."""
        _insert_rule(server_con, match_field="merchant_name", match_type="exact",
                     match_value="Tesco", match_field_2="amount")
        assert main._rule_suggestion_already_covered(server_con, "merchant_name", "Tesco") is False

    def test_no_rules_at_all_does_not_cover_it(self, server_con):
        assert main._rule_suggestion_already_covered(server_con, "merchant_name", "Tesco") is False


class TestSyncRuleSuggestions:
    def _sync(self, server_con, bot, candidates):
        body = main.SyncRuleSuggestionsRequest(candidates=candidates)
        with patch.object(main, "get_con", return_value=server_con), \
             patch.object(main, "bot", bot), \
             patch.dict("os.environ", {"TELEGRAM_CHAT_ID": "12345"}), \
             patch.object(main, "verify_api_key", new=_ok_api_key):
            return _run(main.sync_rule_suggestions(body, api_key="k"))

    def test_new_candidate_is_stored_and_a_card_sent(self, server_con):
        bot = MagicMock()
        result = self._sync(server_con, bot, [_candidate()])
        assert result == {"received": 1}
        bot.send_rule_suggestion.assert_called_once()
        row = server_con.execute(
            "SELECT match_field, match_value, status FROM rule_suggestions"
        ).fetchone()
        assert row == ("merchant_name", "Tesco", "pending")

    def test_candidate_already_asked_about_is_skipped(self, server_con):
        server_con.execute(
            """INSERT INTO rule_suggestions
               (id, match_field, match_value, category, subcategory, txn_count, auto_context, status, proposed_at)
               VALUES (1, 'merchant_name', 'Tesco', 'X', 'Y', 5, '', 'denied', NOW())"""
        )
        bot = MagicMock()
        result = self._sync(server_con, bot, [_candidate()])
        assert result == {"received": 0}
        bot.send_rule_suggestion.assert_not_called()

    def test_candidate_already_covered_by_an_enabled_rule_is_skipped(self, server_con):
        _insert_rule(server_con, match_field="merchant_name", match_type="exact", match_value="Tesco")
        bot = MagicMock()
        result = self._sync(server_con, bot, [_candidate()])
        assert result == {"received": 0}
        bot.send_rule_suggestion.assert_not_called()

    def test_multiple_new_candidates_each_get_their_own_card(self, server_con):
        bot = MagicMock()
        result = self._sync(server_con, bot, [
            _candidate(match_value="Tesco"), _candidate(match_value="Asda"),
        ])
        assert result == {"received": 2}
        assert bot.send_rule_suggestion.call_count == 2


class TestRuleSuggestionCallback:
    def _seed(self, con, id=1, status="pending", match_field="merchant_name",
              match_value="Tesco", auto_context="Weekly shop"):
        con.execute(
            """INSERT INTO rule_suggestions
               (id, match_field, match_value, category, subcategory, txn_count, auto_context, status, proposed_at)
               VALUES (?, ?, ?, 'Food & Drink', 'Groceries', 5, ?, ?, NOW())""",
            [id, match_field, match_value, auto_context, status],
        )

    def _tap(self, server_con, data):
        bot = MagicMock()
        update, req = _callback(data)
        with patch.object(main, "get_con", return_value=server_con), \
             patch.object(main, "bot", bot), \
             patch.object(main.TelegramUpdate, "model_validate_json", return_value=update):
            _run(main.recieve_telegram(req))
        return bot

    def test_approve_creates_a_rule_and_marks_approved(self, server_con):
        self._seed(server_con)
        self._tap(server_con, "rulesug:approve:1")
        rule = server_con.execute(
            "SELECT match_field, match_type, match_value, auto_context FROM rules"
        ).fetchone()
        assert rule == ("merchant_name", "exact", "Tesco", "Weekly shop")
        status = server_con.execute("SELECT status FROM rule_suggestions WHERE id = 1").fetchone()[0]
        assert status == "approved"

    def test_created_rule_actually_matches_what_it_was_suggested_for(self, server_con):
        """Round-trip check: the rule approval creates must actually fire for
        the exact pattern it was suggested from."""
        self._seed(server_con)
        self._tap(server_con, "rulesug:approve:1")
        rule = server_con.execute("SELECT match_type, match_value FROM rules").fetchone()
        assert _matches("Tesco", rule[0], rule[1]) is True

    def test_deny_marks_denied_and_creates_no_rule(self, server_con):
        self._seed(server_con)
        self._tap(server_con, "rulesug:deny:1")
        assert server_con.execute("SELECT COUNT(*) FROM rules").fetchone()[0] == 0
        status = server_con.execute("SELECT status FROM rule_suggestions WHERE id = 1").fetchone()[0]
        assert status == "denied"

    def test_tapping_an_already_decided_suggestion_does_not_create_a_second_rule(self, server_con):
        self._seed(server_con, status="approved")
        bot = self._tap(server_con, "rulesug:approve:1")
        assert server_con.execute("SELECT COUNT(*) FROM rules").fetchone()[0] == 0
        assert "already" in bot.send_message.call_args.args[1].lower()

    def test_tapping_a_nonexistent_suggestion_does_not_crash(self, server_con):
        bot = self._tap(server_con, "rulesug:approve:999")
        assert "no longer exists" in bot.send_message.call_args.args[1].lower()
