"""Recording calls to Claude (src/pipeline/llm_calls.py): what's stored, the
cost at that day's prices, retries, and what the AI page reads back -- plus
the rest of what's now collected: which rule fired, settings changes, and
each run's step timings and memory."""
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import streamlit as st
from anthropic.types import Message
from streamlit.testing.v1 import AppTest

import activity
import database_functions as dbf
import llm_calls
import process
from tests.local_helpers import ensure_category, insert_transaction

_DASHBOARD_PATH = str(Path(__file__).parent.parent / "src" / "dashboard" / "dashboard.py")


def _message(text='[{"id": "tx_1", "category": "Food & Drink"}]', model="claude-sonnet-4-6",
             input_tokens=1000, output_tokens=200, cache_read=0, cache_write=0, thinking=None) -> Message:
    content = [{"type": "thinking", "thinking": thinking, "signature": "sig"}] if thinking else []
    content.append({"type": "text", "text": text})
    return Message.model_validate({
        "id": "msg_1", "type": "message", "role": "assistant", "model": model, "content": content,
        "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens,
                  "cache_read_input_tokens": cache_read, "cache_creation_input_tokens": cache_write},
    })


def _client(*results):
    """A stand-in client whose messages.create returns (or raises) each result in turn."""
    queue = list(results)

    def create(**kwargs):
        r = queue.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
    return SimpleNamespace(messages=SimpleNamespace(create=create))


def _a_prompt(transactions):
    return "Classify these"


def _call(db, client=None, **kw):
    return llm_calls.create(client or _client(_message()), job="classify_parents", prompt_fn=_a_prompt,
                            txn_ids=["tx_1", "tx_2"], model="claude-sonnet-4-6", max_tokens=4096,
                            messages=[{"role": "user", "content": "Classify these"}], **kw)


def _row(db):
    cur = db.execute("SELECT * FROM llm_calls ORDER BY call_id DESC LIMIT 1")
    return dict(zip([c.name for c in cur.description], cur.fetchone()))


@pytest.fixture(autouse=True)
def _no_waits(monkeypatch):
    monkeypatch.setattr(llm_calls, "_sleep", lambda s: None)
    llm_calls.current_run_id = None


# ── Prices ────────────────────────────────────────────────────────────────────

class TestCost:
    def test_sonnet_4_6(self):
        prices = llm_calls.prices_for("claude-sonnet-4-6", date(2026, 11, 9))
        tokens = {"input": 1_000_000, "cache_write_5m": 0, "cache_write_1h": 0, "cache_read": 0, "output": 100_000}
        assert llm_calls.cost(prices, tokens) == pytest.approx(3 + 1.5)

    def test_cache_tokens_at_their_own_rates(self):
        prices = llm_calls.prices_for("claude-sonnet-5-5", date(2026, 11, 9))
        tokens = {"input": 0, "cache_write_5m": 1_000_000, "cache_write_1h": 1_000_000,
                  "cache_read": 1_000_000, "output": 0}
        assert llm_calls.cost(prices, tokens) == pytest.approx(2.50 + 4 + 0.10)

    def test_haiku_5_5_long_prompts_cost_more(self):
        prices = llm_calls.prices_for("claude-haiku-5-5", date(2026, 11, 9))
        short = {"input": 100_000, "cache_write_5m": 0, "cache_write_1h": 0, "cache_read": 0, "output": 0}
        long = {**short, "input": 100_001}
        assert llm_calls.cost(prices, short) == pytest.approx(0.01)
        assert llm_calls.cost(prices, long) == pytest.approx(100_001 * 0.50 / 1_000_000)

    def test_an_unpriced_model_has_no_cost(self):
        assert llm_calls.prices_for("some-other-model", date(2026, 11, 9)) is None
        assert llm_calls.cost(None, {}) is None

    def test_prices_apply_from_their_date(self, monkeypatch):
        monkeypatch.setitem(llm_calls.PRICES, "m", [(date(2026, 1, 1), {"input": 1}), (date(2026, 6, 1), {"input": 2})])
        assert llm_calls.prices_for("m", date(2026, 5, 31))["input"] == 1
        assert llm_calls.prices_for("m", date(2026, 6, 1)) == {"input": 2, "since": "2026-06-01"}


# ── Recording ─────────────────────────────────────────────────────────────────

