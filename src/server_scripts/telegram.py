import os
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



    def send_message(self, chat_id: int, text: str):
        url = f"https://api.telegram.org/bot{self.api_key}/sendMessage"
        response = requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"})
        if response.status_code != 200:
            log.error(f"Message failed to send: {response.text}")

    def send_skip_confirm(self, chat_id: int, transaction_id: str):
        url = f"https://api.telegram.org/bot{self.api_key}/sendMessage"
        requests.post(url, json={
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

    def ack_callback(self, callback_query_id: str):
        requests.post(
            f"https://api.telegram.org/bot{self.api_key}/answerCallbackQuery",
            json={"callback_query_id": callback_query_id}
        )




    def send_card(self, payload):
        from datetime import datetime
        url = f"https://api.telegram.org/bot{self.api_key}/sendMessage"
        data = payload['data']

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

        dt = datetime.fromisoformat(data['created'].replace("Z", "+00:00"))
        date_str = dt.strftime("%d %b %Y · %H:%M")

        merchant = data.get('merchant')
        counterparty = data.get('counterparty')
        is_merchant = isinstance(merchant, dict)
        is_counterparty = isinstance(counterparty, dict)

        emoji = merchant.get('emoji', '💸') if is_merchant else '💸'
        merchant_name = merchant.get('name') if is_merchant else None
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
            settled_dt = datetime.fromisoformat(data['settled'].replace("Z", "+00:00"))
            lines.append(f"✅ <b>Settled</b>: {settled_dt.strftime('%d %b %Y · %H:%M')}")

        if location:
            lines.append(f"📍 <b>Location</b>: {location}")

        lines += [
            "",
            f"🔖 <b>ID</b>: <code>{data['id']}</code>",
        ]

        text = "\n".join(lines)

        payload_send = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "reply_markup": {
                "inline_keyboard": [[
                    {"text": "✏️ Enrich", "callback_data": f"enrich:{data['id']}"},
                    {"text": "⏭ Skip", "callback_data": f"skip_confirm:{data['id']}"},
                ]]
            }
        }
        response = requests.post(url, json=payload_send)
        return response.json()




if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    bot = TelegramBot()