"""End-to-end tests for the real-time category-proposal round trip.

local holds new (parent, subcategory) pairs -> server stores + sends cards ->
user taps -> server records -> local collects and applies. Server routes are
called directly with get_con patched to the in-memory fixture, matching
test_taxonomy_flow.py's approach for the older, monthly proposal flow.
"""
import asyncio
from unittest.mock import MagicMock, patch

import main


def _run(coro):
    return asyncio.run(coro)


async def _ok_api_key(api_key):
    return True


def _proposal_entry(local_id=1, parent="Tax", parent_is_new=True, sub="Self Assessment", count=1):
    return main.CategoryProposalEntry(
        id=local_id, parent_name=parent, parent_is_new=parent_is_new,
        subcategory_name=sub, txn_count=count, examples=["HMRC self assessment"],
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
        body = main.SyncCategoryProposalsRequest(
            proposals=[_proposal_entry(1, "Tax"), _proposal_entry(2, "Gifts", sub="Wedding Gifts")])
        with patch.object(main, "get_con", return_value=server_con), \
             patch.object(main, "bot", bot), \
             patch.dict("os.environ", {"TELEGRAM_CHAT_ID": "12345"}), \
             patch.object(main, "verify_api_key", new=_ok_api_key):
            result = _run(main.sync_category_proposals(body, api_key="k"))

        assert result == {"received": 2}
        assert bot.send_category_proposal.call_count == 2
        rows = server_con.execute(
            "SELECT local_id, status FROM category_proposals ORDER BY local_id").fetchall()
        assert rows == [(1, "pending"), (2, "pending")]

    def test_no_cards_are_sent_when_there_is_nothing_to_propose(self, server_con):
        bot = MagicMock()
        body = main.SyncCategoryProposalsRequest(proposals=[])
        with patch.object(main, "get_con", return_value=server_con), \
             patch.object(main, "bot", bot), \
             patch.object(main, "verify_api_key", new=_ok_api_key):
            result = _run(main.sync_category_proposals(body, api_key="k"))
        assert result == {"received": 0}
        bot.send_category_proposal.assert_not_called()

    def test_a_still_pending_proposal_from_an_earlier_run_today_is_not_cleared(self, server_con):
        """Unlike the monthly taxonomy sync, these arrive continuously through
        the day -- a later sync must not wipe an earlier one still awaiting a
        decision."""
        bot = MagicMock()
        env = {"TELEGRAM_CHAT_ID": "12345"}
        with patch.object(main, "get_con", return_value=server_con), \
             patch.object(main, "bot", bot), \
             patch.dict("os.environ", env), \
             patch.object(main, "verify_api_key", new=_ok_api_key):
            _run(main.sync_category_proposals(
                main.SyncCategoryProposalsRequest(proposals=[_proposal_entry(1, "Tax")]), "k"))
            _run(main.sync_category_proposals(
                main.SyncCategoryProposalsRequest(proposals=[_proposal_entry(2, "Gifts", sub="Wedding Gifts")]), "k"))
        names = {r[0] for r in server_con.execute(
            "SELECT parent_name FROM category_proposals").fetchall()}
        assert names == {"Tax", "Gifts"}

    def test_resyncing_the_same_local_id_does_not_duplicate(self, server_con):
        bot = MagicMock()
        with patch.object(main, "get_con", return_value=server_con), \
             patch.object(main, "bot", bot), \
             patch.dict("os.environ", {"TELEGRAM_CHAT_ID": "12345"}), \
             patch.object(main, "verify_api_key", new=_ok_api_key):
            _run(main.sync_category_proposals(
                main.SyncCategoryProposalsRequest(proposals=[_proposal_entry(1, "Tax")]), "k"))
            _run(main.sync_category_proposals(
                main.SyncCategoryProposalsRequest(proposals=[_proposal_entry(1, "Tax")]), "k"))
        assert server_con.execute("SELECT COUNT(*) FROM category_proposals").fetchone()[0] == 1


class TestApproveDenyCallbacks:
    def _seed(self, con, status="pending"):
        con.execute(
            """INSERT INTO category_proposals
               (local_id, parent_name, parent_is_new, subcategory_name, txn_count, examples, status, sent_at)
               VALUES (1, 'Tax', TRUE, 'Self Assessment', 1, '[]', ?, NOW())""", [status])

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
        bot = self._tap(server_con, "catprop:approve:1")
        status = server_con.execute(
            "SELECT status FROM category_proposals WHERE local_id = 1").fetchone()[0]
        assert status == "approved"
        assert "Approved" in bot.send_message.call_args[0][1]

    def test_deny_records_the_decision(self, server_con):
        self._seed(server_con)
        bot = self._tap(server_con, "catprop:deny:1")
        status = server_con.execute(
            "SELECT status FROM category_proposals WHERE local_id = 1").fetchone()[0]
        assert status == "denied"
        assert "Denied" in bot.send_message.call_args[0][1]

    def test_the_server_never_applies_anything_itself(self, server_con):
        self._seed(server_con)
        self._tap(server_con, "catprop:approve:1")
        tables = {r[0] for r in server_con.execute("SHOW TABLES").fetchall()}
        assert "subcategories" not in tables and "transactions" not in tables

    def test_a_second_tap_is_rejected_rather_than_reapplied(self, server_con):
        self._seed(server_con, status="approved")
        bot = self._tap(server_con, "catprop:deny:1")
        status = server_con.execute(
            "SELECT status FROM category_proposals WHERE local_id = 1").fetchone()[0]
        assert status == "approved"
        assert "Already approved" in bot.send_message.call_args[0][1]

    def test_a_tap_on_an_unknown_proposal_is_handled(self, server_con):
        bot = self._tap(server_con, "catprop:approve:999")
        assert "expired" in bot.send_message.call_args[0][1]

    def test_taxprop_and_catprop_callbacks_do_not_cross_wires(self, server_con):
        """Both prefixes are handled by the same webhook -- a catprop tap must
        never touch the (differently-shaped) taxonomy_proposals table."""
        server_con.execute(
            """CREATE TABLE IF NOT EXISTS taxonomy_proposals_probe AS
               SELECT 1""")  # sanity that server_con has the full schema already
        self._seed(server_con)
        self._tap(server_con, "catprop:approve:1")
        # taxonomy_proposals exists (full schema) but must remain untouched/empty
        assert server_con.execute("SELECT COUNT(*) FROM taxonomy_proposals").fetchone()[0] == 0


class TestCollectedEndpoint:
    def test_marks_rows_collected(self, server_con):
        server_con.execute(
            """INSERT INTO category_proposals
               (local_id, parent_name, parent_is_new, subcategory_name, txn_count, examples, status, sent_at)
               VALUES (1, 'Tax', TRUE, 'Self Assessment', 1, '[]', 'approved', NOW())""")
        with patch.object(main, "get_con", return_value=server_con), \
             patch.object(main, "verify_api_key", new=_ok_api_key):
            result = _run(main.category_decisions_collected(main.MarkProcessedRequest(ids=["1"]), api_key="k"))
        assert result == {"collected": 1}
        assert server_con.execute(
            "SELECT status FROM category_proposals WHERE local_id = 1").fetchone()[0] == "collected"

    def test_decisions_endpoint_only_returns_settled_rows(self, server_con):
        server_con.execute(
            """INSERT INTO category_proposals
               (local_id, parent_name, parent_is_new, subcategory_name, txn_count, examples, status, sent_at)
               VALUES (1, 'Tax', TRUE, 'Self Assessment', 1, '[]', 'pending', NOW())""")
        server_con.execute(
            """INSERT INTO category_proposals
               (local_id, parent_name, parent_is_new, subcategory_name, txn_count, examples, status, sent_at)
               VALUES (2, 'Gifts', TRUE, 'Wedding Gifts', 1, '[]', 'approved', NOW())""")
        with patch.object(main, "get_con", return_value=server_con), \
             patch.object(main, "verify_api_key", new=_ok_api_key):
            result = _run(main.category_decisions(api_key="k"))
        assert [d["id"] for d in result["decisions"]] == [2]
