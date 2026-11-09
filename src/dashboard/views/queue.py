"""Queue: payments still waiting for something -- an answer from you, or a
category from the classifier -- with how long they've waited and how often
you've been asked. Pick one to answer, skip or delete it."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from database_functions import get_lifetime_stats, get_queue_transactions
from payment_actions import STATUS_LABELS, payee, payment_actions, payment_label
from ui import _card_rows

QUEUE_COLOR = "rgba(127, 140, 141, 0.16)"


@st.cache_data(ttl=30, show_spinner="Loading the queue...")
def load_queue() -> list[dict]:
    return get_queue_transactions()


def _age(since: datetime, now: datetime) -> str:
    """'3 days', '5 hours', 'just now'."""
    hours = (now - since).total_seconds() / 3600
    if hours < 1:
        return "just now"
    if hours < 48:
        return f"{int(hours)} hour{'s' if int(hours) != 1 else ''}"
    return f"{int(hours // 24)} days"


def _new_selection() -> None:
    """The table's rows change after an action, so its selection would point
    at a different payment: a fresh key starts it unselected."""
    st.session_state.queue_version = st.session_state.get("queue_version", 0) + 1


txns = load_queue()
stats = get_lifetime_stats()
now = datetime.now(ZoneInfo("Europe/London")).replace(tzinfo=None)

pending = [t for t in txns if t["status"] == "pending"]
enriched = [t for t in txns if t["status"] == "enriched"]
held = [t for t in txns if t["status"] == "held"]
waiting = sorted(pending + enriched + held, key=lambda t: t["created_at"], reverse=True)
skipped = [t for t in txns if t["status"] == "skipped"]
oldest = min((t["created_at"] for t in waiting), default=None)

st.caption(
    "Payments still waiting for something: an answer from you (Telegram asks about these), or a category "
    "from the classifier, which runs every hour from 08:00 to 22:00. Pick one to answer, skip or delete it."
)
_card_rows([
    ("Needs your answer", str(len(pending)), QUEUE_COLOR, "No context yet.", ""),
    ("Waiting for the classifier", str(len(enriched)), QUEUE_COLOR,
     "Answered, but not categorised yet: done on the next hourly run.", ""),
    ("Held for a new category", str(len(held)), QUEUE_COLOR,
     "The classifier suggested new categories for these; a Telegram card is waiting for your pick.", ""),
    ("Oldest waiting", _age(oldest, now) if oldest else "—", QUEUE_COLOR,
     "How long ago the oldest waiting payment happened.", ""),
    ("Payments received", f"{stats['received']:,}", QUEUE_COLOR, "Every payment Monzo has sent, ever.", ""),
    ("Questions sent", f"{stats['asked']:,}", QUEUE_COLOR, "Telegram cards and follow-up nudges.", ""),
    ("Answered", f"{stats['answered']:,}", QUEUE_COLOR, "Payments given context, by you or a rule.", ""),
    ("Categorised", f"{stats['classified']:,}", QUEUE_COLOR, "Payments with a category now.", ""),
])

views = {
    f"Waiting ({len(waiting)})": waiting,
    f"Needs your answer ({len(pending)})": pending,
    f"Waiting for the classifier ({len(enriched)})": enriched,
    f"Held for a new category ({len(held)})": held,
    f"Skipped ({len(skipped)})": skipped,
    f"Everything ({len(txns)})": txns,
}
choice = st.radio("Show", list(views), horizontal=True, key="queue_view", on_change=_new_selection,
                  label_visibility="collapsed")
shown = views[choice]

if not shown and choice.startswith("Waiting ("):
    st.success("Nothing waiting. Every payment has an answer and a category.")
elif not shown:
    st.info("Nothing here.")
else:
    table = pd.DataFrame([{
        "Date": t["created_at"], "Payee": payee(t), "Amount": float(t["amount"]),
        "Status": STATUS_LABELS[t["status"]], "Context": t["user_context"] or "",
        "Times asked": t["times_asked"], "Waiting": _age(t["created_at"], now),
    } for t in shown])
    event = st.dataframe(
        table, hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row",
        key=f"queue_table_{st.session_state.get('queue_version', 0)}",
        height=min(35 * (len(table) + 1) + 2, 460),
        column_config={
            "Date": st.column_config.DatetimeColumn("Date", format="D MMM YYYY, HH:mm", width="small"),
            "Amount": st.column_config.NumberColumn("Amount", format="£%,.2f", width="small"),
            "Times asked": st.column_config.NumberColumn("Times asked", width="small"),
        },
    )
    rows = event.selection.rows
    if not rows:
        st.caption("Select a row to answer, skip or delete that payment.")
    else:
        t = shown[rows[0]]
        with st.container(border=True):
            st.markdown(f"**{payment_label(t)}** · {STATUS_LABELS[t['status']]}")
            payment_actions(t, "queue", after=_new_selection, after_delete=_new_selection)
            if st.button("Full details and history", key="queue_open_details"):
                st.session_state.payment_id = t["id"]
                st.switch_page("views/payment.py")
