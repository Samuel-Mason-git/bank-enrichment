"""End-to-end tests for the taxonomy proposal round trip.

local proposes -> server stores + sends cards -> user taps -> server records
-> local collects and applies. Server routes are called directly with get_con
patched to the in-memory fixture, matching test_main_routes.py.
"""
import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

import main
import taxonomy_review as tr


def _run(coro):
    return asyncio.run(coro)


async def _ok_api_key(api_key):
    """Stand-in for the real API-key check. A fresh coroutine per call, since a
    single pre-made awaitable cannot be awaited more than once."""
    return True


def _proposal_entry(local_id=1, name="Breakfast Out", count=8, action="create",
                    target_parent="Food & Drink"):
    return main.TaxonomyProposalEntry(
        id=local_id, parent_name="Food & Drink", source_sub="Lunches Out",
        action=action, target_parent=target_parent, proposed_sub=name,
        rationale="These are breakfasts, not lunches.",
        evidence_count=count, examples=["Breakfast wrap", "Breakfast sub"],
    )


def _callback(data: str):
    cq = MagicMock()
    cq.data = data
    cq.id = "cbq_1"
    cq.from_user.id = 12345
    update = MagicMock()
    update.callback_query = cq
    update.message = None
    req = MagicMock()

    async def _body():
        return b"{}"

    req.body = _body
    return update, req


class TestServerReceivesProposals:
    def test_stores_and_sends_one_card_per_proposal(self, server_con):
        bot = MagicMock()
        body = main.SyncTaxonomyProposalsRequest(
            proposals=[_proposal_entry(1, "Breakfast Out"), _proposal_entry(2, "Commuter Rail")])
        with patch.object(main, "get_con", return_value=server_con), \
             patch.object(main, "bot", bot), \
             patch.dict("os.environ", {"TELEGRAM_CHAT_ID": "12345"}), \
             patch.object(main, "verify_api_key", new=_ok_api_key):
            result = _run(main.sync_taxonomy_proposals(body, api_key="k"))

        assert result == {"received": 2}
        # One intro card, then one card per proposal -- the shape asked for.
        assert bot.send_taxonomy_intro.call_count == 1
        assert bot.send_taxonomy_intro.call_args[0][1] == 2
        assert bot.send_taxonomy_proposal.call_count == 2
        rows = server_con.execute(
            "SELECT local_id, status FROM taxonomy_proposals ORDER BY local_id").fetchall()
        assert rows == [(1, "pending"), (2, "pending")]

    def test_no_cards_are_sent_when_there_is_nothing_to_propose(self, server_con):
        """A quiet month must be silent, not send an intro card saying zero."""
        bot = MagicMock()
        body = main.SyncTaxonomyProposalsRequest(proposals=[])
        with patch.object(main, "get_con", return_value=server_con), \
             patch.object(main, "bot", bot), \
             patch.object(main, "verify_api_key", new=_ok_api_key):
            result = _run(main.sync_taxonomy_proposals(body, api_key="k"))
        assert result == {"received": 0}
        bot.send_taxonomy_intro.assert_not_called()
        bot.send_taxonomy_proposal.assert_not_called()

    def test_a_new_month_clears_undecided_proposals(self, server_con):
        """Last month's unanswered suggestions must not pile up alongside this
        month's -- they were superseded by a fresh review of the same data."""
        bot = MagicMock()
        env = {"TELEGRAM_CHAT_ID": "12345"}
        with patch.object(main, "get_con", return_value=server_con), \
             patch.object(main, "bot", bot), \
             patch.dict("os.environ", env), \
             patch.object(main, "verify_api_key", new=_ok_api_key):
            _run(main.sync_taxonomy_proposals(
                main.SyncTaxonomyProposalsRequest(proposals=[_proposal_entry(1, "Old Idea")]), "k"))
            _run(main.sync_taxonomy_proposals(
                main.SyncTaxonomyProposalsRequest(proposals=[_proposal_entry(2, "New Idea")]), "k"))
        names = [r[0] for r in server_con.execute(
            "SELECT proposed_sub FROM taxonomy_proposals").fetchall()]
        assert names == ["New Idea"]

    def test_a_decided_proposal_survives_the_next_sync(self, server_con):
        """Only pending ones are cleared -- an approval waiting to be collected
        by the local run must not be wiped by the next month's review."""
        bot = MagicMock()
        with patch.object(main, "get_con", return_value=server_con), \
             patch.object(main, "bot", bot), \
             patch.dict("os.environ", {"TELEGRAM_CHAT_ID": "12345"}), \
             patch.object(main, "verify_api_key", new=_ok_api_key):
            _run(main.sync_taxonomy_proposals(
                main.SyncTaxonomyProposalsRequest(proposals=[_proposal_entry(1, "Approved Idea")]), "k"))
            server_con.execute(
                "UPDATE taxonomy_proposals SET status = 'approved' WHERE local_id = 1")
            _run(main.sync_taxonomy_proposals(
                main.SyncTaxonomyProposalsRequest(proposals=[_proposal_entry(2, "New Idea")]), "k"))
        rows = dict(server_con.execute(
            "SELECT proposed_sub, status FROM taxonomy_proposals").fetchall())
        assert rows == {"Approved Idea": "approved", "New Idea": "pending"}


