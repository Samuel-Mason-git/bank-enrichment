"""The finance agent (src/pipeline/finance_agent.py): its tools against a real
schema, the read-only SQL guard, and the question loop with a fake client."""
import json
from types import SimpleNamespace

import pytest

import finance_agent as fa
from tests.local_helpers import insert_transaction


# ── data ──────────────────────────────────────────────────────────────────────
#
# October 2026, as the Overview page would count it:
#   spend 57 (20 + 30 groceries, 7 unclassified), income 2000, saved 200 net,
#   transferred out 500 / in 100. The skipped -999 counts nowhere.

@pytest.fixture
def money(db):
    rows = [
        ("g1", -20, "2026-10-05", "Food & Drink", "Groceries", dict(merchant_name="Tesco", user_context="weekly shop")),
        ("g2", -30, "2026-10-20", "Food & Drink", "Groceries", dict(merchant_name="Tesco")),
        ("r1", -50, "2026-09-10", "Food & Drink", "Restaurants", dict(merchant_name="Nandos")),
        ("s1", 2000, "2026-10-01", "Income", "Salary", dict(counterparty_name="ACME LTD")),
        ("t1", -500, "2026-10-02", "Transfers", "Outbound Transfer", dict(counterparty_name="Me")),
        ("t2", 100, "2026-10-03", "Transfers", "Inbound Transfer", dict(counterparty_name="Me")),
        ("p1", -300, "2026-10-04", "Savings", "Pot", dict(description="pot_123")),
        ("p2", 100, "2026-10-15", "Savings", "Pot", dict(description="pot_123")),
        ("u1", -7, "2026-10-07", None, None, dict(merchant_name="Corner Shop")),
        ("k1", -999, "2026-10-06", "Food & Drink", "Groceries", dict(merchant_name="Tesco", skipped=True)),
    ]
    for tid, amount, day, cat, sub, extra in rows:
        insert_transaction(db, tid, amount=amount, created_at=f"{day} 12:00:00",
                           llm_category=cat, llm_subcategory=sub, **extra)
    savings = db.execute("SELECT id FROM parent_categories WHERE name = 'Savings'").fetchone()[0]
    db.execute("INSERT INTO category_roles (parent_id, subcategory_id, role) VALUES (%s, NULL, 'saving')", [savings])
    db.execute("INSERT INTO subscriptions (id, name, amount, frequency, active, created_at) VALUES "
               "(1, 'Netflix', 10.99, 'monthly', TRUE, now()), (2, 'Domain', 12, 'annual', TRUE, now()), "
               "(3, 'Old gym', 30, 'monthly', FALSE, now())")
    return db


OCT = {"date_from": "2026-10-01", "date_to": "2026-10-31"}


# ── get_totals ────────────────────────────────────────────────────────────────

def test_totals_count_money_the_way_the_overview_does(money):
    t = fa.get_totals(OCT)["totals"]
    assert t == {"spend": 57.0, "income": 2000.0, "saved": 200.0, "transferred_out": 500.0,
                 "transferred_in": 100.0, "transactions": 8}


def test_totals_by_category_and_month(money):
    by_cat = {g["category"]: g for g in fa.get_totals({**OCT, "group_by": "category"})["groups"]}
    assert by_cat["Food & Drink"]["spend"] == 50.0
    assert by_cat["Unclassified"]["spend"] == 7.0
    assert "spend" not in by_cat["Income"]                       # zero figures are left out
    months = fa.get_totals({"category": "food & drink", "group_by": "month"})["groups"]
    assert [(m["month"], m["spend"]) for m in months] == [("2026-09", 50.0), ("2026-10", 50.0)]


def test_totals_by_subcategory_and_merchant(money):
    subs = fa.get_totals({**OCT, "group_by": "subcategory", "category": "Food & Drink"})["groups"]
    assert subs == [{"category": "Food & Drink", "subcategory": "Groceries", "spend": 50.0, "transactions": 2}]
    merchants = fa.get_totals({**OCT, "group_by": "merchant"})["groups"]
    assert merchants[0]["merchant"] == "ACME LTD"                 # biggest figure first


def test_totals_reject_a_bad_date_or_grouping(money):
    with pytest.raises(fa.ToolError):
        fa.get_totals({"date_from": "last month"})
    with pytest.raises(fa.ToolError):
        fa.get_totals({"group_by": "week"})


# ── find_transactions ─────────────────────────────────────────────────────────

