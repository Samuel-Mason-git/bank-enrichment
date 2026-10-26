"""The category-proposal round trip, end to end on the shared tables.

The pipeline holds transactions behind a proposal of new (parent, subcategory)
options -> the server sends its card -> you tap one option, "None of these" or
"Try again" -> the server records it on the proposal -> the next pipeline run
acts on it. The pipeline side goes through its own connection (`db`), the server side
through psycopg (`server_con`), both on the same Postgres schema.
"""
import json
from unittest.mock import MagicMock

import category_proposals as cp
from follow_up_tg import run_new_cards
from tests.local_helpers import insert_transaction
from tests.server_helpers import replies, tap


def _option(parent="Tax", sub="Self Assessment", is_new=True, rationale="Best fit.", **flags):
    return {"parent_name": parent, "subcategory_name": sub, "parent_is_new": is_new, "rationale": rationale, **flags}


OPTIONS = [_option("Tax", "Self Assessment"), _option("Professional Services", "Tax Payments", is_new=False)]


def _proposed(db, txn_ids=("tx_1",), options=OPTIONS):
    for txn_id in txn_ids:
        insert_transaction(db, txn_id, amount=-300.0, merchant_name="HMRC", user_context="tax bill")
    proposal_id, _ = cp.register_group(list(options), list(txn_ids))
    return proposal_id


def _send(server_con):
    bot = MagicMock()
    run_new_cards(bot)
    return bot


def _status(con, proposal_id):
    return con.execute(
        "SELECT status, selected_option, regenerate_requested FROM category_proposals WHERE id = %s", [proposal_id]
    ).fetchone()


class TestTheRoundTrip:
    def test_a_chosen_option_is_created_and_the_held_transactions_filed_under_it(self, db, server_con):
        pid = _proposed(db, ["tx_1", "tx_2"])

        card = _send(server_con).send_category_proposal.call_args.args[1]
        assert card["options"] == OPTIONS and card["txn_count"] == 2
        assert card["examples"][0].startswith("-£300.00")

        tap(server_con, f"catprop:select:{pid}:1")
        assert cp.collect_decisions() == 1

        for txn_id in ("tx_1", "tx_2"):
            row = db.execute(
                "SELECT llm_category, llm_subcategory, pending_category_proposal_id FROM transactions WHERE id = %s",
                [txn_id]).fetchone()
            assert row == ("Professional Services", "Tax Payments", None)
        assert _status(server_con, pid)[0] == "applied"
        assert db.execute("SELECT COUNT(*) FROM parent_categories WHERE name = 'Tax'").fetchone()[0] == 0, \
            "the option not chosen is never created"

    def test_none_of_these_releases_the_transactions_and_rules_the_names_out(self, db, server_con):
        pid = _proposed(db)
        _send(server_con)
        tap(server_con, f"catprop:denyall:{pid}")
        cp.collect_decisions()

        assert db.execute(
            "SELECT llm_category, pending_category_proposal_id FROM transactions WHERE id = 'tx_1'"
        ).fetchone() == (None, None)
        assert cp.denied_parent_names() == {"tax"}
        assert cp.denied_sub_names() == {"self assessment", "tax payments"}

    def test_try_again_keeps_them_held_for_a_fresh_proposal(self, db, server_con):
        pid = _proposed(db)
        _send(server_con)
        tap(server_con, f"catprop:regenerate:{pid}")
        cp.collect_decisions()

        waiting = cp.pending_regenerations()
        assert [(w["old_id"], w["txn_ids"]) for w in waiting] == [(pid, ["tx_1"])]
        assert cp.denied_parent_names() == set(), "asking for other ideas doesn't rule these out"

    def test_a_card_is_only_sent_once(self, db, server_con):
        _proposed(db)
        _send(server_con)
        assert _send(server_con).send_category_proposal.call_count == 0

    def test_a_second_look_cards_flags_reach_the_card(self, db, server_con):
        """Without them every placement-judge card would be drawn as a
        new-category one."""
        _proposed(db, options=[
            _option("Health", "Dental", is_new=False, judge=True),
            _option("Food & Drink", "Alcohol", is_new=False, judge=True, is_original=True),
        ])
        sent = _send(server_con).send_category_proposal.call_args.args[1]["options"]
        assert [o["judge"] for o in sent] == [True, True]
        assert [o.get("is_original", False) for o in sent] == [False, True]


class TestTheTaps:
    """What the server records for each button, and what it says back."""

    def _pending(self, server_con, status="pending"):
        server_con.execute(
            "INSERT INTO category_proposals (id, options, txn_ids, status, proposed_at, sent_at) "
            "VALUES (1, %s, '[\"tx_1\"]', %s, NOW(), NOW())",
            [json.dumps(OPTIONS), status])

    def test_selecting_records_the_option(self, server_con):
        self._pending(server_con)
        bot = tap(server_con, "catprop:select:1:1")
        assert _status(server_con, 1) == ("selected", 1, False)
        assert "Tax Payments" in replies(bot)[0]

    def test_none_of_these_is_recorded_as_denied(self, server_con):
        self._pending(server_con)
        bot = tap(server_con, "catprop:denyall:1")
        assert _status(server_con, 1) == ("denied", None, False)
        assert "None of these" in replies(bot)[0]

    def test_try_again_is_denied_with_the_flag_set(self, server_con):
        self._pending(server_con)
        bot = tap(server_con, "catprop:regenerate:1")
        assert _status(server_con, 1) == ("denied", None, True)
        assert "fresh set of options" in replies(bot)[0]

    def test_the_server_never_files_anything_itself(self, server_con):
        self._pending(server_con)
        tap(server_con, "catprop:select:1:0")
        assert server_con.execute("SELECT COUNT(*) FROM label_events").fetchone()[0] == 0
        assert server_con.execute("SELECT COUNT(*) FROM parent_categories").fetchone()[0] == 0

    def test_a_second_tap_is_refused(self, server_con):
        self._pending(server_con, status="selected")
        bot = tap(server_con, "catprop:denyall:1")
        assert _status(server_con, 1)[0] == "selected"
        assert "Already selected" in replies(bot)[0]

    def test_a_tap_on_an_unknown_proposal_says_so(self, server_con):
        assert "expired" in replies(tap(server_con, "catprop:select:999:0"))[0]

    def test_it_never_touches_the_monthly_proposals(self, server_con):
        self._pending(server_con)
        tap(server_con, "catprop:select:1:0")
        assert server_con.execute("SELECT COUNT(*) FROM taxonomy_proposals").fetchone()[0] == 0