class TestApproveDenyCallbacks:
    def _seed(self, con, status="pending"):
        con.execute(
            """INSERT INTO taxonomy_proposals
               (local_id, parent_name, source_sub, proposed_sub, rationale,
                evidence_count, examples, status, sent_at)
               VALUES (1, 'Food & Drink', 'Lunches Out', 'Meal Deals', 'Desk lunches.',
                       8, '[]', ?, NOW())""", [status])

    def _tap(self, server_con, data):
        bot = MagicMock()
        update, req = _callback(data)
        with patch.object(main, "get_con", return_value=server_con), \
             patch.object(main, "bot", bot), \
             patch.object(main.TelegramUpdate, "model_validate_json", return_value=update):
            _run(main.recieve_telegram(req))
        return bot

    def test_approve_records_the_decision(self, server_con):
        self._seed(server_con)
        bot = self._tap(server_con, "taxprop:approve:1")
        status = server_con.execute(
            "SELECT status FROM taxonomy_proposals WHERE local_id = 1").fetchone()[0]
        assert status == "approved"
        assert "Approved" in bot.send_message.call_args[0][1]

    def test_deny_records_the_decision(self, server_con):
        self._seed(server_con)
        bot = self._tap(server_con, "taxprop:deny:1")
        status = server_con.execute(
            "SELECT status FROM taxonomy_proposals WHERE local_id = 1").fetchone()[0]
        assert status == "denied"

    def test_the_server_never_applies_anything_itself(self, server_con):
        """The taxonomy and transactions live in the LOCAL database. The server
        recording a decision must be the whole of what it does."""
        self._seed(server_con)
        self._tap(server_con, "taxprop:approve:1")
        tables = {r[0] for r in server_con.execute("SHOW TABLES").fetchall()}
        assert "subcategories" not in tables and "transactions" not in tables

    def test_a_second_tap_is_rejected_rather_than_reapplied(self, server_con):
        """Telegram cards stay tappable forever -- tapping Approve again a week
        later must not re-run the move."""
        self._seed(server_con, status="approved")
        bot = self._tap(server_con, "taxprop:deny:1")
        status = server_con.execute(
            "SELECT status FROM taxonomy_proposals WHERE local_id = 1").fetchone()[0]
        assert status == "approved", "the original decision must stand"
        assert "Already approved" in bot.send_message.call_args[0][1]

    def test_a_tap_on_an_unknown_proposal_is_handled(self, server_con):
        bot = self._tap(server_con, "taxprop:approve:999")
        assert "expired" in bot.send_message.call_args[0][1]


