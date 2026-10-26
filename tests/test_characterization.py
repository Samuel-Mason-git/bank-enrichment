"""Everything the pipeline and the dashboard's data layer do, pinned down.

One realistic dataset, a fixed clock, and a fixed sequence of the things the
pipeline does -- read every view of the data, classify, hold transactions
behind proposals, act on decisions, reshape the taxonomy, send summaries, back
up. Every return value, and the whole content of every table after each step,
is compared with tests/golden/characterization.json.

That file was recorded with the code as it was before the pipeline moved from
DuckDB to psycopg, so this is what proves the move changed nothing: same
answers, same rows. Any later change that alters what a function returns or
writes shows up here too.

To re-record it after a deliberate change, run this file with
UPDATE_GOLDEN=1 and review the diff of the JSON like any other change. To see
exactly what came out when it fails, CHAR_DUMP=<file> writes this run's
results there, in the same form, for diffing against the golden file.
"""
import json
import os
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

import alerts
import category_proposals as cp
import database_functions as dbf
import judge_backlog as jb
import llm_labelling as ll
import monthly_summary
import placement_judge as pj
import rule_suggestions as rs
import taxonomy_review as tr
import weekly_summary
from tests.local_helpers import insert_transaction

GOLDEN = Path(__file__).parent / "golden" / "characterization.json"
TODAY = date(2026, 10, 26)

TABLES = {
    "parent_categories": "id", "subcategories": "id", "category_roles": "parent_id, subcategory_id, role",
    "context_events": "event_id", "label_events": "event_id", "rules": "id",
    "rule_suggestions": "id", "taxonomy_proposals": "id", "category_proposals": "id",
    "judge_reviews": "txn_id", "subscriptions": "id", "summaries": "period, period_key",
}
# One row per transaction, the way everything reads them.
VIEW_COLUMNS = ("id, amount, created_at, merchant_name, counterparty_name, user_context, skipped, "
                "llm_category, llm_subcategory, llm_model, llm_confidence, parent_id, subcategory_id, "
                "pending_category_proposal_id")


class FrozenDate(date):
    @classmethod
    def today(cls):
        return TODAY


# ── Making values comparable ──────────────────────────────────────────────────

def _normal(value, started: datetime):
    """The same answer from either driver looks the same here: JSON text and
    parsed JSON alike become the parsed value, numbers lose their type, and a
    timestamp written while the test ran (it can't be fixed in advance)
    becomes "<now>"."""
    if isinstance(value, dict):
        return {str(k): _normal(v, started) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [_normal(v, started) for v in value]
        return sorted(items, key=json.dumps) if isinstance(value, (set, frozenset)) else items
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float, Decimal)):
        return round(float(value), 4)
    if isinstance(value, datetime):
        return "<now>" if value.replace(tzinfo=None) >= started - timedelta(minutes=5) else value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        if value[:1] in "[{":
            try:
                return _normal(json.loads(value), started)
            except ValueError:
                pass
        try:
            parsed = datetime.fromisoformat(value)
            if parsed.replace(tzinfo=None) >= started - timedelta(minutes=5):
                return "<now>"
        except ValueError:
            pass
        return value
    if isinstance(value, Path):
        return value.name
    return str(value)


class Recorder:
    def __init__(self, pg):
        self.pg = pg
        self.started = datetime.now()
        self.out: dict = {}

    def value(self, name, value):
        assert name not in self.out, name
        self.out[name] = _normal(value, self.started)

    def tables(self, step):
        snap = {}
        for table, order in TABLES.items():
            cur = self.pg.execute(f"SELECT * FROM {table} ORDER BY {order}")
            cols = [c.name for c in cur.description]
            snap[table] = [dict(zip(cols, row)) for row in cur.fetchall()]
        cur = self.pg.execute(f"SELECT {VIEW_COLUMNS} FROM transactions ORDER BY id")
        cols = [c.name for c in cur.description]
        snap["transactions"] = [dict(zip(cols, row)) for row in cur.fetchall()]
        self.value(f"after {step}", snap)


# ── The dataset ───────────────────────────────────────────────────────────────

