"""Budget: a monthly spending budget per parent category, and how each is
going -- worked out by budgets.py, the same code behind the Telegram messages
and the Ask agent, so every figure matches.

Spent counts as the Overview counts spend (outgoing spend-role money): pot
moves, transfers and refunds never use up a budget, and unclassified spending
counts once it's classified. Budgets apply to every month; the month picker
only changes which month you're looking at."""
from datetime import date

import plotly.graph_objects as go
import streamlit as st

import budgets as bg
from ui import _card_rows, _fmt, settings_link

# The budget maths reads the whole ledger, so the page keeps its answers for a
# minute (and they're cleared when a budget is saved), rather than redoing them
# on every click.
@st.cache_data(ttl=60, show_spinner=False)
def _standing(month: date) -> bg.MonthBudgets:
    return bg.month_budgets(month=month)


@st.cache_data(ttl=60, show_spinner=False)
def _history() -> list[dict]:
    return bg.history(6)


GREEN, AMBER, RED, TRACK, PACE = "#2e9d5b", "#e0a526", "#d64545", "rgba(128,128,128,0.18)", "#1f3a5f"
STATE_COLOUR = {"over": RED, "near": AMBER, "pace": AMBER, "ok": GREEN}
STATE_LABEL = {"over": "🔴 Over", "near": "🟠 Nearly used", "pace": "🟠 Off pace", "ok": "🟢 On track"}
CARD = "rgba(31, 58, 95, 0.12)"
MUTED = "#8a94a0"


def _fill_chart(mb: bg.MonthBudgets) -> None:
    """A fixed 0-100% scale, so every bar is comparable. Spending fills each bar
    to the budget line; an over-budget bar is red and labelled past the line with
    how far over and by how much. Same design as the Telegram chart."""
    rows = list(reversed(mb.statuses))
    labels = [f"{s.category}  ·  {_fmt(s.spent)} of {_fmt(s.budget)} ({s.used:.0%})" for s in rows]
    fig = go.Figure()
    fig.add_bar(y=labels, x=[1.0] * len(rows), orientation="h", marker_color=TRACK, hoverinfo="skip",
                showlegend=False)
    fig.add_bar(y=labels, x=[min(s.used, 1.0) for s in rows], orientation="h",
                marker_color=[RED if s.state == "over" else STATE_COLOUR[s.state] for s in rows],
                showlegend=False, customdata=[s.note for s in rows], hovertemplate="%{y}<br>%{customdata}<extra></extra>")
    for label, s in zip(labels, rows):
        if s.used > 1.0:
            fig.add_annotation(x=1.03, y=label, text=f"<b>+{s.used - 1.0:.0%} over</b><br>{_fmt(s.spent - s.budget)} extra",
                               showarrow=False, xanchor="left", align="left", font={"color": RED})
    fig.add_shape(type="line", x0=1.0, x1=1.0, y0=-0.6, y1=len(rows) - 0.4,
                  line={"color": MUTED, "width": 1, "dash": "dash"})
    if mb.current:
        for i in range(len(rows)):
            fig.add_shape(type="line", x0=mb.pace, x1=mb.pace, y0=i - 0.45, y1=i + 0.45,
                          line={"color": PACE, "width": 3})
    # Legend entries, drawn as empty traces so the lines above get a key.
    fig.add_scatter(x=[None], y=[None], mode="lines", line={"color": PACE, "width": 3},
                    name=f"On pace: where spending would be by now (day {mb.day} of {mb.days})" if mb.current else "",
                    showlegend=mb.current, hoverinfo="skip")
    fig.add_scatter(x=[None], y=[None], mode="lines", line={"color": MUTED, "width": 1, "dash": "dash"},
                    name="Budget (100%)", hoverinfo="skip")
    fig.update_layout(barmode="overlay", height=150 + 52 * len(rows),
                      margin={"t": 24, "b": 70, "l": 10, "r": 150},
                      legend={"orientation": "h", "yanchor": "top", "y": -0.12, "xanchor": "left", "x": 0},
                      xaxis={"range": [0, 1.42], "tickvals": [0, 0.25, 0.5, 0.75, 1.0],
                             "ticktext": ["0%", "25%", "50%", "75%", "100%"], "showgrid": False},
                      yaxis={"showgrid": False},
                      annotations=[{"x": 1.0, "y": 1.0, "xref": "x", "yref": "paper", "text": "budget",
                                    "showarrow": False, "yanchor": "bottom", "font": {"color": MUTED}}])
    st.plotly_chart(fig, width="stretch", key="budget_fill")


def _history_chart() -> None:
    points = _history()
    if not any(p["spent"] for p in points):
        return
    labels = [p["month"].strftime("%b %Y") for p in points]
    fig = go.Figure()
    fig.add_bar(x=labels, y=[p["spent"] for p in points], name="Spent", marker_color=PACE,
                hovertemplate="%{x}<br>£%{y:,.2f}<extra></extra>")
    fig.add_scatter(x=labels, y=[p["budget"] for p in points], name="Budget (today's)", mode="lines",
                    line={"color": RED, "dash": "dash"})
    fig.update_layout(height=320, margin={"t": 30, "b": 10, "l": 10, "r": 10}, yaxis_tickprefix="£",
                      legend={"orientation": "h", "y": 1.12})
    st.plotly_chart(fig, width="stretch", key="budget_history")


