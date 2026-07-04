import json
import time
from unittest.mock import MagicMock

from follow_up_tg import run_requester, run_cleanup


def _payload_json(txn_id="tx_001", merchant_name="Tesco", amount_pence=-500):
    return json.dumps({
        "data": {
            "id": txn_id,
            "amount": amount_pence,
            "currency": "GBP",
            "description": "Weekly shop",
            "category": "groceries",
            "created": "2026-01-15T10:00:00Z",
            "merchant": {"name": merchant_name, "emoji": "🛒", "category": "groceries"},
        }
    })


def _ago(seconds: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - seconds))


def _insert_queue_row(con, id, payload, received_at, status="pending", request_count=0,
                       last_requested_at=None, skipped=False, enriched_at=None):
    con.execute(
        """INSERT INTO webhook_queue
           (id, payload, received_at, status, request_count, last_requested_at, skipped, enriched_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        [id, payload, received_at, status, request_count, last_requested_at, skipped, enriched_at]
    )


class TestRunRequesterMissedSend:
    def test_recovers_missed_initial_send(self, server_con):
        _insert_queue_row(server_con, "tx_001", _payload_json(), received_at=_ago(86_400 * 2))
        bot = MagicMock()
        run_requester(bot)
        bot.send_card.assert_called_once()
        row = server_con.execute(
            "SELECT request_count, last_requested_at FROM webhook_queue WHERE id = ?", ["tx_001"]
        ).fetchone()
        assert row[0] == 1
        assert row[1] is not None

    def test_ignores_recent_pending_transactions(self, server_con):
        _insert_queue_row(server_con, "tx_001", _payload_json(), received_at=_ago(60))
        bot = MagicMock()
        run_requester(bot)
        bot.send_card.assert_not_called()

    def test_skipped_transactions_excluded(self, server_con):
        _insert_queue_row(server_con, "tx_001", _payload_json(), received_at=_ago(86_400 * 2), skipped=True)
        bot = MagicMock()
        run_requester(bot)
        bot.send_card.assert_not_called()

    def test_falls_back_gracefully_when_quick_categories_fails(self, server_con, monkeypatch):
        _insert_queue_row(server_con, "tx_001", _payload_json(), received_at=_ago(86_400 * 2))
        monkeypatch.setattr("follow_up_tg.get_quick_categories", MagicMock(side_effect=RuntimeError("db down")))
        bot = MagicMock()
        run_requester(bot)
        bot.send_card.assert_called_once()
        _, kwargs = bot.send_card.call_args
        assert kwargs["quick_categories"] == []


class TestRunRequesterFollowUps:
    def test_sends_first_follow_up(self, server_con):
        _insert_queue_row(
            server_con, "tx_001", _payload_json(), received_at=_ago(86_400 * 3),
            status="pending", request_count=1, last_requested_at=_ago(3_700)
        )
        bot = MagicMock()
        run_requester(bot)
        bot.send_card.assert_called_once()
        _, kwargs = bot.send_card.call_args
        assert kwargs["follow_up"] == 1
        row = server_con.execute("SELECT request_count FROM webhook_queue WHERE id = ?", ["tx_001"]).fetchone()
        assert row[0] == 2

    def test_does_not_send_before_delay_elapsed(self, server_con):
        _insert_queue_row(
            server_con, "tx_001", _payload_json(), received_at=_ago(86_400 * 3),
            status="pending", request_count=1, last_requested_at=_ago(60)
        )
        bot = MagicMock()
        run_requester(bot)
        bot.send_card.assert_not_called()

    def test_auto_skips_after_final_stage(self, server_con):
        _insert_queue_row(
            server_con, "tx_001", _payload_json(), received_at=_ago(86_400 * 10),
            status="pending", request_count=4, last_requested_at=_ago(86_400 * 8)
        )
        bot = MagicMock()
        run_requester(bot)
        bot.send_card.assert_not_called()
        row = server_con.execute(
            "SELECT skipped, status, user_context FROM webhook_queue WHERE id = ?", ["tx_001"]
        ).fetchone()
        assert row[0] is True
        assert row[1] == "enriched"
        assert row[2] == "Auto-skipped"

    def test_no_pending_transactions_does_nothing(self, server_con):
        bot = MagicMock()
        run_requester(bot)
        bot.send_card.assert_not_called()


class TestRunCleanup:
    def test_deletes_old_processed_transactions(self, server_con):
        _insert_queue_row(
            server_con, "tx_001", _payload_json(), received_at=_ago(86_400 * 10),
            status="processed", enriched_at=_ago(86_400 * 6)
        )
        run_cleanup()
        row = server_con.execute("SELECT id FROM webhook_queue WHERE id = ?", ["tx_001"]).fetchone()
        assert row is None

    def test_keeps_recent_processed_transactions(self, server_con):
        _insert_queue_row(
            server_con, "tx_001", _payload_json(), received_at=_ago(86_400 * 2),
            status="processed", enriched_at=_ago(86_400 * 1)
        )
        run_cleanup()
        row = server_con.execute("SELECT id FROM webhook_queue WHERE id = ?", ["tx_001"]).fetchone()
        assert row is not None

    def test_keeps_pending_transactions_regardless_of_age(self, server_con):
        _insert_queue_row(
            server_con, "tx_001", _payload_json(), received_at=_ago(86_400 * 30), status="pending"
        )
        run_cleanup()
        row = server_con.execute("SELECT id FROM webhook_queue WHERE id = ?", ["tx_001"]).fetchone()
        assert row is not None