class TestRecording:
    def test_everything_about_a_call_is_kept(self, db):
        response = _call(db, client=_client(_message(thinking="Hmm, groceries.", cache_read=500)))
        assert response.content[-1].text.startswith("[")
        r = _row(db)
        assert (r["job"], r["provider"], r["model"], r["response_model"], r["status"]) == (
            "classify_parents", "anthropic", "claude-sonnet-4-6", "claude-sonnet-4-6", "ok")
        assert r["request"] == {"messages": [{"role": "user", "content": "Classify these"}]}
        assert r["params"] == {"model": "claude-sonnet-4-6", "max_tokens": 4096}
        assert r["response_text"].startswith("[") and r["thinking"] == "Hmm, groceries."
        assert (r["input_tokens"], r["cache_read_tokens"], r["output_tokens"]) == (1000, 500, 200)
        assert float(r["cost_usd"]) == pytest.approx((1000 * 3 + 500 * 0.30 + 200 * 15) / 1_000_000)
        assert r["prices"]["input"] == 3 and r["prices"]["since"]
        assert r["txn_ids"] == ["tx_1", "tx_2"]
        assert r["prompt_version"].startswith("tests.test_llm_calls._a_prompt@")
        assert r["response_id"] == "msg_1" and r["duration_ms"] >= 0 and r["attempts"] == 1

    def test_whether_the_reply_was_usable(self, db):
        _call(db)
        llm_calls.parsed(True, 14, 15)
        r = _row(db)
        assert (r["parsed"], r["answered"], r["expected"]) == (True, 14, 15)

    def test_the_run_it_belonged_to(self, db):
        llm_calls.current_run_id = 42
        _call(db)
        assert _row(db)["run_id"] == 42

    def test_retries_are_counted_with_their_reasons(self, db, monkeypatch):
        class Overloaded(Exception):
            pass
        monkeypatch.setattr(llm_calls, "_retryable", lambda e: isinstance(e, Overloaded))
        _call(db, client=_client(Overloaded("529"), Overloaded("529 again"), _message()))
        r = _row(db)
        assert r["attempts"] == 3 and r["status"] == "ok"
        assert r["retry_errors"] == ["Overloaded: 529", "Overloaded: 529 again"]

    def test_a_failed_call_is_recorded_and_still_raised(self, db):
        with pytest.raises(ValueError):
            _call(db, client=_client(ValueError("bad request")))
        r = _row(db)
        assert r["status"] == "error" and "bad request" in r["error"] and r["cost_usd"] is None

    def test_failing_to_record_never_breaks_the_call(self, db, monkeypatch):
        monkeypatch.setattr(llm_calls, "get_con", lambda: (_ for _ in ()).throw(OSError("db gone")))
        assert _call(db).id == "msg_1"

    def test_old_text_is_cleared_but_the_numbers_stay(self, db):
        _call(db)
        db.execute("UPDATE llm_calls SET started_at = now() - INTERVAL '400 days'")
        llm_calls.clear_old_text(db)
        r = _row(db)
        assert r["request"] is None and r["response_text"] is None and r["text_cleared_at"] is not None
        assert r["output_tokens"] == 200 and r["cost_usd"] is not None


# ── Reading ───────────────────────────────────────────────────────────────────

