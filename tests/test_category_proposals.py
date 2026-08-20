from unittest.mock import patch

import category_proposals as cp


def _seed_pending_txns(db, ids, description="Some payment"):
    for txn_id in ids:
        db.execute(
            """INSERT INTO transactions (id, amount, currency, description, skipped)
               VALUES (?, -100.0, 'GBP', ?, FALSE)""",
            [txn_id, description],
        )


class TestRegisterGroup:
    def test_creates_a_pending_proposal_and_locks_the_transactions(self, db):
        _seed_pending_txns(db, ["tx_1", "tx_2"])
        proposal_id, is_new = cp.register_group("Tax", True, "Self Assessment", ["tx_1", "tx_2"])
        assert is_new is True
        row = db.execute(
            "SELECT parent_name, parent_is_new, subcategory_name, status FROM category_proposals WHERE id = ?",
            [proposal_id],
        ).fetchone()
        assert row == ("Tax", True, "Self Assessment", "pending")
        locked = {r[0] for r in db.execute(
            "SELECT id FROM transactions WHERE pending_category_proposal_id = ?", [proposal_id]
        ).fetchall()}
        assert locked == {"tx_1", "tx_2"}

    def test_a_second_batch_proposing_the_same_pair_merges_in(self, db):
        _seed_pending_txns(db, ["tx_1", "tx_2"])
        first_id, _ = cp.register_group("Tax", True, "Self Assessment", ["tx_1"])
        second_id, is_new = cp.register_group("tax", True, "self assessment", ["tx_2"])
        assert second_id == first_id
        assert is_new is False
        locked = {r[0] for r in db.execute(
            "SELECT id FROM transactions WHERE pending_category_proposal_id = ?", [first_id]
        ).fetchall()}
        assert locked == {"tx_1", "tx_2"}

    def test_a_denied_proposal_is_not_merged_into_a_new_group_starts_fresh(self, db):
        """Registering the same name again after a denial must ask again, not
        silently reattach to the row the user already said no to."""
        _seed_pending_txns(db, ["tx_1", "tx_2"])
        first_id, _ = cp.register_group("Tax", True, "Self Assessment", ["tx_1"])
        db.execute("UPDATE category_proposals SET status = 'denied' WHERE id = ?", [first_id])
        second_id, is_new = cp.register_group("Tax", True, "Self Assessment", ["tx_2"])
        assert second_id != first_id
        assert is_new is True


class TestDeniedNames:
    def test_only_denied_rows_are_reported(self, db):
        db.execute(
            """INSERT INTO category_proposals (id, parent_name, parent_is_new, subcategory_name, status, proposed_at)
               VALUES (1, 'Tax', TRUE, 'Self Assessment', 'denied', NOW())"""
        )
        db.execute(
            """INSERT INTO category_proposals (id, parent_name, parent_is_new, subcategory_name, status, proposed_at)
               VALUES (2, 'Gifts', TRUE, 'Wedding Gifts', 'pending', NOW())"""
        )
        assert cp.denied_parent_names() == {"tax"}
        assert cp.denied_sub_names() == {"self assessment"}

    def test_denied_sub_names_are_global_regardless_of_parent(self, db):
        """A denied subcategory idea must not resurface under a different
        parent -- it's the same declined idea."""
        db.execute(
            """INSERT INTO category_proposals (id, parent_name, parent_is_new, subcategory_name, status, proposed_at)
               VALUES (1, 'Bills & Utilities', FALSE, 'Council Tax', 'denied', NOW())"""
        )
        assert cp.denied_sub_names() == {"council tax"}

    def test_denied_sub_of_an_existing_parent_does_not_block_the_parent_name(self, db):
        """parent_is_new=FALSE means only the subcategory was novel -- the
        parent itself was never proposed, so it must not be blocklisted."""
        db.execute(
            """INSERT INTO category_proposals (id, parent_name, parent_is_new, subcategory_name, status, proposed_at)
               VALUES (1, 'Bills & Utilities', FALSE, 'Council Tax', 'denied', NOW())"""
        )
        assert cp.denied_parent_names() == set()


