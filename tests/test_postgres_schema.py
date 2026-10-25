"""sql/postgres/schema.sql: the new tables (docs/server-move.md §4).

The views stand in for today's tables, so these check they give the same
answers today's code does: the transactions view reads a payload the way
write_to_db flattens it, the latest event wins, and roles, the classifier's
to-do list and the quick-tap buttons come out as before.

These need a Postgres (the `pg` fixture), so they run in CI and are skipped on
a machine without one."""
import json
from datetime import datetime
from decimal import Decimal

TABLES = {
    "transactions_raw", "context_events", "label_events",
    "parent_categories", "subcategories", "category_roles",
    "rules", "rule_suggestions", "taxonomy_proposals", "category_proposals",
    "judge_reviews", "subscriptions", "summaries",
}
VIEWS = {"transactions", "transaction_roles", "needs_context", "needs_label", "quick_categories"}

# The columns today's transactions table has (sql/tables.sql), minus
# processed_at, which nothing reads. The view must offer every one of them.
OLD_TRANSACTION_COLUMNS = [
    "id", "amount", "currency", "description", "monzo_category", "merchant_name",
    "merchant_category", "counterparty_name", "is_load", "created_at", "settled_at",
    "raw_payload", "user_context", "skipped", "received_at", "enriched_at",
    "llm_category", "llm_subcategory", "llm_confidence", "llm_model", "classified_at",
    "pending_category_proposal_id",
]


def _payload(amount=-450, merchant="Tesco", counterparty=None, created="2026-10-01T12:34:56.789Z",
             settled="", is_load=False):
    return {
        "type": "transaction.created",
        "data": {
            "amount": amount, "currency": "GBP", "description": "TESCO STORES",
            "category": "groceries",
            "merchant": {"name": merchant, "category": "groceries"} if merchant else None,
            "counterparty": {"name": counterparty} if counterparty else {},
            "is_load": is_load, "created": created, "settled": settled,
        },
    }


def _txn(pg, txn_id, **payload):
    pg.execute(
        "INSERT INTO transactions_raw (id, payload, received_at) VALUES (%s, %s, '2026-10-01 12:35')",
        [txn_id, json.dumps(_payload(**payload))],
    )


def _context(pg, txn_id, context=None, skipped=False, source="telegram", override=None):
    pg.execute(
        "INSERT INTO context_events (txn_id, user_context, skipped, source, created_at_override) "
        "VALUES (%s, %s, %s, %s, %s)",
        [txn_id, context, skipped, source, override],
    )


def _taxonomy(pg):
    for i, name in enumerate(["Food", "Fun", "Income", "Investments", "Transfers"], start=1):
        pg.execute("INSERT INTO parent_categories VALUES (%s, %s, '2026-01-01')", [i, name])
    subs = [("Groceries", 1), ("Takeaway", 1), ("Gigs", 2), ("Salary", 3), ("ISA", 4), ("Pot", 5)]
    for i, (name, parent) in enumerate(subs, start=1):
        pg.execute("INSERT INTO subcategories VALUES (%s, %s, %s, '2026-01-01')", [i, name, parent])


def _label(pg, txn_id, parent_id, subcategory_id, source="llm", model="claude", confidence=0.9):
    pg.execute(
        "INSERT INTO label_events (txn_id, parent_id, subcategory_id, source, model, confidence) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        [txn_id, parent_id, subcategory_id, source, model, confidence],
    )


def _one(pg, sql, params=()):
    cur = pg.execute(sql, params)
    columns = [c.name for c in cur.description]
    row = cur.fetchone()
    return dict(zip(columns, row)) if row else None


def test_exactly_the_planned_tables_and_views(pg):
    rows = pg.execute(
        "SELECT table_name, table_type FROM information_schema.tables "
        "WHERE table_schema = current_schema()"
    ).fetchall()
    assert {n for n, t in rows if t == "BASE TABLE"} == TABLES
    assert {n for n, t in rows if t == "VIEW"} == VIEWS


def test_the_schema_can_be_applied_twice(pg):
    from tests.conftest import _PG_SCHEMA_PATH
    pg.execute(_PG_SCHEMA_PATH.read_text())


def test_the_transactions_view_has_every_column_the_old_table_had(pg):
    columns = [r[0] for r in pg.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = 'transactions' ORDER BY ordinal_position"
    ).fetchall()]
    assert columns[:len(OLD_TRANSACTION_COLUMNS)] == OLD_TRANSACTION_COLUMNS


