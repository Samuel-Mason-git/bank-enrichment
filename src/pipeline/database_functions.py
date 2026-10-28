"""The pipeline's and the dashboard's side of the one Postgres database (the
data box's `vault`, schema `bank_enrichment` -- see docs/server-move.md).

Transactions are read through the `transactions` view, under the column names
the old table had. Writes to a transaction are history, not updates: a label is
a new label_events row, a corrected date or context a new context_events row."""
import json
import logging
import os
import shutil
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg
from dotenv import load_dotenv
from psycopg.types.json import Jsonb

load_dotenv(Path(__file__).parent.parent.parent / "config" / ".env")

log = logging.getLogger(__name__)

_con: psycopg.Connection | None = None
_read_only = False

# What "a day" means to the dashboards and summaries (the views show London time).
LONDON = ZoneInfo("Europe/London")

# What a parent category counts as when no role has been set for it -- the same
# defaults the transaction_roles view uses (sql/postgres/schema.sql).
_DEFAULT_ROLE = """CASE p.name WHEN 'Income' THEN 'income'
                               WHEN 'Investments' THEN 'investment'
                               WHEN 'Transfers' THEN 'transfer' END"""

# Each labelled transaction's current label, as a CTE body: its newest label
# event, with the parent its subcategory has now -- exactly what the
# transactions view gives as parent_id/subcategory_id. For counting what each
# category holds, it reads only label_events, where joining the view rebuilt
# every transaction's current version first (seconds, per query).
CURRENT_LABELS = """SELECT DISTINCT ON (l.txn_id) l.txn_id, l.subcategory_id,
                          COALESCE(sc.parent_id, l.parent_id) AS parent_id
                   FROM label_events l
                   LEFT JOIN subcategories sc ON sc.id = l.subcategory_id
                   ORDER BY l.txn_id, l.event_id DESC"""


def log_path(filename: str) -> str:
    """Where a script's log file goes: LOG_DIR, or logs/ in the repo."""
    folder = Path(os.getenv("LOG_DIR") or Path(__file__).parent.parent.parent / "logs")
    folder.mkdir(parents=True, exist_ok=True)
    return str(folder / filename)


# ── Connection ────────────────────────────────────────────────────────────────

def _connect(read_only: bool = False) -> psycopg.Connection:
    """The login's own schema is first on its search path, so unqualified table
    names are bank_enrichment's. Each statement commits on its own, as the
    server's do."""
    e = os.environ
    con = psycopg.connect(
        host=e["DB_HOST"], port=e.get("DB_PORT", "5432"), dbname=e["DB_NAME"],
        user=e["DB_USER"], password=e["DB_PASSWORD"], connect_timeout=10, autocommit=True,
        # Times are read and written in UTC whatever the server's own setting.
        options="-c TimeZone=UTC",
    )
    if read_only:
        con.execute("SET default_transaction_read_only = on")
    return con


def get_con() -> psycopg.Connection:
    """The connection, made again if it has dropped (the VPN blinking, the data
    box restarting) -- a long-lived dashboard would otherwise fail every query
    until restarted."""
    global _con
    if _con is None:
        raise RuntimeError("Database not initialised — call init_db() first")
    if _con.closed or _con.broken:
        log.warning("Database connection lost -- reconnecting")
        _con = _connect(_read_only)
    return _con


def init_db(read_only: bool = False) -> None:
    """Connect to Postgres. The tables themselves are made by the server (it
    applies sql/postgres/schema.sql on every start). read_only refuses every
    write, for looking without touching."""
    global _con, _read_only
    if _con is not None:
        return
    _read_only = read_only
    _con = _connect(read_only)
    if not read_only:
        _seed_taxonomy()


