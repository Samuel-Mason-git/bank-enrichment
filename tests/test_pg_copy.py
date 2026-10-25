"""pg_copy.py: today's two DuckDB databases into the new Postgres tables.

A small version of both databases is built with every case the copy has to get
right -- a corrected date, a parent-only label, a held transaction, decisions
the PC hasn't collected yet, transactions still only in the server's queue --
then copied, checked, and looked at directly. Needs a Postgres (the `pg`
fixture), so these run in CI."""
import json

import duckdb
import pytest

import database_functions
from pg_copy import CopyProblems, NEW_TABLES, check_all, copy_all
from server_db import split_sql_statements
from tests.conftest import _SERVER_SQL_PATH


def _payload(txn_id, amount, created, merchant="Tesco"):
    return {
        "type": "transaction.created",
        "data": {
            "id": txn_id, "amount": amount, "currency": "GBP", "description": merchant.upper(),
            "category": "groceries", "merchant": {"name": merchant, "category": "groceries"},
            "counterparty": {}, "is_load": False, "created": created, "settled": "",
        },
    }


@pytest.fixture
def server():
    con = duckdb.connect(":memory:")
    for statement in split_sql_statements(_SERVER_SQL_PATH.read_text()):
        con.execute(statement)
    yield con
    con.close()


