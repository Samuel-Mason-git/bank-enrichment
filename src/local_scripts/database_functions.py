import json
import logging
import os
import shutil
import time
from pathlib import Path
from dotenv import load_dotenv
import duckdb

load_dotenv(Path(__file__).parent.parent.parent / "config" / ".env")

DB_PATH = os.getenv("DB_PATH")

log = logging.getLogger(__name__)

_con: duckdb.DuckDBPyConnection | None = None


# ── Connection ────────────────────────────────────────────────────────────────

def get_con() -> duckdb.DuckDBPyConnection:
    global _con
    if _con is None:
        raise RuntimeError("Database not initialised — call init_db() first")
    return _con


def split_sql_statements(sql: str) -> list[str]:
    """Split a schema file into executable statements.

    Comments are stripped BEFORE splitting on the semicolon. Splitting naively
    meant one semicolon inside a comment cut a CREATE TABLE in half, and the
    resulting fragments are invalid SQL -- so the whole schema load failed on
    something as innocuous as "sent for approval; the pipeline applies it".
    Quotes are tracked so a '--' inside a string literal is left alone."""
    cleaned = []
    for line in sql.splitlines():
        if line.strip().startswith("--"):
            continue
        in_string = False
        for i, char in enumerate(line):
            if char == "'":
                in_string = not in_string
            elif char == "-" and not in_string and line[i:i + 2] == "--":
                line = line[:i]
                break
        cleaned.append(line)
    return [s.strip() for s in "\n".join(cleaned).split(";") if s.strip()]


def init_db(read_only: bool = False) -> None:
    global _con
    if _con is not None:
        return
    if not read_only:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    _con = duckdb.connect(DB_PATH, read_only=read_only)
    if not read_only:
        sql_path = Path(__file__).parent.parent.parent / "sql" / "tables.sql"
        for statement in split_sql_statements(sql_path.read_text()):
            _con.execute(statement)
        _migrate()
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


# (table, column, type) added to databases created before the column existed.
# CREATE TABLE IF NOT EXISTS does nothing once a table exists, so a column added
# to tables.sql never reaches a database that already has that table -- the file
# and the live database silently disagree until something fails at runtime.
# Every column added to an EXISTING table must also be listed here.
MIGRATIONS = [
    ("subscriptions", "merchant_name", "VARCHAR(255)"),
    ("monthly_summaries", "total_invested", "DECIMAL(19,4)"),
    ("weekly_summaries", "total_invested", "DECIMAL(19,4)"),
    ("transactions", "pending_category_proposal_id", "INTEGER"),
]


def _columns(con, table: str) -> set[str]:
    return {r[0] for r in con.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
        [table],
    ).fetchall()}


def _apply_migrations(con, migrations: list[tuple[str, str, str]]) -> list[str]:
    """Add any missing columns. Additive only -- nothing is dropped, renamed or
    rewritten, so this is safe to run on every startup and safe to re-run.
    Returns what it added, for logging."""
    added = []
    for table, column, column_type in migrations:
        existing = _columns(con, table)
        if not existing:
            continue  # table isn't there yet, so CREATE TABLE will include it
        if column not in existing:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")
            added.append(f"{table}.{column}")
    return added


def _migrate() -> None:
    """Additive schema migrations — safe to run on every startup."""
    added = _apply_migrations(_con, MIGRATIONS)
    if added:
        log.info(f"Applied schema migrations: {', '.join(added)}")


