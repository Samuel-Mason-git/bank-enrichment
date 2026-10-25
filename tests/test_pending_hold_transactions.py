"""A card payment's amount isn't always final. The classic case: a UK fuel
pump places a flat ~£99/£100 authorisation hold before it knows how much fuel
was actually taken, then the real amount is billed as its OWN separate
transaction (its own id) once the pump finishes -- not a correction to this
one. Monzo marks the hold with amount_is_pending=true.

Real case: a £99.00 hold on 2026-10-18 10:26, sent to Telegram as if it were a
genuine charge, while the real £27.15 fuel purchase had already arrived as its
own transaction twenty minutes earlier.
"""
import asyncio
import json
from unittest.mock import MagicMock, patch

import main
from tests.server_helpers import status, stored, transaction


def _run(coro):
    return asyncio.run(coro)


def _webhook(txn_id="tx_hold", amount=-9900, amount_is_pending=None, merchant=None):
    payload = {
        "type": "transaction.created",
        "data": {
            "account_id": "acc_1", "amount": amount, "created": "2026-10-18T10:26:27.000Z",
            "currency": "GBP", "description": "SHELL A41 WATFORD GBR",
            "id": txn_id, "category": "transport", "is_load": False,
            "merchant": {"name": merchant} if merchant else None,
        },
    }
    if amount_is_pending is not None:
        payload["data"]["amount_is_pending"] = amount_is_pending
    req = MagicMock()

    async def _body():
        return json.dumps(payload).encode()

    req.body = _body
    return req


def _post(server_con, req, bot=None):
    with patch.object(main, "get_con", return_value=server_con), \
         patch.object(main, "bot", bot or MagicMock()), \
         patch.object(main, "check_rules", return_value=main.NO_MATCH), \
         patch.object(main, "get_quick_categories", return_value=[]):
        return _run(main.recieve_monzo(req))


class TestPendingHoldsAreSkipped:
    def test_a_pending_hold_is_auto_skipped(self, server_con):
        _post(server_con, _webhook(amount_is_pending=True))
        t = transaction(server_con, "tx_hold")
        assert status(server_con, "tx_hold") == "skipped"
        assert t["skipped"] is True
        assert t["user_context"] == "Pending authorisation hold (amount not final)"

    def test_no_telegram_card_is_sent_for_a_pending_hold(self, server_con):
        """The whole point -- a false alarm for ~£99 that was never the real
        charge, with the actual amount arriving as a wholly separate transaction."""
        bot = MagicMock()
        _post(server_con, _webhook(amount_is_pending=True), bot)
        bot.send_card.assert_not_called()

    def test_it_is_still_recorded_rather_than_dropped(self, server_con):
        """A true record of the attempt, and it keeps the id, so the duplicate
        webhooks that follow are recognised as duplicates."""
        _post(server_con, _webhook(amount_is_pending=True))
        assert server_con.execute(
            "SELECT COUNT(*) FROM transactions_raw WHERE id = 'tx_hold'").fetchone()[0] == 1

    def test_rules_are_not_run_for_a_pending_hold(self, server_con):
        """A rule matching the merchant would otherwise auto-enrich a hold
        amount that was never going to be the real charge."""
        with patch.object(main, "get_con", return_value=server_con), \
             patch.object(main, "bot", MagicMock()), \
             patch.object(main, "check_rules") as rules, \
             patch.object(main, "get_quick_categories", return_value=[]):
            _run(main.recieve_monzo(_webhook(amount_is_pending=True)))
        rules.assert_not_called()


class TestNormalTransactionsAreUnaffected:
    def test_amount_is_pending_false_is_treated_normally(self, server_con):
        bot = MagicMock()
        _post(server_con, _webhook(txn_id="tx_ok", amount_is_pending=False), bot)
        assert status(server_con, "tx_ok") == "pending"
        bot.send_card.assert_called_once()

    def test_amount_is_pending_absent_is_treated_normally(self, server_con):
        """Most transactions simply never carry this field at all."""
        bot = MagicMock()
        _post(server_con, _webhook(txn_id="tx_ok"), bot)
        assert status(server_con, "tx_ok") == "pending"
        bot.send_card.assert_called_once()

    def test_the_real_settled_charge_is_a_separate_transaction_and_is_unaffected(self, server_con):
        """The actual sequence observed: the real charge arrives as its own id,
        and is not held back by the pending hold that follows it."""
        bot = MagicMock()
        _post(server_con, _webhook(txn_id="tx_real", amount=-2715), bot)
        _post(server_con, _webhook(txn_id="tx_hold", amount=-9900, amount_is_pending=True), bot)
        assert stored(server_con) == {"tx_real": "pending", "tx_hold": "skipped"}
        assert bot.send_card.call_count == 1