_DEFAULT_TAXONOMY = {
    "Bills & Utilities": [
        "Council Tax", "Electricity", "Gas", "Insurance", "Internet",
        "Mobile Phone", "Rent", "Water",
    ],
    "Career & Learning": [
        "Books", "Certifications", "Online Learning",
        "Professional Memberships", "Software Tools",
    ],
    "Entertainment": [
        "Cinema", "Events", "Hobbies", "Streaming", "Video Games",
    ],
    "Food & Drink": [
        "Bars & Pubs", "Coffee Shops", "Groceries", "Lunches Out",
        "Restaurants", "Snacks", "Soft Drinks", "Takeaway",
    ],
    "Health": [
        "Dental", "GP / Medical", "Optical & Vision Care", "Pharmacy & Medication",
    ],
    "Holidays": [
        "Accommodation", "Car Rental", "Holiday Drinks",
        "Holiday Food", "Holiday Shopping", "Local Transport",
    ],
    "Income": [
        "Dividends", "Gifts Received", "Interest", "Other Income",
        "Property Income", "Refunds", "Salary",
    ],
    "Investments": [
        "Cash ISA Contributions", "Crypto Purchases",
        "General Investment Account Contributions", "Pension Contributions",
        "Savings Deposits", "Stocks & Shares ISA Contributions",
    ],
    "Personal Care & Consumables": [
        "Haircuts", "Personal Care & Hygiene", "Tobacco & Nicotine", "Toiletries",
    ],
    "Shopping": [
        "Clothing", "Electronics", "General Retail", "Gifts",
        "Home Goods", "Stationery & Office", "Technology",
    ],
    "Subscriptions & Software": [
        "AI Tools", "Cloud Storage", "News & Publications", "Streaming",
    ],
    "Transfers": [
        "Inbound Transfer", "Outbound Transfer",
    ],
    "Transport": [
        "Car Insurance", "Car Maintenance", "Fuel", "Parking",
        "Public Transport", "Taxi & Rideshare", "Vehicle Tax",
    ],
}


def _seed_taxonomy() -> None:
    """Insert the default taxonomy, but only when there are no live categories
    at all -- a new install, or after the taxonomy has been wiped. Once any
    live parent category exists (default or custom), seeding is skipped, so a
    taxonomy you've rebuilt your own way never gets the defaults added back
    alongside it."""
    con = get_con()
    if con.execute("SELECT COUNT(*) FROM parent_categories WHERE archived_at IS NULL").fetchone()[0] > 0:
        return
    for parent_name, subs in _DEFAULT_TAXONOMY.items():
        parent_id = upsert_parent(parent_name)
        for sub_name in subs:
            upsert_subcategory(sub_name, parent_id)

    # Refunds are money you already spent coming back, not new income — excluded
    # from Income totals by default (overridable in the dashboard's Settings tab).
    refunds = con.execute(
        """SELECT s.id, s.parent_id FROM subcategories s
           JOIN parent_categories p ON p.id = s.parent_id
           WHERE s.name = 'Refunds' AND p.name = 'Income'
             AND s.archived_at IS NULL AND p.archived_at IS NULL"""
    ).fetchone()
    if refunds:
        refunds_id, income_parent_id = refunds
        con.execute(
            "INSERT INTO category_roles (parent_id, subcategory_id, role) VALUES (%s, %s, 'excluded')",
            [income_parent_id, refunds_id]
        )


# ── Fetch ─────────────────────────────────────────────────────────────────────

def get_all_transactions() -> list[dict]:
    return _rows("SELECT * FROM transactions ORDER BY created_at DESC")


def get_transaction(transaction_id: str) -> dict | None:
    rows = _rows("SELECT * FROM transactions WHERE id = %s", [transaction_id])
    return rows[0] if rows else None


def get_unclassified() -> list[dict]:
    """The classifier's to-do list: answered, not skipped, not fully labelled,
    and not held back behind a category proposal awaiting a Telegram decision
    (otherwise every run would re-submit those to the LLM and risk a duplicate
    proposal and card)."""
    return _rows("SELECT * FROM needs_label ORDER BY created_at DESC")


def get_parents() -> list[dict]:
    """Live parent categories, with how many transactions each holds."""
    return _rows(
        f"""WITH labels AS ({CURRENT_LABELS})
           SELECT p.id, p.name, p.created_at,
                  COUNT(t.txn_id) AS transaction_count,
                  COALESCE(pr.role, {_DEFAULT_ROLE}, 'spend') AS role
           FROM parent_categories p
           LEFT JOIN labels t ON t.parent_id = p.id
           LEFT JOIN category_roles pr ON pr.parent_id = p.id AND pr.subcategory_id IS NULL
           WHERE p.archived_at IS NULL
           GROUP BY p.id, p.name, p.created_at, pr.role
           ORDER BY transaction_count DESC, p.name ASC"""
    )


def get_subcategories() -> list[dict]:
    """Live subcategories, with how many transactions each holds."""
    return _rows(
        f"""WITH labels AS ({CURRENT_LABELS})
           SELECT s.id, s.name, s.parent_id, p.name AS parent_name, s.created_at,
                  COUNT(t.txn_id) AS transaction_count,
                  sr.role AS role_override,
                  COALESCE(pr.role, {_DEFAULT_ROLE}, 'spend') AS parent_role
           FROM subcategories s
           JOIN parent_categories p ON p.id = s.parent_id
           LEFT JOIN labels t ON t.subcategory_id = s.id
           LEFT JOIN category_roles sr ON sr.subcategory_id = s.id
           LEFT JOIN category_roles pr ON pr.parent_id = p.id AND pr.subcategory_id IS NULL
           WHERE s.archived_at IS NULL AND p.archived_at IS NULL
           GROUP BY s.id, s.name, s.parent_id, p.name, s.created_at, sr.role, pr.role
           ORDER BY p.name ASC, transaction_count DESC, s.name ASC"""
    )