def test_find_by_text_matches_merchant_and_note_and_totals_every_match(money):
    out = fa.find_transactions({"text": "tesco", "limit": 1})
    assert out["matches"] == 2 and out["money_out"] == 50.0       # the skipped one isn't counted
    assert len(out["transactions"]) == 1 and "showing 1 of 2" in out["note"]
    assert fa.find_transactions({"text": "weekly shop"})["transactions"][0]["id"] == "g1"


def test_find_filters_direction_size_and_sorts(money):
    out = fa.find_transactions({**OCT, "direction": "in", "sort": "largest"})
    assert [t["id"] for t in out["transactions"]] == ["s1", "t2", "p2"]
    out = fa.find_transactions({**OCT, "direction": "out", "min_amount": 25, "max_amount": 400})
    assert {t["id"] for t in out["transactions"]} == {"g2", "p1"}
    with pytest.raises(fa.ToolError):
        fa.find_transactions({"direction": "sideways"})


# ── subscriptions and categories ──────────────────────────────────────────────

def test_subscriptions_cost_only_the_active_ones(money):
    out = fa.list_subscriptions({})
    assert out["active_per_month"] == pytest.approx(10.99 + 1.0)
    assert out["active_per_year"] == pytest.approx((10.99 + 1.0) * 12)
    assert {s["name"]: s["active"] for s in out["subscriptions"]} == {"Netflix": True, "Domain": True, "Old gym": False}


def test_categories_carry_their_roles(money):
    cats = {c["name"]: c for c in fa.list_categories({})["categories"]}
    assert cats["Savings"]["role"] == "saving" and cats["Transfers"]["role"] == "transfer"
    assert {s["name"] for s in cats["Food & Drink"]["subcategories"]} == {"Groceries", "Restaurants"}


# ── run_sql ───────────────────────────────────────────────────────────────────

def test_sql_runs_a_select(money):
    out = fa.run_sql({"query": "SELECT llm_category, SUM(amount) AS total FROM transaction_roles "
                               "WHERE id IN ('g1', 'g2') GROUP BY 1;"})
    assert out == {"columns": ["llm_category", "total"], "rows": [["Food & Drink", -50.0]]}


@pytest.mark.parametrize("query", [
    "DELETE FROM subscriptions",
    "SELECT 1; DELETE FROM subscriptions",
    "",
])
def test_sql_refuses_anything_but_one_select(money, query):
    with pytest.raises(fa.ToolError):
        fa.run_sql({"query": query})
    assert money.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0] == 3


def test_sql_cannot_write_even_disguised_as_a_select(money):
    with pytest.raises(fa.ToolError, match="read-only"):
        fa.run_sql({"query": "WITH gone AS (DELETE FROM subscriptions RETURNING 1) SELECT * FROM gone"})
    assert money.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0] == 3


def test_sql_times_out_and_caps_rows(money, monkeypatch):
    monkeypatch.setattr(fa, "SQL_TIMEOUT_MS", 200)
    with pytest.raises(fa.ToolError, match="(?i)cancel"):
        fa.run_sql({"query": "SELECT pg_sleep(2)"})
    monkeypatch.setattr(fa, "SQL_MAX_ROWS", 5)
    out = fa.run_sql({"query": "SELECT generate_series(1, 50) AS n"})
    assert len(out["rows"]) == 5 and "cut at 5" in out["note"]


def test_the_connection_still_works_after_a_failed_query(money):
    with pytest.raises(fa.ToolError):
        fa.run_sql({"query": "SELECT * FROM no_such_table"})
    assert fa.get_totals(OCT)["totals"]["spend"] == 57.0


# ── run_tool ──────────────────────────────────────────────────────────────────

def test_run_tool_turns_problems_into_readable_errors(money):
    assert fa.run_tool("nope", {}) == ("No tool called 'nope'.", True)
    text, is_error = fa.run_tool("get_totals", {"group_by": "week"})
    assert is_error and "group_by" in text
    text, is_error = fa.run_tool("get_totals", OCT)
    assert not is_error and json.loads(text)["totals"]["spend"] == 57.0


def test_every_tool_has_a_definition_and_a_label():
    assert {t["name"] for t in fa.TOOLS} == set(fa.TOOL_FUNCTIONS) == set(fa.TOOL_LABELS)


# ── the loop ──────────────────────────────────────────────────────────────────

def _usage(i=1000, o=100):
    return SimpleNamespace(input_tokens=i, output_tokens=o, cache_creation_input_tokens=0, cache_read_input_tokens=0)


def tool_use(name, args, id="tu1"):
    return SimpleNamespace(content=[SimpleNamespace(type="thinking", thinking="", signature="s"),
                                    SimpleNamespace(type="tool_use", id=id, name=name, input=args)],
                           stop_reason="tool_use", usage=_usage(), model="claude-haiku-5-5")


