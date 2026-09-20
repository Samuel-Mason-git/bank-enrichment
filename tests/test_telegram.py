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
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"ok": True}
        with patch("telegram.requests.post", return_value=mock_resp) as mock_post:
            bot.send_card(_payload())
        _, kwargs = mock_post.call_args
        keyboard = kwargs["json"]["reply_markup"]["inline_keyboard"]
        assert len(keyboard) == 1
        assert [b["text"] for b in keyboard[0]] == ["✏️ Enrich", "⏭ Skip"]

    def test_quick_categories_rendered_above_enrich_skip(self):
        bot = TelegramBot()
        mock_resp = MagicMock(status_code=200)
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
        mock_resp = MagicMock(status_code=200)
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
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"ok": True}
        quick_categories = [{"id": 7, "category": "X", "subcategory": "Y"}]
        with patch("telegram.requests.post", return_value=mock_resp) as mock_post:
            bot.send_card(_payload(txn_id="tx_999"), quick_categories=quick_categories)
        _, kwargs = mock_post.call_args
        keyboard = kwargs["json"]["reply_markup"]["inline_keyboard"]
        assert keyboard[0][0]["callback_data"] == "quickcat:tx_999:7"

    def test_empty_quick_categories_list_same_as_none(self):
        bot = TelegramBot()
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"ok": True}
        with patch("telegram.requests.post", return_value=mock_resp) as mock_post:
            bot.send_card(_payload(), quick_categories=[])
        _, kwargs = mock_post.call_args
        keyboard = kwargs["json"]["reply_markup"]["inline_keyboard"]
        assert len(keyboard) == 1


class TestSendCardFollowUpText:
    def test_full_card_includes_details(self):
        bot = TelegramBot()
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"ok": True}
        with patch("telegram.requests.post", return_value=mock_resp) as mock_post:
            bot.send_card(_payload())
        text = mock_post.call_args[1]["json"]["text"]
        assert "Currency" in text
        assert "Status" in text
        assert "Created" in text
        assert "Reminder" not in text

    def test_follow_up_is_condensed(self):
        bot = TelegramBot()
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"ok": True}
        with patch("telegram.requests.post", return_value=mock_resp) as mock_post:
            bot.send_card(_payload(), follow_up=1)
        text = mock_post.call_args[1]["json"]["text"]
        assert "Currency" not in text
        assert "Status" not in text
        assert "Transaction Category" not in text
        assert "Reminder" in text

    def test_follow_up_keeps_essential_recall_info(self):
        bot = TelegramBot()
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"ok": True}
        with patch("telegram.requests.post", return_value=mock_resp) as mock_post:
            bot.send_card(_payload(txn_id="tx_042", merchant_name="Tesco", amount_pence=-1234), follow_up=2)
        text = mock_post.call_args[1]["json"]["text"]
        assert "£12.34" in text
        assert "Tesco" in text
        assert "Weekly shop" in text
        assert "tx_042" in text

    def test_follow_up_labels_by_stage(self):
        bot = TelegramBot()
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"ok": True}
        expected = {1: "1 hour ago", 2: "1 day ago", 3: "2 days ago"}
        for stage, label in expected.items():
            with patch("telegram.requests.post", return_value=mock_resp) as mock_post:
                bot.send_card(_payload(), follow_up=stage)
            text = mock_post.call_args[1]["json"]["text"]
            assert label in text

    def test_follow_up_still_has_quick_category_buttons(self):
        bot = TelegramBot()
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"ok": True}
        quick_categories = [{"id": 1, "category": "Food & Drink", "subcategory": "Groceries"}]
        with patch("telegram.requests.post", return_value=mock_resp) as mock_post:
            bot.send_card(_payload(), follow_up=1, quick_categories=quick_categories)
        keyboard = mock_post.call_args[1]["json"]["reply_markup"]["inline_keyboard"]
        assert keyboard[0][0]["callback_data"] == "quickcat:tx_001:1"