class TestLocalAppliesDecisions:
    def _seed_local(self, db, ids=("tx_1", "tx_2", "tx_3")):
        db.execute("""INSERT INTO parent_categories (id, name, created_at)
                      VALUES (1, 'Food & Drink', NOW())""")
        db.execute("""INSERT INTO subcategories (id, name, parent_id, created_at)
                      VALUES (1, 'Lunches Out', 1, NOW())""")
        for i in ids:
            db.execute(
                """INSERT INTO transactions (id, amount, currency, created_at,
                       llm_category, llm_subcategory)
                   VALUES (?, -3.5, 'GBP', NOW(), 'Food & Drink', 'Lunches Out')""", [i])
        db.execute(
            """INSERT INTO taxonomy_proposals
               (id, parent_name, source_sub, proposed_sub, rationale, evidence_ids,
                evidence_count, status, proposed_at, run_key)
               VALUES (1, 'Food & Drink', 'Lunches Out', 'Meal Deals', 'Desk lunches.',
                       ?, ?, 'pending', NOW(), '2026-08')""",
            [json.dumps(list(ids)), len(ids)])

    def test_approval_creates_the_subcategory_and_moves_the_evidence(self, db, tmp_path, monkeypatch):
        import database_functions
        monkeypatch.setattr(database_functions, "DB_PATH", str(tmp_path / "t.db"))
        self._seed_local(db)
        db.execute("""INSERT INTO transactions (id, amount, currency, created_at,
                          llm_category, llm_subcategory)
                      VALUES ('tx_other', -9.0, 'GBP', NOW(), 'Food & Drink', 'Lunches Out')""")

        with patch.object(tr, "fetch_decisions",
                          return_value=[{"id": 1, "proposed_sub": "Meal Deals", "status": "approved"}]), \
             patch.object(tr, "confirm_collected"):
            applied = tr.collect_decisions()

        assert applied == 1
        moved = {r[0] for r in db.execute(
            "SELECT id FROM transactions WHERE llm_subcategory = 'Meal Deals'").fetchall()}
        assert moved == {"tx_1", "tx_2", "tx_3"}
        # The transaction that was not on the card must be untouched.
        assert db.execute(
            "SELECT llm_subcategory FROM transactions WHERE id = 'tx_other'").fetchone()[0] == "Lunches Out"
        assert db.execute(
            "SELECT COUNT(*) FROM subcategories WHERE name = 'Meal Deals'").fetchone()[0] == 1

    def test_a_backup_is_taken_before_anything_moves(self, db, tmp_path, monkeypatch):
        import database_functions
        monkeypatch.setattr(database_functions, "DB_PATH", str(tmp_path / "t.db"))
        self._seed_local(db)
        with patch.object(tr, "fetch_decisions",
                          return_value=[{"id": 1, "proposed_sub": "Meal Deals", "status": "approved"}]), \
             patch.object(tr, "confirm_collected"), \
             patch.object(database_functions, "backup_db") as backup:
            tr.collect_decisions()
        backup.assert_called_once()

    def test_denial_changes_nothing_but_is_recorded(self, db, tmp_path, monkeypatch):
        import database_functions
        monkeypatch.setattr(database_functions, "DB_PATH", str(tmp_path / "t.db"))
        self._seed_local(db)
        with patch.object(tr, "fetch_decisions",
                          return_value=[{"id": 1, "proposed_sub": "Meal Deals", "status": "denied"}]), \
             patch.object(tr, "confirm_collected"):
            applied = tr.collect_decisions()
        assert applied == 0
        assert db.execute(
            "SELECT COUNT(*) FROM subcategories WHERE name = 'Meal Deals'").fetchone()[0] == 0
        assert db.execute(
            "SELECT status FROM taxonomy_proposals WHERE id = 1").fetchone()[0] == "denied"

    def test_a_manually_recategorised_transaction_is_not_dragged_back(self, db, tmp_path, monkeypatch):
        """Between the card being sent and approved, the user may have moved a
        transaction by hand in the dashboard. Approving an older proposal must
        not override that later, deliberate decision."""
        import database_functions
        monkeypatch.setattr(database_functions, "DB_PATH", str(tmp_path / "t.db"))
        self._seed_local(db)
        db.execute("""UPDATE transactions SET llm_category = 'Shopping',
                          llm_subcategory = 'General Retail' WHERE id = 'tx_2'""")
        with patch.object(tr, "fetch_decisions",
                          return_value=[{"id": 1, "proposed_sub": "Meal Deals", "status": "approved"}]), \
             patch.object(tr, "confirm_collected"):
            tr.collect_decisions()
        assert db.execute(
            "SELECT llm_subcategory FROM transactions WHERE id = 'tx_2'").fetchone()[0] == "General Retail"
        moved = {r[0] for r in db.execute(
            "SELECT id FROM transactions WHERE llm_subcategory = 'Meal Deals'").fetchall()}
        assert moved == {"tx_1", "tx_3"}

    def test_decisions_are_only_confirmed_after_they_are_applied(self, db, tmp_path, monkeypatch):
        """If confirming ran first, a crash mid-apply would lose the approval
        with no way to retry it."""
        import database_functions
        monkeypatch.setattr(database_functions, "DB_PATH", str(tmp_path / "t.db"))
        self._seed_local(db)
        order = []
        with patch.object(tr, "fetch_decisions",
                          return_value=[{"id": 1, "proposed_sub": "Meal Deals", "status": "approved"}]), \
             patch.object(tr, "apply_approved", side_effect=lambda p: order.append("apply") or 3), \
             patch.object(tr, "confirm_collected", side_effect=lambda ids: order.append("confirm")):
            tr.collect_decisions()
        assert order == ["apply", "confirm"]

    def test_nothing_happens_when_there_are_no_decisions(self, db):
        with patch.object(tr, "fetch_decisions", return_value=[]):
            assert tr.collect_decisions() == 0


