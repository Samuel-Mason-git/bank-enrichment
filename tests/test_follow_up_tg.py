import json
import time
from unittest.mock import MagicMock

from follow_up_tg import run_category_follow_ups, run_new_cards, run_requester, run_taxonomy_follow_ups
from tests.server_helpers import add_context, requests_sent, status, transaction


def _payload(txn_id="tx_001", merchant_name="Tesco", amount_pence=-500):
    return {"data": {
        "id": txn_id, "amount": amount_pence, "currency": "GBP", "description": "Weekly shop",
        "category": "groceries", "created": "2026-01-15T10:00:00Z",
        "merchant": {"name": merchant_name, "emoji": "🛒", "category": "groceries"},
    }}


def _direct_debit_payload(txn_id="tx_001", counterparty_name="Octopus Energy", amount_pence=-8000):
    """A direct debit: no merchant block at all, only a counterparty."""
    return {"data": {
        "id": txn_id, "amount": amount_pence, "currency": "GBP", "description": "89GJTS7",
        "category": "bills", "created": "2026-01-15T10:00:00Z",
        "counterparty": {"name": counterparty_name},
    }}


def _ago(seconds: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - seconds))


def _arrived(con, txn_id="tx_001", payload=None, received_at=None, requests=(), skipped=False):
    """A transaction that arrived at `received_at`, with a card or follow-up
    sent at each time in `requests`, and (if `skipped`) skipped."""
    con.execute("INSERT INTO transactions_raw (id, payload, received_at) VALUES (%s, %s, %s)",
                [txn_id, json.dumps(payload or _payload(txn_id)), received_at or _ago(60)])
    for at in requests:
        con.execute("INSERT INTO context_events (txn_id, source, set_at) VALUES (%s, 'request', %s)", [txn_id, at])
    if skipped:
        add_context(con, txn_id, "Skipped", skipped=True)


class TestRunRequesterMissedSend:
    def test_recovers_missed_initial_send(self, server_con):
        _arrived(server_con, received_at=_ago(86_400 * 2))
        bot = MagicMock()
        run_requester(bot)
        bot.send_card.assert_called_once()
        assert requests_sent(server_con, "tx_001") == 1

    def test_ignores_recent_pending_transactions(self, server_con):
        _arrived(server_con, received_at=_ago(60))
        bot = MagicMock()
        run_requester(bot)
        bot.send_card.assert_not_called()

    def test_answered_or_skipped_transactions_are_excluded(self, server_con):
        _arrived(server_con, received_at=_ago(86_400 * 2), skipped=True)
        bot = MagicMock()
        run_requester(bot)
        bot.send_card.assert_not_called()

    def test_falls_back_gracefully_when_quick_categories_fails(self, server_con, monkeypatch):
        _arrived(server_con, received_at=_ago(86_400 * 2))
        monkeypatch.setattr("follow_up_tg.get_quick_categories", MagicMock(side_effect=RuntimeError("db down")))
        bot = MagicMock()
        run_requester(bot)
        bot.send_card.assert_called_once()
        assert bot.send_card.call_args.kwargs["quick_categories"] == []

    def test_missed_send_looks_up_quick_categories_by_counterparty_when_merchant_is_absent(self, server_con, monkeypatch):
        """A direct debit/bank transfer never populates merchant, only
        counterparty -- without the fallback, recovering a missed initial send
        for a recurring bill always used the generic top-5 instead of that
        payee's own quick-tap history."""
        _arrived(server_con, payload=_direct_debit_payload(), received_at=_ago(86_400 * 2))
        mock_get = MagicMock(return_value=[])
        monkeypatch.setattr("follow_up_tg.get_quick_categories", mock_get)
        run_requester(MagicMock())
        mock_get.assert_called_once_with("Octopus Energy")