def test_monzo_fields_come_out_of_the_payload_as_write_to_db_flattened_them(pg):
    _txn(pg, "tx1", amount=-450, created="2026-10-01T12:34:56.789Z", settled="")
    _txn(pg, "tx2", amount=2500, merchant=None, counterparty="Mum",
         settled="2026-10-02T08:00:00Z", is_load=True)
    one = _one(pg, "SELECT * FROM transactions WHERE id = 'tx1'")
    assert one["amount"] == Decimal("-4.5")
    assert (one["currency"], one["description"], one["monzo_category"]) == ("GBP", "TESCO STORES", "groceries")
    assert (one["merchant_name"], one["merchant_category"], one["counterparty_name"]) == ("Tesco", "groceries", None)
    assert one["is_load"] is False
    assert one["created_at"] == datetime(2026, 10, 1, 12, 34, 56, 789000)  # UTC, no zone
    assert one["settled_at"] is None  # Monzo sends "" until it settles
    assert one["raw_payload"]["data"]["amount"] == -450
    two = _one(pg, "SELECT * FROM transactions WHERE id = 'tx2'")
    assert (two["amount"], two["merchant_name"], two["counterparty_name"]) == (Decimal("25"), None, "Mum")
    assert two["settled_at"] == datetime(2026, 10, 2, 8, 0)
    assert two["is_load"] is True


def test_a_new_transaction_has_no_context_or_label_yet(pg):
    _txn(pg, "tx1")
    t = _one(pg, "SELECT * FROM transactions")
    assert (t["user_context"], t["skipped"], t["enriched_at"], t["llm_category"]) == (None, False, None, None)


def test_the_latest_context_wins_and_requests_dont_count_as_context(pg):
    _txn(pg, "tx1")
    _context(pg, "tx1", "first")
    _context(pg, "tx1", "second", source="dashboard")
    _context(pg, "tx1", source="request")
    t = _one(pg, "SELECT user_context, skipped FROM transactions")
    assert t == {"user_context": "second", "skipped": False}


def test_a_corrected_date_replaces_monzos(pg):
    _txn(pg, "tx1", created="2026-10-01T12:00:00Z")
    _context(pg, "tx1", "x", source="dashboard", override=datetime(2026, 9, 30, 9, 0))
    assert _one(pg, "SELECT created_at FROM transactions")["created_at"] == datetime(2026, 9, 30, 9, 0)


def test_the_latest_label_wins_and_a_parent_only_label_shows_as_such(pg):
    _taxonomy(pg)
    _txn(pg, "tx1")
    _label(pg, "tx1", 1, 1, confidence=0.8)
    _label(pg, "tx1", 1, 2, source="manual", model=None, confidence=None)
    t = _one(pg, "SELECT llm_category, llm_subcategory, llm_model FROM transactions")
    assert t == {"llm_category": "Food", "llm_subcategory": "Takeaway", "llm_model": None}
    _label(pg, "tx1", 1, None, source="manual", model=None, confidence=None)
    t = _one(pg, "SELECT llm_category, llm_subcategory FROM transactions")
    assert t == {"llm_category": "Food", "llm_subcategory": None}


def test_an_archived_category_still_names_its_old_labels_and_its_name_can_be_reused(pg):
    _taxonomy(pg)
    _txn(pg, "tx1")
    _label(pg, "tx1", 1, 1)
    pg.execute("UPDATE subcategories SET archived_at = '2026-10-05' WHERE id = 1")
    assert _one(pg, "SELECT llm_subcategory FROM transactions")["llm_subcategory"] == "Groceries"
    pg.execute("INSERT INTO subcategories VALUES (7, 'Groceries', 1, '2026-10-06')")


def test_needs_context_counts_follow_ups_until_there_is_an_answer(pg):
    _txn(pg, "tx1")
    assert _one(pg, "SELECT request_count FROM needs_context")["request_count"] == 0
    _context(pg, "tx1", source="request")
    _context(pg, "tx1", source="request")
    assert _one(pg, "SELECT request_count FROM needs_context")["request_count"] == 2
    _context(pg, "tx1", source="timeout")
    assert _one(pg, "SELECT * FROM needs_context") is None


