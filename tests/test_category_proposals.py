import json
from unittest.mock import patch

import category_proposals as cp


def _seed_pending_txns(db, ids, description="Some payment"):
    for txn_id in ids:
        db.execute(
            """INSERT INTO transactions (id, amount, currency, description, skipped)
               VALUES (?, -100.0, 'GBP', ?, FALSE)""",
            [txn_id, description],
        )


def _option(parent="Tax", sub="Self Assessment", is_new=True, rationale="Best fit."):
    return {"parent_name": parent, "subcategory_name": sub, "parent_is_new": is_new, "rationale": rationale}


class TestRegisterGroup:
    def test_creates_a_pending_proposal_with_its_options_and_locks_the_transactions(self, db):
        _seed_pending_txns(db, ["tx_1", "tx_2"])
        options = [_option("Tax", "Self Assessment"), _option("Professional Services", "Tax Payments", is_new=False)]
        proposal_id, is_new = cp.register_group(options, ["tx_1", "tx_2"])
        assert is_new is True
        row = db.execute(
            "SELECT options, status FROM category_proposals WHERE id = ?", [proposal_id]
        ).fetchone()
        assert json.loads(row[0]) == options
        assert row[1] == "pending"
        locked = {r[0] for r in db.execute(
            "SELECT id FROM transactions WHERE pending_category_proposal_id = ?", [proposal_id]
        ).fetchall()}
        assert locked == {"tx_1", "tx_2"}

    def test_a_second_batch_with_the_identical_option_set_merges_in(self, db):
        """Same options, different casing/order -- still the same proposal, so
        the second batch's transactions join the first card rather than
        spawning a duplicate."""
        _seed_pending_txns(db, ["tx_1", "tx_2"])
        first_options = [_option("Tax", "Self Assessment"), _option("Professional Services", "Tax Payments", is_new=False)]
        second_options = [_option("professional services", "tax payments", is_new=False), _option("tax", "self assessment")]
        first_id, _ = cp.register_group(first_options, ["tx_1"])
        second_id, is_new = cp.register_group(second_options, ["tx_2"])
        assert second_id == first_id
        assert is_new is False
        locked = {r[0] for r in db.execute(
            "SELECT id FROM transactions WHERE pending_category_proposal_id = ?", [first_id]
        ).fetchall()}
        assert locked == {"tx_1", "tx_2"}

    def test_a_different_option_set_does_not_merge(self, db):
        _seed_pending_txns(db, ["tx_1", "tx_2"])
        first_id, _ = cp.register_group([_option("Tax", "Self Assessment")], ["tx_1"])
        second_id, is_new = cp.register_group([_option("Bills & Utilities", "Council Tax", is_new=False)], ["tx_2"])
        assert second_id != first_id
        assert is_new is True

    def test_a_denied_proposal_is_not_merged_into_a_new_group_starts_fresh(self, db):
        """Registering the identical option set again after a denial must ask
        again, not silently reattach to the row the user already said no to."""
        _seed_pending_txns(db, ["tx_1", "tx_2"])
        options = [_option("Tax", "Self Assessment")]
        first_id, _ = cp.register_group(options, ["tx_1"])
        db.execute("UPDATE category_proposals SET status = 'denied' WHERE id = ?", [first_id])
        second_id, is_new = cp.register_group(options, ["tx_2"])
        assert second_id != first_id
        assert is_new is True


