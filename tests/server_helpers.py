"""Building blocks for the server's tests, on the new Postgres tables: a
transaction arriving, a small taxonomy, and reading a transaction back the
way the rest of the app sees it."""
import json

# id: (parent id, category, subcategory)
TAXONOMY = {
    1: (1, "Food & Drink", "Groceries"),
    2: (1, "Food & Drink", "Takeaway"),
    3: (2, "Bills & Utilities", "Rent"),
    4: (3, "Transfers", "Pot"),
}


def seed_taxonomy(con) -> None:
    parents = {pid: name for pid, name, _ in TAXONOMY.values()}
    for pid, name in parents.items():
        con.execute("INSERT INTO parent_categories (id, name, created_at) VALUES (%s, %s, '2026-01-01')", [pid, name])
    for sid, (pid, _, name) in TAXONOMY.items():
        con.execute("INSERT INTO subcategories (id, name, parent_id, created_at) VALUES (%s, %s, %s, '2026-01-01')",
                    [sid, name, pid])


def monzo_payload(txn_id="tx_1", amount=-450, merchant="Tesco", counterparty=None, description="TESCO STORES",
                  category="groceries", scheme=None, created="2026-10-01T12:00:00.000Z", **extra) -> dict:
    data = {
        "account_id": "acc_1", "amount": amount, "created": created, "currency": "GBP",
        "description": description, "id": txn_id, "category": category, "is_load": False,
        "merchant": {"name": merchant, "category": category} if merchant else None,
        "counterparty": {"name": counterparty} if counterparty else {},
    }
    if scheme:
        data["scheme"] = scheme
    data.update(extra)
    return {"type": "transaction.created", "data": data}


def add_transaction(con, txn_id="tx_1", received_at="2026-10-01 12:00:01", **payload_fields) -> dict:
    """A transaction that has arrived and been stored, with nothing else yet."""
    payload = monzo_payload(txn_id, **payload_fields)
    con.execute("INSERT INTO transactions_raw (id, payload, received_at) VALUES (%s, %s, %s)",
                [txn_id, json.dumps(payload), received_at])
    return payload


def add_context(con, txn_id, context=None, skipped=False, source="telegram", set_at="2026-10-01 12:05:00"):
    con.execute("INSERT INTO context_events (txn_id, user_context, skipped, source, set_at) VALUES (%s, %s, %s, %s, %s)",
                [txn_id, context, skipped, source, set_at])


def add_request(con, txn_id, set_at="2026-10-01 12:00:02"):
    con.execute("INSERT INTO context_events (txn_id, source, set_at) VALUES (%s, 'request', %s)", [txn_id, set_at])


def add_label(con, txn_id, subcategory_id, source="llm"):
    parent_id = TAXONOMY[subcategory_id][0]
    con.execute("INSERT INTO label_events (txn_id, parent_id, subcategory_id, source) VALUES (%s, %s, %s, %s)",
                [txn_id, parent_id, subcategory_id, source])


def transaction(con, txn_id="tx_1") -> dict | None:
    """One row of the transactions view, as a dict."""
    cur = con.execute("SELECT * FROM transactions WHERE id = %s", [txn_id])
    row = cur.fetchone()
    return dict(zip([c.name for c in cur.description], row)) if row else None


def requests_sent(con, txn_id="tx_1") -> int:
    return con.execute("SELECT COUNT(*) FROM context_events WHERE txn_id = %s AND source = 'request'",
                       [txn_id]).fetchone()[0]


def label_sources(con, txn_id="tx_1") -> list[str]:
    return [r[0] for r in con.execute(
        "SELECT source FROM label_events WHERE txn_id = %s ORDER BY event_id", [txn_id]).fetchall()]


def add_rule(con, rule_id=1, name="Rule", match_field="merchant_name", match_type="contains",
             match_value="Tesco", auto_context="", auto_skip=False, subcategory_id=None, enabled=True,
             match_field_2=None, match_type_2=None, match_value_2=None):
    con.execute(
        """INSERT INTO rules (id, name, match_field, match_type, match_value, auto_context, auto_skip,
                              subcategory_id, enabled, match_field_2, match_type_2, match_value_2)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        [rule_id, name, match_field, match_type, match_value, auto_context, auto_skip, subcategory_id,
         enabled, match_field_2, match_type_2, match_value_2],
    )


def tap(con, data: str, user_id: int = 12345):
    """A tap on a Telegram button, through the real webhook handler. Returns
    the mocked bot, to see what was said back."""
    import asyncio
    from unittest.mock import MagicMock, patch
    import main

    cq = MagicMock(data=data, id="cbq_1")
    cq.from_user.id = user_id
    update = MagicMock(callback_query=cq, message=None)
    request = MagicMock()

    async def _body():
        return b"{}"

    request.body = _body
    request.headers = {"x-telegram-bot-api-secret-token": "test-secret"}
    bot = MagicMock()
    with patch.object(main, "get_con", return_value=con), \
         patch.object(main, "bot", bot), \
         patch.object(main, "TELEGRAM_WEBHOOK_SECRET", "test-secret"), \
         patch.dict("os.environ", {"TELEGRAM_CHAT_ID": "12345"}), \
         patch.object(main.TelegramUpdate, "model_validate_json", return_value=update):
        asyncio.run(main.recieve_telegram(request))
    return bot


def replies(bot) -> list[str]:
    """Everything the bot sent back as a plain message."""
    return [c.args[1] for c in bot.send_message.call_args_list]


def status(con, txn_id="tx_1") -> str | None:
    """Where a transaction is up to, as the dashboard sees it: pending (no
    answer yet), enriched (answered, not classified), classified, or skipped."""
    from main import STATUS_SQL
    row = con.execute(f"SELECT {STATUS_SQL} FROM transactions t WHERE t.id = %s", [txn_id]).fetchone()
    return row[0] if row else None


def stored(con) -> dict[str, str]:
    """{transaction id: status} for every transaction stored."""
    from main import STATUS_SQL
    return dict(con.execute(f"SELECT t.id, {STATUS_SQL} FROM transactions t").fetchall())
