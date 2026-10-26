"""Tests for the recurring-pattern rule suggester (local side)."""
from unittest.mock import MagicMock, patch

import rule_suggestions
from tests.local_helpers import insert_transaction


def _add(con, n, merchant=None, counterparty=None, category="Food & Drink", subcategory="Groceries",
         user_context=None, skipped=False, start=0):
    """n transactions with the same merchant/counterparty, all filed the same way."""
    for i in range(start, start + n):
        insert_transaction(con, f"tx{i}", merchant_name=merchant, counterparty_name=counterparty,
                           user_context=user_context, skipped=skipped,
                           llm_category=category, llm_subcategory=subcategory)


class TestFindCandidates:
    def test_five_consistent_occurrences_qualify(self, db):
        _add(db, 5, merchant="Tesco")
        candidates = rule_suggestions.find_candidates()
        assert len(candidates) == 1
        c = candidates[0]
        assert c["match_field"] == "merchant_name"
        assert c["match_value"] == "Tesco"
        assert c["category"] == "Food & Drink"
        assert c["subcategory"] == "Groceries"
        assert c["txn_count"] == 5

    def test_four_occurrences_do_not_qualify(self, db):
        _add(db, 4, merchant="Tesco")
        assert rule_suggestions.find_candidates() == []

    def test_inconsistent_category_does_not_qualify(self, db):
        """Four as Groceries, one as Household -- not a reliable pattern,
        even though the merchant repeats 5 times."""
        _add(db, 4, merchant="Tesco")
        _add(db, 1, merchant="Tesco", category="Household", subcategory="Supplies", start=4)
        assert rule_suggestions.find_candidates() == []

    def test_falls_back_to_counterparty_name_when_no_merchant(self, db):
        _add(db, 5, counterparty="A Friend", category="Transfers", subcategory="Inbound Transfer")
        candidates = rule_suggestions.find_candidates()
        assert len(candidates) == 1
        assert candidates[0]["match_field"] == "counterparty_name"
        assert candidates[0]["match_value"] == "A Friend"

    def test_skipped_transactions_are_excluded(self, db):
        _add(db, 5, merchant="Tesco", skipped=True)
        assert rule_suggestions.find_candidates() == []

    def test_no_merchant_or_counterparty_is_excluded(self, db):
        _add(db, 5)
        assert rule_suggestions.find_candidates() == []

    def test_dominant_context_is_reused(self, db):
        _add(db, 5, merchant="Tesco", user_context="Weekly shop")
        candidates = rule_suggestions.find_candidates()
        assert candidates[0]["auto_context"] == "Weekly shop"

    def test_no_dominant_context_falls_back_to_a_generic_one(self, db):
        contexts = ["Shop A", "Shop B", "Shop C", "Shop D", "Shop E"]
        for i, ctx in enumerate(contexts):
            _add(db, 1, merchant="Tesco", user_context=ctx, start=i)
        candidates = rule_suggestions.find_candidates()
        assert candidates[0]["auto_context"] == "Tesco purchase"


class TestDominantContext:
    def test_majority_wins(self):
        assert rule_suggestions._dominant_context(["Shop", "Shop", "Shop", "Other"]) == "Shop"

    def test_exactly_half_is_not_a_majority(self):
        assert rule_suggestions._dominant_context(["Shop", "Shop", "Other", "Other"]) is None

    def test_a_single_occurrence_is_not_enough_even_if_unopposed(self):
        assert rule_suggestions._dominant_context(["Shop"]) is None

    def test_empty_list_returns_none(self):
        assert rule_suggestions._dominant_context([]) is None


def _suggested(con):
    return [r[0] for r in con.execute("SELECT match_value FROM rule_suggestions ORDER BY id").fetchall()]


