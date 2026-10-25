"""Copy today's two DuckDB databases into the new Postgres tables, and check
the copy. Step 1 of docs/server-move.md §10.

    python pg_copy.py both --local-db LOCAL.db --server-db SERVER.db
    python pg_copy.py copy  ...   # just the copy
    python pg_copy.py check ...   # just the check

The local file is the PC's (DB_PATH). The server's lives on the app box and is
fetched first, as a copy:

    ssh oracle 'sudo cp ~/bank-enrichment/data/bank_enrichment_server.db* /tmp/ && sudo chown ubuntu /tmp/bank_enrichment_server.db*'
    scp 'oracle:/tmp/bank_enrichment_server.db*' .

Only ever reads -- and only copies of -- the DuckDB files, and only ever writes
the new tables in Postgres (sql/postgres/schema.sql). The copy empties and
refills every table in one transaction, so it either all lands or nothing
changes, and can be run as often as needed. Anything it can't map (a category
name that isn't in the taxonomy) stops it before anything is written, rather
than being guessed.

Postgres is reached with DB_HOST, DB_PORT, DB_NAME, DB_USER and DB_PASSWORD
from config/.env.
"""
import argparse
import json
import os
import shutil
import sys
import tempfile
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import duckdb
import psycopg
from psycopg.types.json import Jsonb

# Emptied together and refilled parents first, so every foreign key resolves.
NEW_TABLES = [
    "parent_categories", "subcategories", "category_roles",
    "transactions_raw", "context_events", "label_events",
    "rules", "rule_suggestions", "taxonomy_proposals", "category_proposals",
    "judge_reviews", "subscriptions", "summaries",
]

# Today's transactions columns, as the new view offers them (processed_at,
# which nothing reads, is dropped).
TRANSACTION_COLUMNS = [
    "id", "amount", "currency", "description", "monzo_category", "merchant_name",
    "merchant_category", "counterparty_name", "is_load", "created_at", "settled_at",
    "raw_payload", "user_context", "skipped", "received_at", "enriched_at",
    "llm_category", "llm_subcategory", "llm_confidence", "llm_model", "classified_at",
    "pending_category_proposal_id",
]

_SHOW = 5  # differing rows listed per table before just counting them


class CopyProblems(Exception):
    """Data that can't be mapped onto the new tables without guessing."""

    def __init__(self, problems: list[str]):
        super().__init__(f"{len(problems)} problem(s)")
        self.problems = problems


@contextmanager
def snapshot(path: str | Path):
    """A connection to a throwaway copy of the DuckDB file at `path`, with its
    .wal if it has one. The live file is never opened: something else usually
    has it open, and DuckDB won't share a file another process is writing."""
    path = Path(path)
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / path.name
        shutil.copy2(path, copy)
        wal = path.with_name(path.name + ".wal")
        if wal.exists():
            shutil.copy2(wal, copy.with_name(copy.name + ".wal"))
        # Not read-only: opening the copy may replay its .wal into it.
        con = duckdb.connect(str(copy))
        try:
            yield con
        finally:
            con.close()


def connect_postgres() -> psycopg.Connection:
    e = os.environ
    return psycopg.connect(
        host=e["DB_HOST"], port=e.get("DB_PORT", "5432"), dbname=e["DB_NAME"],
        user=e["DB_USER"], password=e["DB_PASSWORD"], connect_timeout=10,
    )


def _rows(con, sql: str, params=()) -> list[dict]:
    cur = con.execute(sql, params)
    columns = [c[0] for c in cur.description]
    return [dict(zip(columns, r)) for r in cur.fetchall()]


def _has_table(con, table: str) -> bool:
    return bool(con.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = ?", [table]
    ).fetchone())


def _monzo_created(payload: dict):
    """The transaction's own date as UTC without a zone, as the new view reads it."""
    created = (payload.get("data") or {}).get("created")
    if not created:
        return None
    return datetime.fromisoformat(created).astimezone(timezone.utc).replace(tzinfo=None)


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