def _seed(db, pg):
    dbf._seed_taxonomy()
    dbf.set_subcategory_role(dbf.label_ids("Investments", "Savings Deposits")[1], "saving")

    def t(id, amount, when, **kw):
        insert_transaction(db, id, amount=amount, created_at=when, **kw)

    # Groceries at Tesco, every week -- a rule suggestion in waiting.
    for i in range(6):
        t(f"tesco_{i}", -20 - i, f"2026-0{6 + i // 2}-{3 + 7 * (i % 2):02d} 10:00:00", merchant_name="Tesco",
          user_context="Weekly shop", llm_category="Food & Drink", llm_subcategory="Groceries",
          llm_model="claude-sonnet", llm_confidence=0.9)
    t("rent_1", -1315, "2026-07-01 09:00:00", counterparty_name="Landlord", user_context="Rent",
      llm_category="Bills & Utilities", llm_subcategory="Rent", llm_model="manual")
    t("rent_2", -1315, "2026-08-01 09:00:00", counterparty_name="Landlord", user_context="Rent",
      llm_category="Bills & Utilities", llm_subcategory="Rent", llm_model="claude-sonnet")
    t("deposit", -282, "2026-08-15 12:00:00", counterparty_name="Lettings Co", user_context="Holding deposit",
      llm_category="Bills & Utilities", llm_subcategory="Rent", llm_model="claude-sonnet")
    t("salary_1", 2500, "2026-07-25 08:00:00", counterparty_name="ACME LTD", user_context="Salary",
      llm_category="Income", llm_subcategory="Salary", llm_model="rule")
    t("salary_2", 2500, "2026-08-25 08:00:00", counterparty_name="ACME LTD", user_context="Salary",
      llm_category="Income", llm_subcategory="Salary", llm_model="rule")
    t("refund", 15.5, "2026-08-20 14:00:00", merchant_name="Amazon", user_context="Returned a cable",
      llm_category="Income", llm_subcategory="Refunds", llm_model="claude-sonnet")
    t("isa", -250, "2026-09-02 07:00:00", counterparty_name="Trading 212", user_context="ISA top-up",
      llm_category="Investments", llm_subcategory="Stocks & Shares ISA Contributions", llm_model="manual")
    t("pot_in", -100, "2026-09-03 07:00:00", user_context="Moved into savings pot",
      llm_category="Investments", llm_subcategory="Savings Deposits", llm_model="rule", scheme="uk_retail_pot")
    t("pot_out", 40, "2026-09-20 07:00:00", user_context="Moved out of savings pot",
      llm_category="Investments", llm_subcategory="Savings Deposits", llm_model="rule", scheme="uk_retail_pot")
    t("to_friend", -30, "2026-09-05 19:00:00", counterparty_name="Sam Friend", user_context="Dinner split",
      llm_category="Transfers", llm_subcategory="Outbound Transfer", llm_model="quick-tap")
    t("coffee", -3.4, "2026-09-10 08:30:00", merchant_name="Pret", user_context="Coffee",
      llm_category="Food & Drink", llm_model="claude-sonnet")  # parent-only label
    t("netflix", -10.99, "2026-09-12 00:01:00", merchant_name="Netflix", user_context="Netflix",
      llm_category="Subscriptions & Software", llm_subcategory="Streaming", llm_model="claude-sonnet")
    t("skipped", -5, "2026-09-14 12:00:00", merchant_name="Test", skipped=True)
    t("unanswered", -12, "2026-10-20 12:00:00", merchant_name="Boots", answered=False)
    t("tapped", -8.75, "2026-10-21 13:00:00", merchant_name="Sainsbury's", user_context="Food & Drink - Groceries")
    t("hmrc", -3000, "2026-10-22 09:00:00", counterparty_name="HMRC", user_context="Self assessment")
    t("dentist", -45, "2026-10-23 16:00:00", merchant_name="Hub Dental", user_context="Check-up")
    t("fuel", -60, "2026-10-24 18:00:00", merchant_name="Shell", user_context="Petrol")
    t("held", -99, "2026-10-24 19:00:00", merchant_name="Mystery", user_context="Something odd")

    pg.execute("""INSERT INTO rules (id, name, match_field, match_type, match_value, auto_context, subcategory_id)
                  VALUES (1, 'Rent', 'counterparty_name', 'exact', 'Landlord', 'Rent', %s)""",
               [dbf.label_ids("Bills & Utilities", "Rent")[1]])
    pg.execute("""INSERT INTO rules (id, name, match_field, match_type, match_value, enabled)
                  VALUES (2, 'Old', 'merchant_name', 'contains', 'Netflix', FALSE)""")
    pg.execute("""INSERT INTO category_proposals (id, options, txn_ids, examples, status, proposed_at)
                  VALUES (1, '[{"parent_name": "Mystery", "subcategory_name": "Odd Things", "parent_is_new": true,
                               "rationale": "x"}]', '["held"]', '[]', 'pending', '2026-10-24 20:00')""")
    pg.execute("""INSERT INTO category_proposals (id, options, txn_ids, status, regenerate_requested, proposed_at)
                  VALUES (2, '[{"parent_name": "Gifts", "subcategory_name": "Weddings", "parent_is_new": true,
                               "rationale": "y"}]', '[]', 'denied', FALSE, '2026-09-01')""")
    pg.execute("""INSERT INTO taxonomy_proposals (id, parent_name, source_sub, proposed_sub, rationale, evidence_ids,
                      status, run_key, proposed_at)
                  VALUES (1, 'Food & Drink', 'Groceries', 'Meal Deals', 'z', '["tesco_0"]', 'denied', '2026-09',
                          '2026-09-01')""")
    pg.execute("""INSERT INTO judge_reviews VALUES ('rent_1', 'landlord', 'Bills & Utilities', 'Rent', 'kept',
                  '2026-08-02')""")
    pg.execute("""INSERT INTO subscriptions (id, name, merchant_name, amount, frequency, active, created_at)
                  VALUES (1, 'Netflix', 'Netflix', 10.99, 'monthly', TRUE, '2026-09-12')""")
    pg.execute("""INSERT INTO summaries VALUES ('week', '2026-W30', 20, 0, -20, 0, 0, '2026-07-27'),
                                               ('month', '2026-06', 41, 0, -41, 0, 0, '2026-07-01')""")