def reply(text, stop="end_turn"):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason=stop,
                           usage=_usage(), model="claude-haiku-5-5")


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.messages = self

    def create(self, **kwargs):
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        out = self.responses.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def test_a_question_runs_tools_then_answers(money):
    client = FakeClient([tool_use("get_totals", OCT), reply("You spent £57.00 in October.")])
    steps = []
    answer = fa.FinanceAgent(client, daily_limit_usd=1).ask("How much did I spend in October?", on_step=steps.append)

    assert answer.text == "You spent £57.00 in October."
    assert [s.tool for s in answer.steps] == ["get_totals"] and steps == answer.steps
    assert json.loads(answer.steps[0].output)["totals"]["spend"] == 57.0
    assert answer.cost_usd > 0
    # The second request carried the tool's result back, and the thinking block unchanged.
    second = client.requests[1]["messages"]
    assert second[1]["content"][0].type == "thinking"
    assert second[2]["content"][0]["type"] == "tool_result" and second[2]["content"][0]["tool_use_id"] == "tu1"
    first = client.requests[0]
    assert first["model"] == "claude-haiku-5-5" and first["thinking"] == {"type": "adaptive"}
    assert first["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_each_call_is_recorded_with_tool_names_only(money):
    fa.FinanceAgent(FakeClient([tool_use("list_categories", {}), reply("ok")]), daily_limit_usd=1).ask("q")
    rows = money.execute("SELECT job, params->'tools', cost_usd FROM llm_calls ORDER BY call_id").fetchall()
    assert len(rows) == 2 and rows[0][0] == "finance_agent"
    assert rows[0][1] == [t["name"] for t in fa.TOOLS]
    assert fa.spent_today() == pytest.approx(sum(float(r[2]) for r in rows))


def test_follow_ups_keep_the_conversation(money):
    agent = fa.FinanceAgent(FakeClient([reply("first"), reply("second")]), daily_limit_usd=1)
    agent.ask("one")
    agent.ask("and two?")
    assert [m["role"] for m in agent.messages] == ["user", "assistant", "user", "assistant"]


def test_a_bad_tool_call_goes_back_as_an_error_the_model_can_fix(money):
    client = FakeClient([tool_use("get_totals", {"group_by": "week"}), reply("Sorry, fixed.")])
    answer = fa.FinanceAgent(client, daily_limit_usd=1).ask("q")
    assert answer.steps[0].is_error
    assert client.requests[1]["messages"][2]["content"][0]["is_error"] is True


def test_tool_rounds_are_capped_and_the_last_call_must_answer(money, monkeypatch):
    monkeypatch.setattr(fa, "MAX_TOOL_ROUNDS", 2)
    client = FakeClient([tool_use("list_categories", {}, id="a"), tool_use("list_categories", {}, id="b"),
                         reply("Best I can do.")])
    answer = fa.FinanceAgent(client, daily_limit_usd=1).ask("q")
    assert answer.text == "Best I can do." and len(answer.steps) == 2
    assert client.requests[2]["tool_choice"] == {"type": "none"}
    assert "tool_choice" not in client.requests[1]
    assert client.requests[2]["messages"][-1]["content"][-1]["type"] == "text"   # told to answer now


def test_the_daily_limit_stops_a_question_before_anything_is_sent(money):
    money.execute("INSERT INTO llm_calls (started_at, job, model, cost_usd) VALUES (now(), 'finance_agent', 'm', 0.5)")
    client = FakeClient([])
    with pytest.raises(fa.DailyLimitReached):
        fa.FinanceAgent(client, daily_limit_usd=0.30).ask("q")
    assert client.requests == []


def test_a_failed_call_leaves_the_conversation_as_it_was(money, monkeypatch):
    import llm_calls
    monkeypatch.setattr(llm_calls, "_sleep", lambda s: None)
    agent = fa.FinanceAgent(FakeClient([reply("first"), RuntimeError("API down")]), daily_limit_usd=1)
    agent.ask("one")
    with pytest.raises(RuntimeError):
        agent.ask("two")
    assert len(agent.messages) == 2


def test_cut_off_and_refused_answers_say_so(money):
    assert "cut off" in fa.FinanceAgent(FakeClient([reply("Partial", stop="max_tokens")]), daily_limit_usd=1).ask("q").text
    refused = fa.FinanceAgent(FakeClient([reply("", stop="refusal")]), daily_limit_usd=1).ask("q")
    assert refused.text == "I couldn't answer that one. Try rephrasing it."


def test_the_prompt_carries_todays_date():
    from datetime import date
    assert "Thursday 12 November 2026" in fa.system_prompt(date(2026, 11, 12))