def search(term: str) -> list[dict]:
    like = f"%{term}%"
    return _rows(
        """SELECT * FROM transactions
           WHERE description ILIKE %s
           OR user_context ILIKE %s
           OR merchant_name ILIKE %s
           OR counterparty_name ILIKE %s
           ORDER BY created_at DESC""",
        [like, like, like, like]
    )


# ── Stats ─────────────────────────────────────────────────────────────────────

def get_stats() -> dict:
    con = get_con()
    total = con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    total_spend = con.execute(
        "SELECT SUM(amount) FROM transactions WHERE amount < 0"
    ).fetchone()[0] or 0
    total_income = con.execute(
        "SELECT SUM(amount) FROM transactions WHERE amount > 0"
    ).fetchone()[0] or 0
    unclassified = con.execute(
        "SELECT COUNT(*) FROM transactions WHERE llm_category IS NULL AND skipped = FALSE"
    ).fetchone()[0]
    skipped = con.execute(
        "SELECT COUNT(*) FROM transactions WHERE skipped = TRUE"
    ).fetchone()[0]
    by_category = con.execute(
        """SELECT llm_category, COUNT(*), SUM(amount)
           FROM transactions
           WHERE llm_category IS NOT NULL
           GROUP BY llm_category
           ORDER BY SUM(amount) ASC"""
    ).fetchall()
    return {
        "total": total,
        "total_spend": abs(total_spend),
        "total_income": total_income,
        "unclassified": unclassified,
        "skipped": skipped,
        "by_category": [
            {"category": r[0], "count": r[1], "total": r[2]}
            for r in by_category
        ],
    }


# ── Writing history ───────────────────────────────────────────────────────────

def now() -> datetime:
    """The time now, in UTC -- every time is stored zone-aware."""
    return datetime.now(timezone.utc)


_now = now


# What each kind of classification is recorded as. Anything else came from the
# classifier itself.
_SOURCE_BY_MODEL = {
    "quick-tap": "quick_tap", "rule": "rule", "category-proposal": "proposal",
    "taxonomy-proposal": "proposal", "manual": "manual", "judge": "judge",
}


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


def label_ids(category: str | None, subcategory: str | None) -> tuple[int | None, int | None]:
    """(parent id, subcategory id) of a live category pair, by name. Either is
    None if it doesn't exist (or the name was None)."""
    con = get_con()
    if category is None:
        return None, None
    parent = con.execute(
        "SELECT id FROM parent_categories WHERE name = %s AND archived_at IS NULL", [category]
    ).fetchone()
    if not parent:
        return None, None
    if subcategory is None:
        return parent[0], None
    sub = con.execute(
        "SELECT id FROM subcategories WHERE name = %s AND parent_id = %s AND archived_at IS NULL",
        [subcategory, parent[0]],
    ).fetchone()
    return parent[0], (sub[0] if sub else None)


def release_holds(transaction_ids: list[str]) -> None:
    """Take these transactions out of whatever category proposal is holding
    them back from the classifier."""
    if not transaction_ids:
        return
    con = get_con()
    wanted = set(transaction_ids)
    for proposal_id, held in con.execute(
        "SELECT id, txn_ids FROM category_proposals WHERE txn_ids <> '[]'"
    ).fetchall():
        kept = [t for t in held if t not in wanted]
        if len(kept) != len(held):
            con.execute("UPDATE category_proposals SET txn_ids = %s WHERE id = %s", [Jsonb(kept), proposal_id])