class TestDeniedNames:
    def test_only_denied_rows_are_reported(self, db):
        db.execute(
            "INSERT INTO category_proposals (id, options, status, proposed_at) VALUES (1, ?, 'denied', NOW())",
            [json.dumps([_option("Tax", "Self Assessment")])],
        )
        db.execute(
            "INSERT INTO category_proposals (id, options, status, proposed_at) VALUES (2, ?, 'pending', NOW())",
            [json.dumps([_option("Gifts", "Wedding Gifts")])],
        )
        assert cp.denied_parent_names() == {"tax"}
        assert cp.denied_sub_names() == {"self assessment"}

    def test_every_option_on_a_denied_card_is_blocked_not_just_one(self, db):
        """Denying the card rejects the whole set of offered ideas, not just
        whichever option happened to be listed first."""
        db.execute(
            "INSERT INTO category_proposals (id, options, status, proposed_at) VALUES (1, ?, 'denied', NOW())",
            [json.dumps([
                _option("Tax", "Self Assessment"),
                _option("Professional Services", "Tax Payments", is_new=False),
                _option("Bills & Utilities", "Council Tax", is_new=False),
            ])],
        )
        assert cp.denied_parent_names() == {"tax"}
        assert cp.denied_sub_names() == {"self assessment", "tax payments", "council tax"}

    def test_denied_sub_of_an_existing_parent_does_not_block_the_parent_name(self, db):
        """parent_is_new=False means only the subcategory was novel -- the
        parent itself was never proposed, so it must not be blocklisted."""
        db.execute(
            "INSERT INTO category_proposals (id, options, status, proposed_at) VALUES (1, ?, 'denied', NOW())",
            [json.dumps([_option("Bills & Utilities", "Council Tax", is_new=False)])],
        )
        assert cp.denied_parent_names() == set()
        assert cp.denied_sub_names() == {"council tax"}

    def test_a_regenerate_request_does_not_permanently_block_its_options(self, db):
        """A "Try again" tap is recorded as 'denied' under the hood so the
        transactions can be found via the FK, but asking for different ideas
        isn't a verdict that the shown ones were wrong -- only a genuine
        "None of these" should reach the permanent blocklist."""
        db.execute(
            """INSERT INTO category_proposals (id, options, status, regenerate_requested, proposed_at)
               VALUES (1, ?, 'denied', TRUE, NOW())""",
            [json.dumps([_option("Tax", "Self Assessment")])],
        )
        assert cp.denied_parent_names() == set()
        assert cp.denied_sub_names() == set()


class TestApplySelected:
    def test_creates_the_chosen_option_and_classifies_exactly_the_locked_transactions(self, db):
        _seed_pending_txns(db, ["tx_1", "tx_2"])
        db.execute(
            """INSERT INTO transactions (id, amount, currency, description, skipped)
               VALUES ('tx_other', -5.0, 'GBP', 'Unrelated', FALSE)"""
        )
        options = [_option("Tax", "Self Assessment"), _option("Professional Services", "Tax Payments", is_new=False)]
        proposal_id, _ = cp.register_group(options, ["tx_1", "tx_2"])

        count = cp.apply_selected(proposal_id, 0)

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
        # The option NOT chosen must not have been created.
        assert db.execute("SELECT COUNT(*) FROM subcategories WHERE name = 'Tax Payments'").fetchone()[0] == 0

    def test_picking_a_non_primary_option_creates_that_one_not_the_first(self, db):
        _seed_pending_txns(db, ["tx_1"])
        options = [_option("Tax", "Self Assessment"), _option("Professional Services", "Tax Payments", is_new=False)]
        proposal_id, _ = cp.register_group(options, ["tx_1"])

        cp.apply_selected(proposal_id, 1)

        assert db.execute(
            "SELECT llm_category, llm_subcategory FROM transactions WHERE id = 'tx_1'"
        ).fetchone() == ("Professional Services", "Tax Payments")
        assert db.execute("SELECT COUNT(*) FROM parent_categories WHERE name = 'Tax'").fetchone()[0] == 0

    def test_an_out_of_range_option_index_is_a_no_op(self, db):
        _seed_pending_txns(db, ["tx_1"])
        proposal_id, _ = cp.register_group([_option()], ["tx_1"])
        assert cp.apply_selected(proposal_id, 5) == 0
        assert db.execute("SELECT llm_category FROM transactions WHERE id = 'tx_1'").fetchone()[0] is None

    def test_a_manually_classified_transaction_is_not_overwritten(self, db):
        """Between the card being sent and answered, the user (or a quick-tap
        match) may have classified the waiting transaction another way."""
        _seed_pending_txns(db, ["tx_1", "tx_2"])
        proposal_id, _ = cp.register_group([_option()], ["tx_1", "tx_2"])
        from database_functions import update_classification
        update_classification("tx_1", "Shopping", "General Retail", 1.0, "manual")

        count = cp.apply_selected(proposal_id, 0)

        assert count == 1
        assert db.execute(
            "SELECT llm_category, llm_subcategory FROM transactions WHERE id = 'tx_1'"
        ).fetchone() == ("Shopping", "General Retail")
        assert db.execute(
            "SELECT llm_category, llm_subcategory FROM transactions WHERE id = 'tx_2'"
        ).fetchone() == ("Tax", "Self Assessment")

    def test_unknown_proposal_id_is_a_no_op(self, db):
        assert cp.apply_selected(999, 0) == 0

    def test_nothing_left_waiting_is_a_no_op(self, db):
        _seed_pending_txns(db, ["tx_1"])
        proposal_id, _ = cp.register_group([_option()], ["tx_1"])
        from database_functions import update_classification
        update_classification("tx_1", "Shopping", "General Retail", 1.0, "manual")
        assert cp.apply_selected(proposal_id, 0) == 0