def _server_only(left: dict, kind: str, problems: list[str], notes: Counter) -> None:
    """Proposals the server still has but the PC doesn't. One the PC already
    collected is history from before the PC's own copy was cleared (the PC's
    proposals were wiped and renumbered from 999001 once), so it's left out
    and counted. One it never collected would be a Telegram decision the PC
    never saw, so that stops the copy."""
    for local_id, p in left.items():
        if p.get("status") == "collected":
            notes[f"old {kind}s left out: already collected, no longer on the PC"] += 1
        else:
            problems.append(f"{kind} {local_id} is on the server ({p.get('status')}) but not the PC")


# ── Building the new rows ────────────────────────────────────────────────────

def build(local, server) -> tuple[dict[str, list[dict]], dict]:
    """Every row for every new table, from the two DuckDB connections, plus a
    few counts worth showing. Raises CopyProblems rather than guessing."""
    problems: list[str] = []
    notes: Counter = Counter()
    rows: dict[str, list[dict]] = {t: [] for t in NEW_TABLES}

    # Taxonomy, and the name -> id lookups everything else needs.
    rows["parent_categories"] = _rows(local, "SELECT id, name, created_at FROM parent_categories ORDER BY id")
    rows["subcategories"] = _rows(local, "SELECT id, name, parent_id, created_at FROM subcategories ORDER BY id")
    rows["category_roles"] = _rows(local, "SELECT parent_id, subcategory_id, role FROM category_roles")
    parent_id = {p["name"]: p["id"] for p in rows["parent_categories"]}
    sub_id = {(s["parent_id"], s["name"]): s["id"] for s in rows["subcategories"]}

    def ids(category, subcategory, where):
        """(parent_id, subcategory_id) for a pair of names, or a problem."""
        pid = parent_id.get(category)
        if pid is None:
            problems.append(f"{where}: category {category!r} isn't in the taxonomy")
            return None, None
        if subcategory is None:
            return pid, None
        sid = sub_id.get((pid, subcategory))
        if sid is None:
            problems.append(f"{where}: subcategory {category!r} / {subcategory!r} isn't in the taxonomy")
        return pid, sid

    # Proposals are needed first: a held transaction's hold lives on its proposal.
    held: dict[int, list[str]] = defaultdict(list)

    # Transactions on the PC: the raw payload, then their context and label as
    # their first events.
    for t in _rows(local, "SELECT * FROM transactions ORDER BY received_at, id"):
        payload = _json(t["raw_payload"])
        received_at = t["received_at"] or t["enriched_at"] or t["processed_at"] or t["created_at"]
        if t["received_at"] is None:
            notes["transactions with no received_at (used the next date along)"] += 1
        rows["transactions_raw"].append({"id": t["id"], "payload": payload, "received_at": received_at})

        override = None
        if t["created_at"] is not None and t["created_at"] != _monzo_created(payload):
            override = t["created_at"]
            notes["transactions whose date was corrected"] += 1
        rows["context_events"].append({
            "txn_id": t["id"], "user_context": t["user_context"], "skipped": bool(t["skipped"]),
            "created_at_override": override, "source": "migration",
            "set_at": t["enriched_at"] or received_at,
        })

        if t["llm_category"] is not None:
            pid, sid = ids(t["llm_category"], t["llm_subcategory"], f"transaction {t['id']}")
            rows["label_events"].append({
                "txn_id": t["id"], "parent_id": pid, "subcategory_id": sid, "source": "migration",
                "model": t["llm_model"], "confidence": t["llm_confidence"],
                "set_at": t["classified_at"] or t["processed_at"] or received_at,
            })
        if t["pending_category_proposal_id"] is not None:
            held[t["pending_category_proposal_id"]].append(t["id"])

    # Transactions still only in the server's queue (arrived, not yet pulled to
    # the PC): the payload, any cards and nudges sent, and the answer if there's one.
    local_ids = {r["id"] for r in rows["transactions_raw"]}
    for q in _rows(server, "SELECT * FROM webhook_queue ORDER BY received_at, id"):
        if q["id"] in local_ids:
            continue
        notes["transactions only on the server so far"] += 1
        rows["transactions_raw"].append({"id": q["id"], "payload": _json(q["payload"]), "received_at": q["received_at"]})
        asked = q["request_count"] or 0
        for i in range(asked):
            # Only the last nudge's time is kept today; earlier ones get the arrival time.
            last = i == asked - 1 and q["last_requested_at"] is not None
            rows["context_events"].append({
                "txn_id": q["id"], "user_context": None, "skipped": False, "created_at_override": None,
                "source": "request", "set_at": q["last_requested_at"] if last else q["received_at"],
            })
        if q["status"] in ("enriched", "processed"):
            rows["context_events"].append({
                "txn_id": q["id"], "user_context": q["user_context"], "skipped": bool(q["skipped"]),
                "created_at_override": None, "source": "migration",
                "set_at": q["enriched_at"] or q["received_at"],
            })
            if q["rule_category"] and q["rule_subcategory"]:
                pid = parent_id.get(q["rule_category"])
                sid = sub_id.get((pid, q["rule_subcategory"]))
                if sid is not None:  # today a stale rule placement is left for the classifier too
                    rows["label_events"].append({
                        "txn_id": q["id"], "parent_id": pid, "subcategory_id": sid, "source": "rule",
                        "model": "rule", "confidence": None, "set_at": q["enriched_at"] or q["received_at"],
                    })

    # Rules: a fixed category's names become its subcategory's id.
    for r in _rows(server, "SELECT * FROM rules ORDER BY id"):
        sid = None
        if r["category"] and r["subcategory"]:
            _, sid = ids(r["category"], r["subcategory"], f"rule {r['id']} ({r['name']})")
        rows["rules"].append({
            "id": r["id"], "name": r["name"],
            "match_field": r["match_field"], "match_type": r["match_type"], "match_value": r["match_value"],
            "match_field_2": r["match_field_2"], "match_type_2": r["match_type_2"],
            "match_value_2": r["match_value_2"], "auto_context": r["auto_context"] or "",
            "auto_skip": bool(r["auto_skip"]), "subcategory_id": sid,
            "enabled": True if r["enabled"] is None else bool(r["enabled"]),
        })

    rows["rule_suggestions"] = _rows(server, """
        SELECT id, match_field, match_value, category, subcategory, txn_count, auto_context,
               status, proposed_at, decided_at,
               -- Every suggestion here already had its card: the server sent it on arrival.
               proposed_at AS sent_at FROM rule_suggestions ORDER BY id""")

    # Taxonomy proposals: the local proposal, plus the server's record of the
    # Telegram side. A decision the PC hasn't collected yet counts.
    server_tax = {p["local_id"]: p for p in _rows(server, "SELECT * FROM taxonomy_proposals")}
    for p in _rows(local, "SELECT * FROM taxonomy_proposals ORDER BY id"):
        s = server_tax.pop(p["id"], {})
        status, decided_at = p["status"], p["decided_at"]
        if status == "pending" and s.get("status") in ("approved", "denied"):
            status, decided_at = s["status"], s["decided_at"]
        rows["taxonomy_proposals"].append({
            "id": p["id"], "action": p["action"], "parent_name": p["parent_name"],
            "source_sub": p["source_sub"], "target_parent": p["target_parent"] or "",
            "proposed_sub": p["proposed_sub"], "rationale": p["rationale"],
            "evidence_ids": _json(p["evidence_ids"]), "examples": _json(s.get("examples") or "[]"),
            "status": status, "run_key": p["run_key"],
            "proposed_at": p["proposed_at"], "sent_at": s.get("sent_at"), "decided_at": decided_at,
            "applied_at": p["applied_at"], "follow_up_count": s.get("follow_up_count") or 0,
            "last_nudged_at": s.get("last_nudged_at"),
        })
    _server_only(server_tax, "taxonomy proposal", problems, notes)

    # Category proposals, the same way, with the transactions each one holds.
    server_cat = {p["local_id"]: p for p in _rows(server, "SELECT * FROM category_proposals")}
    for p in _rows(local, "SELECT * FROM category_proposals ORDER BY id"):
        s = server_cat.pop(p["id"], {})
        status, selected, regenerate, decided_at = (
            p["status"], p["selected_option"], bool(p["regenerate_requested"]), p["decided_at"])
        if status == "pending" and s.get("status") in ("selected", "denied"):
            status, selected, decided_at = s["status"], s["selected_option"], s["decided_at"]
            regenerate = bool(s["regenerate_requested"])
        rows["category_proposals"].append({
            "id": p["id"], "options": _json(p["options"]), "txn_ids": sorted(held.pop(p["id"], [])),
            "examples": _json(s.get("examples") or "[]"),
            "status": status, "selected_option": selected, "regenerate_requested": regenerate,
            "proposed_at": p["proposed_at"], "sent_at": s.get("sent_at"), "decided_at": decided_at,
            "applied_at": p["applied_at"],
        })
    _server_only(server_cat, "category proposal", problems, notes)
    for proposal_id, txn_ids in held.items():
        problems.append(f"{len(txn_ids)} transaction(s) held by category proposal {proposal_id}, which doesn't exist")

    rows["judge_reviews"] = _rows(local, """
        SELECT txn_id, merchant_key, category, subcategory, outcome, reviewed_at FROM judge_reviews""")
    for s in _rows(local, "SELECT * FROM subscriptions ORDER BY id"):
        s["active"] = True if s["active"] is None else bool(s["active"])
        rows["subscriptions"].append(s)

    for period, table in (("week", "weekly_summaries"), ("month", "monthly_summaries")):
        for s in _rows(local, f"SELECT * FROM {table} ORDER BY send_date"):
            rows["summaries"].append({
                "period": period, "period_key": s["send_date"], "total_spend": s["total_spend"],
                "total_income": s["total_income"], "net": s["net"],
                "total_invested": s["total_invested"], "total_saved": s["total_saved"], "sent_at": s["sent_at"],
            })

    if problems:
        raise CopyProblems(problems)
    return rows, dict(notes)