def _seed_taxonomy() -> None:
    """Insert the default taxonomy, but only into a genuinely empty database --
    once any parent category exists (default or custom), seeding is skipped
    entirely. This previously ran on every startup and topped up any missing
    default categories/subcategories by name even on an otherwise-populated,
    user-customized taxonomy -- so wiping the taxonomy and rebuilding your own
    under different names would silently get the full default set added back
    in alongside it on the next start."""
    con = _con
    if con.execute("SELECT COUNT(*) FROM parent_categories").fetchone()[0] > 0:
        return
    for parent_name, subs in _DEFAULT_TAXONOMY.items():
        next_id = con.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM parent_categories").fetchone()[0]
        con.execute(
            "INSERT INTO parent_categories (id, name, created_at) VALUES (?, ?, NOW())",
            [next_id, parent_name],
        )
        parent_id = next_id
        for sub_name in subs:
            next_id = con.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM subcategories").fetchone()[0]
            con.execute(
                "INSERT INTO subcategories (id, name, parent_id, created_at) VALUES (?, ?, ?, NOW())",
                [next_id, sub_name, parent_id],
            )

    # Refunds are money you already spent coming back, not new income — excluded
    # from Income totals by default (overridable in the dashboard's Settings tab).
    refunds = con.execute(
        """SELECT s.id, s.parent_id FROM subcategories s
           JOIN parent_categories p ON p.id = s.parent_id
           WHERE s.name = 'Refunds' AND p.name = 'Income'"""
    ).fetchone()
    if refunds:
        refunds_id, income_parent_id = refunds
        con.execute(
            "INSERT INTO category_roles (parent_id, subcategory_id, role) VALUES (?, ?, 'excluded')",
            [income_parent_id, refunds_id]
        )


# ── Fetch ─────────────────────────────────────────────────────────────────────

def get_all_transactions() -> list[dict]:
    return _rows("SELECT * FROM transactions ORDER BY created_at DESC")


def get_transaction(transaction_id: str) -> dict | None:
    rows = _rows("SELECT * FROM transactions WHERE id = ?", [transaction_id])
    return rows[0] if rows else None


def get_unclassified() -> list[dict]:
    # Transactions locked behind a pending category-creation proposal are
    # excluded until a Telegram decision comes back -- otherwise every run
    # would re-submit them to the LLM and risk a duplicate proposal/card.
    return _rows(
        """SELECT * FROM transactions
           WHERE (llm_category IS NULL OR llm_subcategory IS NULL)
           AND skipped = FALSE
           AND pending_category_proposal_id IS NULL
           ORDER BY created_at DESC"""
    )


def get_by_category(category: str) -> list[dict]:
    return _rows(
        "SELECT * FROM transactions WHERE llm_category = ? ORDER BY created_at DESC",
        [category]
    )


def get_skipped() -> list[dict]:
    return _rows(
        "SELECT * FROM transactions WHERE skipped = TRUE ORDER BY created_at DESC"
    )


def get_recent(n: int = 10) -> list[dict]:
    return _rows(
        "SELECT * FROM transactions ORDER BY created_at DESC LIMIT ?", [n]
    )

def get_parents() -> list[dict]:
    return _rows(
        """SELECT p.id, p.name, p.created_at,
                  COUNT(t.id) AS transaction_count,
                  COALESCE(pr.role, default_role_for_parent(p.name), 'spend') AS role
           FROM parent_categories p
           LEFT JOIN transactions t ON t.llm_category = p.name
           LEFT JOIN category_roles pr ON pr.parent_id = p.id AND pr.subcategory_id IS NULL
           GROUP BY p.id, p.name, p.created_at, pr.role
           ORDER BY transaction_count DESC, p.name ASC"""
    )


def get_subcategories() -> list[dict]:
    return _rows(
        """SELECT s.id, s.name, s.parent_id, p.name AS parent_name, s.created_at,
                  COUNT(t.id) AS transaction_count,
                  sr.role AS role_override,
                  COALESCE(pr.role, default_role_for_parent(p.name), 'spend') AS parent_role
           FROM subcategories s
           JOIN parent_categories p ON p.id = s.parent_id
           LEFT JOIN transactions t ON t.llm_subcategory = s.name
                                    AND t.llm_category = p.name
           LEFT JOIN category_roles sr ON sr.subcategory_id = s.id
           LEFT JOIN category_roles pr ON pr.parent_id = p.id AND pr.subcategory_id IS NULL
           GROUP BY s.id, s.name, s.parent_id, p.name, s.created_at, sr.role, pr.role
           ORDER BY p.name ASC, transaction_count DESC, s.name ASC"""
    )