def _edit_budgets(mb: bg.MonthBudgets) -> None:
    current = {s.parent_id: s for s in mb.statuses}
    rows = [{"parent_id": s.parent_id, "category": s.category, "spent": s.spent, "budget": s.budget,
             "suggested": None} for s in mb.statuses]
    rows += [{"parent_id": u["parent_id"], "category": u["category"], "spent": u["spent"], "budget": None,
              "suggested": u["suggested"]} for u in mb.unbudgeted]
    rows.sort(key=lambda r: r["category"])
    if not rows:
        st.info("There are no spending categories to budget yet.")
        return

    missing = [r for r in rows if not r["budget"] and r["suggested"]]
    if missing and st.button(f"Fill in suggestions for {len(missing)} categor{'y' if len(missing) == 1 else 'ies'} "
                             "without a budget", key="budget_fill_suggestions"):
        for r in missing:
            st.session_state[f"budget_{r['parent_id']}"] = float(r["suggested"])
        st.rerun()

    with st.form("budget_form"):
        st.caption("A monthly amount per category; 0 for none. Suggestions are the average of your last three "
                   "full months, rounded up to £10.")
        for r in rows:
            name, spent, box = st.columns([3, 2, 2])
            name.markdown(f"**{r['category']}**")
            spent.caption(f"{_fmt(r['spent'])} spent in {mb.month:%B}"
                          + (f" · suggested {_fmt(r['suggested'])}" if r["suggested"] and not r["budget"] else ""))
            key = f"budget_{r['parent_id']}"
            if key not in st.session_state:
                st.session_state[key] = float(r["budget"] or 0)
            box.number_input(f"Budget for {r['category']}", key=key, min_value=0.0, max_value=float(bg.MAX_BUDGET),
                             step=10.0, format="%.0f", label_visibility="collapsed")
        if st.form_submit_button("Save budgets", type="primary"):
            changed, problems = 0, []
            for r in rows:
                value = float(st.session_state[f"budget_{r['parent_id']}"] or 0)
                had = current.get(r["parent_id"])
                try:
                    if value <= 0 and had:
                        bg.clear_budget(r["parent_id"])
                        changed += 1
                    elif value > 0 and (not had or round(value, 2) != round(had.budget, 2)):
                        bg.set_budget(r["parent_id"], value)
                        changed += 1
                except bg.BudgetError as e:
                    problems.append(f"{r['category']}: {e}")
            for p in problems:
                st.error(p)
            if not problems:
                st.cache_data.clear()
                st.session_state["budget_saved"] = changed
                st.rerun()
    if "budget_saved" in st.session_state:
        n = st.session_state.pop("budget_saved")
        st.success(f"Saved {n} change{'s' if n != 1 else ''}." if n else "Nothing had changed.")


st.header("Budget")
st.caption("A monthly spending budget per category. Spent counts as the Overview counts spend, so pot moves, "
           "transfers and refunds never use one up; unclassified spending counts once it's classified.")

today = bg.today_london()
months = [bg.add_months(bg.month_start(today), -i) for i in range(12)]
month = st.selectbox("Month", months, format_func=lambda d: d.strftime("%B %Y"), key="budget_month")
mb = _standing(month)
mb_now = mb if mb.current else _standing(bg.month_start(today))

if mb.statuses:
    left = mb.budgeted - mb.spent
    cards = [("Budgeted", _fmt(mb.budgeted), CARD, "The monthly budgets added up.", ""),
             ("Spent", _fmt(mb.spent), CARD, f"Spent in budgeted categories in {mb.month:%B}.", ""),
             ("Left" if left >= 0 else "Over", _fmt(abs(left)), CARD, "Budgeted minus spent.", "")]
    if mb.current:
        cards.append(("Heading for", _fmt(mb.projected), CARD,
                      f"Where {mb.month:%B} ends if spending carries on at its rate so far (day {mb.day} of {mb.days}).", ""))
    else:
        over = [s for s in mb.statuses if s.state == "over"]
        cards.append(("Over budget", f"{len(over)} of {len(mb.statuses)}", CARD, "Categories that went over.", ""))
    _card_rows(cards)
    _fill_chart(mb)
    with st.expander("Each category"):
        st.dataframe(
            [{"Category": s.category, "Budget": _fmt(s.budget), "Spent": _fmt(s.spent), "Left": _fmt(s.left),
              "Used": f"{s.used:.0%}", "Status": STATE_LABEL[s.state], "": s.note} for s in mb.statuses],
            hide_index=True, width="stretch")
else:
    st.info("No budgets yet. Set some below: the suggestions are a good start. You can also ask on the Ask page or "
            "with /ask on Telegram, and confirm what the agent proposes.")

st.subheader("Telegram messages and alerts")
st.caption("The budget message and the alerts are set in Settings. The budgets themselves are set here.")
settings_link("Budgets")

st.subheader("Set budgets")
_edit_budgets(mb)

st.subheader("The last six months")
_history_chart()