def update_classification(
    transaction_id: str,
    category: str | None,
    subcategory: str | None,
    confidence: float | None,
    model: str,
    source: str | None = None,
) -> None:
    """A new label for a transaction. Names that aren't (or are no longer) in
    the taxonomy can't be recorded against an id, so they raise rather than
    file the transaction somewhere that doesn't exist. A transaction that has
    just been classified -- by any path -- is never still waiting on a
    category decision, so any hold on it is released too."""
    parent_id, subcategory_id = label_ids(category, subcategory)
    if category is not None and (parent_id is None or (subcategory is not None and subcategory_id is None)):
        raise ValueError(f"{category!r} / {subcategory!r} isn't in the taxonomy")
    get_con().execute(
        """INSERT INTO label_events (txn_id, parent_id, subcategory_id, source, model, confidence, set_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
        [transaction_id, parent_id, subcategory_id, source or _SOURCE_BY_MODEL.get(model, "llm"),
         model, confidence, _now()],
    )
    release_holds([transaction_id])


def clear_label(transaction_id: str, keep_parent: bool = False) -> None:
    """Take a transaction's label away (or just its subcategory), as deleting a
    category does."""
    t = get_transaction(transaction_id)
    parent_id = t["parent_id"] if (t and keep_parent) else None
    get_con().execute(
        "INSERT INTO label_events (txn_id, parent_id, subcategory_id, source, set_at) VALUES (%s, %s, NULL, 'manual', %s)",
        [transaction_id, parent_id, _now()],
    )


def update_transaction_details(transaction_id: str, created_at, user_context: str | None) -> None:
    """A corrected date and/or context from the dashboard, as a new context
    event. The date is kept as an override only if it differs from Monzo's
    own, so Monzo's stays in the payload untouched."""
    con = get_con()
    current = con.execute(
        """SELECT skipped, raw_payload -> 'data' ->> 'created'
           FROM transactions WHERE id = %s""",
        [transaction_id],
    ).fetchone()
    if not current:
        raise ValueError(f"No transaction {transaction_id}")
    skipped, monzo_created = current
    override = None
    if created_at:
        # A date typed in the dashboard is London time, as the dashboard shows it.
        wanted = created_at if isinstance(created_at, datetime) else datetime.fromisoformat(str(created_at))
        if wanted.tzinfo is None:
            wanted = wanted.replace(tzinfo=LONDON)
        monzo = datetime.fromisoformat(monzo_created.replace("Z", "+00:00")) if monzo_created else None
        if monzo is None or wanted != monzo:
            override = wanted
    con.execute(
        """INSERT INTO context_events (txn_id, user_context, skipped, created_at_override, source, set_at)
           VALUES (%s, %s, %s, %s, 'dashboard', %s)""",
        [transaction_id, user_context, bool(skipped), override, _now()],
    )


def apply_quick_tap_classifications(review=None) -> int:
    """Classify transactions whose user_context exactly matches an existing
    'Category - Subcategory' pair (written by a Telegram quick-tap button),
    skipping the LLM entirely for those. Returns the number classified.

    review, if given, is called as review(transaction, category, subcategory)
    before each tap is applied and returns True when it has taken the
    transaction over -- the placement judge does this when it thinks a tap was a
    mis-tap, holding the transaction behind a Telegram card. It is then left
    unclassified here, and excluded from later runs by the hold rather than
    matched again."""
    con = get_con()
    taxonomy = {
        f"{parent} - {sub}": (parent, sub)
        for parent, sub in con.execute(
            """SELECT p.name, s.name FROM subcategories s
               JOIN parent_categories p ON p.id = s.parent_id
               WHERE s.archived_at IS NULL AND p.archived_at IS NULL"""
        ).fetchall()
    }
    unclassified = _rows("SELECT * FROM needs_label WHERE user_context IS NOT NULL")
    count = 0
    for txn in unclassified:
        match = taxonomy.get(txn["user_context"])
        if match:
            category, subcategory = match
            if review is not None and review(txn, category, subcategory):
                continue
            update_classification(txn["id"], category, subcategory, confidence=1.0, model="quick-tap")
            count += 1
    return count


# ── Subcategory profiles (context for the classifier and the placement judge) ─

def get_subcategory_stats() -> dict[tuple[str, str], dict]:
    """How big and how regular the transactions already in each subcategory
    are, keyed by (parent name, subcategory name): count, min/median/max
    absolute amount, and the median number of days between transactions once
    there are enough of them for that to mean anything.

    This is what lets a £282 one-off be seen as not belonging next to £1,315
    monthly rent -- the names alone say nothing about either."""
    rows = get_con().execute(
        """SELECT llm_category, llm_subcategory, ABS(amount::float8), created_at
           FROM transactions
           WHERE llm_category IS NOT NULL AND llm_subcategory IS NOT NULL"""
    ).fetchall()
    grouped: dict[tuple[str, str], list] = {}
    for category, subcategory, amount, created_at in rows:
        grouped.setdefault((category, subcategory), []).append((amount, created_at))
    stats = {}
    for key, items in grouped.items():
        amounts = [a for a, _ in items]
        dates = sorted(d for _, d in items if d)
        gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
        stats[key] = {
            "count": len(items),
            "min": min(amounts),
            "median": statistics.median(amounts),
            "max": max(amounts),
            "gap_days": statistics.median(gaps) if len(items) >= 3 and gaps else None,
        }
    return stats


def format_subcategory_stats(stat: dict) -> str:
    if stat["min"] == stat["max"]:
        amounts = f"always £{stat['min']:.2f}"
    else:
        amounts = f"£{stat['min']:.2f}-£{stat['max']:.2f}, median £{stat['median']:.2f}"
    if stat["gap_days"] is not None:
        rhythm = f"typically one every {stat['gap_days']:.0f} day(s)"
    else:
        rhythm = f"only {stat['count']} so far, no clear rhythm"
    return f"amounts {amounts}; {rhythm}"


def get_subcategory_examples(category: str, subcategory: str, limit: int = 6) -> list[dict]:
    return _rows(
        """SELECT amount, created_at, merchant_name, counterparty_name, description, user_context
           FROM transactions
           WHERE llm_category = %s AND llm_subcategory = %s
           ORDER BY created_at DESC LIMIT %s""",
        [category, subcategory, limit],
    )


def get_merchant_history(merchant_key: str, exclude_id: str) -> list[dict]:
    """Every other classified transaction from the same merchant or
    counterparty -- evidence both that something recurs and of where it has
    been put before."""
    return _rows(
        """SELECT llm_category, llm_subcategory, ABS(amount::float8) AS amount
           FROM transactions
           WHERE id != %s AND llm_category IS NOT NULL AND llm_subcategory IS NOT NULL
           AND (LOWER(TRIM(merchant_name)) = %s OR LOWER(TRIM(counterparty_name)) = %s)
           ORDER BY created_at, id""",
        [exclude_id, merchant_key, merchant_key],
    )


def upsert_parent(name: str) -> int:
    """Get or create a live parent category by name. Returns its id."""
    con = get_con()
    row = con.execute(
        "SELECT id FROM parent_categories WHERE name = %s AND archived_at IS NULL", [name]
    ).fetchone()
    if row:
        return row[0]
    return insert_with_next_id(
        con,
        "INSERT INTO parent_categories (id, name, created_at) VALUES ((SELECT COALESCE(MAX(id), 0) + 1 FROM parent_categories), %s, %s) RETURNING id",
        [name, _now()], "parent_categories",
    )


def upsert_subcategory(name: str, parent_id: int) -> int:
    """Get or create a live subcategory by name + parent_id. Returns its id."""
    con = get_con()
    row = con.execute(
        "SELECT id FROM subcategories WHERE name = %s AND parent_id = %s AND archived_at IS NULL",
        [name, parent_id]
    ).fetchone()
    if row:
        return row[0]
    return insert_with_next_id(
        con,
        "INSERT INTO subcategories (id, name, parent_id, created_at) VALUES ((SELECT COALESCE(MAX(id), 0) + 1 FROM subcategories), %s, %s, %s) RETURNING id",
        [name, parent_id, _now()], "subcategories",
    )


# ── Editing the taxonomy (the dashboard's Settings tab) ───────────────────────
#
# Labels point at ids, so renaming or moving a category is one row and every
# transaction follows. A deleted category is archived rather than removed --
# old labels in the history still name it -- and the transactions it held get
# a "cleared" label, so the change is itself part of the history.

def _clear_labels_where(condition: str, params: list, keep_parent: bool = False) -> int:
    """A cleared label (or, with keep_parent, a parent-only one) for every
    labelled transaction matching `condition` on the transactions view."""
    con = get_con()
    count = con.execute(f"SELECT COUNT(*) FROM transactions WHERE {condition}", params).fetchone()[0]
    con.execute(
        f"""INSERT INTO label_events (txn_id, parent_id, subcategory_id, source, set_at)
            SELECT id, {'parent_id' if keep_parent else 'NULL'}, NULL, 'manual', %s
            FROM transactions WHERE {condition} ORDER BY id""",
        [_now(), *params],
    )
    return count


def rename_parent(parent_id: int, name: str) -> None:
    get_con().execute("UPDATE parent_categories SET name = %s WHERE id = %s", [name, parent_id])


def wipe_parent_labels(parent_id: int) -> int:
    """Clear the labels of every transaction under a parent, so they're
    classified again. The categories themselves stay."""
    return _clear_labels_where("parent_id = %s", [parent_id])


def delete_parent(parent_id: int) -> int:
    """Archive a parent and all its subcategories, and clear the labels of the
    transactions under them. Their role overrides go too. Returns how many
    transactions were cleared."""
    con = get_con()
    cleared = _clear_labels_where("parent_id = %s", [parent_id])
    con.execute(
        "DELETE FROM category_roles WHERE parent_id = %s OR subcategory_id IN "
        "(SELECT id FROM subcategories WHERE parent_id = %s)", [parent_id, parent_id]
    )
    con.execute("UPDATE subcategories SET archived_at = %s WHERE parent_id = %s AND archived_at IS NULL",
                [_now(), parent_id])
    con.execute("UPDATE parent_categories SET archived_at = %s WHERE id = %s", [_now(), parent_id])
    return cleared


def delete_subcategory(subcategory_id: int) -> int:
    """Archive a subcategory. Its transactions keep their parent but lose the
    subcategory, as deleting one always has. Returns how many were affected."""
    con = get_con()
    cleared = _clear_labels_where("subcategory_id = %s", [subcategory_id], keep_parent=True)
    con.execute("DELETE FROM category_roles WHERE subcategory_id = %s", [subcategory_id])
    con.execute("UPDATE subcategories SET archived_at = %s WHERE id = %s", [_now(), subcategory_id])
    return cleared


def edit_subcategory(subcategory_id: int, name: str, parent_id: int) -> None:
    """Rename a subcategory and/or move it under another parent. Its
    transactions follow on their own (the view takes a label's parent from its
    subcategory), and so does any role override."""
    con = get_con()
    con.execute("UPDATE subcategories SET name = %s, parent_id = %s WHERE id = %s", [name, parent_id, subcategory_id])
    con.execute("UPDATE category_roles SET parent_id = %s WHERE subcategory_id = %s", [parent_id, subcategory_id])


def move_all_transactions(from_subcategory_id: int, to_subcategory_id: int) -> int:
    """Relabel every transaction in one subcategory into another. Returns how many moved."""
    con = get_con()
    parent_id = con.execute("SELECT parent_id FROM subcategories WHERE id = %s", [to_subcategory_id]).fetchone()[0]
    count = con.execute("SELECT COUNT(*) FROM transactions WHERE subcategory_id = %s",
                        [from_subcategory_id]).fetchone()[0]
    con.execute(
        """INSERT INTO label_events (txn_id, parent_id, subcategory_id, source, model, set_at)
           SELECT id, %s, %s, 'manual', 'manual', %s FROM transactions WHERE subcategory_id = %s ORDER BY id""",
        [parent_id, to_subcategory_id, _now(), from_subcategory_id],
    )
    return count


def wipe_taxonomy() -> int:
    """Clear every label and archive every category, for starting the taxonomy
    again from scratch. The default one is seeded on the next start. Returns
    how many transactions were cleared."""
    con = get_con()
    cleared = _clear_labels_where("llm_category IS NOT NULL", [])
    con.execute("DELETE FROM category_roles")
    con.execute("UPDATE subcategories SET archived_at = %s WHERE archived_at IS NULL", [_now()])
    con.execute("UPDATE parent_categories SET archived_at = %s WHERE archived_at IS NULL", [_now()])
    return cleared


ROLES = ("income", "spend", "investment", "saving", "transfer", "excluded")


def _validate_role(role: str) -> None:
    if role not in ROLES:
        raise ValueError(f"Invalid role {role!r} — must be one of {ROLES}")


def get_totals_by_role(date_from: str, date_to: str) -> dict[str, float]:
    """Directional totals per role in [date_from, date_to]. Always returns all
    of ROLES as keys (0.0 default) so callers never need a .get() guard.

    income sums its positive (incoming) side. "saving"/"investment" net their
    two sides together (a deposit and a later withdrawal are the same pot or
    account, so £500 in then £200 back out is genuinely £300 net saved/
    invested, not two unrelated amounts). Every other role -- "spend", and
    bidirectional ones like "transfer" (an Inbound and an Outbound Transfer
    subcategory can both carry that role) -- sums only its negative (outgoing)
    side as a positive magnitude: those can mix genuinely unrelated inbound and
    outbound movements (a refund in, an unrelated bill out), so summing every
    sign together would let them cancel into a number that looks meaningful
    but isn't (e.g. -674 + 540 nets to -134 instead of reporting the true £674
    that actually went out)."""
    con = get_con()
    rows = con.execute(
        """SELECT role, COALESCE(SUM(
               CASE WHEN role = 'income' THEN GREATEST(amount, 0)
                    WHEN role IN ('saving', 'investment') THEN -amount
                    ELSE GREATEST(-amount, 0) END
           ), 0) FROM transaction_roles
           WHERE skipped = FALSE AND created_at BETWEEN %s AND %s
           GROUP BY role""",
        [date_from, date_to]
    ).fetchall()
    totals = {r[0]: float(r[1]) for r in rows}
    for role in ROLES:
        totals.setdefault(role, 0.0)
    return totals


def get_category_totals_by_role(role: str, date_from: str, date_to: str) -> list[dict]:
    """Per-category breakdown for one role, e.g. "Spend by category". Only
    includes classified transactions -- unclassified ones surface separately
    as their own "Unclassified" bucket wherever this is displayed."""
    _validate_role(role)
    return _rows(
        """SELECT llm_category AS category, SUM(amount) AS total
           FROM transaction_roles
           WHERE role = %s AND llm_category IS NOT NULL
             AND skipped = FALSE AND created_at BETWEEN %s AND %s
           GROUP BY llm_category
           ORDER BY total DESC""",
        [role, date_from, date_to]
    )


def set_parent_role(parent_id: int, role: str) -> None:
    """Set the role for an entire parent category (its subcategories inherit
    this unless they have their own override)."""
    _validate_role(role)
    con = get_con()
    con.execute("DELETE FROM category_roles WHERE parent_id = %s AND subcategory_id IS NULL", [parent_id])
    con.execute(
        "INSERT INTO category_roles (parent_id, subcategory_id, role) VALUES (%s, NULL, %s)",
        [parent_id, role]
    )


def set_subcategory_role(sub_id: int, role: str | None) -> None:
    """Set a role override for one subcategory, or clear it (role=None) to
    fall back to inheriting its parent's role."""
    con = get_con()
    con.execute("DELETE FROM category_roles WHERE subcategory_id = %s", [sub_id])
    if role is not None:
        _validate_role(role)
        parent_id = con.execute("SELECT parent_id FROM subcategories WHERE id = %s", [sub_id]).fetchone()[0]
        con.execute(
            "INSERT INTO category_roles (parent_id, subcategory_id, role) VALUES (%s, %s, %s)",
            [parent_id, sub_id, role]
        )


def get_subscriptions() -> list[dict]:
    return _rows("SELECT * FROM subscriptions ORDER BY active DESC, name ASC")


def upsert_subscription(name: str, amount: float, frequency: str, merchant_name: str | None = None) -> int:
    con = get_con()
    return insert_with_next_id(
        con,
        """INSERT INTO subscriptions (id, name, merchant_name, amount, frequency, active, created_at)
           VALUES ((SELECT COALESCE(MAX(id), 0) + 1 FROM subscriptions), %s, %s, %s, %s, TRUE, %s) RETURNING id""",
        [name, merchant_name, amount, frequency, _now()], "subscriptions",
    )


def update_subscription(sub_id: int, name: str, amount: float, frequency: str, merchant_name: str | None = None) -> None:
    get_con().execute(
        "UPDATE subscriptions SET name = %s, merchant_name = %s, amount = %s, frequency = %s WHERE id = %s",
        [name, merchant_name, amount, frequency, sub_id]
    )


def toggle_subscription(sub_id: int) -> None:
    get_con().execute("UPDATE subscriptions SET active = NOT active WHERE id = %s", [sub_id])


def delete_subscription(sub_id: int) -> None:
    get_con().execute("DELETE FROM subscriptions WHERE id = %s", [sub_id])


# ── "Back up now" ─────────────────────────────────────────────────────────────
#
# Looking after the data is the database's job, not the app's (the data box
# backs it up every night). This is only a manual "save a copy of everything
# before I try something", from the Settings tab: every table as a Parquet
# file in a folder of its own, under BACKUP_DIR. It is never run automatically.

BACKUPS_KEPT = 10
MANIFEST_NAME = "manifest.json"

# Every table there is, so a backup is the whole of it. The views are left out:
# they store nothing, and come back with sql/postgres/schema.sql.
BACKUP_TABLES = [
    "transactions_raw", "transaction_updates", "context_events", "label_events",
    "parent_categories", "subcategories", "category_roles",
    "rules", "rule_suggestions", "taxonomy_proposals", "category_proposals",
    "judge_reviews", "subscriptions", "summaries",
]


def backup_dir() -> Path:
    return Path(os.getenv("BACKUP_DIR") or Path.home() / "bank-enrichment-backups")


def list_backups() -> list[Path]:
    """Newest first. Names sort chronologically, so no stat() call is needed."""
    root = backup_dir()
    if not root.is_dir():
        return []
    return sorted((p for p in root.iterdir() if p.is_dir()), reverse=True)


def backup_db(reason: str = "manual") -> Path:
    """Save every table to backups/<timestamp>_<reason>/ as Parquet, keeping
    the most recent BACKUPS_KEPT. Readable by DuckDB, pandas or anything else.
    JSON columns are saved as their JSON text."""
    import pandas as pd  # only the dashboard backs up; the pipeline never loads pandas
    con = get_con()
    # ':' is not a legal filename character on Windows, so this is a flattened
    # timestamp rather than an ISO one.
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe_reason = "".join(c if c.isalnum() or c in "-_" else "-" for c in reason)[:40]
    target = backup_dir() / f"{stamp}_{safe_reason}"
    suffix = 1
    while target.exists():
        suffix += 1
        target = backup_dir() / f"{stamp}_{safe_reason}-{suffix}"
    target.mkdir(parents=True)
    for table in BACKUP_TABLES:
        cur = con.execute(f"SELECT * FROM {table}")
        columns = [c.name for c in cur.description]
        rows = [[json.dumps(v) if isinstance(v, (dict, list)) else v for v in row] for row in cur.fetchall()]
        pd.DataFrame(rows, columns=columns, dtype=object).to_parquet(target / f"{table}.parquet", index=False)
    _write_manifest(target, reason)
    log.info(f"Backed up database to {target}")
    _prune_backups()
    return target


def _write_manifest(target: Path, reason: str) -> None:
    """Record what the backup actually contains, so the Settings tab can say
    'this one holds 1,240 transactions covering 2025-01 to 2026-08' without
    opening every file to count. A manifest that fails to write must not fail
    the backup -- the data is the thing that matters."""
    con = get_con()
    try:
        row = con.execute(
            "SELECT COUNT(*), COUNT(llm_category), MIN(created_at), MAX(created_at) FROM transactions"
        ).fetchone()
        manifest = {
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "reason": reason,
            "transactions": row[0],
            "classified": row[1],
            "earliest": str(row[2])[:10] if row[2] else None,
            "latest": str(row[3])[:10] if row[3] else None,
            "parent_categories": con.execute(
                "SELECT COUNT(*) FROM parent_categories WHERE archived_at IS NULL").fetchone()[0],
            "subcategories": con.execute(
                "SELECT COUNT(*) FROM subcategories WHERE archived_at IS NULL").fetchone()[0],
            "subscriptions": con.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0],
        }
        (target / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2))
    except Exception as e:
        log.warning(f"Could not write backup manifest for {target}: {e}")


