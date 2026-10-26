"""The server's side of the one Postgres database (the data box's `vault`,
schema `bank_enrichment` -- see docs/server-move.md).

History is only ever added to: a transaction arriving, its context being given
or asked for, a label being set are each a new row (transactions_raw,
context_events, label_events), and the `transactions` view shows the latest of
each. The helpers here are the one place those rows are written from, so every
route records them the same way."""
import json
import logging
import os
import time

import psycopg

log = logging.getLogger(__name__)

_con: psycopg.Connection | None = None

SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "sql", "postgres", "schema.sql")


def _connect() -> psycopg.Connection:
    e = os.environ
    return psycopg.connect(
        host=e["DB_HOST"], port=e.get("DB_PORT", "5432"), dbname=e["DB_NAME"],
        user=e["DB_USER"], password=e["DB_PASSWORD"], connect_timeout=10, autocommit=True,
    )


def get_con() -> psycopg.Connection:
    """The shared connection. Reopened if it has dropped -- the data box
    restarting, a network blip -- so one hiccup doesn't need a server restart."""
    global _con
    if _con is None:
        raise RuntimeError("Database not initialised — call init_db() first")
    if _con.closed or _con.broken:
        log.warning("Database connection lost -- reconnecting")
        _con = _connect()
    return _con


def init_db() -> None:
    """Connect, and bring the schema up to date. schema.sql is safe to apply on
    every start: tables are created only if missing, new columns are added only
    if missing, and the views (which store nothing) are made again."""
    global _con
    _con = _connect()
    with open(SCHEMA_PATH) as f:
        _con.execute(f.read())


def insert_with_next_id(con, sql: str, params, table: str, attempts: int = 3) -> int:
    """Run an INSERT whose id is (SELECT COALESCE(MAX(id), 0) + 1 FROM table),
    ending RETURNING id, and return that id. Two writers at once (the worker,
    a dashboard edit, the server) can pick the same one; the second then tries
    again. Only a clash on the id itself is retried -- any other unique rule
    broken (a name already taken) raises as it always has."""
    for attempt in range(attempts):
        try:
            return con.execute(sql, params).fetchone()[0]
        except psycopg.errors.UniqueViolation as e:
            if e.diag.constraint_name != f"{table}_pkey" or attempt == attempts - 1:
                raise


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ── Recording what happened ──────────────────────────────────────────────────

def latest_label_source(con, txn_id: str) -> str | None:
    row = con.execute(
        "SELECT source FROM label_events WHERE txn_id = %s ORDER BY event_id DESC LIMIT 1", [txn_id]
    ).fetchone()
    return row[0] if row else None


def record_context(con, txn_id: str, context: str | None, *, skipped: bool = False,
                   source: str, clear_rule_label: bool = True) -> None:
    """A transaction's context, given or changed. Each event holds the full
    values, so any date corrected earlier is carried forward.

    A label a rule set came from the transaction's details alone; once you
    say what it was, that's what should decide the category, so a rule's label
    is cleared for the classifier to look again (as clearing rule_category on
    the old queue did). A label anyone else set is left alone."""
    previous = con.execute(
        """SELECT created_at_override FROM context_events
           WHERE txn_id = %s AND source <> 'request' ORDER BY event_id DESC LIMIT 1""",
        [txn_id],
    ).fetchone()
    con.execute(
        """INSERT INTO context_events (txn_id, user_context, skipped, created_at_override, source, set_at)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        [txn_id, context, skipped, previous[0] if previous else None, source, now()],
    )
    if clear_rule_label and latest_label_source(con, txn_id) == "rule":
        record_label(con, txn_id, None, None, source="manual")


def record_request(con, txn_id: str) -> None:
    """A card or a follow-up about this transaction was sent."""
    con.execute(
        "INSERT INTO context_events (txn_id, source, set_at) VALUES (%s, 'request', %s)", [txn_id, now()]
    )


def record_label(con, txn_id: str, parent_id: int | None, subcategory_id: int | None, *,
                 source: str, model: str | None = None, confidence=None) -> None:
    con.execute(
        """INSERT INTO label_events (txn_id, parent_id, subcategory_id, source, model, confidence, set_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
        [txn_id, parent_id, subcategory_id, source, model, confidence, now()],
    )


def subcategory(con, subcategory_id: int) -> tuple[int, str, str] | None:
    """(parent_id, category name, subcategory name) for a live subcategory."""
    return con.execute(
        """SELECT p.id, p.name, s.name FROM subcategories s JOIN parent_categories p ON p.id = s.parent_id
           WHERE s.id = %s AND s.archived_at IS NULL AND p.archived_at IS NULL""",
        [subcategory_id],
    ).fetchone()


def subcategory_id(con, category: str, subcategory_name: str) -> int | None:
    """The id of a live subcategory, by its names."""
    row = con.execute(
        """SELECT s.id FROM subcategories s JOIN parent_categories p ON p.id = s.parent_id
           WHERE p.name = %s AND s.name = %s AND s.archived_at IS NULL AND p.archived_at IS NULL""",
        [category, subcategory_name],
    ).fetchone()
    return row[0] if row else None


def payload(con, txn_id: str) -> dict | None:
    """The transaction as it stands now (transaction_current)."""
    row = con.execute("SELECT payload FROM transaction_current WHERE id = %s", [txn_id]).fetchone()
    return row[0] if row else None


def record_update(con, txn_id: str, payload: dict, source: str) -> bool:
    """Keep a later version of a transaction, unless it says nothing new: the
    same as a version already stored (Monzo sends near-identical webhooks in a
    burst). True if it was kept."""
    seen = con.execute(
        """SELECT EXISTS (SELECT 1 FROM transactions_raw WHERE id = %s AND payload = %s::jsonb)
               OR EXISTS (SELECT 1 FROM transaction_updates WHERE txn_id = %s AND payload = %s::jsonb)""",
        [txn_id, json.dumps(payload), txn_id, json.dumps(payload)],
    ).fetchone()[0]
    if seen:
        return False
    con.execute(
        "INSERT INTO transaction_updates (txn_id, payload, source, received_at) VALUES (%s, %s, %s, %s)",
        [txn_id, json.dumps(payload), source, now()],
    )
    return True


def get_quick_categories(merchant_name: str | None) -> list[dict]:
    """Up to 3 subcategory suggestions for this merchant, else the top 5
    overall. `id` is the subcategory's id, which is what a button carries."""
    con = get_con()
    if merchant_name:
        rows = con.execute(
            """SELECT subcategory_id, category, subcategory FROM quick_categories
               WHERE merchant_name = %s ORDER BY rank LIMIT 3""",
            [merchant_name],
        ).fetchall()
        if rows:
            log.info(f"get_quick_categories: {len(rows)} merchant-specific row(s) for {merchant_name!r}")
            return [{"id": r[0], "category": r[1], "subcategory": r[2]} for r in rows]
        log.info(f"get_quick_categories: no merchant-specific rows for {merchant_name!r}, falling back to general top-5")

    rows = con.execute(
        """SELECT subcategory_id, category, subcategory FROM quick_categories
           WHERE merchant_name IS NULL ORDER BY rank LIMIT 5"""
    ).fetchall()
    log.info(f"get_quick_categories: general fallback returned {len(rows)} row(s)")
    return [{"id": r[0], "category": r[1], "subcategory": r[2]} for r in rows]


def dumps(value) -> str:
    """JSON for a JSONB parameter."""
    return json.dumps(value)
