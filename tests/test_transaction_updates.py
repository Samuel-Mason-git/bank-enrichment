"""Every webhook Monzo sends for a payment is kept, not just the first: the
first in transactions_raw exactly as it arrived, every later one that says
something new in transaction_updates. The transaction as it stands now
(transaction_current, and the transactions view on top of it) is built from
all of them."""
import asyncio
import json
from unittest.mock import MagicMock, patch

import follow_up_tg
import main
from tests.server_helpers import add_context, add_transaction, monzo_payload, transaction


def _webhook(con, payload):
    request = MagicMock(query_params={"token": "t"})

    async def _body():
        return json.dumps(payload).encode()
    request.body = _body
    with patch.object(main, "MONZO_WEBHOOK_SECRET", "t"), patch.object(main, "get_con", return_value=con), \
         patch.object(main, "bot", MagicMock()), patch.object(main, "get_quick_categories", return_value=[]), \
         patch.object(main, "fetch_transaction", return_value=None):
        return asyncio.run(main.recieve_monzo(request))


def _updates(con, txn_id="tx_1"):
    return con.execute("SELECT source, payload FROM transaction_updates WHERE txn_id = %s ORDER BY update_id",
                       [txn_id]).fetchall()


class TestLaterWebhooksAreKept:
    def test_a_settlement_is_kept_and_shows(self, server_con):
        _webhook(server_con, monzo_payload("tx_1"))
        settled = monzo_payload("tx_1", settled="2026-10-02T03:00:00.000Z")
        settled["type"] = "transaction.updated"
        _webhook(server_con, settled)
        assert [s for s, _ in _updates(server_con)] == ["webhook"]
        assert str(transaction(server_con)["settled_at"]) == "2026-10-02 03:00:00"

    def test_the_first_version_is_never_changed(self, server_con):
        first = monzo_payload("tx_1")
        _webhook(server_con, first)
        _webhook(server_con, monzo_payload("tx_1", amount=-500, settled="2026-10-02T03:00:00Z"))
        assert server_con.execute("SELECT payload FROM transactions_raw").fetchone()[0] == first

    def test_a_repeat_of_any_version_is_not_kept_again(self, server_con):
        _webhook(server_con, monzo_payload("tx_1"))
        later = monzo_payload("tx_1", settled="2026-10-02T03:00:00Z")
        _webhook(server_con, later)
        _webhook(server_con, later)
        _webhook(server_con, monzo_payload("tx_1"))
        assert len(_updates(server_con)) == 1

    def test_only_the_first_asks_for_context(self, server_con):
        _webhook(server_con, monzo_payload("tx_1"))
        _webhook(server_con, monzo_payload("tx_1", settled="2026-10-02T03:00:00Z"))
        assert server_con.execute(
            "SELECT COUNT(*) FROM context_events WHERE source = 'request'").fetchone()[0] == 1