class TestMoveActionEndToEnd:
    def test_the_card_reads_as_a_move_not_a_new_category(self, server_con):
        """Approving a move and approving a new category are materially
        different decisions, so the card must not describe one as the other."""
        bot = MagicMock()
        body = main.SyncTaxonomyProposalsRequest(proposals=[
            _proposal_entry(1, "Takeaway", action="move", target_parent="Food & Drink")])
        with patch.object(main, "get_con", return_value=server_con), \
             patch.object(main, "bot", bot), \
             patch.dict("os.environ", {"TELEGRAM_CHAT_ID": "12345"}), \
             patch.object(main, "verify_api_key", new=_ok_api_key):
            _run(main.sync_taxonomy_proposals(body, api_key="k"))
        sent = bot.send_taxonomy_proposal.call_args[0][1]
        assert sent["action"] == "move"
        assert sent["target_parent"] == "Food & Drink"

    def test_local_move_reassigns_into_the_existing_category(self, db, tmp_path, monkeypatch):
        import database_functions
        monkeypatch.setattr(database_functions, "DB_PATH", str(tmp_path / "t.db"))
        db.execute("INSERT INTO parent_categories (id, name, created_at) VALUES (1, 'Food & Drink', NOW())")
        db.execute("INSERT INTO parent_categories (id, name, created_at) VALUES (2, 'Transport', NOW())")
        db.execute("INSERT INTO subcategories (id, name, parent_id, created_at) VALUES (1, 'Lunches Out', 1, NOW())")
        db.execute("INSERT INTO subcategories (id, name, parent_id, created_at) VALUES (2, 'Parking', 2, NOW())")
        for i in ("tx_1", "tx_2"):
            db.execute("""INSERT INTO transactions (id, amount, currency, created_at,
                              llm_category, llm_subcategory)
                          VALUES (?, -3.5, 'GBP', NOW(), 'Food & Drink', 'Lunches Out')""", [i])
        db.execute(
            """INSERT INTO taxonomy_proposals
               (id, parent_name, source_sub, action, target_parent, proposed_sub,
                rationale, evidence_ids, evidence_count, status, proposed_at, run_key)
               VALUES (1, 'Food & Drink', 'Lunches Out', 'move', 'Transport', 'Parking',
                       'Actually parking.', ?, 2, 'pending', NOW(), '2026-08')""",
            [json.dumps(["tx_1", "tx_2"])])

        with patch.object(tr, "fetch_decisions",
                          return_value=[{"id": 1, "proposed_sub": "Parking", "status": "approved"}]), \
             patch.object(tr, "confirm_collected"):
            tr.collect_decisions()

        rows = db.execute(
            "SELECT llm_category, llm_subcategory FROM transactions ORDER BY id").fetchall()
        # BOTH columns must move -- a subcategory under a different parent would
        # otherwise vanish from every category-based view in the dashboard.
        assert rows == [("Transport", "Parking"), ("Transport", "Parking")]
        assert db.execute(
            "SELECT COUNT(*) FROM subcategories WHERE name = 'Parking'").fetchone()[0] == 1, \
            "a move must not create a duplicate of the category it moves into"
