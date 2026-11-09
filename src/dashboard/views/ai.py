"""AI usage: what the calls to Claude cost and did -- spend by day, job,
model and prompt version, every call (its prompt version and wording,
response, thinking, tokens, cost at that day's prices), and how good the answers turned out to
be: how often you corrected the classifier, what the judge caught, and how
the cards Claude's suggestions became were answered.

Spend comes from llm_calls, recorded from when this version was deployed;
the quality figures use all of your history."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.express as px
import psycopg
import streamlit as st

from llm_calls import (
    card_outcomes, classifier_corrections, get_call, get_calls, judge_catches, judge_outcomes, label_sources,
)
from payment_actions import JOB_LABELS, LABEL_SOURCES
from ui import _card_rows

AI_COLOR = "rgba(155, 89, 182, 0.14)"


def _usd(v) -> str:
    """$1,234.56, or $0.0042 for the small amounts single calls cost."""
    if v is None or pd.isna(v):
        return "—"
    v = float(v)
    return f"${v:,.2f}" if abs(v) >= 1 else f"${v:,.4f}"


def _pct(part, whole) -> str:
    return f"{part / whole * 100:.1f}%" if whole else "—"


today = datetime.now(ZoneInfo("Europe/London")).date()
first_of_month = today.replace(day=1)
last_month_end = first_of_month - timedelta(days=1)
PERIODS = {
    "This month": (first_of_month, today, last_month_end.replace(day=1), last_month_end),
    "Last month": (last_month_end.replace(day=1), last_month_end,
                   (last_month_end.replace(day=1) - timedelta(days=1)).replace(day=1),
                   last_month_end.replace(day=1) - timedelta(days=1)),
    "Last 30 days": (today - timedelta(days=29), today, today - timedelta(days=59), today - timedelta(days=30)),
    "All time": (None, None, None, None),
}

st.caption(
    "What the calls to Claude cost and did, and how good their answers turned out to be. Spend is recorded "
    "from when this page arrived; the quality figures below use all of your history. Prices are Anthropic's "
    "(USD) on the day of each call."
)

try:
    choice = st.radio("Period", list(PERIODS), horizontal=True, key="ai_period", label_visibility="collapsed")
    since, until, prev_since, prev_until = PERIODS[choice]
    calls = pd.DataFrame(get_calls(since, until))
    previous = pd.DataFrame(get_calls(prev_since, prev_until)) if prev_since else pd.DataFrame()
except psycopg.errors.UndefinedTable:
    st.info("Calls to Claude are recorded once the server is running this version (it makes the table when it "
            "starts). Nothing to show yet.")
    st.stop()

# ── Spend ─────────────────────────────────────────────────────────────────────
st.subheader("Spend")
if calls.empty:
    st.info("No calls to Claude in this period.")
else:
    for col in ("input_tokens", "cache_write_5m_tokens", "cache_write_1h_tokens", "cache_read_tokens",
                "output_tokens", "duration_ms", "attempts"):
        calls[col] = calls[col].fillna(0).astype(int)
    calls["cost_usd"] = calls["cost_usd"].astype(float)
    calls["prompt_tokens"] = (calls["input_tokens"] + calls["cache_read_tokens"]
                              + calls["cache_write_5m_tokens"] + calls["cache_write_1h_tokens"])
    calls["job_label"] = calls["job"].map(lambda j: JOB_LABELS.get(j, j))
    calls["day"] = pd.to_datetime(calls["started_at"]).dt.date

    spent = calls["cost_usd"].sum()
    prev_spent = previous["cost_usd"].astype(float).sum() if not previous.empty else None
    classified = {t for ids, job in zip(calls["txn_ids"], calls["job"])
                  if job in ("match_existing", "classify_subcategories") for t in ids}
    failed = int((calls["status"] == "error").sum())
    unusable = int((calls["parsed"] == False).sum())  # noqa: E712 -- NULL means not checked
    _card_rows([
        ("Spend", _usd(spent), AI_COLOR,
         f"Previous {choice.lower()}: {_usd(prev_spent)}" if prev_spent is not None else "All calls recorded.", ""),
        ("Calls", f"{len(calls):,}", AI_COLOR, "Calls to Claude, every job.", ""),
        ("Tokens in / out", f"{calls['prompt_tokens'].sum() / 1000:,.0f}k / {calls['output_tokens'].sum() / 1000:,.1f}k",
         AI_COLOR, "Prompt tokens (cached ones included) and output tokens.", ""),
        ("Per payment classified", _usd(spent / len(classified)) if classified else "—", AI_COLOR,
         f"All spend divided by the {len(classified)} payments the classifier looked at.", ""),
        ("Failed calls", _pct(failed, len(calls)), AI_COLOR, f"{failed} call(s) that errored after every retry.", ""),
        ("Unusable replies", _pct(unusable, len(calls)), AI_COLOR,
         f"{unusable} reply(ies) that couldn't be read (bad JSON, cut off).", ""),
        ("Average call", f"{calls['duration_ms'].mean() / 1000:.1f}s", AI_COLOR, "Wall-clock time, retries included.", ""),
        ("Retried calls", str(int((calls["attempts"] > 1).sum())), AI_COLOR,
         "Calls that needed more than one attempt (rate limits, overloads, dropped connections).", ""),
    ])

    by_day = calls.groupby(["day", "job_label"], as_index=False)["cost_usd"].sum()
    fig = px.bar(by_day, x="day", y="cost_usd", color="job_label", labels={"cost_usd": "Cost ($)", "day": "",
                                                                          "job_label": "Job"})
    fig.update_layout(height=280, margin=dict(l=10, r=10, t=10, b=10), legend=dict(orientation="h", y=-0.3),
                      yaxis_tickprefix="$")
    st.plotly_chart(fig, width="stretch", key="ai_by_day")

    def _summary(group_cols: list[str]) -> pd.DataFrame:
        g = calls.groupby(group_cols, dropna=False)
        out = g.agg(calls=("call_id", "count"), cost=("cost_usd", "sum"), prompt=("prompt_tokens", "sum"),
                    output=("output_tokens", "sum"), seconds=("duration_ms", "mean"),
                    failed=("status", lambda s: int((s == "error").sum())),
                    unusable=("parsed", lambda s: int((s == False).sum()))).reset_index()  # noqa: E712
        out["per_call"] = out["cost"] / out["calls"]
        out["seconds"] = out["seconds"] / 1000
        return out.sort_values("cost", ascending=False).rename(
            columns={"calls": "Calls", "failed": "Failed", "unusable": "Unusable"})

    money = {"cost": st.column_config.NumberColumn("Cost", format="$%.4f"),
             "per_call": st.column_config.NumberColumn("Per call", format="$%.4f"),
             "prompt": st.column_config.NumberColumn("Tokens in", format="%d"),
             "output": st.column_config.NumberColumn("Tokens out", format="%d"),
             "seconds": st.column_config.NumberColumn("Avg secs", format="%.1f")}

    st.markdown("**By job**")
    st.dataframe(_summary(["job_label"]).rename(columns={"job_label": "Job"}), hide_index=True,
                 width="stretch", column_config=money)

    st.markdown("**By model**")
    per_model = _summary(["provider", "response_model"]).rename(columns={"provider": "Provider",
                                                                        "response_model": "Model"})
    st.dataframe(per_model, hide_index=True, width="stretch", column_config=money)

    st.markdown("**By prompt version**")
    st.caption("A new version appears whenever a prompt's wording changes, so a change can be judged on cost "
               "and on how often its replies were unusable.")
    versions = _summary(["job_label", "prompt_version"])
    first_used = calls.groupby("prompt_version")["started_at"].min()
    versions["first_used"] = versions["prompt_version"].map(first_used)
    st.dataframe(versions.rename(columns={"job_label": "Job", "prompt_version": "Version",
                                          "first_used": "First used"}),
                 hide_index=True, width="stretch",
                 column_config={**money, "First used": st.column_config.DatetimeColumn(format="D MMM YYYY")})

# ── One call ──────────────────────────────────────────────────────────────────
st.subheader("Calls")


def _show_call(call_id: int) -> None:
    c = get_call(call_id)
    if not c:
        st.warning(f"Call {call_id} isn't recorded.")
        return
    st.markdown(f"**{JOB_LABELS.get(c['job'], c['job'])}** · {c['started_at']:%d %b %Y, %H:%M:%S} · "
                f"{c['response_model'] or c['model']} · {_usd(c['cost_usd'])}")
    facts = {
        "Status": "OK" if c["status"] == "ok" else f"Failed: {c['error']}",
        "Took": f"{(c['duration_ms'] or 0) / 1000:.1f}s over {c['attempts']} attempt(s)",
        "Tokens": (f"{c['input_tokens'] or 0:,} in, {c['cache_read_tokens'] or 0:,} cache read, "
                   f"{(c['cache_write_5m_tokens'] or 0) + (c['cache_write_1h_tokens'] or 0):,} cache write, "
                   f"{c['output_tokens'] or 0:,} out"),
        "Stopped because": c["stop_reason"],
        "Reply usable": {True: f"Yes ({c['answered']} of {c['expected']} answered)" if c["expected"] else "Yes",
                         False: "No", None: "Not checked"}[c["parsed"]],
        "Prices ($/MTok)": (", ".join(f"{k} {v}" for k, v in (c["prices"] or {}).items()
                                      if k not in ("long", "since")) + f" (since {c['prices']['since']})"
                            if c["prices"] else "No price on record for this model"),
        "Prompt version": c["prompt_version"],
        "Run": c["run_id"],
        "Anthropic request id": c["request_id"],
    }
    st.dataframe(pd.DataFrame([(k, str(v)) for k, v in facts.items() if v not in (None, "")],
                              columns=["", "Value"]), hide_index=True, width="stretch")
    if c["retry_errors"]:
        st.warning("Earlier attempts failed: " + " · ".join(c["retry_errors"]))
    if c["txn_ids"]:
        st.caption(f"About {len(c['txn_ids'])} payment(s): " + ", ".join(c["txn_ids"][:20])
                   + (" …" if len(c["txn_ids"]) > 20 else ""))
    if c["prompt_source"]:
        with st.expander(f"The prompt's wording ({c['prompt_version']}, sent as {c['prompt_chars'] or 0:,} "
                         "characters)"):
            st.caption("The code that builds this prompt. The payments above and your taxonomy at the time were "
                       "filled into it; the prompt as sent isn't kept, only its fingerprint.")
            st.code(c["prompt_source"], language="python", wrap_lines=True)
    if c["thinking"]:
        with st.expander("Thinking"):
            st.code(c["thinking"], language=None, wrap_lines=True)
    with st.expander("Response", expanded=True):
        st.code(c["response_text"] or "(none)", language=None, wrap_lines=True)
    with st.expander("Settings and raw usage"):
        st.json({"params": c["params"], "usage": c["usage"], "response_id": c["response_id"],
                 "prompt_sha256": c["prompt_sha256"]})


opened = st.session_state.pop("ai_open_call", None)
if opened:
    with st.container(border=True):
        _show_call(opened)

if calls.empty:
    st.caption("No calls in this period.")
else:
    shown = calls.head(500)
    event = st.dataframe(pd.DataFrame({
        "Time": shown["started_at"], "Job": shown["job_label"], "Model": shown["response_model"].fillna(shown["model"]),
        "Payments": shown["txn_ids"].map(len), "Tokens in": shown["prompt_tokens"], "Tokens out": shown["output_tokens"],
        "Cost": shown["cost_usd"], "Secs": shown["duration_ms"] / 1000,
        "Outcome": [("Failed" if s == "error" else "Unusable reply" if p is False else "OK")
                    for s, p in zip(shown["status"], shown["parsed"])],
    }), hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row",
        key=f"ai_calls_{choice}", height=min(35 * (len(shown) + 1) + 2, 420),
        column_config={"Time": st.column_config.DatetimeColumn(format="D MMM, HH:mm:ss"),
                       "Cost": st.column_config.NumberColumn(format="$%.4f"),
                       "Secs": st.column_config.NumberColumn(format="%.1f")})
    if event.selection.rows:
        with st.container(border=True):
            _show_call(int(shown.iloc[event.selection.rows[0]]["call_id"]))
    else:
        st.caption("Select a call to see exactly what went in, what came out, and what it cost."
                   + (" Showing the newest 500." if len(calls) > 500 else ""))

# ── Quality ───────────────────────────────────────────────────────────────────
st.subheader("How good the answers are")
st.caption("From all of your history, not just the period above.")

q1, q2 = st.columns(2)
with q1:
    st.markdown("**Who categorised your payments**")
    sources = pd.DataFrame(label_sources())
    if not sources.empty:
        sources["source"] = sources["source"].map(lambda s: LABEL_SOURCES.get(s, s).capitalize())
        sources["share"] = sources["payments"] / sources["payments"].sum() * 100
        st.dataframe(sources.rename(columns={"source": "Who", "payments": "Payments"}), hide_index=True,
                     width="stretch", column_config={"share": st.column_config.NumberColumn("Share", format="%.1f%%")})
with q2:
    st.markdown("**How often you corrected the classifier**")
    st.caption("Payments the classifier labelled that you, a quick tap or a new-category pick later moved.")
    corrections = pd.DataFrame(classifier_corrections())
    if not corrections.empty:
        corrections["rate"] = corrections["corrected"] / corrections["labelled"] * 100
        st.dataframe(corrections.rename(columns={"model": "Model", "labelled": "Labelled",
                                                 "corrected": "Corrected"}),
                     hide_index=True, width="stretch",
                     column_config={"rate": st.column_config.NumberColumn("Correction rate", format="%.1f%%")})

st.markdown("**The placement judge**")
outcomes = {r["outcome"]: r["reviews"] for r in judge_outcomes()}
caught, false_alarms = outcomes.get("changed", 0), outcomes.get("kept", 0)
_card_rows([
    ("Reviews", f"{sum(outcomes.values()):,}", AI_COLOR, "Placements the judge looked at.", ""),
    ("Agreed", f"{outcomes.get('approved', 0):,}", AI_COLOR, "Placements it was happy with.", ""),
    ("Caught", f"{caught:,}", AI_COLOR, "Objections you agreed with: the payment moved to its suggestion.", ""),
    ("Hit rate", _pct(caught, caught + false_alarms), AI_COLOR,
     f"Of the objections you've answered: {caught} caught, {false_alarms} false alarm(s) you kept. "
     f"{outcomes.get('held', 0)} still waiting for an answer.", ""),
])
catches = pd.DataFrame(judge_catches())
if not catches.empty:
    catches["outcome"] = catches["outcome"].map({"changed": "Caught: you moved it", "kept": "False alarm: you kept it",
                                                 "held": "Waiting for you"}).fillna(catches["outcome"])
    st.dataframe(catches.drop(columns=["txn_id"]).rename(columns={
        "reviewed_at": "When", "outcome": "Outcome", "was_going_to_be": "Was going to be", "now_is": "Now",
        "payee": "Payee", "amount": "Amount"}), hide_index=True, width="stretch",
        column_config={"When": st.column_config.DatetimeColumn(format="D MMM YYYY"),
                       "Amount": st.column_config.NumberColumn(format="£%,.2f")})

st.markdown("**How Claude's suggestions were answered**")
cards = card_outcomes()
cols = st.columns(len(cards))
for col, (kind, rows) in zip(cols, cards.items()):
    with col:
        st.caption(kind)
        if rows:
            st.dataframe(pd.DataFrame(rows).rename(columns={"status": "Answer", "cards": "Cards",
                                                            "avg_hours_to_answer": "Avg hours to answer"}),
                         hide_index=True, width="stretch")
        else:
            st.caption("None yet.")