def _adapt(value):
    return Jsonb(value) if isinstance(value, (dict, list)) else value


def copy_all(local, server, pg) -> tuple[dict[str, int], dict]:
    """Empty every new table and refill it from the two DuckDB connections, in
    one transaction. Returns ({table: rows written}, notes)."""
    rows, notes = build(local, server)
    with pg.transaction():
        pg.execute("TRUNCATE " + ", ".join(NEW_TABLES) + " RESTART IDENTITY")
        for table in NEW_TABLES:
            if not rows[table]:
                continue
            columns = list(rows[table][0])
            sql = (f"INSERT INTO {table} ({', '.join(columns)}) "
                   f"VALUES ({', '.join(['%s'] * len(columns))})")
            with pg.cursor() as cur:
                cur.executemany(sql, [[_adapt(r[c]) for c in columns] for r in rows[table]])
    return {t: len(rows[t]) for t in NEW_TABLES}, notes


# ── Checking the copy ────────────────────────────────────────────────────────

def _normalise(value):
    """Make a DuckDB value and its Postgres copy compare equal when they hold
    the same thing: JSON compares by content, numbers by value."""
    if isinstance(value, str) and value[:1] in "{[":
        try:
            return json.dumps(json.loads(value), sort_keys=True)
        except ValueError:
            return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True)
    if isinstance(value, float):
        return Decimal(str(value))
    return value