class TestSyncCandidates:
    """filter_worth_a_rule is patched in every test here -- it's an LLM call,
    covered on its own by TestFilterWorthARule; these are about what
    sync_candidates does with what comes back."""

    def _pass_through(self, monkeypatch):
        monkeypatch.setattr(rule_suggestions, "CLAUDE_SECRET", "test-key")
        monkeypatch.setattr(rule_suggestions, "filter_worth_a_rule", lambda client, candidates: (candidates, []))

    def test_stores_a_pending_suggestion_for_the_server_to_send(self, db, monkeypatch):
        self._pass_through(monkeypatch)
        _add(db, 5, merchant="Tesco")
        assert rule_suggestions.sync_candidates() == 1
        row = db.execute(
            "SELECT match_field, category, subcategory, txn_count, status, sent_at FROM rule_suggestions"
        ).fetchone()
        assert row == ("merchant_name", "Food & Drink", "Groceries", 5, "pending", None)

    def test_no_candidates_means_nothing_stored(self, db, monkeypatch):
        self._pass_through(monkeypatch)
        assert rule_suggestions.sync_candidates() == 0
        assert _suggested(db) == []

    def test_a_pattern_is_only_ever_suggested_once(self, db, monkeypatch):
        self._pass_through(monkeypatch)
        _add(db, 5, merchant="Tesco")
        rule_suggestions.sync_candidates()
        db.execute("UPDATE rule_suggestions SET status = 'denied'")
        assert rule_suggestions.sync_candidates() == 0
        assert _suggested(db) == ["Tesco"]

    def test_already_asked_patterns_never_reach_the_llm(self, db, monkeypatch):
        """The dedupe runs before the worthiness check, so Claude isn't paid
        to judge the same pattern twice."""
        self._pass_through(monkeypatch)
        _add(db, 5, merchant="Tesco")
        rule_suggestions.sync_candidates()
        with patch("rule_suggestions.filter_worth_a_rule") as mock_filter:
            rule_suggestions.sync_candidates()
        mock_filter.assert_not_called()

    def test_a_pattern_an_enabled_rule_already_catches_is_dropped(self, db, monkeypatch):
        self._pass_through(monkeypatch)
        _add(db, 5, merchant="Tesco Express")
        db.execute("INSERT INTO rules (id, name, match_field, match_type, match_value) "
                   "VALUES (1, 'Tesco', 'merchant_name', 'contains', 'tesco')")
        assert rule_suggestions.sync_candidates() == 0

    def test_a_disabled_or_two_condition_rule_doesnt_count(self, db, monkeypatch):
        self._pass_through(monkeypatch)
        _add(db, 5, merchant="Tesco")
        db.execute("INSERT INTO rules (id, name, match_field, match_type, match_value, enabled) "
                   "VALUES (1, 'Off', 'merchant_name', 'exact', 'Tesco', FALSE)")
        db.execute("INSERT INTO rules (id, name, match_field, match_type, match_value, "
                   "match_field_2, match_type_2, match_value_2) "
                   "VALUES (2, 'Two', 'merchant_name', 'exact', 'Tesco', 'amount', 'gt', '5')")
        assert rule_suggestions.sync_candidates() == 1

    def test_missing_claude_secret_skips_without_calling_the_llm(self, db, monkeypatch):
        monkeypatch.setattr(rule_suggestions, "CLAUDE_SECRET", None)
        _add(db, 5, merchant="Tesco")
        with patch("rule_suggestions.filter_worth_a_rule") as mock_filter:
            assert rule_suggestions.sync_candidates() == 0
        mock_filter.assert_not_called()
        assert _suggested(db) == []

    def test_a_failed_check_stores_nothing_so_it_is_asked_again_next_run(self, db, monkeypatch):
        monkeypatch.setattr(rule_suggestions, "CLAUDE_SECRET", "test-key")
        monkeypatch.setattr(rule_suggestions, "filter_worth_a_rule", lambda client, candidates: ([], []))
        _add(db, 5, merchant="Tesco")
        assert rule_suggestions.sync_candidates() == 0
        assert _suggested(db) == []

    def test_a_declined_pattern_is_remembered_with_no_card(self, db, monkeypatch):
        monkeypatch.setattr(rule_suggestions, "CLAUDE_SECRET", "test-key")
        monkeypatch.setattr(rule_suggestions, "filter_worth_a_rule", lambda client, candidates: (
            [], [{**c, "llm_reasoning": "A general supermarket."} for c in candidates]))
        _add(db, 5, merchant="Tesco")
        assert rule_suggestions.sync_candidates() == 0
        assert db.execute("SELECT match_value, status, llm_reasoning FROM rule_suggestions").fetchall() == [
            ("Tesco", "declined", "A general supermarket.")]

    def test_a_declined_pattern_never_reaches_the_llm_again(self, db, monkeypatch):
        """The reason for remembering: otherwise the same pattern is paid for
        on every run."""
        monkeypatch.setattr(rule_suggestions, "CLAUDE_SECRET", "test-key")
        monkeypatch.setattr(rule_suggestions, "filter_worth_a_rule", lambda client, candidates: ([], candidates))
        _add(db, 5, merchant="Tesco")
        rule_suggestions.sync_candidates()
        with patch("rule_suggestions.filter_worth_a_rule") as mock_filter:
            rule_suggestions.sync_candidates()
        mock_filter.assert_not_called()

    def test_the_server_never_sends_a_card_for_a_declined_one(self, db, pg, monkeypatch):
        from unittest.mock import MagicMock as Mock
        import server_db
        from follow_up_tg import run_new_cards
        monkeypatch.setattr(rule_suggestions, "CLAUDE_SECRET", "test-key")
        monkeypatch.setattr(rule_suggestions, "filter_worth_a_rule", lambda client, candidates: ([], candidates))
        _add(db, 5, merchant="Tesco")
        rule_suggestions.sync_candidates()
        monkeypatch.setattr(server_db, "_con", pg)
        bot = Mock()
        run_new_cards(bot)
        bot.send_rule_suggestion.assert_not_called()