class TestRunRequesterFollowUps:
    def test_sends_first_follow_up(self, server_con):
        _arrived(server_con, received_at=_ago(86_400 * 3), requests=[_ago(3_700)])
        bot = MagicMock()
        run_requester(bot)
        bot.send_card.assert_called_once()
        assert bot.send_card.call_args.kwargs["follow_up"] == 1
        assert requests_sent(server_con, "tx_001") == 2

    def test_follow_up_looks_up_quick_categories_by_counterparty_when_merchant_is_absent(self, server_con, monkeypatch):
        _arrived(server_con, payload=_direct_debit_payload(), received_at=_ago(86_400 * 3), requests=[_ago(3_700)])
        mock_get = MagicMock(return_value=[])
        monkeypatch.setattr("follow_up_tg.get_quick_categories", mock_get)
        run_requester(MagicMock())
        mock_get.assert_called_once_with("Octopus Energy")

    def test_does_not_send_before_delay_elapsed(self, server_con):
        _arrived(server_con, received_at=_ago(86_400 * 3), requests=[_ago(60)])
        bot = MagicMock()
        run_requester(bot)
        bot.send_card.assert_not_called()

    def test_the_delay_counts_from_the_latest_request(self, server_con):
        _arrived(server_con, received_at=_ago(86_400 * 3), requests=[_ago(86_400 * 2), _ago(60)])
        bot = MagicMock()
        run_requester(bot)
        bot.send_card.assert_not_called()

    def test_auto_skips_after_final_stage(self, server_con):
        _arrived(server_con, received_at=_ago(86_400 * 10),
                 requests=[_ago(86_400 * 10), _ago(86_400 * 9.9), _ago(86_400 * 9), _ago(86_400 * 8)])
        bot = MagicMock()
        run_requester(bot)
        bot.send_card.assert_not_called()
        t = transaction(server_con, "tx_001")
        assert (t["skipped"], t["user_context"]) == (True, "Auto-skipped")
        assert status(server_con, "tx_001") == "skipped"

    def test_no_pending_transactions_does_nothing(self, server_con):
        bot = MagicMock()
        run_requester(bot)
        bot.send_card.assert_not_called()


class TestNewCards:
    """The pipeline adds proposals and suggestions; the server sends their
    cards, once, and only once each has actually gone."""

    def _taxonomy_proposal(self, con, pid=1, status="pending", sent_at=None):
        con.execute(
            """INSERT INTO taxonomy_proposals (id, parent_name, source_sub, proposed_sub, rationale,
                   evidence_ids, examples, status, run_key, proposed_at, sent_at)
               VALUES (%s, 'Food', 'Groceries', 'Bakery', 'bread', '["t1", "t2"]', '["GREGGS -£2"]',
                       %s, '2026-10', NOW(), %s)""",
            [pid, status, sent_at])

    def _category_proposal(self, con, pid=1, sent_at=None):
        con.execute(
            """INSERT INTO category_proposals (id, options, txn_ids, examples, proposed_at, sent_at)
               VALUES (%s, '[{"parent_name": "Food", "subcategory_name": "Bakery", "parent_is_new": false,
                              "rationale": "bread"}]', '["t1"]', '["GREGGS -£2"]', NOW(), %s)""",
            [pid, sent_at])

    def _suggestion(self, con, sid=1, sent_at=None):
        con.execute(
            """INSERT INTO rule_suggestions (id, match_field, match_value, category, subcategory, txn_count,
                   llm_reasoning, proposed_at, sent_at)
               VALUES (%s, 'merchant_name', 'EE', 'Bills', 'Phone', 6, 'always a phone bill', NOW(), %s)""",
            [sid, sent_at])

    def test_sends_each_new_card_with_its_content(self, server_con):
        self._taxonomy_proposal(server_con)
        self._category_proposal(server_con)
        self._suggestion(server_con)
        bot = MagicMock()
        run_new_cards(bot)
        tax = bot.send_taxonomy_proposal.call_args.args[1]
        assert (tax["local_id"], tax["evidence_count"], tax["examples"]) == (1, 2, ["GREGGS -£2"])
        bot.send_taxonomy_intro.assert_called_once()
        cat = bot.send_category_proposal.call_args.args[1]
        assert (cat["txn_count"], cat["examples"]) == (1, ["GREGGS -£2"])
        assert bot.send_rule_suggestion.call_args.args[1]["llm_reasoning"] == "always a phone bill"

    def test_each_is_sent_once(self, server_con):
        self._category_proposal(server_con)
        bot = MagicMock()
        run_new_cards(bot)
        run_new_cards(bot)
        assert bot.send_category_proposal.call_count == 1

    def test_already_sent_or_decided_ones_are_left_alone(self, server_con):
        self._taxonomy_proposal(server_con, pid=1, sent_at="2026-10-01")
        self._taxonomy_proposal(server_con, pid=2, status="expired")
        bot = MagicMock()
        run_new_cards(bot)
        bot.send_taxonomy_proposal.assert_not_called()

    def test_a_failed_send_is_tried_again(self, server_con):
        self._suggestion(server_con)
        bot = MagicMock()
        bot.send_rule_suggestion.side_effect = RuntimeError("Telegram down")
        try:
            run_new_cards(bot)
        except RuntimeError:
            pass
        bot.send_rule_suggestion.side_effect = None
        run_new_cards(bot)
        assert bot.send_rule_suggestion.call_count == 2
        assert server_con.execute("SELECT sent_at IS NOT NULL FROM rule_suggestions").fetchone()[0] is True


