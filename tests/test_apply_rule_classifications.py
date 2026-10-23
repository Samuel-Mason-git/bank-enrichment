"""Local side of fixed-category rules: a transaction a rule tagged on the
server is filed straight into that category, so the classifier never runs
on it -- but only into a category that still exists locally."""
from unittest.mock import MagicMock, patch

from database_functions import (
    apply_rule_classifications, get_unclassified, upsert_parent, upsert_subcategory, write_to_db,
)
import process


def _export_row(id="tx1", rule_category=None, rule_subcategory=None, user_context="Bills - Phone"):
    row = {
        "id": id,
        "payload": {"data": {
            "amount": -3000, "currency": "GBP", "description": "EE LIMITED",
            "merchant": {"name": "EE"}, "counterparty": {}, "is_load": False,
            "created": "2026-10-20T10:00:00", "settled": "2026-10-20T10:00:00",
        }},
        "user_context": user_context, "skipped": False,
        "received_at": "2026-10-20T10:00:01", "enriched_at": "2026-10-20T10:00:02",
    }
    if rule_category is not None:
        row["rule_category"] = rule_category
        row["rule_subcategory"] = rule_subcategory
    return row


def _classification(con, id="tx1"):
    return con.execute(
        "SELECT llm_category, llm_subcategory, llm_model FROM transactions WHERE id = ?", [id]
    ).fetchone()


def _taxonomy(category="Bills", subcategory="Phone"):
    upsert_subcategory(subcategory, upsert_parent(category))


class TestApplyRuleClassifications:
    def test_files_a_tagged_transaction_and_marks_it_as_a_rule(self, db):
        _taxonomy()
        rows = [_export_row(rule_category="Bills", rule_subcategory="Phone")]
        write_to_db(rows)
        assert apply_rule_classifications(rows) == 1
        assert _classification(db) == ("Bills", "Phone", "rule")

    def test_a_filed_transaction_is_not_left_for_the_classifier(self, db):
        _taxonomy()
        rows = [_export_row(rule_category="Bills", rule_subcategory="Phone")]
        write_to_db(rows)
        apply_rule_classifications(rows)
        assert "tx1" not in {t["id"] for t in get_unclassified()}

    def test_a_pair_no_longer_in_the_taxonomy_is_left_for_the_classifier(self, db):
        """The server's dropdown can be a run stale -- filing into a name
        that's since been renamed would quietly recreate it."""
        rows = [_export_row(rule_category="Bills", rule_subcategory="Phone")]
        write_to_db(rows)
        assert apply_rule_classifications(rows) == 0
        assert _classification(db) == (None, None, None)
        assert db.execute("SELECT COUNT(*) FROM parent_categories WHERE name = 'Bills'").fetchone()[0] == 0

    def test_an_untagged_transaction_is_untouched(self, db):
        _taxonomy()
        rows = [_export_row()]
        write_to_db(rows)
        assert apply_rule_classifications(rows) == 0
        assert _classification(db) == (None, None, None)

    def test_never_overwrites_an_existing_classification(self, db):
        _taxonomy()
        _taxonomy("Gifts", "Presents")
        rows = [_export_row(rule_category="Bills", rule_subcategory="Phone")]
        write_to_db(rows)
        db.execute("UPDATE transactions SET llm_category = 'Gifts', llm_subcategory = 'Presents' WHERE id = 'tx1'")
        assert apply_rule_classifications(rows) == 0
        assert _classification(db)[:2] == ("Gifts", "Presents")

    def test_an_older_server_without_the_keys_is_tolerated(self, db):
        rows = [_export_row()]
        assert "rule_category" not in rows[0]
        write_to_db(rows)
        assert apply_rule_classifications(rows) == 0


class TestSyncTaxonomy:
    def test_sends_every_name_pair(self, db):
        _taxonomy("Bills", "Phone")
        _taxonomy("Bills", "Broadband")
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"synced": 2}
        with patch("process.requests.post", return_value=mock_resp) as mock_post:
            process.sync_taxonomy()
        entries = mock_post.call_args.kwargs["json"]["entries"]
        assert {(e["category"], e["subcategory"]) for e in entries} >= {("Bills", "Phone"), ("Bills", "Broadband")}
        assert mock_post.call_args.args[0].endswith("/sync-taxonomy")