class TestApplyApproved:
    def test_creates_the_category_and_classifies_exactly_the_locked_transactions(self, db):
        _seed_pending_txns(db, ["tx_1", "tx_2"])
        db.execute(
            """INSERT INTO transactions (id, amount, currency, description, skipped)
               VALUES ('tx_other', -5.0, 'GBP', 'Unrelated', FALSE)"""
        )
        proposal_id, _ = cp.register_group("Tax", True, "Self Assessment", ["tx_1", "tx_2"])

        count = cp.apply_approved(proposal_id)

        assert count == 2
        rows = db.execute(
            "SELECT id, llm_category, llm_subcategory, pending_category_proposal_id FROM transactions ORDER BY id"
        ).fetchall()
        assert rows == [
            ("tx_1", "Tax", "Self Assessment", None),
            ("tx_2", "Tax", "Self Assessment", None),
            ("tx_other", None, None, None),
        ]
        assert db.execute("SELECT COUNT(*) FROM parent_categories WHERE name = 'Tax'").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM subcategories WHERE name = 'Self Assessment'").fetchone()[0] == 1

    def test_a_manually_classified_transaction_is_not_overwritten(self, db):
        """Between the card being sent and approved, the user (or a quick-tap
        match) may have classified the waiting transaction another way."""
        _seed_pending_txns(db, ["tx_1", "tx_2"])
        proposal_id, _ = cp.register_group("Tax", True, "Self Assessment", ["tx_1", "tx_2"])
        # update_classification() clears the lock as a side effect -- the
        # defensive fix this relies on.
        from database_functions import update_classification
        update_classification("tx_1", "Shopping", "General Retail", 1.0, "manual")

        count = cp.apply_approved(proposal_id)

        assert count == 1
        assert db.execute(
            "SELECT llm_category, llm_subcategory FROM transactions WHERE id = 'tx_1'"
        ).fetchone() == ("Shopping", "General Retail")
        assert db.execute(
            "SELECT llm_category, llm_subcategory FROM transactions WHERE id = 'tx_2'"
        ).fetchone() == ("Tax", "Self Assessment")

    def test_unknown_proposal_id_is_a_no_op(self, db):
        assert cp.apply_approved(999) == 0

    def test_nothing_left_waiting_is_a_no_op(self, db):
        """All locked transactions were already classified some other way by
        the time this got applied -- the proposal is now vacuous."""
        _seed_pending_txns(db, ["tx_1"])
        proposal_id, _ = cp.register_group("Tax", True, "Self Assessment", ["tx_1"])
        from database_functions import update_classification
        update_classification("tx_1", "Shopping", "General Retail", 1.0, "manual")
        assert cp.apply_approved(proposal_id) == 0


class TestDeny:
    def test_unlocks_without_creating_anything(self, db):
        _seed_pending_txns(db, ["tx_1", "tx_2"])
        proposal_id, _ = cp.register_group("Tax", True, "Self Assessment", ["tx_1", "tx_2"])

        freed = cp.deny(proposal_id)

        assert freed == 2
        rows = db.execute(
            "SELECT llm_category, pending_category_proposal_id FROM transactions ORDER BY id"
        ).fetchall()
        assert rows == [(None, None), (None, None)]
        assert db.execute("SELECT COUNT(*) FROM parent_categories WHERE name = 'Tax'").fetchone()[0] == 0

    def test_unknown_proposal_id_is_a_no_op(self, db):
        assert cp.deny(999) == 0