class TestTextMatches:
    def test_exact_ignores_case(self):
        assert rule_suggestions._text_matches("TESCO", "exact", "tesco")

    def test_contains(self):
        assert rule_suggestions._text_matches("Tesco Express", "contains", "express")

    def test_regex(self):
        assert rule_suggestions._text_matches("Tesco 123", "regex", r"tesco \d+")

    def test_a_broken_regex_is_no_match(self):
        assert not rule_suggestions._text_matches("Tesco", "regex", "(")

    def test_an_amount_condition_never_matches_a_name(self):
        assert not rule_suggestions._text_matches("10", "gt", "5")


def _candidate(match_value="Tesco", category="Food & Drink", subcategory="Groceries", txn_count=5):
    return {
        "match_field": "merchant_name", "match_value": match_value,
        "category": category, "subcategory": subcategory,
        "txn_count": txn_count, "auto_context": f"{match_value} purchase",
    }


class TestFilterWorthARule:
    def test_keeps_a_candidate_the_llm_marks_worth_a_rule(self):
        client = MagicMock()
        client.messages.create.return_value.content = [MagicMock(
            text='[{"index": 0, "worth_a_rule": true, "reasoning": "Single-purpose telecom bill"}]'
        )]
        kept, declined = rule_suggestions.filter_worth_a_rule(client, [_candidate("EE", "Bills", "Phone")])
        assert len(kept) == 1 and declined == []
        assert kept[0]["match_value"] == "EE"
        assert kept[0]["llm_reasoning"] == "Single-purpose telecom bill"

    def test_declines_a_candidate_the_llm_marks_not_worth_a_rule(self):
        client = MagicMock()
        client.messages.create.return_value.content = [MagicMock(
            text='[{"index": 0, "worth_a_rule": false, "reasoning": "General supermarket"}]'
        )]
        kept, declined = rule_suggestions.filter_worth_a_rule(client, [_candidate("Lidl")])
        assert kept == []
        assert [(c["match_value"], c["llm_reasoning"]) for c in declined] == [("Lidl", "General supermarket")]

    def test_mixed_verdicts_keep_only_the_worthwhile_ones(self):
        client = MagicMock()
        client.messages.create.return_value.content = [MagicMock(text='''[
            {"index": 0, "worth_a_rule": true, "reasoning": "Single-purpose"},
            {"index": 1, "worth_a_rule": false, "reasoning": "General retailer"}
        ]''')]
        kept, declined = rule_suggestions.filter_worth_a_rule(client, [_candidate("EE"), _candidate("Lidl")])
        assert [c["match_value"] for c in kept] == ["EE"]
        assert [c["match_value"] for c in declined] == ["Lidl"]

    def test_empty_candidate_list_short_circuits_without_calling_the_llm(self):
        client = MagicMock()
        assert rule_suggestions.filter_worth_a_rule(client, []) == ([], [])
        client.messages.create.assert_not_called()

    def test_api_failure_fails_closed_deciding_nothing(self):
        """Neither suggested nor declined: a glitch mustn't dismiss a pattern for good."""
        client = MagicMock()
        client.messages.create.side_effect = Exception("API error")
        assert rule_suggestions.filter_worth_a_rule(client, [_candidate("EE"), _candidate("Lidl")]) == ([], [])

    def test_malformed_json_fails_closed(self):
        client = MagicMock()
        client.messages.create.return_value.content = [MagicMock(text="not json")]
        assert rule_suggestions.filter_worth_a_rule(client, [_candidate("EE")]) == ([], [])

    def test_an_answer_with_no_verdict_decides_nothing(self):
        client = MagicMock()
        client.messages.create.return_value.content = [MagicMock(text='[{"index": 0, "reasoning": "hmm"}]')]
        assert rule_suggestions.filter_worth_a_rule(client, [_candidate("EE")]) == ([], [])

    def test_out_of_range_index_is_ignored_not_crashed_on(self):
        client = MagicMock()
        client.messages.create.return_value.content = [MagicMock(
            text='[{"index": 5, "worth_a_rule": true, "reasoning": "x"}]'
        )]
        assert rule_suggestions.filter_worth_a_rule(client, [_candidate("EE")]) == ([], [])
