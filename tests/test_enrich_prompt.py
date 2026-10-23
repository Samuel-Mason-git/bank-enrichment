"""Tapping Enrich on a Telegram card must tell the user which transaction
they're about to describe -- previously it only echoed the opaque
transaction id, e.g. "Enriching tx_0000BBxyz...", useless for recall."""
import asyncio
import json
from unittest.mock import MagicMock, patch

import main


def _run(coro):
    return asyncio.run(coro)


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


def _insert_txn(con, id="tx1", merchant="Tesco", amount_pence=-1234, description="Weekly shop"):
    payload = json.dumps({"data": {
        "id": id, "amount": amount_pence, "description": description,
        "merchant": {"name": merchant} if merchant else None, "counterparty": {},
    }})
    con.execute(
        "INSERT INTO webhook_queue (id, payload, received_at, status) VALUES (?, ?, NOW(), 'pending')",
        [id, payload],
    )


def _tap_enrich(server_con, transaction_id):
    bot = MagicMock()
    update, req = _callback(f"enrich:{transaction_id}")
    with patch.object(main, "get_con", return_value=server_con), \
         patch.object(main, "bot", bot), \
         patch.object(main.TelegramUpdate, "model_validate_json", return_value=update):
        _run(main.recieve_telegram(req))
    return bot


class TestEnrichPromptIdentifiesTheTransaction:
    def test_prompt_includes_merchant_and_amount(self, server_con):
        _insert_txn(server_con, id="tx1", merchant="Tesco", amount_pence=-1234)
        bot = _tap_enrich(server_con, "tx1")
        text = bot.send_message.call_args.args[1]
        assert "Tesco" in text
        assert "12.34" in text

    def test_falls_back_to_description_with_no_merchant_or_counterparty(self, server_con):
        _insert_txn(server_con, id="tx2", merchant=None, amount_pence=500, description="Bank transfer")
        bot = _tap_enrich(server_con, "tx2")
        text = bot.send_message.call_args.args[1]
        assert "Bank transfer" in text

    def test_unknown_transaction_id_falls_back_to_the_id_itself(self, server_con):
        bot = _tap_enrich(server_con, "tx_does_not_exist")
        text = bot.send_message.call_args.args[1]
        assert "tx_does_not_exist" in text
