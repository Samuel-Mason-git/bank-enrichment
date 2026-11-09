"""Activity: what the app did on any day -- each hourly run (and any it
missed), every warning and error from the server and the worker, and
everything that happened to your payments. Replaces reading the server's log.

Runs and problems are kept 90 days (activity.py); the payment events are your
history, kept for good."""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import psycopg
import plotly.graph_objects as go
import streamlit as st

from database_functions import (
    first_recorded_run_day, get_activity_by_day, get_day_events, get_log_events, get_runs,
)
from payment_actions import LABEL_SOURCES, describe_event
from ui import _card_rows

# The worker's hours (worker.py's FIRST_RUN and LAST_RUN): a run on the hour,
# London time, both included.
FIRST_RUN, LAST_RUN = 8, 22
ACTIVITY_COLOR = "rgba(127, 140, 141, 0.16)"
SUMMARY_WORDS = {
    "category_cards": "new-category card", "taxonomy_cards": "taxonomy card",
    "rule_suggestions": "rule suggestion", "errors": "error",
}


def _did(summary: dict) -> str:
    """'3 labelled by the classifier · 1 rule suggestion' from a run's summary."""
    parts = []
    # Labels first, errors last (the database keeps keys in its own order).
    order = sorted(summary, key=lambda k: (not k.startswith("labelled_"), k == "errors", k))
    for key, n in ((k, summary[k]) for k in order):
        if key.startswith("labelled_"):
            parts.append(f"{n} labelled by {LABEL_SOURCES.get(key.removeprefix('labelled_'), key)}")
        else:
            word = SUMMARY_WORDS.get(key, key)
            parts.append(f"{n} {word}{'s' if n != 1 else ''}")
    return " · ".join(parts) or "Nothing new to do"


def _shift(days: int) -> None:
    st.session_state.activity_day = min(st.session_state.activity_day + timedelta(days=days), today)


def _to_today() -> None:
    st.session_state.activity_day = today


now = datetime.now(ZoneInfo("Europe/London")).replace(tzinfo=None)
today = now.date()
if "activity_day" not in st.session_state:
    st.session_state.activity_day = today

st.caption(
    "What the app did on any day: each hourly run, every warning and error from the server and the worker, "
    "and everything that happened to your payments. Runs and problems are kept for 90 days."
)

# ── The last 30 days ───────────────────────────────────────────────────────────
try:
    by_day = pd.DataFrame(get_activity_by_day(30))
except psycopg.errors.UndefinedTable:
    # The server makes the tables when it starts; this dashboard can be newer.
    st.info("Activity is recorded once the server is running this version (it makes the tables when it "
            "starts). Nothing to show yet.")
    st.stop()
fig = go.Figure()
fig.add_bar(x=by_day["day"], y=by_day["errors"], name="Errors", marker_color="rgba(231, 76, 60, 0.85)")
fig.add_bar(x=by_day["day"], y=by_day["warnings"], name="Warnings", marker_color="rgba(241, 196, 15, 0.8)")
fig.add_scatter(x=by_day["day"], y=by_day["payments"], name="Payments received", mode="lines+markers",
                yaxis="y2", line=dict(color="rgba(52, 152, 219, 0.9)"))
fig.update_layout(
    barmode="stack", height=240, margin=dict(l=10, r=10, t=10, b=10), hovermode="x unified",
    legend=dict(orientation="h", y=-0.25),
    yaxis=dict(title="Problems", rangemode="tozero", tickformat=",d"),
    yaxis2=dict(title="Payments", overlaying="y", side="right", rangemode="tozero", showgrid=False, tickformat=",d"),
)
st.plotly_chart(fig, width="stretch", key="activity_strip")

# ── Pick a day ────────────────────────────────────────────────────────────────
c1, c2, c3, c4 = st.columns([0.6, 2, 0.6, 0.8], vertical_alignment="bottom")
c1.button("◀ Day before", on_click=_shift, args=(-1,), width="stretch")
c2.date_input("Day", key="activity_day", max_value=today, format="DD/MM/YYYY")
c3.button("Day after ▶", on_click=_shift, args=(1,), width="stretch",
          disabled=st.session_state.activity_day >= today)
c4.button("Today", on_click=_to_today, width="stretch", disabled=st.session_state.activity_day == today)
day: date = st.session_state.activity_day

runs = get_runs(day)
problems = get_log_events(day)
events = get_day_events(day)
real_runs = [r for r in runs if r["status"] != "skipped"]
first_day = first_recorded_run_day()
recorded = first_day is not None and day >= first_day

if day == today:
    expected_hours = [h for h in range(FIRST_RUN, LAST_RUN + 1) if h <= now.hour]
else:
    expected_hours = list(range(FIRST_RUN, LAST_RUN + 1))
ran_hours = {r["started_at"].hour for r in real_runs}
missed = [h for h in expected_hours if h not in ran_hours] if recorded else []