def read_backup_manifest(path: Path) -> dict:
    """Empty dict for a backup without a manifest -- it should still list,
    just without the detail."""
    try:
        return json.loads((path / MANIFEST_NAME).read_text())
    except (OSError, ValueError):
        return {}


def backup_size_bytes(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def delete_backup(path: Path | str) -> None:
    """Delete one backup folder.

    This ends in rmtree() on a path that arrives from a UI control, so the
    target is resolved and checked to be a direct child of backups/ first --
    anything else (a symlink out, a traversal, the backups directory itself)
    is refused rather than deleted."""
    target = Path(path).resolve()
    root = backup_dir().resolve()
    if not target.is_dir() or target.parent != root:
        raise ValueError(f"Refusing to delete {path}: not a backup folder inside {root}")
    shutil.rmtree(target)
    log.info(f"Deleted backup {target}")


def _prune_backups() -> None:
    """Keep only the newest BACKUPS_KEPT backups. Failing to delete an old one
    is logged, never raised: a stale backup left on disk is harmless."""
    for old in list_backups()[BACKUPS_KEPT:]:
        try:
            shutil.rmtree(old)
            log.info(f"Pruned old backup {old}")
        except OSError as e:
            log.warning(f"Could not prune old backup {old}: {e}")


# ── Display ───────────────────────────────────────────────────────────────────

def print_transactions(transactions: list[dict]) -> None:
    if not transactions:
        print("No transactions found.")
        return
    for t in transactions:
        print("─" * 60)
        for col, val in t.items():
            if col == "raw_payload":
                print(f"  raw_payload: [use get_transaction(id) to inspect]")
            else:
                print(f"  {col}: {val}")
    print("─" * 60)


def print_stats() -> None:
    s = get_stats()
    print(f"\n{'─' * 40}")
    print(f"  Total transactions : {s['total']}")
    print(f"  Total spend        : £{s['total_spend']:.2f}")
    print(f"  Total income       : £{s['total_income']:.2f}")
    print(f"  Unclassified       : {s['unclassified']}")
    print(f"  Skipped            : {s['skipped']}")
    if s["by_category"]:
        print(f"\n  By category:")
        for c in s["by_category"]:
            print(f"    {c['category']:<25} {c['count']:>4} txns   £{abs(c['total']):.2f}")
    print(f"{'─' * 40}\n")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _rows(sql: str, params: list = []) -> list[dict]:
    result = get_con().execute(sql, params)
    cols = [c.name for c in result.description]
    return [dict(zip(cols, row)) for row in result.fetchall()]


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db(read_only=True)
    print_stats()
    print_transactions(get_all_transactions())
