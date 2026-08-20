import os
import re
import logging
import requests

log = logging.getLogger(__name__)


class TelegramBot:
    def __init__(self):
        self.api_key = os.getenv("TELEGRAM_API")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID")

        if not self.api_key:
            raise ValueError("Missing TELEGRAM_API env variable")

        if not self.chat_id:
            raise ValueError("Missing TELEGRAM_CHAT_ID env variable")

    def _post(self, endpoint: str, payload: dict) -> dict | None:
        """POST to the Telegram Bot API. Never raises — logs and returns None on
        any transport failure, non-200 response, or unparseable body."""
        url = f"https://api.telegram.org/bot{self.api_key}/{endpoint}"
        try:
            response = requests.post(url, json=payload, timeout=10)
        except requests.RequestException as e:
            log.error(f"Telegram API request to {endpoint} failed: {e}")
            return None
        if response.status_code != 200:
            log.error(f"Telegram API {endpoint} returned {response.status_code}: {response.text}")
        try:
            data = response.json()
        except ValueError as e:
            log.error(f"Telegram API {endpoint} returned unparseable response: {e}")
            return None
        if isinstance(data, dict) and not data.get("ok"):
            log.error(f"Telegram API {endpoint} rejected payload: {data}")
        return data

    def send_message(self, chat_id: int, text: str, reply_markup: dict | None = None):
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        self._post("sendMessage", payload)

    def send_skip_confirm(self, chat_id: int, transaction_id: str):
        data = self._post("sendMessage", {
            "chat_id": chat_id,
            "text": f"Skip this transaction?\n\n<code>{transaction_id}</code>",
            "parse_mode": "HTML",
            "reply_markup": {
                "inline_keyboard": [[
                    {"text": "✅ Yes, skip", "callback_data": f"skip_do:{transaction_id}"},
                    {"text": "❌ Cancel",    "callback_data": "skip_cancel"},
                ]]
            }
        })
        if data and data.get("ok"):
            return data["result"]["message_id"]
        return None

    def delete_message(self, chat_id: int, message_id: int):
        self._post("deleteMessage", {"chat_id": chat_id, "message_id": message_id})

    def ack_callback(self, callback_query_id: str):
        self._post("answerCallbackQuery", {"callback_query_id": callback_query_id})




    def send_taxonomy_intro(self, chat_id: int, count: int, follow_up: int = 0):
        """The entry card. Sent once before the individual proposal cards so a
        burst of category questions arrives with an explanation rather than
        appearing out of nowhere a month after the last one."""
        if follow_up:
            text = (
                f"⏰ <b>Still waiting on {count} category suggestion"
                f"{'s' if count != 1 else ''}</b>\n\n"
                "No rush — they'll keep until you decide. Nothing changes unless you approve."
            )
        else:
            text = (
                "🗂 <b>Monthly taxonomy review</b>\n\n"
                f"I looked over your existing categories and found <b>{count}</b> "
                f"group{'s' if count != 1 else ''} of transactions that may deserve "
                "a category of their own.\n\n"
                "Each card below shows what it found and why. "
                "<b>Approving moves only the transactions listed on that card</b> — "
                "denying changes nothing at all."
            )
        self.send_message(chat_id, text)

    def send_taxonomy_proposal(self, chat_id: int, proposal: dict):
        """One card per proposed category, carrying its own evidence so the
        decision can be made from the notification without opening anything."""
        examples = proposal.get("examples") or []
        count = proposal["evidence_count"]
        plural = "s" if count != 1 else ""
        is_move = proposal.get("action") == "move"
        # A move reassigns into a category the user already has, so it must not
        # read as "a new category" -- that is a materially different decision.
        if is_move:
            heading = f"↪️ Move into <b>{proposal['proposed_sub']}</b>"
        else:
            heading = f"🏷 New category: <b>{proposal['proposed_sub']}</b>"
        if is_move:
            target = proposal["proposed_sub"]
            where = f" (in {proposal['target_parent']})" if proposal.get("target_parent") else ""
            action_line = (
                f"Would move <b>{count}</b> transaction{plural} from "
                f"<b>{proposal['source_sub']}</b> into your existing "
                f"<b>{target}</b>{where}."
            )
        else:
            action_line = (
                f"Would split <b>{count}</b> transaction{plural} out of "
                f"<b>{proposal['source_sub']}</b> (in {proposal['parent_name']}) "
                f"into a new category."
            )
        lines = [heading, "", f"<i>{proposal['rationale']}</i>", "", action_line]
        if examples:
            lines.append("")
            lines.append("<b>For example:</b>")
            lines += [f"  • {e}" for e in examples]
        return self._post("sendMessage", {
            "chat_id": chat_id,
            "text": "\n".join(lines),
            "parse_mode": "HTML",
            "reply_markup": {"inline_keyboard": [[
                {"text": "✅ Approve", "callback_data": f"taxprop:approve:{proposal['local_id']}"},
                {"text": "❌ Deny", "callback_data": f"taxprop:deny:{proposal['local_id']}"},
            ]]},
        })

    def send_category_proposal(self, chat_id: int, proposal: dict):
        """Sent when Pass 1/2/3 of the real-time classifier can't cleanly place
        a transaction. Offers up to a few candidate placements as individual
        buttons -- a new parent, a stretch-fit into an existing one, maybe a
        different existing subcategory -- so the user picks instead of the
        classifier having to commit to one guess. Unlike the monthly review
        this can fire on a single transaction -- there's no cluster-size
        minimum. Until answered, the transaction(s) below stay unclassified
        rather than being filed under the nearest existing name."""
        examples = proposal.get("examples") or []
        options = proposal["options"]
        n = proposal["txn_count"]
        plural = "s" if n != 1 else ""
        numbers = ["1️⃣", "2️⃣", "3️⃣"]

        lines = ["🗂 <b>New category needed</b>", "", f"Waiting to classify <b>{n}</b> transaction{plural}:"]
        lines += [f"  • {e}" for e in examples[:4]]
        lines += ["", "Pick whichever fits, ask for different options, or deny them all:"]

        buttons = []
        for i, opt in enumerate(options):
            num = numbers[i] if i < len(numbers) else f"{i + 1}."
            icon = "🆕" if opt["parent_is_new"] else "📁"
            label = f"{opt['parent_name']} › {opt['subcategory_name']}"
            lines.append(f"\n{num} {icon} <b>{label}</b>\n{opt['rationale']}")
            buttons.append([{
                "text": f"{num} {label}",
                "callback_data": f"catprop:select:{proposal['local_id']}:{i}",
            }])
        buttons.append([{"text": "🔄 None of these — try again", "callback_data": f"catprop:regenerate:{proposal['local_id']}"}])
        buttons.append([{"text": "❌ Give up — leave unclassified", "callback_data": f"catprop:denyall:{proposal['local_id']}"}])
        lines += ["", "Until you decide, these stay unclassified."]

        return self._post("sendMessage", {
            "chat_id": chat_id,
            "text": "\n".join(lines),
            "parse_mode": "HTML",
            "reply_markup": {"inline_keyboard": buttons},
        })

    def send_card(self, payload, follow_up: int = 0, quick_categories: list[dict] | None = None):
        from datetime import datetime
        data = payload['data']
        log.info(f"send_card for {data['id']}: received quick_categories={quick_categories!r}")

        currency_symbols = {
            "GBP": "£", "USD": "$", "EUR": "€", "JPY": "¥",
            "CAD": "CA$", "AUD": "A$", "CHF": "Fr", "SEK": "kr",
            "NOK": "kr", "DKK": "kr", "SGD": "S$", "HKD": "HK$",
        }
        currency = data.get('currency', 'GBP')
        symbol = currency_symbols.get(currency, currency + " ")

        amount_pence = data['amount']
        prefix = "-" if amount_pence < 0 else "+"
        amount_str = f"{prefix}{symbol}{abs(amount_pence) / 100:.2f}"

        merchant = data.get('merchant')
        is_merchant = isinstance(merchant, dict)
        emoji = merchant.get('emoji', '💸') if is_merchant else '💸'
        merchant_name = merchant.get('name') if is_merchant else None

        if follow_up > 0:
            follow_up_labels = {1: "1 hour", 2: "1 day", 3: "2 days"}
            lines = [
                f"⏰ <b>Reminder — {follow_up_labels.get(follow_up, f'#{follow_up}')} ago, still unenriched</b>",
                "",
                f"{emoji} {'🔴' if amount_pence < 0 else '🟢'} <b>{amount_str}</b>"
                + (f" · {merchant_name}" if merchant_name else ""),
                f"📝 {data.get('description', '—')}",
                "",
                f"🔖 <code>{data['id']}</code>",
            ]
            text = "\n".join(lines)
        else:
            created = re.sub(r'\.(\d+)', lambda m: '.' + (m.group(1) + '000000')[:6], data['created'].replace("Z", "+00:00"))
            dt = datetime.fromisoformat(created)
            date_str = dt.strftime("%d %b %Y · %H:%M")

            counterparty = data.get('counterparty')
            is_counterparty = isinstance(counterparty, dict)

            counterparty_name = counterparty.get('name') if is_counterparty else None
            merchant_category = merchant.get('category', '').replace('_', ' ').capitalize() if is_merchant else None
            transaction_category = data.get('category', '').replace('_', ' ').capitalize()
            settled = "Settled" if data.get('settled') else "Pending"

            addr = merchant.get('address', {}) if is_merchant else {}
            location_parts = [p for p in [
                addr.get('address'), addr.get('city'),
                addr.get('postcode'), addr.get('region'), addr.get('country')
            ] if p]
            location = ", ".join(location_parts)

            lines = [
                f"{emoji} {'🔴' if amount_pence < 0 else '🟢'} <b>{amount_str}</b>",
                "",
            ]

            if merchant_name:
                lines.append(f"🏪 <b>Merchant</b>: {merchant_name}")
            if counterparty_name:
                lines.append(f"👤 <b>Counterparty</b>: {counterparty_name}")
                if counterparty.get('sort_code') and counterparty.get('account_number'):
                    lines.append(f"🏦 <b>Bank</b>: {counterparty['sort_code']} · {counterparty['account_number']}")

            lines.append(f"📝 <b>Description</b>: {data.get('description', '—')}")
            if merchant_category:
                lines.append(f"🏷 <b>Merchant Category</b>: {merchant_category}")
            lines.append(f"📂 <b>Transaction Category</b>: {transaction_category or '—'}")
            lines += [
                f"💱 <b>Currency</b>: {currency}",
                f"📋 <b>Status</b>: {settled}",
                f"🕐 <b>Created</b>: {date_str}",
            ]

            if data.get('settled'):
                settled_str = re.sub(r'\.(\d+)', lambda m: '.' + (m.group(1) + '000000')[:6], data['settled'].replace("Z", "+00:00"))
                settled_dt = datetime.fromisoformat(settled_str)
                lines.append(f"✅ <b>Settled</b>: {settled_dt.strftime('%d %b %Y · %H:%M')}")

            if location:
                lines.append(f"📍 <b>Location</b>: {location}")

            lines += [
                "",
                f"🔖 <b>ID</b>: <code>{data['id']}</code>",
            ]

            text = "\n".join(lines)

        keyboard_rows = []
        row = []
        for qc in (quick_categories or []):
            row.append({"text": qc["subcategory"], "callback_data": f"quickcat:{data['id']}:{qc['id']}"})
            if len(row) == 3:
                keyboard_rows.append(row)
                row = []
        if row:
            keyboard_rows.append(row)
        keyboard_rows.append([
            {"text": "✏️ Enrich", "callback_data": f"enrich:{data['id']}"},
            {"text": "⏭ Skip", "callback_data": f"skip_confirm:{data['id']}"},
        ])

        payload_send = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "reply_markup": {
                "inline_keyboard": keyboard_rows
            }
        }
        return self._post("sendMessage", payload_send)




if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    bot = TelegramBot()