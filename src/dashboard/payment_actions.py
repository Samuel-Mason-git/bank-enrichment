"""What you can do to one payment from the dashboard: give or change its
context, skip or unskip it, or delete it. Shared by the Queue and Payment
pages; written the way the server writes them (database_functions.set_context)."""
import streamlit as st

from database_functions import delete_transaction, set_context, skip_transaction, unskip_transaction
from ui import _fmt


def payee(t: dict) -> str:
    return t.get("merchant_name") or t.get("counterparty_name") or t.get("description") or t["id"]


def payment_label(t: dict) -> str:
    return f"{t['created_at']:%d %b %Y} · {payee(t)} · {_fmt(float(t['amount']))}"


CONTEXT_SOURCES = {
    "telegram": "on Telegram", "rule": "by a rule", "dashboard": "in the dashboard",
    "timeout": "when no answer came in time", "migration": "in the move from DuckDB",
}
LABEL_SOURCES = {
    "llm": "the classifier", "rule": "a rule", "quick_tap": "a quick tap on Telegram", "manual": "you",
    "judge": "the judge", "proposal": "a new-category proposal", "migration": "the move from DuckDB",
}


# What each kind of call to Claude is for (llm_calls.job).
JOB_LABELS = {
    "match_existing": "Classifier: match to an existing category",
    "classify_parents": "Classifier: pick the category",
    "classify_subcategories": "Classifier: pick the subcategory",
    "propose_alternatives": "Classifier: suggest new categories",
    "regenerate_options": "Classifier: suggest again (Try again)",
    "placement_judge": "Placement judge: second look",
    "rule_suggestions": "Rule suggestions: worth a rule?",
    "taxonomy_review": "Monthly taxonomy review",
}


def describe_event(h: dict) -> tuple[str, str]:
    """(what happened, the detail) for one row of a payment's history
    (get_payment_history, get_day_events)."""
    kind, source = h["kind"], h["source"]
    detail, extra = h.get("detail") or "", h.get("extra") or ""
    if kind == "arrived":
        return "Arrived from Monzo", ""
    if kind == "updated":
        return ("Fetched again from Monzo's API" if source == "api" else "Monzo sent an update"), ""
    if kind == "asked":
        return "Asked you about it", ""
    if kind == "skipped":
        return f"Skipped {CONTEXT_SOURCES.get(source, source)}", extra
    if kind == "context cleared":
        return f"Context cleared {CONTEXT_SOURCES.get(source, source)}", extra
    if kind == "context":
        return f"Context given {CONTEXT_SOURCES.get(source, source)}", " · ".join(x for x in (detail, extra) if x)
    if kind == "labelled":
        return f"Categorised by {LABEL_SOURCES.get(source, source)}", " · ".join(x for x in (detail, extra) if x)
    if kind == "label cleared":
        return f"Category cleared by {LABEL_SOURCES.get(source, source)}", ""
    if kind == "reviewed":
        return "Reviewed by the judge", " · ".join(x for x in (detail, extra) if x)
    if kind == "proposed":
        state = {"pending": "waiting for your pick on Telegram", "selected": "picked, being applied",
                 "applied": "applied", "denied": "none of these", "expired": "expired"}.get(extra, extra)
        return "New categories suggested", f"{detail} · {state}"
    if kind == "llm call":
        return f"Claude: {JOB_LABELS.get(source, source)}", " · ".join(x for x in (detail, extra) if x)
    return kind, detail


STATUS_LABELS = {
    "pending": "Needs your answer",
    "enriched": "Waiting for the classifier",
    "held": "Held: pick its new category on Telegram",
    "skipped": "Skipped",
    "classified": "Done",
}


def _done(message: str, after) -> None:
    st.cache_data.clear()
    st.toast(message)
    if after:
        after()


def _save(txn_id: str, ctx_key: str, after) -> None:
    context = st.session_state[ctx_key].strip()
    if not context:
        st.toast("Write some context first.")
        return
    set_context(txn_id, context)
    _done("Context saved. The classifier picks it up on its next run.", after)


def _skip(txn_id: str, after) -> None:
    skip_transaction(txn_id)
    _done("Skipped.", after)


def _unskip(txn_id: str, after) -> None:
    unskip_transaction(txn_id)
    _done("Counted again. The classifier picks it up on its next run.", after)


def _delete(txn_id: str, prefix: str, after, after_delete) -> None:
    delete_transaction(txn_id)
    st.session_state[f"{prefix}_confirm_del"] = None
    _done("Deleted.", after)
    if after_delete:
        after_delete()


def payment_actions(t: dict, prefix: str, after=None, after_delete=None) -> None:
    """The context box and the Skip/Unskip and Delete buttons for one payment.
    after() runs once anything's changed (from the callback, before the rerun);
    after_delete() too once it's been deleted."""
    tid = t["id"]
    ctx_key = f"{prefix}_ctx_{tid}"
    if ctx_key not in st.session_state:
        st.session_state[ctx_key] = "" if t["skipped"] else (t["user_context"] or "")

    c1, c2 = st.columns([4, 1], vertical_alignment="bottom")
    c1.text_input("Context", key=ctx_key, placeholder="What was this?",
                  help="Saved as your answer, the same as replying on Telegram.")
    c2.button("Save context", key=f"{prefix}_save_{tid}", type="primary", width="stretch",
              on_click=_save, args=(tid, ctx_key, after))

    b1, b2, _ = st.columns([1, 1, 3])
    if t["skipped"]:
        b1.button("Unskip", key=f"{prefix}_unskip_{tid}", width="stretch",
                  on_click=_unskip, args=(tid, after), help="Count it again.")
    else:
        b1.button("Skip", key=f"{prefix}_skip_{tid}", width="stretch",
                  on_click=_skip, args=(tid, after), help="Leave it out of everything, and stop asking.")
    confirm_key = f"{prefix}_confirm_del"
    if b2.button("Delete", key=f"{prefix}_del_{tid}", width="stretch",
                 help="Gone for good, with its history. For test payments and mistakes."):
        st.session_state[confirm_key] = tid

    if st.session_state.get(confirm_key) == tid:
        d1, d2, d3 = st.columns([3, 1, 1])
        d1.warning("Delete this payment and its whole history? This can't be undone (skipping keeps it).")
        d2.button("Delete", key=f"{prefix}_del_yes_{tid}", type="primary", width="stretch",
                  on_click=_delete, args=(tid, prefix, after, after_delete))
        if d3.button("Cancel", key=f"{prefix}_del_no_{tid}", width="stretch"):
            st.session_state[confirm_key] = None
            st.rerun()
