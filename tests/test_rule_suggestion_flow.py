"""The rule-suggestion round trip, end to end on the shared tables.

The pipeline spots a pattern that's always filed the same way and stores a
suggestion -> the server sends its card -> you approve or deny -> approving
makes a rule straight away, which files the next matching transaction itself.
Which patterns get suggested at all is tested in test_rule_suggestions.py.
"""
from unittest.mock import MagicMock

import main
import rule_suggestions
from check_rules import _matches, check_rules
from follow_up_tg import run_new_cards
from tests.local_helpers import insert_transaction
from tests.server_helpers import monzo_payload, replies, tap


def _suggest(db, monkeypatch, merchant="Tesco", times=5):
    monkeypatch.setattr(rule_suggestions, "CLAUDE_SECRET", "test-key")
    monkeypatch.setattr(rule_suggestions, "filter_worth_a_rule",
                        lambda client, candidates: [{**c, "llm_reasoning": "Always groceries."} for c in candidates])
    for i in range(times):
        insert_transaction(db, f"tx{i}", merchant_name=merchant, user_context="Weekly shop",
                           llm_category="Food & Drink", llm_subcategory="Groceries")
    assert rule_suggestions.sync_candidates() == 1
    return db.execute("SELECT id FROM rule_suggestions").fetchone()[0]


def _send():
    bot = MagicMock()
    run_new_cards(bot)
    return bot


class TestTheRoundTrip:
    def test_an_approved_suggestion_files_the_next_matching_transaction(self, db, server_con, monkeypatch):
        sid = _suggest(db, monkeypatch)

        card = _send().send_rule_suggestion.call_args.args[1]
        assert (card["match_value"], card["txn_count"], card["llm_reasoning"]) == ("Tesco", 5, "Always groceries.")

        tap(server_con, f"rulesug:approve:{sid}")

        match = check_rules(main.MonzoOuter.model_validate(monzo_payload("tx_next", merchant="Tesco")).data)
        assert (match.category, match.subcategory, match.context) == ("Food & Drink", "Groceries", "Weekly shop")

    def test_a_denied_suggestion_makes_no_rule_and_is_never_asked_again(self, db, server_con, monkeypatch):
        sid = _suggest(db, monkeypatch)
        _send()
        tap(server_con, f"rulesug:deny:{sid}")

        assert server_con.execute("SELECT COUNT(*) FROM rules").fetchone()[0] == 0
        insert_transaction(db, "tx_more", merchant_name="Tesco", llm_category="Food & Drink", llm_subcategory="Groceries")
        assert rule_suggestions.sync_candidates() == 0

    def test_a_card_is_only_sent_once(self, db, server_con, monkeypatch):
        _suggest(db, monkeypatch)
        _send()
        assert _send().send_rule_suggestion.call_count == 0


class TestTheTaps:
    def _pending(self, con, status="pending", category="Food & Drink", subcategory="Groceries"):
        con.execute(
            """INSERT INTO rule_suggestions
               (id, match_field, match_value, category, subcategory, txn_count, auto_context, status, proposed_at)
               VALUES (1, 'merchant_name', 'Tesco', %s, %s, 5, 'Weekly shop', %s, NOW())""",
            [category, subcategory, status],
        )

    def _taxonomy(self, con):
        con.execute("INSERT INTO parent_categories (id, name, created_at) VALUES (1, 'Food & Drink', NOW())")
        con.execute("INSERT INTO subcategories (id, name, parent_id, created_at) VALUES (7, 'Groceries', 1, NOW())")

    def test_approve_makes_an_exact_rule_with_the_fixed_placement(self, server_con):
        self._taxonomy(server_con)
        self._pending(server_con)
        tap(server_con, "rulesug:approve:1")
        rule = server_con.execute(
            "SELECT match_field, match_type, match_value, auto_context, subcategory_id FROM rules").fetchone()
        assert rule == ("merchant_name", "exact", "Tesco", "Weekly shop", 7)
        assert _matches("Tesco", rule[1], rule[2])
        assert server_con.execute("SELECT status FROM rule_suggestions").fetchone()[0] == "approved"

    def test_a_category_deleted_meanwhile_makes_no_rule(self, server_con):
        """It'll be suggested again if it still fits."""
        self._pending(server_con)
        bot = tap(server_con, "rulesug:approve:1")
        assert server_con.execute("SELECT COUNT(*) FROM rules").fetchone()[0] == 0
        assert server_con.execute("SELECT COUNT(*) FROM rule_suggestions").fetchone()[0] == 0
        assert "doesn't exist any more" in replies(bot)[0]

    def test_deny_makes_no_rule(self, server_con):
        self._taxonomy(server_con)
        self._pending(server_con)
        tap(server_con, "rulesug:deny:1")
        assert server_con.execute("SELECT COUNT(*) FROM rules").fetchone()[0] == 0
        assert server_con.execute("SELECT status FROM rule_suggestions").fetchone()[0] == "denied"

    def test_a_second_tap_makes_no_second_rule(self, server_con):
        self._taxonomy(server_con)
        self._pending(server_con, status="approved")
        bot = tap(server_con, "rulesug:approve:1")
        assert server_con.execute("SELECT COUNT(*) FROM rules").fetchone()[0] == 0
        assert "already" in replies(bot)[0].lower()

    def test_a_tap_on_an_unknown_suggestion_says_so(self, server_con):
        assert "no longer exists" in replies(tap(server_con, "rulesug:approve:999"))[0]
