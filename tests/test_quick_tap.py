"""Tapping a quick-category button on a card, through the real webhook
handler: what it saves, and the buttons it doesn't recognise."""
from tests.server_helpers import add_transaction, replies, seed_taxonomy, status, tap


def _contexts(con, txn_id="tx_1"):
    return [r[0] for r in con.execute(
        "SELECT user_context FROM context_events WHERE txn_id = %s AND source = 'telegram' ORDER BY event_id",
        [txn_id]).fetchall()]


class TestQuickTap:
    def test_saves_the_category_the_button_showed(self, server_con):
        seed_taxonomy(server_con)
        add_transaction(server_con)
        bot = tap(server_con, "qcat:tx_1:2")
        assert _contexts(server_con) == ["Food & Drink - Takeaway"]
        assert status(server_con) == "enriched"
        assert replies(bot) == ["✅ Saved as Food & Drink → Takeaway"]

    def test_a_category_deleted_since_the_card_saves_nothing(self, server_con):
        seed_taxonomy(server_con)
        add_transaction(server_con)
        bot = tap(server_con, "qcat:tx_1:99")
        assert _contexts(server_con) == []
        assert "doesn't exist any more" in replies(bot)[0]

    def test_a_button_from_before_the_move_does_nothing(self, server_con):
        """Cards sent before 26 Oct 2026 had quickcat: buttons, pointing at the
        old database. A tap is acknowledged, so the button stops spinning, and
        nothing else happens."""
        seed_taxonomy(server_con)
        add_transaction(server_con)
        bot = tap(server_con, "quickcat:tx_1:1")
        bot.ack_callback.assert_called_once()
        assert _contexts(server_con) == [] and replies(bot) == []
        assert status(server_con) == "pending"
