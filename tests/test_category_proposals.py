import json

import category_proposals as cp
from database_functions import update_classification
from tests.local_helpers import ensure_category, insert_transaction


def _seed_pending_txns(db, ids, description="Some payment"):
    for txn_id in ids:
        insert_transaction(db, txn_id, amount=-100.0, description=description)


def _option(parent="Tax", sub="Self Assessment", is_new=True, rationale="Best fit."):
    return {"parent_name": parent, "subcategory_name": sub, "parent_is_new": is_new, "rationale": rationale}


class TestRegisterGroup:
    def test_creates_a_pending_proposal_with_its_options_and_locks_the_transactions(self, db):
        _seed_pending_txns(db, ["tx_1", "tx_2"])
        options = [_option("Tax", "Self Assessment"), _option("Professional Services", "Tax Payments", is_new=False)]
        proposal_id, is_new = cp.register_group(options, ["tx_1", "tx_2"])
        assert is_new is True
        row = db.execute(
            "SELECT options, status FROM category_proposals WHERE id = %s", [proposal_id]
        ).fetchone()
        assert row[0] == options
        assert row[1] == "pending"
        locked = {r[0] for r in db.execute(
            "SELECT id FROM transactions WHERE pending_category_proposal_id = %s", [proposal_id]
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
            "SELECT id FROM transactions WHERE pending_category_proposal_id = %s", [first_id]
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
        db.execute("UPDATE category_proposals SET status = 'denied' WHERE id = %s", [first_id])
        second_id, is_new = cp.register_group(options, ["tx_2"])
        assert second_id != first_id
        assert is_new is True


class TestDeniedNames:
    def test_only_denied_rows_are_reported(self, db):
        db.execute(
            "INSERT INTO category_proposals (id, options, status, proposed_at) VALUES (1, %s, 'denied', NOW())",
            [json.dumps([_option("Tax", "Self Assessment")])],
        )
        db.execute(
            "INSERT INTO category_proposals (id, options, status, proposed_at) VALUES (2, %s, 'pending', NOW())",
            [json.dumps([_option("Gifts", "Wedding Gifts")])],
        )
        assert cp.denied_parent_names() == {"tax"}
        assert cp.denied_sub_names() == {"self assessment"}

    def test_every_option_on_a_denied_card_is_blocked_not_just_one(self, db):
        """Denying the card rejects the whole set of offered ideas, not just
        whichever option happened to be listed first."""
        db.execute(
            "INSERT INTO category_proposals (id, options, status, proposed_at) VALUES (1, %s, 'denied', NOW())",
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
            "INSERT INTO category_proposals (id, options, status, proposed_at) VALUES (1, %s, 'denied', NOW())",
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
               VALUES (1, %s, 'denied', TRUE, NOW())""",
            [json.dumps([_option("Tax", "Self Assessment")])],
        )
        assert cp.denied_parent_names() == set()
        assert cp.denied_sub_names() == set()


class TestApplySelected:
    def test_creates_the_chosen_option_and_classifies_exactly_the_locked_transactions(self, db):
        _seed_pending_txns(db, ["tx_1", "tx_2"])
        insert_transaction(db, "tx_other", amount=-5.0, description="Unrelated")
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
        ensure_category(db, "Shopping", "General Retail")
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
        ensure_category(db, "Shopping", "General Retail")
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
            insert_transaction(db, txn_id, amount=-100.0, user_context=ctx, merchant_name="HMRC")
        options = [_option("Tax", "Self Assessment")]
        proposal_id, _ = cp.register_group(options, ["tx_1", "tx_2"])
        db.execute(
            "UPDATE category_proposals SET status = 'denied', regenerate_requested = TRUE WHERE id = %s", [proposal_id]
        )

        pending = cp.pending_regenerations()

        assert len(pending) == 1
        assert pending[0]["old_id"] == proposal_id
        assert pending[0]["previous_options"] == options
        assert set(pending[0]["txn_ids"]) == {"tx_1", "tx_2"}
        assert set(pending[0]["examples"]) == {"HMRC payment", "HMRC"}

    def test_a_plain_denial_is_not_returned(self, db):
        """Deny-all already released its transactions, so it holds nothing --
        which is what makes plain denials invisible here."""
        _seed_pending_txns(db, ["tx_1"])
        proposal_id, _ = cp.register_group([_option()], ["tx_1"])
        cp.deny_all(proposal_id)
        db.execute("UPDATE category_proposals SET status = 'denied' WHERE id = %s", [proposal_id])
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


def _held(db, proposal_id):
    return {r[0] for r in db.execute(
        "SELECT id FROM transactions WHERE pending_category_proposal_id = %s", [proposal_id]
    ).fetchall()}


class TestCollectDecisions:
    """The server records a tap on the proposal row itself; the next run acts on it."""

    def test_a_selection_is_applied_and_recorded(self, db):
        _seed_pending_txns(db, ["tx_1"])
        proposal_id, _ = cp.register_group([_option(), _option("Professional Services", "Tax Payments", is_new=False)], ["tx_1"])
        db.execute("UPDATE category_proposals SET status = 'selected', selected_option = 1 WHERE id = %s", [proposal_id])

        assert cp.collect_decisions() == 1

        row = db.execute("SELECT status, selected_option FROM category_proposals WHERE id = %s", [proposal_id]).fetchone()
        assert row == ("applied", 1)
        assert db.execute(
            "SELECT llm_category, llm_subcategory FROM transactions WHERE id = 'tx_1'"
        ).fetchone() == ("Professional Services", "Tax Payments")
        assert db.execute(
            "SELECT source FROM label_events WHERE txn_id = 'tx_1'"
        ).fetchone()[0] == "proposal"

    def test_an_applied_proposal_is_not_applied_again(self, db):
        """Telegram cards stay tappable forever; once applied, a proposal is done."""
        _seed_pending_txns(db, ["tx_1"])
        proposal_id, _ = cp.register_group([_option()], ["tx_1"])
        db.execute("UPDATE category_proposals SET status = 'selected', selected_option = 0 WHERE id = %s", [proposal_id])
        cp.collect_decisions()
        assert cp.collect_decisions() == 0
        assert db.execute("SELECT COUNT(*) FROM label_events WHERE txn_id = 'tx_1'").fetchone()[0] == 1

    def test_none_of_these_releases_the_transactions(self, db):
        _seed_pending_txns(db, ["tx_1"])
        proposal_id, _ = cp.register_group([_option()], ["tx_1"])
        db.execute("UPDATE category_proposals SET status = 'denied' WHERE id = %s", [proposal_id])

        assert cp.collect_decisions() == 0

        assert db.execute("SELECT status FROM category_proposals WHERE id = %s", [proposal_id]).fetchone()[0] == "denied"
        assert db.execute(
            "SELECT llm_category, pending_category_proposal_id FROM transactions WHERE id = 'tx_1'"
        ).fetchone() == (None, None)

    def test_try_again_keeps_the_transactions_held(self, db):
        """The hold is how pending_regenerations() knows which transactions are
        waiting on fresh options, and what they were already shown."""
        _seed_pending_txns(db, ["tx_1", "tx_2"])
        proposal_id, _ = cp.register_group([_option()], ["tx_1", "tx_2"])
        db.execute("UPDATE category_proposals SET status = 'denied', regenerate_requested = TRUE WHERE id = %s",
                   [proposal_id])

        assert cp.collect_decisions() == 0

        assert _held(db, proposal_id) == {"tx_1", "tx_2"}

    def test_nothing_happens_when_there_are_no_decisions(self, db):
        _seed_pending_txns(db, ["tx_1"])
        proposal_id, _ = cp.register_group([_option()], ["tx_1"])
        assert cp.collect_decisions() == 0
        assert _held(db, proposal_id) == {"tx_1"}


class TestCardLines:
    """The card's lines are stored with the proposal, for the server to send."""

    def _examples(self, db, proposal_id):
        return db.execute("SELECT examples FROM category_proposals WHERE id = %s", [proposal_id]).fetchone()[0]

    def test_a_new_proposal_is_written_unsent_with_up_to_four_lines(self, db):
        for i in range(6):
            insert_transaction(db, f"tx_{i}", amount=-10.0, user_context=f"context {i}",
                               created_at=f"2026-08-0{i + 1} 10:00:00")
        proposal_id, _ = cp.register_group([_option()], [f"tx_{i}" for i in range(6)])
        assert len(self._examples(db, proposal_id)) == 4
        assert db.execute("SELECT sent_at FROM category_proposals WHERE id = %s", [proposal_id]).fetchone()[0] is None

    def test_each_line_says_which_transaction_it_is(self, db):
        """A card that only shows 'Cards' or a quick-tap label can't tell you which
        of several transactions it means."""
        insert_transaction(db, "tx_a", amount=-25.0, description="THE HUB DENTAL",
                           merchant_name="The Hub Dental Practice", user_context="Food & Drink - Alcohol",
                           created_at="2026-08-17 10:00:00")
        proposal_id, _ = cp.register_group([_option()], ["tx_a"])
        assert self._examples(db, proposal_id) == ['-£25.00 · 17 Aug · The Hub Dental Practice — "Food & Drink - Alcohol"']

    def test_a_merged_batch_updates_the_lines(self, db):
        insert_transaction(db, "tx_1", amount=-10.0, merchant_name="First")
        insert_transaction(db, "tx_2", amount=-20.0, merchant_name="Second")
        proposal_id, _ = cp.register_group([_option()], ["tx_1"])
        cp.register_group([_option()], ["tx_2"])
        assert len(self._examples(db, proposal_id)) == 2

    def test_a_transaction_moves_to_the_proposal_now_holding_it(self, db):
        """A transaction is only ever held by one proposal."""
        _seed_pending_txns(db, ["tx_1"])
        first, _ = cp.register_group([_option()], ["tx_1"])
        db.execute("UPDATE category_proposals SET status = 'denied', regenerate_requested = TRUE WHERE id = %s", [first])
        second, _ = cp.register_group([_option("Gifts", "Wedding Gifts")], ["tx_1"])
        assert _held(db, first) == set()
        assert _held(db, second) == {"tx_1"}


class TestCardExample:
    from datetime import datetime as _dt

    def _line(self, **kw):
        base = dict(amount=-25.0, currency="GBP", created_at=self._dt(2026, 8, 7, 9, 0), merchant_name="Tesco",
                    counterparty_name=None, description="TESCO STORES 5578", user_context="weekly shop")
        base.update(kw)
        return cp.card_example(**base)

    def test_full_line(self):
        assert self._line() == '-£25.00 · 7 Aug · Tesco — "weekly shop"'

    def test_money_in_is_marked_with_a_plus(self):
        assert self._line(amount=9.07, merchant_name=None, counterparty_name="Glen Parrish").startswith("+£9.07 · 7 Aug · Glen Parrish")

    def test_payee_falls_back_from_merchant_to_counterparty_to_description(self):
        assert "Glen" in self._line(merchant_name=None, counterparty_name="Glen", description="x")
        assert "RAW DESC" in self._line(merchant_name=None, counterparty_name=None, description="RAW DESC")

    def test_thousands_are_separated_and_other_currencies_named(self):
        assert self._line(amount=-1315.0).startswith("-£1,315.00")
        assert self._line(amount=-12.5, currency="EUR").startswith("-EUR 12.50")

    def test_no_context_means_no_dash_clause(self):
        assert self._line(user_context=None) == "-£25.00 · 7 Aug · Tesco"

    def test_missing_pieces_are_skipped_rather_than_printed_as_none(self):
        line = self._line(amount=None, created_at=None, merchant_name=None, description=None)
        assert "None" not in line and line == "weekly shop"

    def test_long_payee_and_context_are_trimmed_for_a_phone_screen(self):
        line = self._line(merchant_name="M" * 80, user_context="c" * 200)
        assert "M" * 41 not in line and "c" * 61 not in line