kinds = pd.Series([e["kind"] for e in events], dtype="object")
errors = sum(p["level"] in ("ERROR", "CRITICAL") for p in problems)
st.subheader(f"{day:%A %d %B %Y}")
_card_rows([
    ("Runs", f"{len(real_runs)} of {len(expected_hours)}" if recorded else "—", ACTIVITY_COLOR,
     "Hourly runs that happened, of those due (08:00 to 22:00).", ""),
    ("Runs with problems", str(sum(r["status"] == "problems" for r in runs)) if recorded else "—", ACTIVITY_COLOR,
     "A step failed; the rest of the run still went ahead.", ""),
    ("Errors", str(errors), ACTIVITY_COLOR, "Errors logged by the server or the worker.", ""),
    ("Warnings", str(len(problems) - errors), ACTIVITY_COLOR, "Warnings logged by the server or the worker.", ""),
    ("Payments received", str(int((kinds == "arrived").sum())), ACTIVITY_COLOR, "New payments from Monzo.", ""),
    ("Questions sent", str(int((kinds == "asked").sum())), ACTIVITY_COLOR, "Telegram cards and nudges.", ""),
    ("Answers and skips", str(int(kinds.isin(["context", "skipped"]).sum())), ACTIVITY_COLOR,
     "Context given or payments skipped, by you or a rule.", ""),
    ("Categorised", str(int((kinds == "labelled").sum())), ACTIVITY_COLOR,
     "Labels set, by the classifier, a rule, a tap or you.", ""),
])

# ── Runs ──────────────────────────────────────────────────────────────────────
st.subheader("Runs")
if not recorded:
    st.info("Runs weren't recorded yet on this day; recording started "
            + (f"on {first_day:%d %B %Y}." if first_day else "with this version."))
else:
    if missed:
        st.warning(
            "No run at " + ", ".join(f"{h:02d}:00" for h in missed) + ". The worker or the database may have "
            "been down; Telegram says when runs stop working and when they're back."
        )
    if runs:
        outcome = {"ok": "✓ OK", "problems": "⚠ Problems", "skipped": "Skipped (another run was going)",
                   "running": "Still running, or stopped without finishing"}
        st.dataframe(pd.DataFrame([{
            "Started": r["started_at"],
            "Took": f"{r['seconds']:.0f}s" if r["seconds"] is not None else "—",
            "Outcome": outcome.get(r["status"], r["status"]),
            "What it did": _did(r["summary"] or {}),
        } for r in runs]), hide_index=True, width="stretch",
            column_config={"Started": st.column_config.DatetimeColumn("Started", format="HH:mm:ss", width="small"),
                           "Took": st.column_config.Column(width="small")})
    elif not missed:
        st.caption("No runs due yet today.")

# ── Problems ──────────────────────────────────────────────────────────────────
st.subheader(f"Warnings and errors ({len(problems)})")
if not problems:
    st.success("None. Everything ran cleanly.")
else:
    event = st.dataframe(pd.DataFrame([{
        "Time": p["logged_at"], "Where": p["service"].title(), "Level": p["level"].title(),
        "Message": p["message"],
    } for p in problems]), hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row",
        key=f"activity_problems_{day}",
        column_config={"Time": st.column_config.DatetimeColumn("Time", format="HH:mm:ss", width="small"),
                       "Where": st.column_config.Column(width="small"),
                       "Level": st.column_config.Column(width="small")})
    if event.selection.rows:
        p = problems[event.selection.rows[0]]
        st.markdown(f"**{p['level'].title()} from the {p['service']}** ({p['logger']}) at {p['logged_at']:%H:%M:%S}")
        st.code(p["message"] + ("\n\n" + p["detail"] if p["detail"] else ""), language=None)
    else:
        st.caption("Select a row to see the full message and its traceback.")

# ── Payments ──────────────────────────────────────────────────────────────────
st.subheader(f"What happened to your payments ({len(events)})")
if not events:
    st.caption("Nothing happened to any payment on this day.")
else:
    rows = []
    for e in events:
        what, detail = describe_event(e)
        rows.append({"Time": e["at"], "What": what, "Payee": e["payee"],
                     "Amount": float(e["amount"]) if e["amount"] is not None else None, "Detail": detail})
    table = pd.DataFrame(rows)
    shown_kinds = st.multiselect("Show", sorted(table["What"].unique()), key=f"activity_kinds_{day}",
                                 placeholder="Everything")
    if shown_kinds:
        table = table[table["What"].isin(shown_kinds)]
    st.dataframe(table, hide_index=True, width="stretch", height=min(35 * (len(table) + 1) + 2, 560),
                 column_config={
                     "Time": st.column_config.DatetimeColumn("Time", format="HH:mm:ss", width="small"),
                     "Amount": st.column_config.NumberColumn("Amount", format="£%,.2f", width="small"),
                 })
