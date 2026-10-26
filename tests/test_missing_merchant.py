"""Monzo fires several webhooks per payment within half a second, in no set
order, and the rules and the card are decided from the first. Since mid-October
2026 the first has usually been a transaction.updated with merchant: null -- so
a card payment arriving without a merchant is looked up in Monzo's API, which
has it, and the answer kept as an update (the first version stays as it was).

Real case: Sainsbury's on 25 Oct, five webhooks in 0.33s; the stored one said
merchant: null, the API said 'Sainsbury S Milton Key'."""
import asyncio
import json
from unittest.mock import MagicMock, patch

import main
import monzo_oauth

API_TRANSACTION = {
    "account_id": "acc_1", "amount": -944, "created": "2026-10-25T11:04:48.000Z",
    "currency": "GBP", "description": "SAINSBURY S MILTON KEY MILTON KEYNES GBR",
    "id": "tx_1", "category": "groceries", "is_load": False,
    "merchant": {"id": "merch_1", "name": "Sainsbury S Milton Key", "category": "groceries"},
    "counterparty": {},
}


def _webhook(merchant=None, counterparty=None, txn_id="tx_1"):
    data = dict(API_TRANSACTION, id=txn_id, merchant=merchant, counterparty=counterparty or {})
    req = MagicMock()

    async def _body():
        return json.dumps({"type": "transaction.updated", "data": data}).encode()

    req.body = _body
    return req


def _post(server_con, req, fetched=None, bot=None):
    """Post a webhook with Monzo's API answering `fetched`. Returns the mocks
    for the API lookup, the rule check and the quick-tap lookup."""
    fetch = MagicMock(return_value=fetched)
    rules = MagicMock(return_value=main.NO_MATCH)
    quick = MagicMock(return_value=[])
    with patch.object(main, "get_con", return_value=server_con), \
         patch.object(main, "bot", bot or MagicMock()), \
         patch.object(main, "check_rules", rules), \
         patch.object(main, "get_quick_categories", quick), \
         patch.object(main, "fetch_transaction", fetch):
        asyncio.run(main.recieve_monzo(req))
    return fetch, rules, quick


def _stored_merchant(server_con, txn_id="tx_1"):
    """The merchant as the transaction stands now."""
    payload = server_con.execute(
        "SELECT payload FROM transaction_current WHERE id = %s", [txn_id]).fetchone()[0]
    return payload["data"]["merchant"]


class TestACardPaymentWithoutAMerchant:
    def test_is_looked_up_and_stored_with_its_merchant(self, server_con):
        fetch, _, _ = _post(server_con, _webhook(), fetched=API_TRANSACTION)
        fetch.assert_called_once_with("tx_1")
        assert _stored_merchant(server_con)["name"] == "Sainsbury S Milton Key"

    def test_the_first_version_stays_exactly_as_it_arrived(self, server_con):
        _post(server_con, _webhook(), fetched=API_TRANSACTION)
        first = server_con.execute("SELECT payload FROM transactions_raw").fetchone()[0]
        assert first["data"]["merchant"] is None
        source, update = server_con.execute("SELECT source, payload FROM transaction_updates").fetchone()
        assert source == "api" and update["data"]["merchant"]["name"] == "Sainsbury S Milton Key"

    def test_the_update_keeps_monzos_own_shape(self, server_con):
        _post(server_con, _webhook(), fetched=API_TRANSACTION)
        payload = server_con.execute("SELECT payload FROM transaction_updates").fetchone()[0]
        assert payload["type"] == "transaction.updated"
        assert payload["data"]["id"] == "tx_1"

    def test_rules_and_the_card_see_the_merchant(self, server_con):
        _, rules, quick = _post(server_con, _webhook(), fetched=API_TRANSACTION)
        assert rules.call_args[0][0].merchant["name"] == "Sainsbury S Milton Key"
        quick.assert_called_once_with("Sainsbury S Milton Key")

    def test_if_the_api_cant_be_reached_it_carries_on_as_it_arrived(self, server_con):
        bot = MagicMock()
        _, _, quick = _post(server_con, _webhook(), fetched=None, bot=bot)
        assert _stored_merchant(server_con) is None
        quick.assert_called_once_with(None)
        bot.send_card.assert_called_once()

    def test_an_api_answer_with_no_merchant_either_changes_nothing(self, server_con):
        _post(server_con, _webhook(), fetched=dict(API_TRANSACTION, merchant=None))
        assert _stored_merchant(server_con) is None
        assert server_con.execute("SELECT COUNT(*) FROM transaction_updates").fetchone()[0] == 0

    def test_an_unusable_api_answer_keeps_the_webhooks_copy(self, server_con):
        broken = {"merchant": {"name": "X"}}  # missing every required field
        bot = MagicMock()
        _post(server_con, _webhook(), fetched=broken, bot=bot)
        assert _stored_merchant(server_con) is None
        bot.send_card.assert_called_once()


class TestNoLookupWhenThereIsNothingToFind:
    def test_a_webhook_that_has_its_merchant(self, server_con):
        fetch, _, _ = _post(server_con, _webhook(merchant=API_TRANSACTION["merchant"]))
        fetch.assert_not_called()

    def test_a_transfer_which_has_a_counterparty_instead(self, server_con):
        fetch, _, _ = _post(server_con, _webhook(counterparty={"name": "Mum"}))
        fetch.assert_not_called()

    def test_the_duplicate_webhooks_that_follow(self, server_con):
        _post(server_con, _webhook(), fetched=API_TRANSACTION)
        fetch, _, _ = _post(server_con, _webhook(), fetched=API_TRANSACTION)
        fetch.assert_not_called()
        assert server_con.execute("SELECT COUNT(*) FROM transaction_updates").fetchone()[0] == 1, \
            "a repeat of the first webhook says nothing new"


class TestFetchTransaction:
    def test_no_tokens_yet_means_no_call(self):
        with patch.object(monzo_oauth, "_load_tokens", return_value=None), \
             patch.object(monzo_oauth.requests, "get") as get:
            assert monzo_oauth.fetch_transaction("tx_1") is None
        get.assert_not_called()

    def test_asks_for_the_merchant_to_be_expanded(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {"transaction": API_TRANSACTION}
        with patch.object(monzo_oauth, "_load_tokens", return_value={"access_token": "t"}), \
             patch.object(monzo_oauth.requests, "get", return_value=response) as get:
            assert monzo_oauth.fetch_transaction("tx_1") == API_TRANSACTION
        assert get.call_args.kwargs["params"] == {"expand[]": "merchant"}
        assert get.call_args.kwargs["timeout"] <= 5

    def test_an_error_answer_is_none(self):
        with patch.object(monzo_oauth, "_load_tokens", return_value={"access_token": "t"}), \
             patch.object(monzo_oauth.requests, "get", return_value=MagicMock(status_code=401)):
            assert monzo_oauth.fetch_transaction("tx_1") is None

    def test_a_network_failure_is_none_not_an_exception(self):
        with patch.object(monzo_oauth, "_load_tokens", return_value={"access_token": "t"}), \
             patch.object(monzo_oauth.requests, "get", side_effect=OSError("down")):
            assert monzo_oauth.fetch_transaction("tx_1") is None
