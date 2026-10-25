"""Building blocks for the pipeline's tests, on the shared Postgres tables
through the DuckDB connection the pipeline uses (the `db` fixture).

The pipeline's tests describe a transaction the way the old local table held
one -- amount, merchant, context, category -- so `insert_transaction` takes
those same columns and records them the way they really arrive now: a Monzo
payload, a context event, and (if it has a category) a label event."""
import json
import time
from datetime import datetime


def _ts(value):
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)


def ensure_category(con, category: str, subcategory: str | None = None) -> tuple[int, int | None]:
    """(parent id, subcategory id), creating either if it doesn't exist yet,
    so a test can name categories without seeding a taxonomy first."""
    import database_functions
    parent_id = database_functions.upsert_parent(category)
    sub_id = database_functions.upsert_subcategory(subcategory, parent_id) if subcategory else None
    return parent_id, sub_id


def hold(con, proposal_id: int, txn_ids: list[str]) -> None:
    """Hold transactions behind a category proposal (creating a bare pending
    one if the test hasn't made it yet)."""
    row = con.execute("SELECT txn_ids FROM category_proposals WHERE id = ?", [proposal_id]).fetchone()
    if row is None:
        con.execute(
            "INSERT INTO category_proposals (id, options, txn_ids, status, proposed_at) VALUES (?, '[]', ?, 'pending', NOW())",
            [proposal_id, json.dumps(list(txn_ids))],
        )
        return
    held = json.loads(row[0])
    held += [t for t in txn_ids if t not in held]
    import database_functions
    database_functions.set_json(con, "category_proposals", proposal_id, txn_ids=held)


def insert_transaction(
    con, id: str, amount=-10.0, currency="GBP", description=None, monzo_category=None,
    merchant_name=None, merchant_category=None, counterparty_name=None, is_load=False,
    created_at=None, settled_at=None, user_context=None, skipped=False,
    received_at=None, enriched_at=None, llm_category=None, llm_subcategory=None,
    llm_confidence=None, llm_model=None, classified_at=None, pending_category_proposal_id=None,
    answered=True, scheme=None,
) -> None:
    """A transaction in the given state. `answered=False` leaves it with no
    context event at all (still waiting for an answer); otherwise it has one,
    as every transaction the pipeline ever saw used to."""
    created = _ts(created_at) or datetime(2026, 1, 15, 10, 0)
    data = {
        "id": id, "amount": round(float(amount) * 100), "currency": currency,
        "description": description, "category": monzo_category,
        "merchant": ({"name": merchant_name, "category": merchant_category} if merchant_name else None),
        "counterparty": ({"name": counterparty_name} if counterparty_name else {}),
        "is_load": is_load,
        "created": created.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "settled": _ts(settled_at).strftime("%Y-%m-%dT%H:%M:%SZ") if settled_at else "",
    }
    if scheme:
        data["scheme"] = scheme
    con.execute("INSERT INTO transactions_raw (id, payload, received_at) VALUES (?, ?, ?)",
                [id, json.dumps({"type": "transaction.created", "data": data}), _ts(received_at) or created])
    if answered:
        con.execute(
            "INSERT INTO context_events (txn_id, user_context, skipped, source, set_at) VALUES (?, ?, ?, 'migration', ?)",
            [id, user_context, bool(skipped), _ts(enriched_at) or created],
        )
    if llm_category:
        parent_id, sub_id = ensure_category(con, llm_category, llm_subcategory)
        con.execute(
            """INSERT INTO label_events (txn_id, parent_id, subcategory_id, source, model, confidence, set_at)
               VALUES (?, ?, ?, 'migration', ?, ?, ?)""",
            [id, parent_id, sub_id, llm_model, llm_confidence, _ts(classified_at) or created],
        )
    if pending_category_proposal_id is not None:
        hold(con, pending_category_proposal_id, [id])


def write_to_db(con, rows: list[dict]) -> None:
    """The old database_functions.write_to_db's input -- pulled transactions
    as {id, payload, user_context, skipped, received_at, enriched_at} -- stored
    the way such a transaction arrives now. Duplicates are ignored, as they were."""
    for t in rows:
        if con.execute("SELECT 1 FROM transactions_raw WHERE id = ?", [t["id"]]).fetchone():
            continue
        con.execute("INSERT INTO transactions_raw (id, payload, received_at) VALUES (?, ?, ?)",
                    [t["id"], json.dumps(t["payload"]), _ts(t.get("received_at")) or datetime.now()])
        con.execute(
            "INSERT INTO context_events (txn_id, user_context, skipped, source, set_at) VALUES (?, ?, ?, 'migration', ?)",
            [t["id"], t.get("user_context"), bool(t.get("skipped")), _ts(t.get("enriched_at")) or datetime.now()],
        )


def label(con, txn_id: str, category: str | None, subcategory: str | None = None, model="manual") -> None:
    """Set (or, with category None, clear) a transaction's label."""
    if category is None:
        con.execute("INSERT INTO label_events (txn_id, source, set_at) VALUES (?, 'manual', NOW())", [txn_id])
        return
    parent_id, sub_id = ensure_category(con, category, subcategory)
    con.execute(
        "INSERT INTO label_events (txn_id, parent_id, subcategory_id, source, model, set_at) VALUES (?, ?, ?, 'manual', ?, NOW())",
        [txn_id, parent_id, sub_id, model],
    )


def row(con, txn_id: str) -> dict | None:
    cur = con.execute("SELECT * FROM transactions WHERE id = ?", [txn_id])
    cols = [d[0] for d in cur.description]
    r = cur.fetchone()
    return dict(zip(cols, r)) if r else None


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")