@pytest.fixture
def seeded(db, server):
    """Both DuckDB databases, filled the way today's code fills them."""
    local = db
    local.execute("INSERT INTO parent_categories VALUES (1, 'Food', '2026-01-01'), (2, 'Income', '2026-01-01')")
    local.execute("INSERT INTO subcategories VALUES (1, 'Groceries', 1, '2026-01-01'), "
                  "(2, 'Takeaway', 1, '2026-01-01'), (3, 'Salary', 2, '2026-01-01')")
    local.execute("INSERT INTO category_roles VALUES (2, NULL, 'income')")

    def arrive(txn_id, amount, created, context=None, skipped=False):
        # write_to_db itself, so the stored columns are exactly what today's code stores.
        database_functions.write_to_db([{
            "id": txn_id, "payload": _payload(txn_id, amount, created), "user_context": context,
            "skipped": skipped, "received_at": "2026-10-01 10:00:00", "enriched_at": "2026-10-01 10:05:00",
        }])

    arrive("t1", -450, "2026-10-01T09:59:00.123Z", "weekly shop")
    arrive("t2", 250000, "2026-10-01T08:00:00Z", "pay")
    arrive("t3", -100, "2026-10-01T07:00:00Z", skipped=True)
    arrive("t4", -999, "2026-10-01T06:00:00Z", "dinner")
    arrive("t5", -2000, "2026-10-01T05:00:00Z", "something new")

    def label(txn_id, category, subcategory, model="claude", confidence=0.9):
        local.execute(
            "UPDATE transactions SET llm_category = ?, llm_subcategory = ?, llm_model = ?, "
            "llm_confidence = ?, classified_at = '2026-10-01 11:00:00' WHERE id = ?",
            [category, subcategory, model, confidence, txn_id],
        )

    label("t1", "Food", "Groceries")
    label("t2", "Income", "Salary", model="rule", confidence=None)
    label("t4", "Food", None)  # what deleting a subcategory leaves today
    local.execute("UPDATE transactions SET created_at = '2026-09-30 09:00:00' WHERE id = 't2'")  # corrected date

    # t5 is held behind a category proposal the user has picked an option for
    # on the phone, which the PC hasn't collected yet.
    local.execute("INSERT INTO category_proposals (id, options, status, proposed_at) "
                  "VALUES (1, '[{\"parent_name\": \"Food\"}]', 'pending', '2026-10-01 12:00:00')")
    local.execute("UPDATE transactions SET pending_category_proposal_id = 1 WHERE id = 't5'")
    server.execute("INSERT INTO category_proposals (local_id, options, status, selected_option, sent_at, decided_at) "
                   "VALUES (1, '[]', 'selected', 0, '2026-10-01 12:01:00', '2026-10-01 13:00:00')")

    # A taxonomy proposal approved on the phone, not collected yet.
    local.execute(
        "INSERT INTO taxonomy_proposals (id, parent_name, source_sub, proposed_sub, rationale, evidence_ids, "
        "evidence_count, proposed_at, run_key) VALUES (1, 'Food', 'Groceries', 'Bakery', 'bread', '[\"t1\"]', 1, "
        "'2026-10-01 12:00:00', '2026-10')")
    server.execute(
        "INSERT INTO taxonomy_proposals (local_id, parent_name, source_sub, proposed_sub, rationale, status, "
        "sent_at, decided_at, follow_up_count) VALUES (1, 'Food', 'Groceries', 'Bakery', 'bread', 'approved', "
        "'2026-10-01 12:01:00', '2026-10-01 14:00:00', 2)")

    # The server's queue: t1 already pulled to the PC; q1 waiting for an answer
    # after two cards; q2 answered and filed by a rule, not pulled yet.
    server.execute("INSERT INTO webhook_queue (id, payload, received_at, status) VALUES "
                   "('t1', ?, '2026-10-01 10:00:00', 'processed')", [json.dumps(_payload("t1", -450, "x"))])
    server.execute(
        "INSERT INTO webhook_queue (id, payload, received_at, status, request_count, last_requested_at) "
        "VALUES ('q1', ?, '2026-10-02 10:00:00', 'pending', 2, '2026-10-02 16:00:00')",
        [json.dumps(_payload("q1", -300, "2026-10-02T09:59:00Z"))])
    server.execute(
        "INSERT INTO webhook_queue (id, payload, received_at, status, user_context, enriched_at, "
        "rule_category, rule_subcategory) VALUES ('q2', ?, '2026-10-02 11:00:00', 'enriched', 'gift', "
        "'2026-10-02 11:01:00', 'Income', 'Salary')",
        [json.dumps(_payload("q2", 5000, "2026-10-02T10:59:00Z"))])

    server.execute("INSERT INTO rules (id, name, match_field, match_value, category, subcategory) "
                   "VALUES (1, 'Tesco', 'merchant_name', 'Tesco', 'Food', 'Groceries')")
    server.execute("INSERT INTO rules (id, name, match_field, match_value, auto_skip) "
                   "VALUES (2, 'Pots', 'description', 'POT', TRUE)")
    server.execute("INSERT INTO rule_suggestions (id, match_field, match_value, category, subcategory, txn_count, "
                   "proposed_at) VALUES (1, 'merchant_name', 'Greggs', 'Food', 'Takeaway', 6, '2026-10-01')")
    server.execute("INSERT INTO quick_categories VALUES (1, 'Food', 'Groceries', NULL, 0)")

    local.execute("INSERT INTO judge_reviews VALUES ('t1', 'tesco', 'Food', 'Groceries', 'approved', '2026-10-01')")
    local.execute("INSERT INTO subscriptions (id, name, amount, frequency, created_at) "
                  "VALUES (1, 'Netflix', 10.99, 'monthly', '2026-01-01')")
    local.execute("INSERT INTO weekly_summaries (id, send_date, total_spend, total_income, net, sent_at) "
                  "VALUES (1, '2026-W40', 10, 20, 10, '2026-10-05')")
    local.execute("INSERT INTO monthly_summaries (id, send_date, total_spend, total_income, net, sent_at) "
                  "VALUES (1, '2026-09', 100, 200, 100, '2026-10-01')")
    return local, server


def _one(pg, sql, params=()):
    cur = pg.execute(sql, params)
    row = cur.fetchone()
    return dict(zip([c.name for c in cur.description], row)) if row else None


def test_the_copy_checks_out_with_no_differences(seeded, pg):
    local, server = seeded
    counts, notes = copy_all(local, server, pg)
    assert counts["transactions_raw"] == 7  # 5 on the PC + 2 only on the server
    differences, _ = check_all(local, server, pg)
    assert differences == []
    assert notes["transactions only on the server so far"] == 2
    assert notes["transactions whose date was corrected"] == 1


def test_running_it_again_gives_the_same_result(seeded, pg):
    local, server = seeded
    first, _ = copy_all(local, server, pg)
    second, _ = copy_all(local, server, pg)
    assert first == second
    assert check_all(local, server, pg)[0] == []


