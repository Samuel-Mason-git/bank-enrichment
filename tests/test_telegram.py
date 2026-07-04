from unittest.mock import MagicMock, patch

from telegram import TelegramBot


def _payload(txn_id="tx_001", merchant_name="Tesco", amount_pence=-500):
    return {
        "data": {
            "id": txn_id,
            "amount": amount_pence,
            "currency": "GBP",
            "description": "Weekly shop",
            "category": "groceries",
            "created": "2026-01-15T10:00:00Z",
            "merchant": {"name": merchant_name, "emoji": "🛒", "category": "groceries"},
        }
    }


class TestSendCardKeyboard:
    def test_no_quick_categories_only_enrich_and_skip(self):
        bot = TelegramBot()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        with patch("telegram.requests.post", return_value=mock_resp) as mock_post:
            bot.send_card(_payload())
        _, kwargs = mock_post.call_args
        keyboard = kwargs["json"]["reply_markup"]["inline_keyboard"]
        assert len(keyboard) == 1
        assert [b["text"] for b in keyboard[0]] == ["✏️ Enrich", "⏭ Skip"]

    def test_quick_categories_rendered_above_enrich_skip(self):
        bot = TelegramBot()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        quick_categories = [
            {"id": 1, "category": "Food & Drink", "subcategory": "Groceries"},
            {"id": 2, "category": "Food & Drink", "subcategory": "Snacks"},
        ]
        with patch("telegram.requests.post", return_value=mock_resp) as mock_post:
            bot.send_card(_payload(), quick_categories=quick_categories)
        _, kwargs = mock_post.call_args
        keyboard = kwargs["json"]["reply_markup"]["inline_keyboard"]
        assert keyboard[0] == [
            {"text": "Groceries", "callback_data": "quickcat:tx_001:1"},
            {"text": "Snacks", "callback_data": "quickcat:tx_001:2"},
        ]
        assert [b["text"] for b in keyboard[-1]] == ["✏️ Enrich", "⏭ Skip"]

    def test_quick_categories_wrap_after_three_per_row(self):
        bot = TelegramBot()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        quick_categories = [
            {"id": i, "category": "C", "subcategory": f"Sub{i}"} for i in range(1, 6)
        ]
        with patch("telegram.requests.post", return_value=mock_resp) as mock_post:
            bot.send_card(_payload(), quick_categories=quick_categories)
        _, kwargs = mock_post.call_args
        keyboard = kwargs["json"]["reply_markup"]["inline_keyboard"]
        assert len(keyboard[0]) == 3
        assert len(keyboard[1]) == 2
        assert [b["text"] for b in keyboard[2]] == ["✏️ Enrich", "⏭ Skip"]

    def test_callback_data_uses_transaction_id(self):
        bot = TelegramBot()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        quick_categories = [{"id": 7, "category": "X", "subcategory": "Y"}]
        with patch("telegram.requests.post", return_value=mock_resp) as mock_post:
            bot.send_card(_payload(txn_id="tx_999"), quick_categories=quick_categories)
        _, kwargs = mock_post.call_args
        keyboard = kwargs["json"]["reply_markup"]["inline_keyboard"]
        assert keyboard[0][0]["callback_data"] == "quickcat:tx_999:7"

    def test_empty_quick_categories_list_same_as_none(self):
        bot = TelegramBot()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        with patch("telegram.requests.post", return_value=mock_resp) as mock_post:
            bot.send_card(_payload(), quick_categories=[])
        _, kwargs = mock_post.call_args
        keyboard = kwargs["json"]["reply_markup"]["inline_keyboard"]
        assert len(keyboard) == 1


class TestSendMessageReplyMarkup:
    def test_no_reply_markup_by_default(self):
        bot = TelegramBot()
        mock_resp = MagicMock(status_code=200)
        with patch("telegram.requests.post", return_value=mock_resp) as mock_post:
            bot.send_message(123, "hello")
        _, kwargs = mock_post.call_args
        assert "reply_markup" not in kwargs["json"]

    def test_reply_markup_included_when_provided(self):
        bot = TelegramBot()
        mock_resp = MagicMock(status_code=200)
        markup = {"inline_keyboard": [[{"text": "✏️ Edit", "callback_data": "enrich:tx_001"}]]}
        with patch("telegram.requests.post", return_value=mock_resp) as mock_post:
            bot.send_message(123, "saved", reply_markup=markup)
        _, kwargs = mock_post.call_args
        assert kwargs["json"]["reply_markup"] == markup