class TestDenyAll:
    def test_unlocks_without_creating_anything(self, db):
        _seed_pending_txns(db, ["tx_1", "tx_2"])
        proposal_id, _ = cp.register_group([_option(), _option("Professional Services", "Tax Payments", is_new=False)], ["tx_1", "tx_2"])

        freed = cp.deny_all(proposal_id)

        assert freed == 2
        rows = db.execute(
            "SELECT llm_category, pending_category_proposal_id FROM transactions ORDER BY id"
        ).fetchall()
        assert rows == [(None, None), (None, None)]
        assert db.execute("SELECT COUNT(*) FROM parent_categories WHERE name = 'Tax'").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM subcategories WHERE name = 'Tax Payments'").fetchone()[0] == 0

    def test_unknown_proposal_id_is_a_no_op(self, db):
        assert cp.deny_all(999) == 0


class TestPendingRegenerations:
    def test_finds_a_regenerate_flagged_proposal_via_its_locked_transactions(self, db):
        for txn_id, ctx in [("tx_1", "HMRC payment"), ("tx_2", None)]:
            db.execute(
                """INSERT INTO transactions (id, amount, currency, user_context, merchant_name, skipped)
                   VALUES (?, -100.0, 'GBP', ?, 'HMRC', FALSE)""",
                [txn_id, ctx],
            )
        options = [_option("Tax", "Self Assessment")]
        proposal_id, _ = cp.register_group(options, ["tx_1", "tx_2"])
        db.execute(
            "UPDATE category_proposals SET status = 'denied', regenerate_requested = TRUE WHERE id = ?", [proposal_id]
        )

        pending = cp.pending_regenerations()

        assert len(pending) == 1
        assert pending[0]["old_id"] == proposal_id
        assert pending[0]["previous_options"] == options
        assert set(pending[0]["txn_ids"]) == {"tx_1", "tx_2"}
        assert set(pending[0]["examples"]) == {"HMRC payment", "HMRC"}

    def test_a_plain_denial_is_not_returned(self, db):
        """Deny-all already unlocked its transactions, so the FK join finds
        nothing -- this is what makes plain denials naturally invisible here
        without any extra bookkeeping."""
        _seed_pending_txns(db, ["tx_1"])
        proposal_id, _ = cp.register_group([_option()], ["tx_1"])
        cp.deny_all(proposal_id)
        db.execute("UPDATE category_proposals SET status = 'denied' WHERE id = ?", [proposal_id])
        assert cp.pending_regenerations() == []

    def test_a_still_pending_proposal_is_not_returned(self, db):
        _seed_pending_txns(db, ["tx_1"])
        cp.register_group([_option()], ["tx_1"])
        assert cp.pending_regenerations() == []

    def test_an_applied_proposal_is_not_returned(self, db):
        _seed_pending_txns(db, ["tx_1"])
        proposal_id, _ = cp.register_group([_option()], ["tx_1"])
        cp.apply_selected(proposal_id, 0)
        assert cp.pending_regenerations() == []


