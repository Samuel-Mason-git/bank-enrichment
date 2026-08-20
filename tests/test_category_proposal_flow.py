"""End-to-end tests for the real-time category-proposal round trip.

local holds new (parent, subcategory) candidate options -> server stores +
sends a card with one button per option -> user taps select/deny-all ->
server records -> local collects and applies. Server routes are called
directly with get_con patched to the in-memory fixture, matching
test_taxonomy_flow.py's approach for the older, monthly proposal flow.
"""
import asyncio
from unittest.mock import MagicMock, patch

import main


def _run(coro):
    return asyncio.run(coro)


async def _ok_api_key(api_key):
    return True


def _option(parent="Tax", sub="Self Assessment", is_new=True, rationale="Best fit."):
    return main.CategoryOption(parent_name=parent, subcategory_name=sub, parent_is_new=is_new, rationale=rationale)


def _proposal_entry(local_id=1, options=None, count=1):
    return main.CategoryProposalEntry(
        id=local_id, options=options or [_option()], txn_count=count, examples=["HMRC self assessment"],
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
            proposals=[_proposal_entry(1, [_option("Tax", "Self Assessment")]),
                       _proposal_entry(2, [_option("Gifts", "Wedding Gifts")])])
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

    def test_multiple_options_are_stored_and_passed_to_the_card(self, server_con):
        bot = MagicMock()
        options = [_option("Tax", "Self Assessment"), _option("Professional Services", "Tax Payments", is_new=False)]
        body = main.SyncCategoryProposalsRequest(proposals=[_proposal_entry(1, options)])
        with patch.object(main, "get_con", return_value=server_con), \
             patch.object(main, "bot", bot), \
             patch.dict("os.environ", {"TELEGRAM_CHAT_ID": "12345"}), \
             patch.object(main, "verify_api_key", new=_ok_api_key):
            _run(main.sync_category_proposals(body, api_key="k"))

        sent = bot.send_category_proposal.call_args[0][1]
        assert len(sent["options"]) == 2
        assert sent["options"][1]["subcategory_name"] == "Tax Payments"

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
                main.SyncCategoryProposalsRequest(proposals=[_proposal_entry(1, [_option("Tax", "Self Assessment")])]), "k"))
            _run(main.sync_category_proposals(
                main.SyncCategoryProposalsRequest(proposals=[_proposal_entry(2, [_option("Gifts", "Wedding Gifts")])]), "k"))
        assert server_con.execute("SELECT COUNT(*) FROM category_proposals").fetchone()[0] == 2

    def test_resyncing_the_same_local_id_does_not_duplicate(self, server_con):
        bot = MagicMock()
        with patch.object(main, "get_con", return_value=server_con), \
             patch.object(main, "bot", bot), \
             patch.dict("os.environ", {"TELEGRAM_CHAT_ID": "12345"}), \
             patch.object(main, "verify_api_key", new=_ok_api_key):
            _run(main.sync_category_proposals(
                main.SyncCategoryProposalsRequest(proposals=[_proposal_entry(1)]), "k"))
            _run(main.sync_category_proposals(
                main.SyncCategoryProposalsRequest(proposals=[_proposal_entry(1)]), "k"))
        assert server_con.execute("SELECT COUNT(*) FROM category_proposals").fetchone()[0] == 1

    def test_a_local_id_collision_with_a_stale_row_sends_no_phantom_card(self, server_con):
        """Regression test: local ids only ever increase in normal operation,
        but a local database restored from a backup older than the server's
        state (or, as actually happened once, a manual row deletion) can
        reissue an id the server already has history for. INSERT ... ON
        CONFLICT DO NOTHING used to silently no-op while still sending a card
        built from the NEW content -- a card that didn't match what was
        actually stored, so tapping it just replied "Already <old status>."
        The stale row's own content must also be left completely untouched."""
        bot = MagicMock()
        with patch.object(main, "get_con", return_value=server_con), \
             patch.object(main, "bot", bot), \
             patch.dict("os.environ", {"TELEGRAM_CHAT_ID": "12345"}), \
             patch.object(main, "verify_api_key", new=_ok_api_key):
            # An old, long-resolved proposal still sitting in server history.
            _run(main.sync_category_proposals(
                main.SyncCategoryProposalsRequest(proposals=[_proposal_entry(1, [_option("Old", "Idea")])]), "k"))
            server_con.execute("UPDATE category_proposals SET status = 'collected' WHERE local_id = 1")
            bot.reset_mock()

            # A brand new proposal reuses the same id.
            _run(main.sync_category_proposals(
                main.SyncCategoryProposalsRequest(proposals=[_proposal_entry(1, [_option("New", "Idea")])]), "k"))

        bot.send_category_proposal.assert_not_called()
        row = server_con.execute(
            "SELECT options, status FROM category_proposals WHERE local_id = 1").fetchone()
        assert "Old" in row[0] and "New" not in row[0]
        assert row[1] == "collected"