# ── Fakes for the parts that would leave the machine ─────────────────────────

class FakeClaude:
    """Answers the placement judge: objects to the holding deposit and the
    dentist, approves anything else."""

    def __init__(self):
        self.messages = self

    def create(self, model, max_tokens, messages):
        prompt = messages[0]["content"]
        if "Holding deposit" in prompt:
            verdict = {"verdict": "reject", "confidence": 0.9, "issue": "distorts_totals", "reason": "One-off.",
                       "suggested_parent": "Bills & Utilities", "suggested_subcategory": "Rent Deposit",
                       "suggested_is_new": True}
        else:
            verdict = {"verdict": "approve", "confidence": 0.9, "issue": "none", "reason": "Fits.",
                       "suggested_parent": None, "suggested_subcategory": None, "suggested_is_new": False}
        return SimpleNamespace(content=[SimpleNamespace(text=json.dumps(verdict))])


@pytest.fixture
def frozen(monkeypatch):
    for module in (weekly_summary, monthly_summary):
        monkeypatch.setattr(module, "date", FrozenDate)
    sent = []
    monkeypatch.setattr(weekly_summary, "send_telegram", lambda text: sent.append(text) or True)
    monkeypatch.setattr(monthly_summary, "send_telegram", lambda text: sent.append(text) or True)
    monkeypatch.setattr(alerts, "send_telegram", lambda text: sent.append(text) or True)
    monkeypatch.setattr(rs, "CLAUDE_SECRET", "test")
    monkeypatch.setattr(rs, "filter_worth_a_rule",
                        lambda client, cands: [{**c, "llm_reasoning": "Single-purpose."} for c in cands])
    monkeypatch.setattr(ll, "CLAUDE_SECRET", "test")
    monkeypatch.setattr(ll, "anthropic", SimpleNamespace(Anthropic=lambda **kw: FakeClaude()))
    monkeypatch.setattr(pj, "JUDGE_ENABLED", True)
    return sent


# ── The run ───────────────────────────────────────────────────────────────────