class TestCollectDecisions:
    def test_selected_decision_is_applied_and_recorded(self, db):
        _seed_pending_txns(db, ["tx_1"])
        proposal_id, _ = cp.register_group([_option(), _option("Professional Services", "Tax Payments", is_new=False)], ["tx_1"])
        confirmed = []
        with patch.object(cp, "fetch_decisions",
                          return_value=[{"id": proposal_id, "status": "selected", "selected_option": 1}]), \
             patch.object(cp, "confirm_collected", side_effect=lambda ids: confirmed.extend(ids)):
            applied = cp.collect_decisions()
        assert applied == 1
        row = db.execute("SELECT status, selected_option FROM category_proposals WHERE id = ?", [proposal_id]).fetchone()
        assert row == ("applied", 1)
        assert db.execute(
            "SELECT llm_category, llm_subcategory FROM transactions WHERE id = 'tx_1'"
        ).fetchone() == ("Professional Services", "Tax Payments")
        assert confirmed == [proposal_id]

    def test_denied_decision_unlocks_and_is_recorded(self, db):
        _seed_pending_txns(db, ["tx_1"])
        proposal_id, _ = cp.register_group([_option()], ["tx_1"])
        with patch.object(cp, "fetch_decisions",
                          return_value=[{"id": proposal_id, "status": "denied", "selected_option": None}]), \
             patch.object(cp, "confirm_collected"):
            applied = cp.collect_decisions()
        assert applied == 0
        assert db.execute("SELECT status FROM category_proposals WHERE id = ?", [proposal_id]).fetchone()[0] == "denied"
        assert db.execute(
            "SELECT llm_category, pending_category_proposal_id FROM transactions WHERE id = 'tx_1'"
        ).fetchone() == (None, None)

    def test_a_regenerate_decision_leaves_transactions_locked_rather_than_unlocking(self, db):
        """Deliberately does NOT call deny_all(): the transactions must stay
        locked to the (now-denied) proposal so pending_regenerations() can
        still find them via that FK -- unlocking here would lose the link
        between "these transactions" and "what was already shown"."""
        _seed_pending_txns(db, ["tx_1", "tx_2"])
        proposal_id, _ = cp.register_group([_option()], ["tx_1", "tx_2"])
        with patch.object(cp, "fetch_decisions",
                          return_value=[{"id": proposal_id, "status": "denied", "regenerate_requested": True}]), \
             patch.object(cp, "confirm_collected"):
            applied = cp.collect_decisions()
        assert applied == 0
        row = db.execute(
            "SELECT status, regenerate_requested FROM category_proposals WHERE id = ?", [proposal_id]
        ).fetchone()
        assert row == ("denied", True)
        locked = {r[0] for r in db.execute(
            "SELECT id FROM transactions WHERE pending_category_proposal_id = ?", [proposal_id]
        ).fetchall()}
        assert locked == {"tx_1", "tx_2"}

    def test_a_backup_is_taken_before_any_selection_is_applied(self, db):
        _seed_pending_txns(db, ["tx_1"])
        proposal_id, _ = cp.register_group([_option()], ["tx_1"])
        with patch.object(cp, "fetch_decisions",
                          return_value=[{"id": proposal_id, "status": "selected", "selected_option": 0}]), \
             patch.object(cp, "confirm_collected"), \
             patch("database_functions.backup_db") as backup:
            cp.collect_decisions()
        backup.assert_called_once()

    def test_a_second_decision_on_an_already_decided_proposal_is_ignored_but_confirmed(self, db):
        """Telegram cards stay tappable forever -- a stale decision for a
        proposal that already moved on must not be reapplied."""
        _seed_pending_txns(db, ["tx_1"])
        proposal_id, _ = cp.register_group([_option()], ["tx_1"])
        db.execute("UPDATE category_proposals SET status = 'applied' WHERE id = ?", [proposal_id])
        confirmed = []
        with patch.object(cp, "fetch_decisions",
                          return_value=[{"id": proposal_id, "status": "denied", "selected_option": None}]), \
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
    def test_builds_options_and_examples_from_the_locked_transactions_capped_at_four(self, db, monkeypatch):
        for i in range(6):
            db.execute(
                """INSERT INTO transactions (id, amount, currency, user_context, skipped)
                   VALUES (?, -10.0, 'GBP', ?, FALSE)""",
                [f"tx_{i}", f"context {i}"],
            )
        options = [_option(), _option("Professional Services", "Tax Payments", is_new=False)]
        proposal_id, _ = cp.register_group(options, [f"tx_{i}" for i in range(6)])

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
        assert entry["options"] == options
        assert entry["txn_count"] == 6
        assert len(entry["examples"]) == 4

    def test_empty_id_list_makes_no_request(self, monkeypatch):
        called = []
        monkeypatch.setattr(cp.requests, "post", lambda *a, **k: called.append(1))
        cp.sync_new_proposals([])
        assert called == []