def get_top_subcategories(limit: int = 10) -> list[dict]:
    return _rows(
        """SELECT p.name AS category, s.name AS subcategory, COUNT(t.id) AS transaction_count
           FROM subcategories s
           JOIN parent_categories p ON p.id = s.parent_id
           JOIN transactions t ON t.llm_subcategory = s.name AND t.llm_category = p.name
           GROUP BY p.name, s.name
           ORDER BY transaction_count DESC
           LIMIT ?""",
        [limit]
    )


def get_top_merchant_subcategories(merchant_limit: int = 50, per_merchant_limit: int = 3) -> list[dict]:
    """Top subcategories per merchant, for the `merchant_limit` most-transacted
    merchants -- these feed the per-merchant Telegram quick-tap buttons.

    Grouped by merchant_name falling back to counterparty_name (direct debits
    and bank transfers -- rent, utility bills, HMRC-style payments -- almost
    never populate merchant_name at all), or every one of those recurring
    payments would be shut out of quick-tap suggestions entirely and always
    fall back to the generic top-5."""
    return _rows(
        """WITH merchant_totals AS (
               SELECT COALESCE(NULLIF(merchant_name, ''), NULLIF(counterparty_name, '')) AS merchant_name, COUNT(*) AS total
               FROM transactions
               WHERE COALESCE(NULLIF(merchant_name, ''), NULLIF(counterparty_name, '')) IS NOT NULL
                 AND llm_category IS NOT NULL AND llm_subcategory IS NOT NULL
               GROUP BY 1
               ORDER BY total DESC
               LIMIT ?
           )
           SELECT COALESCE(NULLIF(t.merchant_name, ''), NULLIF(t.counterparty_name, '')) AS merchant_name,
                  t.llm_category AS category, t.llm_subcategory AS subcategory,
                  COUNT(*) AS transaction_count,
                  ROW_NUMBER() OVER (
                      PARTITION BY COALESCE(NULLIF(t.merchant_name, ''), NULLIF(t.counterparty_name, ''))
                      ORDER BY COUNT(*) DESC
                  ) AS rank
           FROM transactions t
           JOIN merchant_totals mt ON mt.merchant_name = COALESCE(NULLIF(t.merchant_name, ''), NULLIF(t.counterparty_name, ''))
           WHERE t.llm_category IS NOT NULL AND t.llm_subcategory IS NOT NULL
           GROUP BY 1, t.llm_category, t.llm_subcategory
           QUALIFY rank <= ?
           ORDER BY merchant_name, rank""",
        [merchant_limit, per_merchant_limit]
    )


