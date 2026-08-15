"""A declined payment moves no money, but Monzo fires a webhook for it exactly
like a real transaction -- and the retry that succeeds arrives seconds later as
a separate one. Real case: -£5.98 declined for INSUFFICIENT_FUNDS at 19:52:11,
the same -£5.98 succeeding at 19:53:00.
"""
import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

import main


def _run(coro):
    return asyncio.run(coro)


def _webhook(txn_id="tx_declined", amount=-598, decline_reason=None, merchant="Abr Station Express Lt"):
    payload = {
        "type": "transaction.created",
        "data": {
            "account_id": "acc_1", "amount": amount, "created": "2026-08-15T19:52:11.000Z",
            "currency": "GBP", "description": "ABR Station Express Lt Milton Keynes GBR",
            "id": txn_id, "category": "eating_out", "is_load": False,
            "merchant": {"name": merchant} if merchant else None,
        },
    }
    if decline_reason is not None:
        payload["data"]["decline_reason"] = decline_reason
    req = MagicMock()

    async def _body():
        return json.dumps(payload).encode()

    req.body = _body
    return req


def _post(server_con, req, bot=None):
    with patch.object(main, "get_con", return_value=server_con), \
         patch.object(main, "bot", bot or MagicMock()), \
         patch.object(main, "check_rules", return_value=(None, False)), \
         patch.object(main, "get_quick_categories", return_value=[]):
        return _run(main.recieve_monzo(req))


class TestDeclinedTransactionsAreNotQueued:
    def test_a_declined_payment_is_auto_skipped(self, server_con):
        _post(server_con, _webhook(decline_reason="INSUFFICIENT_FUNDS"))
        row = server_con.execute(
            "SELECT status, skipped, user_context FROM webhook_queue WHERE id = 'tx_declined'"
        ).fetchone()
        assert row[0] == "enriched"
        assert row[1] is True
        assert row[2] == "Declined (INSUFFICIENT_FUNDS)"

    def test_no_telegram_card_is_sent_for_a_decline(self, server_con):
        """The whole point -- being asked "what was this?" about a payment that
        never happened, seconds before being asked about the one that did."""
        bot = MagicMock()
        _post(server_con, _webhook(decline_reason="INSUFFICIENT_FUNDS"), bot)
        bot.send_card.assert_not_called()

    def test_it_is_still_recorded_rather_than_dropped(self, server_con):
        """The attempt is a true record, and keeping the row keeps its id in the
        dedup buffer."""
        _post(server_con, _webhook(decline_reason="INSUFFICIENT_FUNDS"))
        assert server_con.execute(
            "SELECT COUNT(*) FROM webhook_queue WHERE id = 'tx_declined'").fetchone()[0] == 1

    def test_rules_are_not_run_for_a_decline(self, server_con):
        """A rule matching the merchant would otherwise auto-enrich a payment
        that never happened, giving it a real context sentence."""
        with patch.object(main, "get_con", return_value=server_con), \
             patch.object(main, "bot", MagicMock()), \
             patch.object(main, "check_rules") as rules, \
             patch.object(main, "get_quick_categories", return_value=[]):
            _run(main.recieve_monzo(_webhook(decline_reason="INSUFFICIENT_FUNDS")))
        rules.assert_not_called()

    def test_any_decline_reason_is_handled_not_just_insufficient_funds(self, server_con):
        _post(server_con, _webhook(decline_reason="CARD_BLOCKED"))
        assert server_con.execute(
            "SELECT user_context FROM webhook_queue WHERE id = 'tx_declined'"
        ).fetchone()[0] == "Declined (CARD_BLOCKED)"


class TestNormalTransactionsAreUnaffected:
    def test_a_successful_payment_still_queues_and_notifies(self, server_con):
        bot = MagicMock()
        _post(server_con, _webhook(txn_id="tx_ok"), bot)
        row = server_con.execute(
            "SELECT status, skipped FROM webhook_queue WHERE id = 'tx_ok'").fetchone()
        assert row[0] == "pending"
        assert row[1] is False
        bot.send_card.assert_called_once()

    def test_the_retry_after_a_decline_is_treated_normally(self, server_con):
        """The successful retry is a different transaction id arriving seconds
        later, and must behave exactly as any other payment."""
        bot = MagicMock()
        _post(server_con, _webhook(txn_id="tx_declined", decline_reason="INSUFFICIENT_FUNDS"), bot)
        _post(server_con, _webhook(txn_id="tx_retry"), bot)
        rows = dict(server_con.execute(
            "SELECT id, status FROM webhook_queue").fetchall())
        assert rows == {"tx_declined": "enriched", "tx_retry": "pending"}
        assert bot.send_card.call_count == 1

    def test_a_pot_transfer_is_not_mistaken_for_a_decline(self, server_con):
        """Monzo also sets include_in_spending false for pot transfers, so that
        flag is too broad to use here -- only decline_reason is decisive."""
        bot = MagicMock()
        _post(server_con, _webhook(txn_id="tx_pot", amount=10000, merchant=None), bot)
        assert server_con.execute(
            "SELECT status FROM webhook_queue WHERE id = 'tx_pot'").fetchone()[0] == "pending"