def test_everything_behaves_exactly_as_recorded(db, pg, frozen, tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "backups"))
    rec = Recorder(pg)
    _seed(db, pg)
    rec.tables("seeding")

    # Reading
    rec.value("get_all_transactions", dbf.get_all_transactions())
    rec.value("get_transaction", dbf.get_transaction("deposit"))
    rec.value("get_transaction missing", dbf.get_transaction("nope"))
    rec.value("get_unclassified", dbf.get_unclassified())
    rec.value("get_by_category", dbf.get_by_category("Food & Drink"))
    rec.value("get_skipped", dbf.get_skipped())
    rec.value("get_recent", dbf.get_recent(5))
    rec.value("get_parents", dbf.get_parents())
    rec.value("get_subcategories", dbf.get_subcategories())
    rec.value("search", dbf.search("tes"))
    rec.value("search case", dbf.search("LANDLORD"))
    rec.value("get_stats", dbf.get_stats())
    rec.value("label_ids", [dbf.label_ids("Food & Drink", "Groceries"), dbf.label_ids("Food & Drink", None),
                            dbf.label_ids("Nope", "x"), dbf.label_ids(None, None),
                            dbf.label_ids("Food & Drink", "Nope")])
    stats = dbf.get_subcategory_stats()
    rec.value("get_subcategory_stats", {f"{k[0]} / {k[1]}": v for k, v in sorted(stats.items())})
    rec.value("format_subcategory_stats", {f"{k[0]} / {k[1]}": dbf.format_subcategory_stats(v)
                                           for k, v in sorted(stats.items())})
    rec.value("get_subcategory_examples", dbf.get_subcategory_examples("Food & Drink", "Groceries", 3))
    rec.value("get_merchant_history", dbf.get_merchant_history("tesco", "tesco_0"))
    for a, b in [("2026-06-01", "2026-06-30"), ("2026-08-01", "2026-08-31"), ("2026-09-01", "2026-09-30"),
                 ("2026-01-01", "2026-12-31")]:
        rec.value(f"get_totals_by_role {a}", dbf.get_totals_by_role(a, b))
        for role in dbf.ROLES:
            rec.value(f"get_category_totals_by_role {role} {a}", dbf.get_category_totals_by_role(role, a, b))
    rec.value("get_subscriptions", dbf.get_subscriptions())
    rec.value("denied_parent_names", cp.denied_parent_names())
    rec.value("denied_sub_names", cp.denied_sub_names())
    rec.value("held_by", cp.held_by(dbf.get_con(), 1))
    rec.value("_examples", cp._examples(dbf.get_con(), ["held", "hmrc", "tesco_0"]))
    rec.value("pending_regenerations", cp.pending_regenerations())
    rec.value("reviewable_subcategories", tr.reviewable_subcategories())
    rec.value("transactions_in", tr.transactions_in("Food & Drink", "Groceries"))
    rec.value("full_taxonomy", tr.full_taxonomy())
    rec.value("taken_category_names", tr.taken_category_names())
    rec.value("already_reviewed", [tr.already_reviewed("2026-09"), tr.already_reviewed("2026-10")])
    rec.value("previously_proposed_names", tr.previously_proposed_names())
    rec.value("find_candidates", rs.find_candidates())
    rec.value("_not_yet_asked", rs._not_yet_asked(dbf.get_con(), rs.find_candidates()))
    rec.value("judge_backlog candidates", jb.candidates())
    rec.value("get_missing_weeks", weekly_summary.get_missing_weeks())
    rec.value("get_week_stats", {w: weekly_summary.get_week_stats(w)
                                 for w in ["2026-W23", "2026-W31", "2026-W35", "2026-W36", "2026-W43"]})
    rec.value("get_missing_months", monthly_summary.get_missing_months())
    rec.value("get_month_stats", {m: monthly_summary.get_month_stats(m)
                                  for m in ["2026-06", "2026-07", "2026-08", "2026-09"]})

    # Writing: labels and history
    rec.value("quick taps applied", dbf.apply_quick_tap_classifications())
    dbf.update_classification("hmrc", "Bills & Utilities", "Council Tax", 0.7, "claude-sonnet")
    dbf.update_classification("hmrc", "Bills & Utilities", None, None, "manual")
    with pytest.raises(ValueError):
        dbf.update_classification("hmrc", "Nope", "Never", None, "manual")
    dbf.clear_label("coffee")
    dbf.clear_label("netflix", keep_parent=True)
    dbf.update_transaction_details("fuel", "2026-10-24 18:00:00", "Petrol, full tank")
    dbf.update_transaction_details("dentist", datetime(2026, 10, 22, 9, 30), "Check-up, moved")
    rec.tables("labels and history")

    # Category proposals: register, merge, decide
    options = [{"parent_name": "Tax", "subcategory_name": "Self Assessment", "parent_is_new": True,
                "rationale": "Tax."},
               {"parent_name": "Bills & Utilities", "subcategory_name": "Tax Bills", "parent_is_new": False,
                "rationale": "Bills."}]
    rec.value("register new", cp.register_group(options, ["hmrc"]))
    rec.value("register merge", cp.register_group(list(reversed(options)), ["dentist"]))
    rec.value("register moves a hold", cp.register_group(
        [{"parent_name": "Odd", "subcategory_name": "Things", "parent_is_new": True, "rationale": "o"}], ["held"]))
    rec.tables("registering proposals")
    pid = rec.out["register new"][0]
    pg.execute("UPDATE category_proposals SET status = 'selected', selected_option = 0 WHERE id = %s", [int(pid)])
    pg.execute("UPDATE category_proposals SET status = 'denied', regenerate_requested = TRUE WHERE id = 1")
    rec.value("collect_decisions", cp.collect_decisions())
    rec.value("pending_regenerations after", cp.pending_regenerations())
    rec.value("apply_selected unknown", cp.apply_selected(999, 0))
    rec.value("deny_all", cp.deny_all(int(rec.out["register moves a hold"][0])))
    rec.tables("category decisions")

    # Rule suggestions
    rec.value("sync_candidates", rs.sync_candidates())
    rec.value("sync_candidates again", rs.sync_candidates())
    rec.tables("rule suggestions")

    # Monthly taxonomy proposals
    stored = tr.store_proposals([{"parent_name": "Food & Drink", "source_sub": "Groceries", "action": "create",
                                  "target_parent": "Food & Drink", "target_sub": "Big Shops",
                                  "rationale": "Large ones.", "evidence_ids": ["tesco_4", "tesco_5"],
                                  "evidence_count": 2, "examples": ["Weekly shop"]}], "2026-10")
    rec.value("store_proposals", stored)
    tr._record_empty_run("2026-08")
    pg.execute("UPDATE taxonomy_proposals SET status = 'approved' WHERE id = %s", [stored[0]["id"]])
    rec.value("taxonomy collect_decisions", tr.collect_decisions())
    rec.value("already_reviewed after", [tr.already_reviewed("2026-08"), tr.already_reviewed("2026-10")])
    rec.tables("taxonomy decisions")

    # The placement judge and the classifier, with Claude faked
    reviewer = ll.make_placement_reviewer()
    txn = dbf.get_transaction("deposit")
    rec.value("review_tap objects", reviewer.review_tap(txn, "Bills & Utilities", "Rent"))
    rec.value("review_tap settled", reviewer.review_tap(dbf.get_transaction("rent_2"), "Bills & Utilities", "Rent"))
    rec.value("review_tap approves", reviewer.review_tap(dbf.get_transaction("fuel"), "Transport", "Fuel"))
    rec.value("review_tap approves again", reviewer.review_tap(dbf.get_transaction("fuel"), "Transport", "Fuel"))
    monkeypatch.setattr(ll, "match_existing", lambda client, txns, subs: {
        t["id"]: {"category": "Transport", "subcategory": "Fuel"} for t in txns if t["id"] == "fuel"})
    monkeypatch.setattr(ll, "classify_parents", lambda client, txns, parents, denied_parent_names=None: {
        t["id"]: ("Health" if t["id"] == "dentist" else "Shopping") for t in txns})
    monkeypatch.setattr(ll, "classify_subcategories",
                        lambda client, txns, parent, subs, all_parents, denied_sub_names=None: {
                            t["id"]: ("Dental" if parent == "Health" else "Gadgets") for t in txns})
    monkeypatch.setattr(ll, "propose_alternatives", lambda client, groups, parents, subs: {
        0: [{"parent_name": "Shopping", "subcategory_name": "Electronics", "rationale": "Close enough."}]})
    rec.value("unclassified before run", [t["id"] for t in dbf.get_unclassified()])
    ll.run()
    rec.tables("classifier run")

    # Summaries
    weekly_summary.run()
    monthly_summary.run()
    rec.value("telegram messages", frozen)
    rec.tables("summaries")

    # Reshaping the taxonomy
    food = dbf.label_ids("Food & Drink", None)[0]
    groceries = dbf.label_ids("Food & Drink", "Groceries")[1]
    dbf.rename_parent(food, "Food")
    dbf.set_parent_role(food, "spend")
    dbf.set_subcategory_role(groceries, "excluded")
    dbf.set_subcategory_role(groceries, None)
    transport = dbf.label_ids("Transport", None)[0]
    dbf.edit_subcategory(groceries, "Food Shopping", transport)
    rec.value("move_all_transactions", dbf.move_all_transactions(
        dbf.label_ids("Transport", "Food Shopping")[1], dbf.label_ids("Shopping", "General Retail")[1]))
    rec.value("delete_subcategory", dbf.delete_subcategory(dbf.label_ids("Income", "Refunds")[1]))
    rec.value("wipe_parent_labels", dbf.wipe_parent_labels(dbf.label_ids("Investments", None)[0]))
    rec.value("delete_parent", dbf.delete_parent(dbf.label_ids("Transfers", None)[0]))
    rec.value("upsert existing", dbf.upsert_parent("Food"))
    rec.value("upsert new", [dbf.upsert_subcategory("Bakery", dbf.upsert_parent("Treats"))])
    with pytest.raises(ValueError):
        dbf.set_parent_role(food, "nonsense")
    rec.value("parents after reshaping", dbf.get_parents())
    rec.value("subcategories after reshaping", dbf.get_subcategories())
    rec.value("totals after reshaping", dbf.get_totals_by_role("2026-01-01", "2026-12-31"))
    rec.tables("reshaping the taxonomy")

    # Subscriptions
    sid = dbf.upsert_subscription("Spotify", 11.99, "monthly", "Spotify")
    dbf.update_subscription(sid, "Spotify Duo", 14.99, "monthly", "Spotify")
    dbf.toggle_subscription(1)
    dbf.upsert_subscription("Gym", 30, "monthly")
    dbf.delete_subscription(sid)
    rec.value("subscriptions after edits", dbf.get_subscriptions())
    rec.tables("subscriptions")

    # A backup: every table, read back
    import pandas as pd
    target = dbf.backup_db("characterization")
    rec.value("backup files", sorted(p.name for p in target.iterdir()))
    rec.value("backup manifest", {k: v for k, v in dbf.read_backup_manifest(target).items() if k != "created_at"})
    for table in ("label_events", "category_proposals", "subcategories"):
        frame = pd.read_parquet(target / f"{table}.parquet")
        rows = frame.astype(object).where(frame.notna(), None).to_dict("records")
        key = "event_id" if table == "label_events" else "id"
        rec.value(f"backup {table}", sorted(rows, key=lambda r: r[key]))

    # Starting the taxonomy again
    rec.value("wipe_taxonomy", dbf.wipe_taxonomy())
    dbf._seed_taxonomy()
    rec.value("parents after reseeding", [p["name"] for p in dbf.get_parents()])
    rec.tables("wiping and reseeding")

    _compare_or_record(rec.out)


