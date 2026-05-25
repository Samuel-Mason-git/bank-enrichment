import json
import logging
import os
import time
from pathlib import Path
from dotenv import load_dotenv
import duckdb

load_dotenv(Path(__file__).parent.parent.parent / "config" / ".env")

DB_PATH = os.getenv("DB_PATH")

_con: duckdb.DuckDBPyConnection | None = None


# ── Connection ────────────────────────────────────────────────────────────────

def get_con() -> duckdb.DuckDBPyConnection:
    global _con
    if _con is None:
        raise RuntimeError("Database not initialised — call init_db() first")
    return _con


def init_db(read_only: bool = False) -> None:
    global _con
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


# ── Fetch ─────────────────────────────────────────────────────────────────────

def get_all_transactions() -> list[dict]:
    return _rows("SELECT * FROM transactions ORDER BY created_at DESC")


def get_transaction(transaction_id: str) -> dict | None:
    rows = _rows("SELECT * FROM transactions WHERE id = ?", [transaction_id])
    return rows[0] if rows else None


def get_unclassified() -> list[dict]:
    return _rows(
        "SELECT * FROM transactions WHERE llm_category IS NULL AND skipped = FALSE ORDER BY created_at DESC"
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
    log = logging.getLogger(__name__)
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