class TestReading:
    def test_calls_by_date_and_for_a_payment(self, db):
        _call(db)
        assert len(llm_calls.get_calls()) == 1
        assert llm_calls.get_calls(date(2000, 1, 1), date(2000, 1, 2)) == []
        assert [c["job"] for c in llm_calls.calls_for_payment("tx_2")] == ["classify_parents"]
        assert llm_calls.calls_for_payment("tx_9") == []
        call = llm_calls.get_call(llm_calls.get_calls()[0]["call_id"])
        assert call["response_text"].startswith("[")

    def test_corrections_count_payments_you_moved(self, db):
        dbf._seed_taxonomy()
        for i in range(3):
            insert_transaction(db, f"tx_{i}", merchant_name="Tesco", user_context="Food")
        p, s = ensure_category(db, "Food & Drink", "Groceries")
        p2, s2 = ensure_category(db, "Shopping", "Gifts")
        for i in range(3):
            db.execute("INSERT INTO label_events (txn_id, parent_id, subcategory_id, source, model) "
                       "VALUES (%s, %s, %s, 'llm', 'claude-sonnet-4-6')", [f"tx_{i}", p, s])
        db.execute("INSERT INTO label_events (txn_id, parent_id, subcategory_id, source) "
                   "VALUES ('tx_0', %s, %s, 'manual')", [p2, s2])
        assert llm_calls.classifier_corrections() == [
            {"model": "claude-sonnet-4-6", "labelled": 3, "corrected": 1}]
        assert {r["source"]: r["payments"] for r in llm_calls.label_sources()} == {"llm": 2, "manual": 1}

    def test_judge_catches(self, db):
        dbf._seed_taxonomy()
        insert_transaction(db, "tx_1", merchant_name="Boots", user_context="Plasters",
                           llm_category="Health", llm_subcategory="Pharmacy & Medication")
        db.execute("""INSERT INTO judge_reviews (txn_id, category, subcategory, outcome, reviewed_at) VALUES
                      ('tx_1', 'Shopping', 'General Retail', 'changed', now())""")
        assert llm_calls.judge_outcomes() == [{"outcome": "changed", "reviews": 1}]
        catch = llm_calls.judge_catches()[0]
        assert (catch["was_going_to_be"], catch["now_is"], catch["payee"]) == (
            "Shopping › General Retail", "Health › Pharmacy & Medication", "Boots")

    def test_a_payments_history_shows_its_calls(self, db):
        dbf._seed_taxonomy()
        insert_transaction(db, "tx_1", merchant_name="Tesco", user_context="Food")
        _call(db)
        call = next(h for h in dbf.get_payment_history("tx_1") if h["kind"] == "llm call")
        assert call["source"] == "classify_parents" and call["call_id"] is not None
        assert "claude-sonnet-4-6" in call["detail"] and call["extra"].startswith("$")


# ── Which rule fired, and settings changes ────────────────────────────────────

def test_the_history_names_the_rule(db):
    dbf._seed_taxonomy()
    insert_transaction(db, "tx_1", merchant_name="EE", answered=False)
    dbf.add_rule({"name": "Phone bill", "match_field": "merchant_name", "match_type": "exact", "match_value": "EE",
                  "auto_context": "Phone"})
    db.execute("INSERT INTO context_events (txn_id, user_context, source, rule_id) VALUES ('tx_1', 'Phone', 'rule', 1)")
    context = next(h for h in dbf.get_payment_history("tx_1") if h["kind"] == "context")
    assert context["extra"] == 'rule "Phone bill"'


def test_every_settings_change_is_logged(db):
    rule = {"name": "Gym", "match_field": "merchant_name", "match_type": "contains", "match_value": "gym",
            "auto_context": "Gym"}
    rule_id = dbf.add_rule(rule)
    dbf.update_rule(rule_id, rule)            # no real change: not logged
    dbf.update_rule(rule_id, {**rule, "match_value": "the gym"})
    dbf.delete_rule(rule_id)
    rows = db.execute("SELECT action, before->>'match_value', after->>'match_value' FROM settings_changes "
                      "WHERE table_name = 'rules' ORDER BY change_id").fetchall()
    assert rows == [("INSERT", None, "gym"), ("UPDATE", "gym", "the gym"), ("DELETE", "the gym", None)]
    assert dbf.get_settings_changes(date.today())  # read back for the Activity page


# ── Runs ──────────────────────────────────────────────────────────────────────

def test_a_run_records_its_steps_memory_and_calls(db, monkeypatch, tmp_path):
    (tmp_path / "memory.current").write_text(str(150 * 1_048_576))
    (tmp_path / "memory.max").write_text(str(350 * 1_048_576))
    monkeypatch.setattr(activity, "CGROUP", tmp_path)

    def steps():
        with process._timed("classifier"):
            _call(db)
    with patch.object(process, "_steps", side_effect=steps):
        process.main()
    steps_taken, memory, summary = db.execute("SELECT steps, memory, summary FROM runs").fetchone()
    assert "classifier" in steps_taken
    assert memory == {"current_mb": 150, "limit_mb": 350}
    assert summary["llm_calls"] == 1 and summary["llm_cost_usd"] > 0
    assert _row(db)["run_id"] is not None


# ── The page ──────────────────────────────────────────────────────────────────

def test_ai_page_shows_spend_and_a_call(db):
    dbf._seed_taxonomy()
    _call(db)
    st.cache_data.clear()
    at = AppTest.from_file(_DASHBOARD_PATH, default_timeout=30)
    at.switch_page("views/ai.py")
    at.session_state["ai_open_call"] = llm_calls.get_calls()[0]["call_id"]
    at.run()
    assert not at.exception
    cards = " ".join(md.value for md in at.markdown)
    assert "Spend" in cards and "$0.0060" in cards
    assert any("Classifier: pick the category" in md.value for md in at.markdown)