def search(term: str) -> list[dict]:
    like = f"%{term}%"
    return _rows(
        """SELECT * FROM transactions
           WHERE description ILIKE ?
           OR user_context ILIKE ?
           OR merchant_name ILIKE ?
           OR counterparty_name ILIKE ?
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


# ── Write ─────────────────────────────────────────────────────────────────────

def write_to_db(transactions: list[dict]) -> None:
    con = get_con()
    for t in transactions:
        data = t["payload"].get("data", {})
        merchant = data.get("merchant") or {}
        counterparty = data.get("counterparty") or {}
        con.execute(
            """INSERT INTO transactions (
                id, amount, currency, description, monzo_category,
                merchant_name, merchant_category, counterparty_name,
                is_load, created_at, settled_at, raw_payload,
                user_context, skipped, received_at, enriched_at, processed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (id) DO NOTHING""",  # type: ignore
            [
                t["id"],
                data.get("amount", 0) / 100,
                data.get("currency", "GBP"),
                data.get("description"),
                data.get("category"),
                merchant.get("name"),
                merchant.get("category"),
                counterparty.get("name"),
                data.get("is_load"),
                _ts(data.get("created")),
                _ts(data.get("settled")),
                json.dumps(t["payload"]),
                t["user_context"],
                t["skipped"],
                t["received_at"],
                t["enriched_at"],
                time.strftime("%Y-%m-%d %H:%M:%S"),
            ]
        )
        desc = data.get("description", "—")
        amount = data.get("amount", 0) / 100
        log.info(f"  Stored {t['id']} | £{amount:.2f} | {desc} | {t['user_context']}")


def apply_quick_tap_classifications() -> int:
    """Classify transactions whose user_context exactly matches an existing
    'Category - Subcategory' pair (written by a Telegram quick-tap button),
    skipping the LLM entirely for those. Returns the number classified."""
    con = get_con()
    taxonomy = {
        f"{parent} - {sub}": (parent, sub)
        for parent, sub in con.execute(
            """SELECT p.name, s.name FROM subcategories s
               JOIN parent_categories p ON p.id = s.parent_id"""
        ).fetchall()
    }
    unclassified = con.execute(
        """SELECT id, user_context FROM transactions
           WHERE llm_category IS NULL AND skipped = FALSE AND user_context IS NOT NULL"""
    ).fetchall()
    count = 0
    for txn_id, context in unclassified:
        match = taxonomy.get(context)
        if match:
            category, subcategory = match
            update_classification(txn_id, category, subcategory, confidence=1.0, model="quick-tap")
            count += 1
    return count


def upsert_parent(name: str) -> int:
    """Get or create a parent category by name. Returns its id."""
    con = get_con()
    row = con.execute(
        "SELECT id FROM parent_categories WHERE name = ?", [name]
    ).fetchone()
    if row:
        return row[0]
    next_id = con.execute(
        "SELECT COALESCE(MAX(id), 0) + 1 FROM parent_categories"
    ).fetchone()[0]
    con.execute(
        "INSERT INTO parent_categories (id, name, created_at) VALUES (?, ?, ?)",
        [next_id, name, time.strftime("%Y-%m-%d %H:%M:%S")]
    )
    return next_id


def upsert_subcategory(name: str, parent_id: int) -> int:
    """Get or create a subcategory by name + parent_id. Returns its id."""
    con = get_con()
    row = con.execute(
        "SELECT id FROM subcategories WHERE name = ? AND parent_id = ?", [name, parent_id]
    ).fetchone()
    if row:
        return row[0]
    next_id = con.execute(
        "SELECT COALESCE(MAX(id), 0) + 1 FROM subcategories"
    ).fetchone()[0]
    con.execute(
        "INSERT INTO subcategories (id, name, parent_id, created_at) VALUES (?, ?, ?, ?)",
        [next_id, name, parent_id, time.strftime("%Y-%m-%d %H:%M:%S")]
    )
    return next_id


ROLES = ("income", "spend", "investment", "transfer", "excluded")


def _validate_role(role: str) -> None:
    if role not in ROLES:
        raise ValueError(f"Invalid role {role!r} — must be one of {ROLES}")


def get_totals_by_role(date_from: str, date_to: str) -> dict[str, float]:
    """Directional totals per role in [date_from, date_to] -- income sums its
    positive (incoming) side, every other role sums its negative (outgoing)
    side as a positive magnitude. Always returns all 5 keys (0.0 default) so
    callers never need a .get() guard.

    Roles like "transfer" are bidirectional (an Inbound and an Outbound
    Transfer subcategory can both carry that role) -- summing every sign
    together would let them cancel out (e.g. -674 + 540 nets to -134) instead
    of reporting the true £674 that actually went out."""
    con = get_con()
    rows = con.execute(
        """SELECT role, COALESCE(SUM(
               CASE WHEN role = 'income' THEN GREATEST(amount, 0) ELSE GREATEST(-amount, 0) END
           ), 0) FROM transaction_roles
           WHERE skipped = FALSE AND created_at BETWEEN ? AND ?
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
           WHERE role = ? AND llm_category IS NOT NULL
             AND skipped = FALSE AND created_at BETWEEN ? AND ?
           GROUP BY llm_category
           ORDER BY total DESC""",
        [role, date_from, date_to]
    )


def set_parent_role(parent_id: int, role: str) -> None:
    """Set the role for an entire parent category (its subcategories inherit
    this unless they have their own override)."""
    _validate_role(role)
    con = get_con()
    con.execute("DELETE FROM category_roles WHERE parent_id = ? AND subcategory_id IS NULL", [parent_id])
    con.execute(
        "INSERT INTO category_roles (parent_id, subcategory_id, role) VALUES (?, NULL, ?)",
        [parent_id, role]
    )


def set_subcategory_role(sub_id: int, role: str | None) -> None:
    """Set a role override for one subcategory, or clear it (role=None) to
    fall back to inheriting its parent's role."""
    con = get_con()
    con.execute("DELETE FROM category_roles WHERE subcategory_id = ?", [sub_id])
    if role is not None:
        _validate_role(role)
        parent_id = con.execute("SELECT parent_id FROM subcategories WHERE id = ?", [sub_id]).fetchone()[0]
        con.execute(
            "INSERT INTO category_roles (parent_id, subcategory_id, role) VALUES (?, ?, ?)",
            [parent_id, sub_id, role]
        )


def get_subscriptions() -> list[dict]:
    return _rows("SELECT * FROM subscriptions ORDER BY active DESC, name ASC")


def upsert_subscription(name: str, amount: float, frequency: str, merchant_name: str | None = None) -> int:
    con = get_con()
    next_id = con.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM subscriptions").fetchone()[0]
    con.execute(
        "INSERT INTO subscriptions (id, name, merchant_name, amount, frequency, active, created_at) VALUES (?, ?, ?, ?, ?, TRUE, ?)",
        [next_id, name, merchant_name, amount, frequency, time.strftime("%Y-%m-%d %H:%M:%S")]
    )
    return next_id


def update_subscription(sub_id: int, name: str, amount: float, frequency: str, merchant_name: str | None = None) -> None:
    get_con().execute(
        "UPDATE subscriptions SET name = ?, merchant_name = ?, amount = ?, frequency = ? WHERE id = ?",
        [name, merchant_name, amount, frequency, sub_id]
    )


def toggle_subscription(sub_id: int) -> None:
    get_con().execute("UPDATE subscriptions SET active = NOT active WHERE id = ?", [sub_id])


def delete_subscription(sub_id: int) -> None:
    get_con().execute("DELETE FROM subscriptions WHERE id = ?", [sub_id])


# ── Backups ───────────────────────────────────────────────────────────────────

BACKUPS_KEPT = 10
MANIFEST_NAME = "manifest.json"


def backup_dir() -> Path:
    """backups/ sits alongside the database file, so a backup travels with the
    data it protects rather than living next to the code."""
    return Path(DB_PATH).parent / "backups"


def list_backups() -> list[Path]:
    """Newest first. Names sort chronologically, so no stat() call is needed."""
    root = backup_dir()
    if not root.is_dir():
        return []
    return sorted((p for p in root.iterdir() if p.is_dir()), reverse=True)


def backup_db(reason: str = "manual") -> Path:
    """Snapshot the whole database into backups/<timestamp>_<reason>/ before
    something irreversible happens, keeping the most recent BACKUPS_KEPT.

    EXPORT DATABASE rather than copying the .db file: the file is open and may
    have un-checkpointed changes sitting in its WAL, so a copy taken mid-session
    is not guaranteed to be a consistent database. The export is a logical dump
    (schema.sql + load.sql + one Parquet file per table) restorable with
    IMPORT DATABASE, and it stays readable even if the .db format changes
    between DuckDB versions."""
    con = get_con()
    # ':' is not a legal filename character on Windows, so this is a flattened
    # timestamp rather than an ISO one.
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe_reason = "".join(c if c.isalnum() or c in "-_" else "-" for c in reason)[:40]
    target = backup_dir() / f"{stamp}_{safe_reason}"
    # Two destructive actions inside the same second would otherwise export into
    # a directory that already holds another backup's files.
    suffix = 1
    while target.exists():
        suffix += 1
        target = backup_dir() / f"{stamp}_{safe_reason}-{suffix}"
    target.mkdir(parents=True)
    # DuckDB parses this path itself, so the quotes have to be escaped rather
    # than passed as a bind parameter (EXPORT DATABASE takes no parameters).
    con.execute(f"EXPORT DATABASE '{str(target).replace(chr(39), chr(39) * 2)}' (FORMAT PARQUET)")
    _write_manifest(target, reason)
    log.info(f"Backed up database to {target}")
    _prune_backups()
    return target


def _write_manifest(target: Path, reason: str) -> None:
    """Record what the backup actually contains, so the Settings tab can say
    'this one holds 1,240 transactions covering 2025-01 to 2026-08' without
    opening every Parquet file to count. Written after the export so the counts
    describe exactly what was captured.

    A manifest that fails to write must not fail the backup -- the exported
    data is the thing that matters, and a missing manifest only costs the
    listing some detail."""
    con = get_con()
    try:
        row = con.execute("""
            SELECT COUNT(*),
                   COUNT(llm_category),
                   MIN(created_at),
                   MAX(created_at)
            FROM transactions
        """).fetchone()
        manifest = {
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "reason": reason,
            "transactions": row[0],
            "classified": row[1],
            "earliest": str(row[2])[:10] if row[2] else None,
            "latest": str(row[3])[:10] if row[3] else None,
            "parent_categories": con.execute("SELECT COUNT(*) FROM parent_categories").fetchone()[0],
            "subcategories": con.execute("SELECT COUNT(*) FROM subcategories").fetchone()[0],
            "subscriptions": con.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0],
        }
        (target / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2))
    except Exception as e:
        log.warning(f"Could not write backup manifest for {target}: {e}")


