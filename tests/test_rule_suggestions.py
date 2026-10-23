"""Tests for the recurring-pattern rule suggester (local side)."""
from unittest.mock import MagicMock, patch

from database_functions import write_to_db
import rule_suggestions


def _txn(id, merchant=None, counterparty=None, category=None, subcategory=None,
         user_context=None, skipped=False):
    return {
        "id": id,
        "payload": {"data": {
            "amount": -1000, "currency": "GBP", "description": "desc",
            "merchant": {"name": merchant} if merchant else None,
            "counterparty": {"name": counterparty} if counterparty else {},
            "is_load": False,
            "created": "2026-01-01T10:00:00", "settled": "2026-01-01T10:00:00",
        }},
        "user_context": user_context, "skipped": skipped,
        "received_at": "2026-01-01T10:00:01", "enriched_at": "2026-01-01T10:00:02",
    }


def _classify(con, txn_id, category, subcategory):
    con.execute(
        "UPDATE transactions SET llm_category = ?, llm_subcategory = ?, classified_at = NOW() WHERE id = ?",
        [category, subcategory, txn_id],
    )


class TestFindCandidates:
    def test_five_consistent_occurrences_qualify(self, db):
        for i in range(5):
            write_to_db([_txn(f"tx{i}", merchant="Tesco")])
            _classify(db, f"tx{i}", "Food & Drink", "Groceries")
        candidates = rule_suggestions.find_candidates()
        assert len(candidates) == 1
        c = candidates[0]
        assert c["match_field"] == "merchant_name"
        assert c["match_value"] == "Tesco"
        assert c["category"] == "Food & Drink"
        assert c["subcategory"] == "Groceries"
        assert c["txn_count"] == 5

    def test_four_occurrences_do_not_qualify(self, db):
        for i in range(4):
            write_to_db([_txn(f"tx{i}", merchant="Tesco")])
            _classify(db, f"tx{i}", "Food & Drink", "Groceries")
        assert rule_suggestions.find_candidates() == []

    def test_inconsistent_category_does_not_qualify(self, db):
        """Four as Groceries, one as Household -- not a reliable pattern,
        even though the merchant repeats 5 times."""
        for i in range(4):
            write_to_db([_txn(f"tx{i}", merchant="Tesco")])
            _classify(db, f"tx{i}", "Food & Drink", "Groceries")
        write_to_db([_txn("tx4", merchant="Tesco")])
        _classify(db, "tx4", "Household", "Supplies")
        assert rule_suggestions.find_candidates() == []

    def test_falls_back_to_counterparty_name_when_no_merchant(self, db):
        for i in range(5):
            write_to_db([_txn(f"tx{i}", counterparty="A Friend")])
            _classify(db, f"tx{i}", "Transfers", "Inbound Transfer")
        candidates = rule_suggestions.find_candidates()
        assert len(candidates) == 1
        assert candidates[0]["match_field"] == "counterparty_name"
        assert candidates[0]["match_value"] == "A Friend"

    def test_skipped_transactions_are_excluded(self, db):
        for i in range(5):
            write_to_db([_txn(f"tx{i}", merchant="Tesco", skipped=True)])
            _classify(db, f"tx{i}", "Food & Drink", "Groceries")
        assert rule_suggestions.find_candidates() == []

    def test_no_merchant_or_counterparty_is_excluded(self, db):
        for i in range(5):
            write_to_db([_txn(f"tx{i}")])
            _classify(db, f"tx{i}", "Food & Drink", "Groceries")
        assert rule_suggestions.find_candidates() == []

    def test_dominant_context_is_reused(self, db):
        for i in range(5):
            write_to_db([_txn(f"tx{i}", merchant="Tesco", user_context="Weekly shop")])
            _classify(db, f"tx{i}", "Food & Drink", "Groceries")
        candidates = rule_suggestions.find_candidates()
        assert candidates[0]["auto_context"] == "Weekly shop"

    def test_no_dominant_context_falls_back_to_a_generic_one(self, db):
        contexts = ["Shop A", "Shop B", "Shop C", "Shop D", "Shop E"]
        for i, ctx in enumerate(contexts):
            write_to_db([_txn(f"tx{i}", merchant="Tesco", user_context=ctx)])
            _classify(db, f"tx{i}", "Food & Drink", "Groceries")
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


