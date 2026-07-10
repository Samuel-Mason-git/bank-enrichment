import json
import logging
import os
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


def init_db(read_only: bool = False) -> None:
    global _con
    if _con is not None:
        return
    if not read_only:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    _con = duckdb.connect(DB_PATH, read_only=read_only)
    if not read_only:
        sql_path = Path(__file__).parent.parent.parent / "sql" / "tables.sql"
        sql = sql_path.read_text()
        for statement in sql.split(";"):
            statement = statement.strip()
            if statement:
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


def _migrate() -> None:
    """Additive schema migrations — safe to run on every startup."""
    cols = {r[0] for r in _con.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'subscriptions'"
    ).fetchall()}
    if "merchant_name" not in cols:
        _con.execute("ALTER TABLE subscriptions ADD COLUMN merchant_name VARCHAR(255)")

    for table in ("monthly_summaries", "weekly_summaries"):
        cols = {r[0] for r in _con.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = ?", [table]
        ).fetchall()}
        if "total_invested" not in cols:
            _con.execute(f"ALTER TABLE {table} ADD COLUMN total_invested DECIMAL(19,4)")


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
    return _rows(
        """SELECT * FROM transactions
           WHERE (llm_category IS NULL OR llm_subcategory IS NULL)
           AND skipped = FALSE
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
    """Top subcategories per merchant, for the `merchant_limit` most-transacted merchants."""
    return _rows(
        """WITH merchant_totals AS (
               SELECT merchant_name, COUNT(*) AS total
               FROM transactions
               WHERE merchant_name IS NOT NULL
                 AND llm_category IS NOT NULL AND llm_subcategory IS NOT NULL
               GROUP BY merchant_name
               ORDER BY total DESC
               LIMIT ?
           )
           SELECT t.merchant_name, t.llm_category AS category, t.llm_subcategory AS subcategory,
                  COUNT(*) AS transaction_count,
                  ROW_NUMBER() OVER (PARTITION BY t.merchant_name ORDER BY COUNT(*) DESC) AS rank
           FROM transactions t
           JOIN merchant_totals mt ON mt.merchant_name = t.merchant_name
           WHERE t.llm_category IS NOT NULL AND t.llm_subcategory IS NOT NULL
           GROUP BY t.merchant_name, t.llm_category, t.llm_subcategory
           QUALIFY rank <= ?
           ORDER BY t.merchant_name, rank""",
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
    get_con().execute(
        """UPDATE transactions
           SET llm_category = ?, llm_subcategory = ?, llm_confidence = ?,
               llm_model = ?, classified_at = ?
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
