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
    def test_posts_every_candidate_and_returns_the_count(self, db):
        for i in range(5):
            write_to_db([_txn(f"tx{i}", merchant="Tesco")])
            _classify(db, f"tx{i}", "Food & Drink", "Groceries")
        mock_resp = MagicMock(status_code=200)
        with patch("rule_suggestions.requests.post", return_value=mock_resp) as mock_post:
            result = rule_suggestions.sync_candidates()
        assert result == 1
        assert mock_post.call_args.kwargs["json"]["candidates"][0]["match_value"] == "Tesco"

    def test_no_candidates_means_no_request(self, db):
        with patch("rule_suggestions.requests.post") as mock_post:
            result = rule_suggestions.sync_candidates()
        assert result == 0
        mock_post.assert_not_called()

    def test_404_is_treated_as_not_yet_deployed(self, db):
        for i in range(5):
            write_to_db([_txn(f"tx{i}", merchant="Tesco")])
            _classify(db, f"tx{i}", "Food & Drink", "Groceries")
        mock_resp = MagicMock(status_code=404)
        with patch("rule_suggestions.requests.post", return_value=mock_resp):
            result = rule_suggestions.sync_candidates()
        assert result == 0
