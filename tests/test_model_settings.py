"""Per-job model settings (llm_calls.request_kwargs / response_text) and the
scoring and budget in model_eval.py. No API or database needed."""
import json
from types import SimpleNamespace

import pytest

import llm_calls
import llm_labelling as ll
import model_eval as me


# ── request_kwargs ────────────────────────────────────────────────────────────

def test_plain_model_is_sent_as_before():
    assert llm_calls.request_kwargs({"model": "claude-sonnet-4-6"}, 4096) == {
        "model": "claude-sonnet-4-6", "max_tokens": 4096}


def test_haiku_with_thinking_gets_effort_and_room_to_think():
    kw = llm_calls.request_kwargs({"model": "claude-haiku-5-5", "effort": "low", "thinking": True}, 600)
    assert kw == {"model": "claude-haiku-5-5", "max_tokens": llm_calls.THINKING_MAX_TOKENS,
                  "output_config": {"effort": "low"}, "thinking": {"type": "adaptive"}}


@pytest.mark.parametrize("model, off", [
    ("claude-haiku-5-5", {"type": "disabled"}),
    ("claude-sonnet-5-5", {"type": "between_tools"}),
])
def test_thinking_off_is_spelled_per_model(model, off):
    kw = llm_calls.request_kwargs({"model": model, "thinking": False}, 4096)
    assert kw["thinking"] == off
    assert kw["max_tokens"] == 4096


def test_thinking_off_on_a_model_that_cannot_raises():
    with pytest.raises(ValueError):
        llm_calls.request_kwargs({"model": "claude-opus-5-5", "thinking": False}, 4096)


def test_5_5_models_think_by_default():
    assert llm_calls.thinks({"model": "claude-haiku-5-5"})
    assert llm_calls.thinks({"model": "claude-opus-5-5"})
    assert not llm_calls.thinks({"model": "claude-sonnet-4-6"})
    assert llm_calls.request_kwargs({"model": "claude-haiku-5-5"}, 600)["max_tokens"] == llm_calls.THINKING_MAX_TOKENS


def test_every_job_has_valid_settings():
    for job, settings in ll.JOBS.items():
        llm_calls.request_kwargs(settings, ll.MAX_TOKENS)
        assert llm_calls.prices_for(settings["model"], llm_calls.date.today()), job


# ── response_text ─────────────────────────────────────────────────────────────

def test_response_text_skips_thinking_blocks():
    r = SimpleNamespace(content=[SimpleNamespace(type="thinking", thinking="hmm", text=None),
                                 SimpleNamespace(type="text", text=' [{"id": 1}] ')])
    assert llm_calls.response_text(r) == '[{"id": 1}]'


def test_response_text_reads_untyped_test_doubles():
    assert llm_calls.response_text(SimpleNamespace(content=[SimpleNamespace(text="hi")])) == "hi"


def test_response_text_of_nothing_is_empty():
    assert llm_calls.response_text(SimpleNamespace(content=[])) == ""


# ── model_eval ────────────────────────────────────────────────────────────────

def test_budget_refuses_a_call_that_would_go_over():
    b = me.Budget(0.10)
    assert b.reserve(0.06)
    assert not b.reserve(0.06)          # the first is still in flight
    b.settle(0.06, 0.02)
    assert b.reserve(0.06)              # it cost less than reserved
    assert b.skipped == 1
    assert b.spent == pytest.approx(0.02)


def _txn(i, cat="Food & Drink", sub="Groceries", source="migration"):
    return {"id": f"t{i}", "amount": -5.0, "merchant_name": "Shop", "llm_category": cat,
            "llm_subcategory": sub, "label_source": source}


def _ok(text, cost=0.01):
    return {"text": text if isinstance(text, str) else json.dumps(text), "stop": "end_turn",
            "cost": cost, "secs": 1.0, "output_tokens": 10}


def test_score_counts_right_wrong_and_unmatched():
    t1, t2, t3 = _txn(1), _txn(2, source="llm"), _txn(3, "Transport", "Fuel", source="quick_tap")
    tasks = [
        {"kind": "pass0", "txns": [t1, t2, t3]},
        {"kind": "pass1", "txns": [t1, t2, t3]},
        {"kind": "pass2", "txns": [t1, t3], "parent": "x"},
        {"kind": "judge", "txns": [t1], "placed": ("Food & Drink", "Groceries"), "right": True},
        {"kind": "judge", "txns": [t2], "placed": ("Transport", "Fuel"), "right": False},
    ]
    reject = {"verdict": "reject", "confidence": 0.9, "suggested_parent": "Food & Drink",
              "suggested_subcategory": "Groceries"}
    results = [
        _ok([{"id": "t1", "category": "food & drink", "subcategory": "groceries"},   # right (case ignored)
             {"id": "t2", "category": "Shopping", "subcategory": "Gifts"},          # wrong match
             {"id": "t3", "category": None, "subcategory": None}]),                 # no match
        _ok([{"id": "t1", "category": "Food & Drink"}, {"id": "t2", "category": "Food & Drink"},
             {"id": "t3", "category": "Shopping"}]),
        _ok([{"id": "t1", "subcategory": "Groceries"}, {"id": "t3", "subcategory": "Fuel"}]),
        _ok({"verdict": "approve", "confidence": 0.9}),
        _ok(reject),
    ]
    s, misses = me.score(tasks, results)
    assert (s["p0_n"], s["p0_matched"], s["p0_wrong"]) == (3, 2, 1)
    assert s["p1"]["all"] == [2, 3]
    assert s["p1"]["you/rules"] == [0, 1]
    assert s["p2"]["all"] == [2, 2]
    assert (s["judge_right_ok"], s["judge_right_n"], s["judge_wrong_caught"], s["judge_wrong_n"]) == (1, 1, 1, 1)
    assert s["cost"] == pytest.approx(0.05)
    assert {m["kind"] for m in misses} == {"pass0 WRONG MATCH", "pass1"}


def test_score_counts_failures_and_budget_skips():
    tasks = [{"kind": "pass1", "txns": [_txn(1)]}] * 4
    results = [{"skipped": True}, {"error": "RateLimitError"},
               {**_ok("[]"), "stop": "max_tokens"}, _ok("not json")]
    s, misses = me.score(tasks, results)
    assert (s["skipped"], s["calls"], s["failed"]) == (1, 3, 3)
    assert len(misses) == 3


def test_estimate_is_higher_when_thinking():
    task = {"kind": "judge", "txns": [_txn(1)], "prompt": "x" * 3300}
    plain = me.estimate(task, {"model": "claude-haiku-5-5", "thinking": False})
    thinking = me.estimate(task, {"model": "claude-haiku-5-5", "thinking": True})
    assert 0 < plain < thinking


def test_report_prints_without_a_baseline(capsys):
    s, _ = me.score([{"kind": "pass1", "txns": [_txn(1)]}], [_ok([{"id": "t1", "category": "Food & Drink"}])])
    me.report({"haiku-5-5 low": s})
    assert "haiku-5-5 low" in capsys.readouterr().out