class TestTheTransactionAsItStandsNow:
    def test_the_newest_version_wins(self, server_con):
        add_transaction(server_con, "tx_1", amount=-450)
        server_con.execute("INSERT INTO transaction_updates (txn_id, payload, source) VALUES (%s, %s, 'webhook')",
                           ["tx_1", json.dumps(monzo_payload("tx_1", amount=-475))])
        assert float(transaction(server_con)["amount"]) == -4.75

    def test_a_later_version_without_a_merchant_doesnt_lose_it(self, server_con):
        """The October race: a later webhook carrying merchant: null."""
        add_transaction(server_con, "tx_1", merchant="Tesco")
        server_con.execute("INSERT INTO transaction_updates (txn_id, payload, source) VALUES (%s, %s, 'webhook')",
                           ["tx_1", json.dumps(monzo_payload("tx_1", merchant=None, amount=-460))])
        row = transaction(server_con)
        assert row["merchant_name"] == "Tesco" and float(row["amount"]) == -4.6

    def test_a_merchant_filled_in_later_shows(self, server_con):
        add_transaction(server_con, "tx_1", merchant=None)
        server_con.execute("INSERT INTO transaction_updates (txn_id, payload, source) VALUES (%s, %s, 'api')",
                           ["tx_1", json.dumps(monzo_payload("tx_1", merchant="Sainsbury's"))])
        assert transaction(server_con)["merchant_name"] == "Sainsbury's"

    def test_a_later_unsettled_version_doesnt_unsettle_it(self, server_con):
        add_transaction(server_con, "tx_1", settled="2026-10-02T03:00:00Z")
        server_con.execute("INSERT INTO transaction_updates (txn_id, payload, source) VALUES (%s, %s, 'webhook')",
                           ["tx_1", json.dumps(monzo_payload("tx_1", settled=""))])
        assert transaction(server_con)["settled_at"] is not None

    def test_with_no_updates_it_is_the_first_version(self, server_con):
        first = add_transaction(server_con, "tx_1")
        assert transaction(server_con)["raw_payload"]["data"]["merchant"] == first["data"]["merchant"]

    def test_a_reminder_card_shows_the_merchant_found_since(self, server_con):
        add_transaction(server_con, "tx_1", merchant=None, received_at="2000-01-01 00:00:00")
        server_con.execute("INSERT INTO transaction_updates (txn_id, payload, source) VALUES (%s, %s, 'api')",
                           ["tx_1", json.dumps(monzo_payload("tx_1", merchant="Sainsbury's"))])
        bot = MagicMock()
        follow_up_tg.run_requester(bot)
        card = bot.send_card.call_args.args[0]
        assert card["data"]["merchant"]["name"] == "Sainsbury's"


class TestMerchantRepair:
    def _card_payment(self, con, txn_id="tx_1", received_at=None, merchant=None, scheme="mastercard"):
        from datetime import datetime, timedelta
        when = received_at or (datetime.now() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
        add_transaction(con, txn_id, merchant=merchant, received_at=when, scheme=scheme)
        add_context(con, txn_id, "something")

    def _repair(self, con, fetched):
        follow_up_tg._last_repair = 0.0
        with patch.object(follow_up_tg, "fetch_transaction", return_value=fetched) as fetch:
            follow_up_tg.run_merchant_repair(MagicMock())
        return fetch

    def test_a_card_payment_without_a_merchant_is_looked_up(self, server_con):
        self._card_payment(server_con)
        found = monzo_payload("tx_1", merchant="Sainsbury's")["data"]
        fetch = self._repair(server_con, found)
        fetch.assert_called_once_with("tx_1")
        assert transaction(server_con)["merchant_name"] == "Sainsbury's"

    def test_once_monzo_has_answered_it_isnt_asked_again(self, server_con):
        self._card_payment(server_con)
        self._repair(server_con, monzo_payload("tx_1", merchant=None)["data"])
        fetch = self._repair(server_con, None)
        fetch.assert_not_called()

    def test_if_monzo_cant_be_asked_it_tries_again_later(self, server_con):
        self._card_payment(server_con)
        self._repair(server_con, None)
        fetch = self._repair(server_con, None)
        fetch.assert_called_once()

    def test_not_more_than_hourly(self, server_con):
        self._card_payment(server_con)
        follow_up_tg._last_repair = 0.0
        with patch.object(follow_up_tg, "fetch_transaction", return_value=None) as fetch:
            follow_up_tg.run_merchant_repair(MagicMock())
            follow_up_tg.run_merchant_repair(MagicMock())
        assert fetch.call_count == 1

    def test_only_card_payments(self, server_con):
        self._card_payment(server_con, scheme="uk_retail_pot")
        self._repair(server_con, None).assert_not_called()

    def test_not_ones_that_have_a_merchant(self, server_con):
        self._card_payment(server_con, merchant="Tesco")
        self._repair(server_con, None).assert_not_called()

    def test_not_after_a_week(self, server_con):
        self._card_payment(server_con, received_at="2000-01-01 00:00:00")
        self._repair(server_con, None).assert_not_called()
