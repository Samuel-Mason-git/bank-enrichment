"""What happens when something outside fails: Telegram, Monzo's API, the
database. A blip must be retried, and a card that didn't go out must never be
recorded as sent."""
import asyncio
import json
from datetime import date
from unittest.mock import MagicMock, patch

import pytest
import requests

import follow_up_tg
import main
import monzo_oauth
import telegram
import worker
from tests.server_helpers import add_transaction, monzo_payload, requests_sent


def _response(status, body=None):
    return MagicMock(status_code=status, text=str(body), json=lambda: body if body is not None else {})


OK = _response(200, {"ok": True, "result": {"message_id": 5}})


class TestTelegramRetries:
    def _bot(self):
        return telegram.TelegramBot()

    def test_a_connection_blip_is_retried(self, retry_waits):
        with patch("telegram.requests.post", side_effect=[requests.ConnectionError("down"), OK]) as post:
            assert self._bot()._post("sendMessage", {"text": "x"})["ok"] is True
        assert post.call_count == 2 and retry_waits == [telegram.RETRY_WAITS[0]]

    def test_telegrams_own_5xx_is_retried(self, retry_waits):
        with patch("telegram.requests.post", side_effect=[_response(502, {"ok": False}), OK]) as post:
            assert self._bot()._post("sendMessage", {})["ok"] is True
        assert post.call_count == 2

    def test_slow_down_waits_as_long_as_telegram_asks(self, retry_waits):
        busy = _response(429, {"ok": False, "parameters": {"retry_after": 7}})
        with patch("telegram.requests.post", side_effect=[busy, OK]):
            self._bot()._post("sendMessage", {})
        assert retry_waits == [7]

    def test_a_wait_too_long_to_be_worth_it_gives_up(self, retry_waits):
        busy = _response(429, {"ok": False, "parameters": {"retry_after": 600}})
        with patch("telegram.requests.post", return_value=busy) as post:
            self._bot()._post("sendMessage", {})
        assert post.call_count == 1 and retry_waits == []

    def test_a_rejected_message_is_not_retried(self, retry_waits):
        """A 400 means the message itself is wrong; sending it again won't help."""
        with patch("telegram.requests.post", return_value=_response(400, {"ok": False})) as post:
            self._bot()._post("sendMessage", {})
        assert post.call_count == 1

    def test_it_gives_up_after_three_tries(self, retry_waits):
        with patch("telegram.requests.post", side_effect=requests.Timeout("slow")) as post:
            assert self._bot()._post("sendMessage", {}) is None
        assert post.call_count == 3 and retry_waits == list(telegram.RETRY_WAITS)

    def test_a_card_that_never_goes_out_raises(self, retry_waits):
        with patch("telegram.requests.post", side_effect=requests.ConnectionError("down")):
            with pytest.raises(telegram.TelegramSendError):
                self._bot().send_rule_suggestion(1, {"id": 1, "match_field": "merchant_name", "match_value": "EE",
                                                     "category": "Bills", "subcategory": "Phone", "txn_count": 5,
                                                     "auto_context": "Phone"})

    def test_a_plain_reply_never_raises(self, retry_waits):
        with patch("telegram.requests.post", side_effect=requests.ConnectionError("down")):
            self._bot().send_message(1, "hello")

    def test_a_button_acknowledgement_is_never_retried(self, retry_waits):
        """It only clears the spinner, and has to be quick."""
        with patch("telegram.requests.post", side_effect=requests.ConnectionError("down")) as post:
            self._bot().ack_callback("cb")
        assert post.call_count == 1 and retry_waits == []


def _failing_bot():
    """The real bot, with Telegram unreachable."""
    patcher = patch("telegram.requests.post", side_effect=requests.ConnectionError("down"))
    patcher.start()
    return telegram.TelegramBot(), patcher