class TestTaxonomyFollowUps:
    def test_nudges_a_proposal_left_undecided(self, server_con):
        server_con.execute(
            """INSERT INTO taxonomy_proposals (id, parent_name, source_sub, proposed_sub, rationale,
                   evidence_ids, status, run_key, proposed_at, sent_at)
               VALUES (1, 'Food', 'Groceries', 'Bakery', 'bread', '["t1"]', 'pending', '2026-10', NOW(), %s)""",
            [_ago(86_400 * 4)])
        bot = MagicMock()
        run_taxonomy_follow_ups(bot)
        bot.send_taxonomy_proposal.assert_called_once()
        assert server_con.execute("SELECT follow_up_count FROM taxonomy_proposals").fetchone()[0] == 1
        run_taxonomy_follow_ups(bot)
        assert bot.send_taxonomy_proposal.call_count == 1  # the next nudge isn't due for a week


class TestCategoryCardFollowUps:
    """A new-category card holds its payments until answered, so an unanswered
    one is sent again a day later, once more three days after that, and then
    left to wait -- never applied, never asked about again."""

    def _card(self, con, sent_ago, follow_ups=0, nudged_ago=None, status="pending"):
        con.execute(
            """INSERT INTO category_proposals (id, options, txn_ids, examples, status, proposed_at, sent_at,
                   follow_up_count, last_nudged_at)
               VALUES (1, %s, '["tx_atm"]', '["ATM · −£20.00"]', %s, NOW(), %s, %s, %s)""",
            [json.dumps([{"parent_name": "Transport", "subcategory_name": "Road & Parking Permits",
                          "parent_is_new": False, "rationale": "A permit."}]),
             status, _ago(sent_ago), follow_ups, _ago(nudged_ago) if nudged_ago else None])

    def test_reminded_a_day_after_it_was_sent(self, server_con):
        self._card(server_con, sent_ago=86_400 + 60)
        bot = MagicMock()
        run_category_follow_ups(bot)
        card = bot.send_category_proposal.call_args.args[1]
        assert (card["local_id"], card["follow_up"], card["last_reminder"], card["txn_count"]) == (1, 1, False, 1)
        assert server_con.execute("SELECT follow_up_count FROM category_proposals").fetchone()[0] == 1

    def test_not_before_a_day(self, server_con):
        self._card(server_con, sent_ago=3_600)
        bot = MagicMock()
        run_category_follow_ups(bot)
        bot.send_category_proposal.assert_not_called()

    def test_the_second_is_the_last(self, server_con):
        self._card(server_con, sent_ago=86_400 * 5, follow_ups=1, nudged_ago=86_400 * 3 + 60)
        bot = MagicMock()
        run_category_follow_ups(bot)
        assert bot.send_category_proposal.call_args.args[1]["last_reminder"] is True
        run_category_follow_ups(bot)
        assert bot.send_category_proposal.call_count == 1, "two reminders, then it stops asking"

    def test_an_answered_card_isnt_chased(self, server_con):
        self._card(server_con, sent_ago=86_400 * 2, status="selected")
        bot = MagicMock()
        run_category_follow_ups(bot)
        bot.send_category_proposal.assert_not_called()


def test_a_reminder_card_says_so():
    from telegram import TelegramBot
    bot = TelegramBot.__new__(TelegramBot)
    sent = []
    bot._send_tracked = lambda payload: sent.append(payload["text"])
    card = {"local_id": 1, "txn_count": 1, "examples": ["ATM"], "options": [
        {"parent_name": "Transport", "subcategory_name": "Parking", "parent_is_new": False, "rationale": "x"}]}
    bot.send_category_proposal(1, {**card, "follow_up": 1, "last_reminder": False})
    bot.send_category_proposal(1, {**card, "follow_up": 2, "last_reminder": True})
    bot.send_category_proposal(1, card)
    assert sent[0].startswith("⏰ <i>Reminder") and sent[1].startswith("⏰ <i>Last reminder")
    assert not sent[2].startswith("⏰")