def test_a_corrected_date_becomes_an_override_and_monzos_dates_are_left_alone(seeded, pg):
    copy_all(*seeded, pg)
    overrides = dict(pg.execute(
        "SELECT txn_id, created_at_override FROM context_events WHERE source = 'migration'").fetchall())
    assert str(overrides["t2"]) == "2026-09-30 09:00:00"
    assert overrides["t1"] is None and overrides["t3"] is None


def test_labels_keep_their_model_and_a_parent_only_label_stays_parent_only(seeded, pg):
    copy_all(*seeded, pg)
    t2 = _one(pg, "SELECT llm_category, llm_subcategory, llm_model FROM transactions WHERE id = 't2'")
    assert t2 == {"llm_category": "Income", "llm_subcategory": "Salary", "llm_model": "rule"}
    t4 = _one(pg, "SELECT llm_category, llm_subcategory FROM transactions WHERE id = 't4'")
    assert t4 == {"llm_category": "Food", "llm_subcategory": None}


def test_the_classifiers_to_do_list_matches_todays(seeded, pg):
    local, server = seeded
    copy_all(local, server, pg)
    old = {r[0] for r in local.execute(
        "SELECT id FROM transactions WHERE (llm_category IS NULL OR llm_subcategory IS NULL) "
        "AND skipped = FALSE AND pending_category_proposal_id IS NULL").fetchall()}
    new = {r[0] for r in pg.execute(
        "SELECT id FROM needs_label WHERE id IN ('t1', 't2', 't3', 't4', 't5')").fetchall()}
    assert new == old == {"t4"}  # t3 skipped, t5 held


def test_a_held_transaction_stays_held_and_an_uncollected_choice_counts(seeded, pg):
    copy_all(*seeded, pg)
    proposal = _one(pg, "SELECT status, selected_option, txn_ids, sent_at FROM category_proposals")
    assert (proposal["status"], proposal["selected_option"], proposal["txn_ids"]) == ("selected", 0, ["t5"])
    assert proposal["sent_at"] is not None
    tax = _one(pg, "SELECT status, follow_up_count, decided_at FROM taxonomy_proposals")
    assert (tax["status"], tax["follow_up_count"]) == ("approved", 2)
    assert tax["decided_at"] is not None


def test_transactions_only_on_the_server_come_across_with_their_follow_ups(seeded, pg):
    copy_all(*seeded, pg)
    waiting = _one(pg, "SELECT id, request_count FROM needs_context")
    assert waiting == {"id": "q1", "request_count": 2}
    q2 = _one(pg, "SELECT user_context, llm_category, llm_subcategory FROM transactions WHERE id = 'q2'")
    assert q2 == {"user_context": "gift", "llm_category": "Income", "llm_subcategory": "Salary"}
    assert _one(pg, "SELECT source FROM label_events WHERE txn_id = 'q2'") == {"source": "rule"}


def test_rules_point_at_ids_and_summaries_merge(seeded, pg):
    copy_all(*seeded, pg)
    assert dict(pg.execute("SELECT id, subcategory_id FROM rules").fetchall()) == {1: 1, 2: None}
    assert pg.execute("SELECT period, period_key FROM summaries ORDER BY period").fetchall() == [
        ("month", "2026-09"), ("week", "2026-W40")]


def test_a_name_that_isnt_in_the_taxonomy_stops_the_copy_before_anything_is_written(seeded, pg):
    local, server = seeded
    copy_all(local, server, pg)
    local.execute("UPDATE transactions SET llm_category = 'Gone' WHERE id = 't1'")
    with pytest.raises(CopyProblems) as e:
        copy_all(local, server, pg)
    assert any("'Gone'" in p for p in e.value.problems)
    # The earlier copy is untouched.
    assert pg.execute("SELECT count(*) FROM transactions_raw").fetchone()[0] == 7


def test_the_check_notices_a_difference(seeded, pg):
    local, server = seeded
    copy_all(local, server, pg)
    pg.execute("UPDATE subscriptions SET amount = 12.99")
    differences, _ = check_all(local, server, pg)
    assert differences and differences[0].startswith("subscriptions")


def test_every_new_table_is_covered():
    assert len(NEW_TABLES) == 13