class TestCollectDecisions:
    def test_approved_decision_is_applied_and_recorded(self, db):
        _seed_pending_txns(db, ["tx_1"])
        proposal_id, _ = cp.register_group("Tax", True, "Self Assessment", ["tx_1"])
        confirmed = []
        with patch.object(cp, "fetch_decisions",
                          return_value=[{"id": proposal_id, "subcategory_name": "Self Assessment", "status": "approved"}]), \
             patch.object(cp, "confirm_collected", side_effect=lambda ids: confirmed.extend(ids)):
            applied = cp.collect_decisions()
        assert applied == 1
        assert db.execute("SELECT status FROM category_proposals WHERE id = ?", [proposal_id]).fetchone()[0] == "applied"
        assert db.execute("SELECT llm_category FROM transactions WHERE id = 'tx_1'").fetchone()[0] == "Tax"
        assert confirmed == [proposal_id]

    def test_denied_decision_unlocks_and_is_recorded(self, db):
        _seed_pending_txns(db, ["tx_1"])
        proposal_id, _ = cp.register_group("Tax", True, "Self Assessment", ["tx_1"])
        with patch.object(cp, "fetch_decisions",
                          return_value=[{"id": proposal_id, "subcategory_name": "Self Assessment", "status": "denied"}]), \
             patch.object(cp, "confirm_collected"):
            applied = cp.collect_decisions()
        assert applied == 0
        assert db.execute("SELECT status FROM category_proposals WHERE id = ?", [proposal_id]).fetchone()[0] == "denied"
        assert db.execute(
            "SELECT llm_category, pending_category_proposal_id FROM transactions WHERE id = 'tx_1'"
        ).fetchone() == (None, None)

    def test_a_backup_is_taken_before_any_approval_is_applied(self, db):
        _seed_pending_txns(db, ["tx_1"])
        proposal_id, _ = cp.register_group("Tax", True, "Self Assessment", ["tx_1"])
        with patch.object(cp, "fetch_decisions",
                          return_value=[{"id": proposal_id, "subcategory_name": "Self Assessment", "status": "approved"}]), \
             patch.object(cp, "confirm_collected"), \
             patch("database_functions.backup_db") as backup:
            cp.collect_decisions()
        backup.assert_called_once()

    def test_a_second_decision_on_an_already_decided_proposal_is_ignored_but_confirmed(self, db):
        """Telegram cards stay tappable forever -- a stale decision for a
        proposal that already moved on must not be reapplied."""
        _seed_pending_txns(db, ["tx_1"])
        proposal_id, _ = cp.register_group("Tax", True, "Self Assessment", ["tx_1"])
        db.execute("UPDATE category_proposals SET status = 'applied' WHERE id = ?", [proposal_id])
        confirmed = []
        with patch.object(cp, "fetch_decisions",
                          return_value=[{"id": proposal_id, "subcategory_name": "Self Assessment", "status": "denied"}]), \
             patch.object(cp, "confirm_collected", side_effect=lambda ids: confirmed.extend(ids)):
            applied = cp.collect_decisions()
        assert applied == 0
        assert db.execute("SELECT status FROM category_proposals WHERE id = ?", [proposal_id]).fetchone()[0] == "applied"
        assert confirmed == [proposal_id]

    def test_nothing_happens_when_there_are_no_decisions(self, db):
        with patch.object(cp, "fetch_decisions", return_value=[]):
            assert cp.collect_decisions() == 0


class TestServerNotYetDeployed:
    def test_404_is_reported_as_unsupported(self, monkeypatch):
        class _Resp:
            status_code, ok = 404, False
        monkeypatch.setattr(cp.requests, "get", lambda *a, **k: _Resp())
        assert cp.server_supports_proposals() is False

    def test_an_unreachable_server_is_reported_as_unsupported(self, monkeypatch):
        def _boom(*a, **k):
            raise cp.requests.ConnectionError("no route to host")
        monkeypatch.setattr(cp.requests, "get", _boom)
        assert cp.server_supports_proposals() is False

    def test_a_healthy_server_is_supported(self, monkeypatch):
        class _Resp:
            status_code, ok = 200, True
        monkeypatch.setattr(cp.requests, "get", lambda *a, **k: _Resp())
        assert cp.server_supports_proposals() is True


class TestSyncNewProposals:
    def test_builds_examples_from_the_locked_transactions_capped_at_four(self, db, monkeypatch):
        for i in range(6):
            db.execute(
                """INSERT INTO transactions (id, amount, currency, user_context, skipped)
                   VALUES (?, -10.0, 'GBP', ?, FALSE)""",
                [f"tx_{i}", f"context {i}"],
            )
        proposal_id, _ = cp.register_group("Tax", True, "Self Assessment", [f"tx_{i}" for i in range(6)])

        posted = {}
        def fake_post(url, headers, json, timeout):
            posted["url"], posted["json"] = url, json
            class _Resp:
                def raise_for_status(self):
                    pass
            return _Resp()
        monkeypatch.setattr(cp.requests, "post", fake_post)

        cp.sync_new_proposals([proposal_id])

        assert posted["url"].endswith("/sync-category-proposals")
        entry = posted["json"]["proposals"][0]
        assert entry["parent_name"] == "Tax"
        assert entry["parent_is_new"] is True
        assert entry["subcategory_name"] == "Self Assessment"
        assert entry["txn_count"] == 6
        assert len(entry["examples"]) == 4

    def test_empty_id_list_makes_no_request(self, monkeypatch):
        called = []
        monkeypatch.setattr(cp.requests, "post", lambda *a, **k: called.append(1))
        cp.sync_new_proposals([])
        assert called == []