def read_backup_manifest(path: Path) -> dict:
    """Empty dict for a backup taken before manifests existed, or a folder a
    human dropped into backups/ by hand -- both should still list, just without
    the detail."""
    try:
        return json.loads((path / MANIFEST_NAME).read_text())
    except (OSError, ValueError):
        return {}


def backup_size_bytes(path: Path) -> int:
    """Total size of the export on disk. Walks the tree rather than listing one
    level, since EXPORT DATABASE is free to nest its data files."""
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def delete_backup(path: Path | str) -> None:
    """Delete one backup folder.

    This ends in rmtree() on a path that arrives from a UI control, so the
    target is resolved and checked to be a direct child of backups/ first --
    anything else (a symlink out, a traversal, the database directory itself)
    is refused rather than deleted."""
    target = Path(path).resolve()
    root = backup_dir().resolve()
    if not target.is_dir() or target.parent != root:
        raise ValueError(f"Refusing to delete {path}: not a backup folder inside {root}")
    shutil.rmtree(target)
    log.info(f"Deleted backup {target}")


def _prune_backups() -> None:
    """Keep only the newest BACKUPS_KEPT backups. Failing to delete an old one
    must never take down the action that triggered the backup -- a stale backup
    left on disk is harmless, a crashed wipe is not."""
    for old in list_backups()[BACKUPS_KEPT:]:
        try:
            shutil.rmtree(old)
            log.info(f"Pruned old backup {old}")
        except OSError as e:
            log.warning(f"Could not prune old backup {old}: {e}")