def _diff(name: str, old: list[tuple], new: list[tuple], columns: list[str]) -> list[str]:
    old_c = Counter(tuple(_normalise(v) for v in r) for r in old)
    new_c = Counter(tuple(_normalise(v) for v in r) for r in new)
    if old_c == new_c:
        return []
    only_old, only_new = list((old_c - new_c).elements()), list((new_c - old_c).elements())
    out = [f"{name}: {len(old)} rows before, {len(new)} after; "
           f"{len(only_old)} only in DuckDB, {len(only_new)} only in Postgres"]
    for label, extra in (("only in DuckDB", only_old), ("only in Postgres", only_new)):
        for row in extra[:_SHOW]:
            out.append(f"    {label}: {dict(zip(columns, row))}")
        if len(extra) > _SHOW:
            out.append(f"    ... and {len(extra) - _SHOW} more {label}")
    return out


def check_all(local, server, pg) -> tuple[list[str], list[str]]:
    """(differences, for-information) between the DuckDB data and the new
    tables. No differences means the copy is exact."""
    differences: list[str] = []
    info: list[str] = []

    def both(name, duck_sql, pg_sql, duck=local):
        old = duck.execute(duck_sql)
        columns = [c[0] for c in old.description]
        differences.extend(_diff(name, old.fetchall(), pg.execute(pg_sql).fetchall(), columns))

    # Every transaction on the PC, column by column, against the new view.
    cols = ", ".join(TRANSACTION_COLUMNS)
    local_ids = [r[0] for r in local.execute("SELECT id FROM transactions").fetchall()]
    differences.extend(_diff(
        "transactions",
        local.execute(f"SELECT {cols} FROM transactions").fetchall(),
        pg.execute(f"SELECT {cols} FROM transactions WHERE id = ANY(%s)", [local_ids]).fetchall(),
        TRANSACTION_COLUMNS,
    ))

    # The money, the way the dashboard adds it up: totals by month and role.
    totals = ("SELECT strftime(created_at, '%Y-%m') AS month, role, SUM(amount) AS total "
              "FROM transaction_roles GROUP BY 1, 2")
    differences.extend(_diff(
        "monthly totals by role",
        local.execute(totals).fetchall(),
        pg.execute("SELECT to_char(created_at, 'YYYY-MM'), role, SUM(amount) FROM transaction_roles "
                   "WHERE id = ANY(%s) GROUP BY 1, 2", [local_ids]).fetchall(),
        ["month", "role", "total"],
    ))

    # Transactions only on the server so far: all there, with an answer where
    # the server has one.
    queue = server.execute("SELECT id, status FROM webhook_queue").fetchall()
    only_server = [(i, s) for i, s in queue if i not in set(local_ids)]
    differences.extend(_diff(
        "transactions only on the server",
        [(i, s in ("enriched", "processed")) for i, s in only_server],
        pg.execute("SELECT id, enriched_at IS NOT NULL FROM transactions WHERE id = ANY(%s)",
                   [[i for i, _ in only_server]]).fetchall(),
        ["id", "answered"],
    ))

    both("parent_categories", "SELECT id, name, created_at FROM parent_categories",
         "SELECT id, name, created_at FROM parent_categories WHERE archived_at IS NULL")
    both("subcategories", "SELECT id, name, parent_id, created_at FROM subcategories",
         "SELECT id, name, parent_id, created_at FROM subcategories WHERE archived_at IS NULL")
    both("category_roles", "SELECT parent_id, subcategory_id, role FROM category_roles",
         "SELECT parent_id, subcategory_id, role FROM category_roles")
    both("judge_reviews",
         "SELECT txn_id, merchant_key, category, subcategory, outcome, reviewed_at FROM judge_reviews",
         "SELECT txn_id, merchant_key, category, subcategory, outcome, reviewed_at FROM judge_reviews")
    both("subscriptions",
         "SELECT id, name, merchant_name, amount, frequency, COALESCE(active, TRUE), created_at FROM subscriptions",
         "SELECT id, name, merchant_name, amount, frequency, active, created_at FROM subscriptions")
    both("summaries",
         "SELECT 'week', send_date, total_spend, total_income, net, total_invested, total_saved, sent_at "
         "FROM weekly_summaries UNION ALL "
         "SELECT 'month', send_date, total_spend, total_income, net, total_invested, total_saved, sent_at "
         "FROM monthly_summaries",
         "SELECT period, period_key, total_spend, total_income, net, total_invested, total_saved, sent_at "
         "FROM summaries")
    both("rule_suggestions",
         "SELECT id, match_field, match_value, category, subcategory, txn_count, auto_context, status, "
         "proposed_at, decided_at FROM rule_suggestions",
         "SELECT id, match_field, match_value, category, subcategory, txn_count, auto_context, status, "
         "proposed_at, decided_at FROM rule_suggestions", duck=server)

    # Rules, with each fixed category turned back into names.
    old_rules = server.execute(
        "SELECT id, name, match_field, match_type, match_value, match_field_2, match_type_2, match_value_2, "
        "COALESCE(auto_context, ''), COALESCE(auto_skip, FALSE), category, subcategory, COALESCE(enabled, TRUE) "
        "FROM rules").fetchall()
    old_rules = [r[:10] + ((r[10], r[11]) if r[10] and r[11] else (None, None)) + r[12:] for r in old_rules]
    differences.extend(_diff(
        "rules", old_rules,
        pg.execute(
            "SELECT r.id, r.name, r.match_field, r.match_type, r.match_value, r.match_field_2, r.match_type_2, "
            "r.match_value_2, r.auto_context, r.auto_skip, p.name, s.name, r.enabled FROM rules r "
            "LEFT JOIN subcategories s ON s.id = r.subcategory_id "
            "LEFT JOIN parent_categories p ON p.id = s.parent_id").fetchall(),
        ["id", "name", "match_field", "match_type", "match_value", "match_field_2", "match_type_2",
         "match_value_2", "auto_context", "auto_skip", "category", "subcategory", "enabled"],
    ))

    # Proposals: every one there, with what was proposed unchanged.
    both("taxonomy_proposals",
         "SELECT id, action, parent_name, source_sub, COALESCE(target_parent, ''), proposed_sub, rationale, "
         "evidence_ids, run_key, proposed_at FROM taxonomy_proposals",
         "SELECT id, action, parent_name, source_sub, target_parent, proposed_sub, rationale, "
         "evidence_ids, run_key, proposed_at FROM taxonomy_proposals")
    both("category_proposals", "SELECT id, options, proposed_at FROM category_proposals",
         "SELECT id, options, proposed_at FROM category_proposals")

    # The quick-tap buttons are worked out fresh now, where the server's copy is
    # up to a run old, and ties can fall either way: shown, not failed.
    if _has_table(server, "quick_categories"):
        old_q = {tuple(r) for r in server.execute(
            "SELECT merchant_name, category, subcategory FROM quick_categories").fetchall()}
        new_q = {tuple(r) for r in pg.execute(
            "SELECT merchant_name, category, subcategory FROM quick_categories").fetchall()}
        if old_q != new_q:
            info.append(f"quick-tap buttons: {len(old_q & new_q)} the same, {len(old_q - new_q)} only on "
                        f"the server, {len(new_q - old_q)} only in the new view")
    return differences, info