class TestSyncCandidates:
    """filter_worth_a_rule is patched to a pass-through in every test here --
    it's an LLM call, covered on its own merits by TestFilterWorthARule, and
    these tests are only about what sync_candidates does with what comes
    back from it."""

    def _pass_through(self, monkeypatch):
        monkeypatch.setattr(rule_suggestions, "filter_worth_a_rule", lambda client, candidates: candidates)

    def test_posts_every_candidate_and_returns_the_count(self, db, monkeypatch):
        self._pass_through(monkeypatch)
        for i in range(5):
            write_to_db([_txn(f"tx{i}", merchant="Tesco")])
            _classify(db, f"tx{i}", "Food & Drink", "Groceries")
        mock_resp = MagicMock(status_code=200)
        with patch("rule_suggestions.requests.post", return_value=mock_resp) as mock_post:
            result = rule_suggestions.sync_candidates()
        assert result == 1
        assert mock_post.call_args.kwargs["json"]["candidates"][0]["match_value"] == "Tesco"

    def test_no_candidates_means_no_request(self, db, monkeypatch):
        self._pass_through(monkeypatch)
        with patch("rule_suggestions.requests.post") as mock_post:
            result = rule_suggestions.sync_candidates()
        assert result == 0
        mock_post.assert_not_called()

    def test_404_is_treated_as_not_yet_deployed(self, db, monkeypatch):
        self._pass_through(monkeypatch)
        for i in range(5):
            write_to_db([_txn(f"tx{i}", merchant="Tesco")])
            _classify(db, f"tx{i}", "Food & Drink", "Groceries")
        mock_resp = MagicMock(status_code=404)
        with patch("rule_suggestions.requests.post", return_value=mock_resp):
            result = rule_suggestions.sync_candidates()
        assert result == 0

    def test_missing_claude_secret_skips_without_calling_the_llm_or_posting(self, db, monkeypatch):
        monkeypatch.setattr(rule_suggestions, "CLAUDE_SECRET", None)
        for i in range(5):
            write_to_db([_txn(f"tx{i}", merchant="Tesco")])
            _classify(db, f"tx{i}", "Food & Drink", "Groceries")
        with patch("rule_suggestions.filter_worth_a_rule") as mock_filter, \
             patch("rule_suggestions.requests.post") as mock_post:
            result = rule_suggestions.sync_candidates()
        assert result == 0
        mock_filter.assert_not_called()
        mock_post.assert_not_called()

    def test_llm_rejecting_every_candidate_means_no_request(self, db, monkeypatch):
        monkeypatch.setattr(rule_suggestions, "filter_worth_a_rule", lambda client, candidates: [])
        for i in range(5):
            write_to_db([_txn(f"tx{i}", merchant="Tesco")])
            _classify(db, f"tx{i}", "Food & Drink", "Groceries")
        with patch("rule_suggestions.requests.post") as mock_post:
            result = rule_suggestions.sync_candidates()
        assert result == 0
        mock_post.assert_not_called()


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
        kept = rule_suggestions.filter_worth_a_rule(client, [_candidate("EE", "Bills", "Phone")])
        assert len(kept) == 1
        assert kept[0]["match_value"] == "EE"
        assert kept[0]["llm_reasoning"] == "Single-purpose telecom bill"

    def test_drops_a_candidate_the_llm_marks_not_worth_a_rule(self):
        client = MagicMock()
        client.messages.create.return_value.content = [MagicMock(
            text='[{"index": 0, "worth_a_rule": false, "reasoning": "General supermarket"}]'
        )]
        kept = rule_suggestions.filter_worth_a_rule(client, [_candidate("Lidl")])
        assert kept == []

    def test_mixed_verdicts_keep_only_the_worthwhile_ones(self):
        client = MagicMock()
        client.messages.create.return_value.content = [MagicMock(text='''[
            {"index": 0, "worth_a_rule": true, "reasoning": "Single-purpose"},
            {"index": 1, "worth_a_rule": false, "reasoning": "General retailer"}
        ]''')]
        kept = rule_suggestions.filter_worth_a_rule(client, [_candidate("EE"), _candidate("Lidl")])
        assert [c["match_value"] for c in kept] == ["EE"]

    def test_empty_candidate_list_short_circuits_without_calling_the_llm(self):
        client = MagicMock()
        assert rule_suggestions.filter_worth_a_rule(client, []) == []
        client.messages.create.assert_not_called()

    def test_api_failure_fails_closed_dropping_everything(self):
        client = MagicMock()
        client.messages.create.side_effect = Exception("API error")
        kept = rule_suggestions.filter_worth_a_rule(client, [_candidate("EE"), _candidate("Lidl")])
        assert kept == []

    def test_malformed_json_fails_closed(self):
        client = MagicMock()
        client.messages.create.return_value.content = [MagicMock(text="not json")]
        kept = rule_suggestions.filter_worth_a_rule(client, [_candidate("EE")])
        assert kept == []

    def test_out_of_range_index_is_ignored_not_crashed_on(self):
        client = MagicMock()
        client.messages.create.return_value.content = [MagicMock(
            text='[{"index": 5, "worth_a_rule": true, "reasoning": "x"}]'
        )]
        kept = rule_suggestions.filter_worth_a_rule(client, [_candidate("EE")])
        assert kept == []
