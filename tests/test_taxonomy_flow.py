"""The monthly taxonomy-proposal round trip, end to end on the shared tables.

The pipeline's review stores this month's proposals -> the server sends their
cards -> you approve or deny -> the server records it on the proposal -> the
next pipeline run creates the subcategory and moves exactly the transactions
the card listed. The pipeline side goes through DuckDB (`db`), the server side
through psycopg (`server_con`), both on the same Postgres schema.
"""
from unittest.mock import MagicMock

import database_functions
import taxonomy_review as tr
from follow_up_tg import run_new_cards
from tests.local_helpers import ensure_category, insert_transaction
from tests.server_helpers import replies, tap


def _proposal(target_sub="Breakfast Out", evidence=("tx_1", "tx_2"), action="create",
              target_parent="Food & Drink", source_sub="Lunches Out"):
    return {"parent_name": "Food & Drink", "source_sub": source_sub, "action": action,
            "target_parent": target_parent, "target_sub": target_sub, "rationale": "Breakfasts, not lunches.",
            "evidence_ids": list(evidence), "evidence_count": len(evidence), "examples": ["Breakfast"]}


def _lunches(db, ids=("tx_1", "tx_2", "tx_3")):
    for txn_id in ids:
        insert_transaction(db, txn_id, amount=-3.5, llm_category="Food & Drink", llm_subcategory="Lunches Out")


def _filed(db):
    return dict((r[0], (r[1], r[2])) for r in db.execute(
        "SELECT id, llm_category, llm_subcategory FROM transactions ORDER BY id").fetchall())


def _send():
    bot = MagicMock()
    run_new_cards(bot)
    return bot


class TestTheRoundTrip:
    def test_an_approval_creates_the_subcategory_and_moves_only_the_listed_transactions(self, db, server_con):
        _lunches(db)
        [stored] = tr.store_proposals([_proposal()], "2026-08")

        bot = _send()
        bot.send_taxonomy_intro.assert_called_once()
        card = bot.send_taxonomy_proposal.call_args.args[1]
        assert (card["proposed_sub"], card["evidence_count"], card["examples"]) == ("Breakfast Out", 2, ["Breakfast"])

        tap(server_con, f"taxprop:approve:{stored['id']}")
        assert tr.collect_decisions() == 1

        assert _filed(db) == {
            "tx_1": ("Food & Drink", "Breakfast Out"),
            "tx_2": ("Food & Drink", "Breakfast Out"),
            "tx_3": ("Food & Drink", "Lunches Out"),
        }
        assert server_con.execute("SELECT status FROM taxonomy_proposals").fetchone()[0] == "applied"
        assert tr.collect_decisions() == 0, "applied once, never again"

    def test_a_denial_changes_nothing_and_the_name_stays_ruled_out(self, db, server_con):
        _lunches(db)
        [stored] = tr.store_proposals([_proposal()], "2026-08")
        _send()
        tap(server_con, f"taxprop:deny:{stored['id']}")

        assert tr.collect_decisions() == 0
        assert {v[1] for v in _filed(db).values()} == {"Lunches Out"}
        assert "breakfast out" in tr.previously_proposed_names()

    def test_a_transaction_moved_by_hand_meanwhile_is_not_dragged_back(self, db, server_con):
        """An approval should never override a later, deliberate decision."""
        _lunches(db)
        [stored] = tr.store_proposals([_proposal()], "2026-08")
        ensure_category(db, "Shopping", "General Retail")
        database_functions.update_classification("tx_2", "Shopping", "General Retail", 1.0, "manual")
        tap(server_con, f"taxprop:approve:{stored['id']}")
        tr.collect_decisions()

        assert _filed(db)["tx_1"] == ("Food & Drink", "Breakfast Out")
        assert _filed(db)["tx_2"] == ("Shopping", "General Retail")

    def test_a_move_files_into_the_existing_category_under_its_own_parent(self, db, server_con):
        _lunches(db, ("tx_1", "tx_2"))
        ensure_category(db, "Transport", "Parking")
        [stored] = tr.store_proposals(
            [_proposal("Parking", action="move", target_parent="Transport")], "2026-08")

        card = _send().send_taxonomy_proposal.call_args.args[1]
        assert (card["action"], card["target_parent"]) == ("move", "Transport"), \
            "the card must read as a move, not a new category"

        tap(server_con, f"taxprop:approve:{stored['id']}")
        tr.collect_decisions()

        # Both must move -- a subcategory under the wrong parent would vanish
        # from every category-based view.
        assert set(_filed(db).values()) == {("Transport", "Parking")}
        assert db.execute("SELECT COUNT(*) FROM subcategories WHERE name = 'Parking'").fetchone()[0] == 1, \
            "a move must not create a duplicate of the category it moves into"

    def test_a_quiet_month_sends_nothing(self, db, server_con):
        tr._record_empty_run("2026-08")
        bot = _send()
        bot.send_taxonomy_intro.assert_not_called()
        bot.send_taxonomy_proposal.assert_not_called()

    def test_a_new_month_supersedes_last_months_unanswered_cards(self, db, server_con):
        tr.store_proposals([_proposal("Breakfast Out")], "2026-07")
        _send()
        tr.store_proposals([_proposal("Meal Deals")], "2026-08")
        cards = [c.args[1]["proposed_sub"] for c in _send().send_taxonomy_proposal.call_args_list]
        assert cards == ["Meal Deals"]

    def test_an_approval_waiting_to_be_applied_survives_the_next_review(self, db, server_con):
        _lunches(db)
        [stored] = tr.store_proposals([_proposal()], "2026-07")
        tap(server_con, f"taxprop:approve:{stored['id']}")
        tr.store_proposals([_proposal("Meal Deals")], "2026-08")
        assert tr.collect_decisions() == 1


class TestTheTaps:
    def _pending(self, server_con, status="pending"):
        server_con.execute(
            """INSERT INTO taxonomy_proposals (id, parent_name, source_sub, proposed_sub, rationale,
                   evidence_ids, status, run_key, proposed_at, sent_at)
               VALUES (1, 'Food & Drink', 'Lunches Out', 'Breakfast Out', 'x', '["tx_1"]', %s,
                       '2026-08', NOW(), NOW())""", [status])

    def _status(self, server_con):
        return server_con.execute("SELECT status FROM taxonomy_proposals WHERE id = 1").fetchone()[0]

    def test_approve_is_recorded(self, server_con):
        self._pending(server_con)
        bot = tap(server_con, "taxprop:approve:1")
        assert self._status(server_con) == "approved"
        assert "Approved" in replies(bot)[0]

    def test_deny_is_recorded(self, server_con):
        self._pending(server_con)
        bot = tap(server_con, "taxprop:deny:1")
        assert self._status(server_con) == "denied"
        assert "won't be suggested again" in replies(bot)[0]

    def test_the_server_never_applies_anything_itself(self, server_con):
        self._pending(server_con)
        tap(server_con, "taxprop:approve:1")
        assert server_con.execute("SELECT COUNT(*) FROM subcategories").fetchone()[0] == 0
        assert server_con.execute("SELECT COUNT(*) FROM label_events").fetchone()[0] == 0

    def test_a_second_tap_is_refused(self, server_con):
        """Telegram cards stay tappable forever."""
        self._pending(server_con, status="applied")
        bot = tap(server_con, "taxprop:deny:1")
        assert self._status(server_con) == "applied"
        assert "Already applied" in replies(bot)[0]

    def test_a_tap_on_an_unknown_proposal_says_so(self, server_con):
        assert "expired" in replies(tap(server_con, "taxprop:approve:999"))[0]