class TestSelectDenyCallbacks:
    def _seed(self, con, status="pending", options=None):
        import json
        con.execute(
            """INSERT INTO category_proposals (local_id, options, txn_count, examples, status, sent_at)
               VALUES (1, ?, 1, '[]', ?, NOW())""",
            [json.dumps(options or [
                {"parent_name": "Tax", "subcategory_name": "Self Assessment", "parent_is_new": True, "rationale": "x"},
                {"parent_name": "Professional Services", "subcategory_name": "Tax Payments", "parent_is_new": False, "rationale": "y"},
            ]), status],
        )

    def _tap(self, server_con, data):
        bot = MagicMock()
        update, req = _callback(data)
        with patch.object(main, "get_con", return_value=server_con), \
             patch.object(main, "bot", bot), \
             patch.object(main.TelegramUpdate, "model_validate_json", return_value=update):
            _run(main.recieve_telegram(req))
        return bot

    def test_selecting_an_option_records_the_decision_and_index(self, server_con):
        self._seed(server_con)
        bot = self._tap(server_con, "catprop:select:1:1")
        row = server_con.execute(
            "SELECT status, selected_option FROM category_proposals WHERE local_id = 1").fetchone()
        assert row == ("selected", 1)
        assert "Tax Payments" in bot.send_message.call_args[0][1]

    def test_deny_all_records_the_decision(self, server_con):
        self._seed(server_con)
        bot = self._tap(server_con, "catprop:denyall:1")
        row = server_con.execute(
            "SELECT status, selected_option FROM category_proposals WHERE local_id = 1").fetchone()
        assert row == ("denied", None)
        assert "None of these" in bot.send_message.call_args[0][1]

    def test_regenerate_is_recorded_as_denied_with_the_flag_set(self, server_con):
        """Recorded as 'denied' under the hood so the same collection path
        (status IN ('selected','denied')) picks it up, but flagged so the
        local side knows to ask Claude for fresh options instead of just
        unlocking the transactions."""
        self._seed(server_con)
        bot = self._tap(server_con, "catprop:regenerate:1")
        row = server_con.execute(
            "SELECT status, selected_option, regenerate_requested FROM category_proposals WHERE local_id = 1"
        ).fetchone()
        assert row == ("denied", None, True)
        assert "fresh set of options" in bot.send_message.call_args[0][1]

    def test_the_server_never_applies_anything_itself(self, server_con):
        self._seed(server_con)
        self._tap(server_con, "catprop:select:1:0")
        tables = {r[0] for r in server_con.execute("SHOW TABLES").fetchall()}
        assert "subcategories" not in tables and "transactions" not in tables

    def test_a_second_tap_is_rejected_rather_than_reapplied(self, server_con):
        self._seed(server_con, status="selected")
        bot = self._tap(server_con, "catprop:denyall:1")
        row = server_con.execute(
            "SELECT status FROM category_proposals WHERE local_id = 1").fetchone()
        assert row[0] == "selected"
        assert "Already selected" in bot.send_message.call_args[0][1]

    def test_a_tap_on_an_unknown_proposal_is_handled(self, server_con):
        bot = self._tap(server_con, "catprop:select:999:0")
        assert "expired" in bot.send_message.call_args[0][1]

    def test_taxprop_and_catprop_callbacks_do_not_cross_wires(self, server_con):
        """Both prefixes are handled by the same webhook -- a catprop tap must
        never touch the (differently-shaped) taxonomy_proposals table."""
        self._seed(server_con)
        self._tap(server_con, "catprop:select:1:0")
        assert server_con.execute("SELECT COUNT(*) FROM taxonomy_proposals").fetchone()[0] == 0


class TestCollectedEndpoint:
    def test_marks_rows_collected(self, server_con):
        import json
        server_con.execute(
            """INSERT INTO category_proposals (local_id, options, txn_count, examples, status, selected_option, sent_at)
               VALUES (1, ?, 1, '[]', 'selected', 0, NOW())""",
            [json.dumps([{"parent_name": "Tax", "subcategory_name": "Self Assessment", "parent_is_new": True, "rationale": "x"}])],
        )
        with patch.object(main, "get_con", return_value=server_con), \
             patch.object(main, "verify_api_key", new=_ok_api_key):
            result = _run(main.category_decisions_collected(main.MarkProcessedRequest(ids=["1"]), api_key="k"))
        assert result == {"collected": 1}
        assert server_con.execute(
            "SELECT status FROM category_proposals WHERE local_id = 1").fetchone()[0] == "collected"

    def test_decisions_endpoint_only_returns_settled_rows_and_includes_selected_option(self, server_con):
        import json
        opts = json.dumps([{"parent_name": "Tax", "subcategory_name": "Self Assessment", "parent_is_new": True, "rationale": "x"}])
        server_con.execute(
            "INSERT INTO category_proposals (local_id, options, txn_count, examples, status, sent_at) VALUES (1, ?, 1, '[]', 'pending', NOW())",
            [opts])
        server_con.execute(
            "INSERT INTO category_proposals (local_id, options, txn_count, examples, status, selected_option, sent_at) VALUES (2, ?, 1, '[]', 'selected', 1, NOW())",
            [opts])
        with patch.object(main, "get_con", return_value=server_con), \
             patch.object(main, "verify_api_key", new=_ok_api_key):
            result = _run(main.category_decisions(api_key="k"))
        assert result["decisions"] == [
            {"id": 2, "status": "selected", "selected_option": 1, "regenerate_requested": False}
        ]

    def test_decisions_endpoint_reports_regenerate_requested(self, server_con):
        import json
        opts = json.dumps([{"parent_name": "Tax", "subcategory_name": "Self Assessment", "parent_is_new": True, "rationale": "x"}])
        server_con.execute(
            """INSERT INTO category_proposals
               (local_id, options, txn_count, examples, status, regenerate_requested, sent_at)
               VALUES (1, ?, 1, '[]', 'denied', TRUE, NOW())""",
            [opts])
        with patch.object(main, "get_con", return_value=server_con), \
             patch.object(main, "verify_api_key", new=_ok_api_key):
            result = _run(main.category_decisions(api_key="k"))
        assert result["decisions"] == [
            {"id": 1, "status": "denied", "selected_option": None, "regenerate_requested": True}
        ]