def main(argv=None) -> int:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent.parent / "config" / ".env")

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("action", choices=["copy", "check", "both"])
    parser.add_argument("--local-db", default=os.environ.get("DB_PATH"),
                        help="the PC's DuckDB file (default: DB_PATH)")
    parser.add_argument("--server-db", required=True, help="a copy of the server's DuckDB file")
    args = parser.parse_args(argv)

    with snapshot(args.local_db) as local, snapshot(args.server_db) as server, connect_postgres() as pg:
        pg.autocommit = True
        if args.action in ("copy", "both"):
            try:
                counts, notes = copy_all(local, server, pg)
            except CopyProblems as e:
                print("Nothing was copied -- these need sorting out first:")
                print("\n".join(f"  {p}" for p in e.problems))
                return 1
            print(f"Copied {sum(counts.values())} rows:")
            for table, n in counts.items():
                print(f"  {table:20} {n}")
            for note, n in notes.items():
                print(f"  ({n} {note})")
        if args.action in ("check", "both"):
            differences, info = check_all(local, server, pg)
            for line in info:
                print(f"For information: {line}")
            if differences:
                print(f"{sum(1 for d in differences if not d.startswith(' '))} difference(s):")
                print("\n".join(differences))
                return 1
            print("0 differences")
    return 0


if __name__ == "__main__":
    sys.exit(main())