def _compare_or_record(out: dict):
    if os.environ.get("CHAR_DUMP"):
        Path(os.environ["CHAR_DUMP"]).write_text(json.dumps(out, sort_keys=True, ensure_ascii=False), encoding="utf-8")
    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(json.dumps(out, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
        pytest.skip("golden file re-recorded -- review its diff")
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    actual = json.loads(json.dumps(out, sort_keys=True, ensure_ascii=False))
    differing = [k for k in expected if expected[k] != actual.get(k)]
    missing = [k for k in actual if k not in expected]
    lines = []
    for k in differing[:8]:
        e, a = expected[k], actual.get(k)
        if isinstance(e, dict) and isinstance(a, dict):
            for sub in sorted(set(e) | set(a)):
                if e.get(sub) != a.get(sub):
                    lines.append(f"{k} / {sub}:\n  recorded: {json.dumps(e.get(sub))[:600]}\n"
                                 f"  now:      {json.dumps(a.get(sub))[:600]}")
        else:
            lines.append(f"{k}:\n  recorded: {json.dumps(e)[:600]}\n  now:      {json.dumps(a)[:600]}")
    assert not differing and not missing, (
        f"{len(differing)} recorded result(s) differ, {len(missing)} new: {differing + missing}\n" + "\n".join(lines))