def clear_db() -> None:
    con = get_con()
    count = con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    if count == 0:
        print("Database is already empty.")
        return
    confirm = input(f"This will permanently delete all {count} transactions. Type YES to confirm: ")
    if confirm.strip() == "YES":
        con.execute("DELETE FROM transactions")
        print(f"Deleted {count} transactions.")
    else:
        print("Cancelled.")


def update_classification(
    transaction_id: str,
    category: str,
    subcategory: str | None,
    confidence: float | None,
    model: str,
) -> None:
    # Clearing pending_category_proposal_id here too: a transaction that has
    # just been classified -- by any path -- is never still waiting on a
    # category decision, so a proposal approved later must not overwrite it.
    get_con().execute(
        """UPDATE transactions
           SET llm_category = ?, llm_subcategory = ?, llm_confidence = ?,
               llm_model = ?, classified_at = ?, pending_category_proposal_id = NULL
           WHERE id = ?""",
        [category, subcategory, confidence, model,
         time.strftime("%Y-%m-%d %H:%M:%S"), transaction_id]
    )


def update_transaction_details(transaction_id: str, created_at: str, user_context: str | None) -> None:
    get_con().execute(
        """UPDATE transactions
           SET created_at = ?, user_context = ?
           WHERE id = ?""",
        [created_at, user_context, transaction_id]
    )


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

def _ts(val):
    return val if val else None


def _rows(sql: str, params: list = []) -> list[dict]:
    result = get_con().execute(sql, params)
    cols = [d[0] for d in result.description]
    return [dict(zip(cols, row)) for row in result.fetchall()]


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db(read_only=True)
    print_stats()
    print_transactions(get_all_transactions())