class TestACardThatDidntGoOutIsSentAgain:
    def test_a_proposal_card_stays_unsent_and_goes_on_the_next_round(self, server_con, retry_waits):
        server_con.execute(
            """INSERT INTO category_proposals (id, options, txn_ids, proposed_at)
               VALUES (1, '[{"parent_name": "Tax", "subcategory_name": "SA", "parent_is_new": true,
                            "rationale": "x"}]', '["t1"]', NOW())""")
        bot, patcher = _failing_bot()
        try:
            with pytest.raises(telegram.TelegramSendError):
                follow_up_tg.run_new_cards(bot)
        finally:
            patcher.stop()
        assert server_con.execute("SELECT sent_at FROM category_proposals").fetchone()[0] is None

        with patch("telegram.requests.post", return_value=OK):
            follow_up_tg.run_new_cards(telegram.TelegramBot())
        assert server_con.execute("SELECT sent_at FROM category_proposals").fetchone()[0] is not None

    def test_a_first_card_that_failed_is_not_recorded_as_sent(self, server_con, retry_waits):
        """So the follow-up loop sees it as never sent, and sends it."""
        request = MagicMock(query_params={"token": "t"})
        body = json.dumps(monzo_payload("tx_2")).encode()

        async def _body():
            return body
        request.body = _body
        bot, patcher = _failing_bot()
        try:
            with patch.object(main, "bot", bot), patch.object(main, "MONZO_WEBHOOK_SECRET", "t"), \
                 patch.object(main, "get_con", return_value=server_con), \
                 patch.object(main, "get_quick_categories", return_value=[]), \
                 patch.object(main, "fetch_transaction", return_value=None):
                asyncio.run(main.recieve_monzo(request))
        finally:
            patcher.stop()
        assert requests_sent(server_con, "tx_2") == 0
        assert server_con.execute("SELECT COUNT(*) FROM needs_context WHERE id = 'tx_2'").fetchone()[0] == 1

    def test_a_missed_first_card_goes_out_after_ten_minutes_not_a_day(self, server_con):
        add_transaction(server_con, "tx_1", received_at="2000-01-01 00:00:00")
        bot = MagicMock()
        with patch.object(follow_up_tg, "_cutoff", side_effect=lambda seconds: f"cutoff-{seconds}") as cutoff:
            try:
                follow_up_tg.run_requester(bot)
            except Exception:
                pass
        assert cutoff.call_args_list[0].args == (follow_up_tg.MISSED_CARD_AFTER,)
        assert follow_up_tg.MISSED_CARD_AFTER == 600


class TestMonzoLookupRetries:
    @pytest.fixture(autouse=True)
    def _tokens(self, monkeypatch):
        monkeypatch.setattr(monzo_oauth, "_load_tokens", lambda: {"access_token": "a"})

    def test_a_blip_is_tried_once_more(self, retry_waits):
        good = _response(200, {"transaction": {"id": "tx_1", "merchant": {"name": "Tesco"}}})
        with patch("monzo_oauth.requests.get", side_effect=[requests.ConnectionError("x"), good]) as get:
            assert monzo_oauth.fetch_transaction("tx_1")["merchant"]["name"] == "Tesco"
        assert get.call_count == 2 and retry_waits == [1]

    def test_monzos_5xx_is_tried_once_more(self, retry_waits):
        with patch("monzo_oauth.requests.get", return_value=_response(503)) as get:
            assert monzo_oauth.fetch_transaction("tx_1") is None
        assert get.call_count == 2

    def test_a_refusal_is_not_retried(self, retry_waits):
        """A 401/403/404 won't change in a second."""
        with patch("monzo_oauth.requests.get", return_value=_response(403)) as get:
            assert monzo_oauth.fetch_transaction("tx_1") is None
        assert get.call_count == 1 and retry_waits == []

    def test_no_authorization_means_no_call(self, monkeypatch):
        monkeypatch.setattr(monzo_oauth, "_load_tokens", lambda: None)
        with patch("monzo_oauth.requests.get") as get:
            assert monzo_oauth.fetch_transaction("tx_1") is None
        get.assert_not_called()


class TestHealth:
    def test_up_with_the_database(self, server_con):
        with patch.object(main, "get_con", return_value=server_con):
            assert main.health() == {"ok": True}

    def test_down_without_it(self):
        broken = MagicMock()
        broken.execute.side_effect = RuntimeError("no route")
        with patch.object(main, "get_con", return_value=broken):
            with pytest.raises(main.HTTPException) as e:
                main.health()
        assert e.value.status_code == 503

    def test_needs_no_login(self):
        from fastapi.testclient import TestClient
        with patch.object(main, "get_con", return_value=MagicMock()):
            response = TestClient(main.app).get("/health")
        assert response.status_code == 200 and response.json() == {"ok": True}


class TestWeeklyCheckIn:
    def test_sent_on_sundays_after_a_good_run(self, db, alert_post):
        worker._after_run(0, date(2026, 11, 1))  # a Sunday
        text = alert_post.call_args.kwargs["json"]["text"]
        assert "bank-enrichment is fine" in text and "waiting for your answer" in text

    def test_not_on_other_days(self, db, alert_post):
        worker._after_run(0, date(2026, 11, 2))
        alert_post.assert_not_called()

    def test_not_after_a_run_that_failed(self, db, alert_post):
        worker._after_run(1, date(2026, 11, 1))
        alert_post.assert_not_called()

    def test_a_failing_check_in_never_breaks_the_worker(self, alert_post, monkeypatch):
        monkeypatch.setattr(worker, "weekly_check_in", lambda: (_ for _ in ()).throw(RuntimeError("x")))
        worker._after_run(0, date(2026, 11, 1))