class TestSendAlert:
    def _sent_text(self, title, message):
        bot = TelegramBot()
        mock_resp = MagicMock(status_code=200)
        with patch("telegram.requests.post", return_value=mock_resp) as mock_post:
            bot.send_alert(123, title, message)
        payload = mock_post.call_args[1]["json"]
        assert payload["chat_id"] == 123 and payload["parse_mode"] == "HTML"
        return payload["text"]

    def test_leads_with_the_title_in_bold_then_the_message(self):
        text = self._sent_text("Anthropic credit balance too low", "Top up to resume.")
        assert text == "⚠️ <b>Anthropic credit balance too low</b>\n\nTop up to resume."

    def test_html_in_the_text_is_escaped_so_telegram_does_not_reject_it(self):
        text = self._sent_text("a < b", "x <script> & y")
        assert "a &lt; b" in text and "&lt;script&gt; &amp; y" in text


class TestSendCategoryProposalCards:
    NEW = {"parent_name": "Tax", "subcategory_name": "Self Assessment", "parent_is_new": True, "rationale": "Best fit."}
    SUGGESTION = {"parent_name": "Health", "subcategory_name": "Dental", "parent_is_new": False,
                  "rationale": "Second look: it is a dentist.", "judge": True, "is_original": False}
    KEEP = {"parent_name": "Food & Drink", "subcategory_name": "Alcohol", "parent_is_new": False,
            "rationale": "Keep it where it was.", "judge": True, "is_original": True}

    def _card(self, options):
        bot = TelegramBot()
        mock_resp = MagicMock(status_code=200)
        with patch("telegram.requests.post", return_value=mock_resp) as mock_post:
            bot.send_category_proposal(123, {"local_id": 7, "options": options, "txn_count": 1, "examples": ["Dentist"]})
        payload = mock_post.call_args[1]["json"]
        buttons = [b[0] for b in payload["reply_markup"]["inline_keyboard"]]
        return payload["text"], buttons

    def test_a_new_category_card_is_unchanged(self):
        text, buttons = self._card([self.NEW])
        assert "New category needed" in text and "Second look" not in text
        assert "🆕" in text
        assert [b["text"] for b in buttons][-2:] == ["🔄 None of these — try again", "❌ Give up — leave unclassified"]

    def test_a_second_look_card_says_what_it_is_really_asking(self):
        text, buttons = self._card([self.SUGGESTION, self.KEEP])
        assert "Second look" in text and "New category needed" not in text
        assert "may be filed in the wrong place" in text
        assert "Pick where it belongs" in text

    def test_the_keep_option_gets_its_own_icon_and_the_others_keep_theirs(self):
        text, _ = self._card([self.SUGGESTION, self.KEEP])
        assert "↩️ <b>Food &amp; Drink › Alcohol</b>".replace("&amp;", "&") in text
        assert "📁 <b>Health › Dental</b>" in text

    def test_a_second_look_card_words_its_buttons_for_what_they_do(self):
        _, buttons = self._card([self.SUGGESTION, self.KEEP])
        assert [b["text"] for b in buttons][-2:] == ["🔄 Neither — try again", "❌ Skip — no change"]
        assert [b["callback_data"] for b in buttons][:2] == ["catprop:select:7:0", "catprop:select:7:1"]

    def test_a_second_look_card_says_where_the_transaction_currently_sits(self):
        """Without this you have to work out which option is the current placement
        before you can see what is supposedly wrong with it."""
        text, _ = self._card([self.SUGGESTION, self.KEEP])
        assert "Currently filed under: <b>Food & Drink › Alcohol</b>" in text
        assert text.index("Currently filed under") < text.index("Pick where it belongs")

    def test_the_keep_button_says_it_is_keeping_and_names_the_placement(self):
        _, buttons = self._card([self.SUGGESTION, self.KEEP])
        assert buttons[0]["text"] == "1️⃣ Health › Dental"
        assert buttons[1]["text"] == "2️⃣ ↩️ Keep in Food & Drink › Alcohol"

    def test_a_second_look_footer_does_not_claim_history_becomes_unclassified(self):
        text, _ = self._card([self.SUGGESTION, self.KEEP])
        assert "Nothing changes until you choose." in text and "stay unclassified" not in text

    def test_a_new_category_card_has_no_current_placement_line_and_keeps_its_footer(self):
        text, buttons = self._card([self.NEW])
        assert "Currently filed under" not in text and "Keep in" not in " ".join(b["text"] for b in buttons)
        assert "Until you decide, these stay unclassified." in text

    def test_options_from_an_older_local_side_without_the_flags_still_render(self):
        text, _ = self._card([self.NEW])
        assert "Tax › Self Assessment" in text


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