def test_needs_label_is_the_classifiers_to_do_list(pg):
    _taxonomy(pg)
    for txn_id in ["waiting", "ready", "skipped", "done", "parent_only", "held"]:
        _txn(pg, txn_id)
    for txn_id in ["ready", "done", "parent_only", "held"]:
        _context(pg, txn_id, "ctx")
    _context(pg, "skipped", skipped=True)
    _label(pg, "done", 1, 1)
    _label(pg, "parent_only", 1, None)
    pg.execute(
        "INSERT INTO category_proposals (id, options, txn_ids, proposed_at) "
        "VALUES (1, '[]', '[\"held\"]', '2026-10-01')"
    )
    todo = {r[0] for r in pg.execute("SELECT id FROM needs_label").fetchall()}
    assert todo == {"ready", "parent_only"}
    assert _one(pg, "SELECT pending_category_proposal_id FROM transactions WHERE id = 'held'") == \
        {"pending_category_proposal_id": 1}

    pg.execute("UPDATE category_proposals SET status = 'denied'")
    todo = {r[0] for r in pg.execute("SELECT id FROM needs_label").fetchall()}
    assert todo == {"ready", "parent_only", "held"}


def test_transaction_roles_gives_the_same_answers_as_today(db, pg):
    """The same transactions, set up the old way in DuckDB and the new way in
    Postgres, must get the same role, for every way a role can be decided."""
    cases = [
        # (id, amount, parent_id, subcategory_id)
        ("sub_override", -5, 1, 1),        # subcategory role set: saving
        ("parent_override", -5, 2, 3),     # parent role set: excluded
        ("default_income", 900, 3, 4),     # built-in default by name
        ("default_invest", -50, 4, 5),
        ("default_transfer", -10, 5, 6),
        ("plain_spend", -3, 1, 2),         # classified, no role: spend
        ("unclassified_in", 7, None, None),
        ("unclassified_out", -7, None, None),
    ]
    parents = {1: "Food", 2: "Fun", 3: "Income", 4: "Investments", 5: "Transfers"}
    subs = {1: ("Groceries", 1), 2: ("Takeaway", 1), 3: ("Gigs", 2), 4: ("Salary", 3), 5: ("ISA", 4), 6: ("Pot", 5)}

    _taxonomy(pg)
    for pid, name in parents.items():
        db.execute("INSERT INTO parent_categories VALUES (?, ?, '2026-01-01')", [pid, name])
    for sid, (name, parent) in subs.items():
        db.execute("INSERT INTO subcategories VALUES (?, ?, ?, '2026-01-01')", [sid, name, parent])
    for con in (db, pg):
        con.execute("INSERT INTO category_roles VALUES (1, 1, 'saving')")
        con.execute("INSERT INTO category_roles VALUES (2, NULL, 'excluded')")

    for txn_id, amount, parent_id, sub_id in cases:
        category = parents.get(parent_id)
        subcategory = subs[sub_id][0] if sub_id else None
        db.execute(
            "INSERT INTO transactions (id, amount, currency, llm_category, llm_subcategory) "
            "VALUES (?, ?, 'GBP', ?, ?)",
            [txn_id, Decimal(amount), category, subcategory],
        )
        _txn(pg, txn_id, amount=amount * 100)
        if parent_id:
            _label(pg, txn_id, parent_id, sub_id)

    query = "SELECT id, role FROM transaction_roles ORDER BY id"
    old = db.execute(query).fetchall()
    assert pg.execute(query).fetchall() == old
    assert dict(old)["sub_override"] == "saving"
    assert dict(old)["parent_override"] == "excluded"


def test_quick_categories_are_the_top_overall_and_each_merchants_top_three(pg):
    _taxonomy(pg)
    n = 0

    def labelled(merchant, sub_id, times, counterparty=None):
        nonlocal n
        for _ in range(times):
            n += 1
            _txn(pg, f"t{n}", merchant=merchant, counterparty=counterparty)
            parent = {1: 1, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5}[sub_id]
            _label(pg, f"t{n}", parent, sub_id)

    labelled("Tesco", 1, 5)          # Groceries x5
    labelled("Tesco", 2, 2)          # Takeaway x2
    labelled("Tesco", 3, 1)
    labelled("Tesco", 6, 1)          # a fourth category at Tesco: not in its top 3
    labelled(None, 4, 3, counterparty="ACME LTD")  # a direct debit: keyed by counterparty

    rows = pg.execute(
        "SELECT merchant_name, subcategory, rank FROM quick_categories ORDER BY merchant_name NULLS FIRST, rank"
    ).fetchall()
    overall = [(s, r) for m, s, r in rows if m is None]
    assert overall[:2] == [("Groceries", 0), ("Salary", 1)]
    assert [(m, s, r) for m, s, r in rows if m == "ACME LTD"] == [("ACME LTD", "Salary", 0)]
    tesco = [(s, r) for m, s, r in rows if m == "Tesco"]
    assert tesco[:2] == [("Groceries", 0), ("Takeaway", 1)]
    assert len(tesco) == 3
