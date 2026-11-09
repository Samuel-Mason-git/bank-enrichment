"""Payment details: one payment in full -- every field Monzo sent, what it has
now, everything that's happened to it (arriving, questions, answers, labels,
reviews), and the raw payload. Answer, skip or delete it from here too.

The open payment is in the address (?id=tx_...), so a page can be bookmarked
or linked to."""
import pandas as pd
import streamlit as st

from database_functions import get_payment, get_payment_history, get_queue_transactions
from payment_actions import STATUS_LABELS, payee, payment_actions, payment_label
from ui import _fmt


@st.cache_data(ttl=30, show_spinner="Loading payments...")
def load_payments() -> list[dict]:
    return get_queue_transactions()


CONTEXT_SOURCES = {
    "telegram": "on Telegram", "rule": "by a rule", "dashboard": "in the dashboard",
    "timeout": "when no answer came in time", "migration": "in the move from DuckDB",
}
LABEL_SOURCES = {
    "llm": "the classifier", "rule": "a rule", "quick_tap": "a quick tap on Telegram", "manual": "you",
    "judge": "the judge", "proposal": "a new-category proposal", "migration": "the move from DuckDB",
}


def _describe(h: dict) -> tuple[str, str]:
    """(what happened, the detail) for one history row."""
    kind, source, detail, extra = h["kind"], h["source"], h["detail"] or "", h["extra"] or ""
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
        return "Reviewed by the judge", f"{detail} · {extra}"
    if kind == "proposed":
        state = {"pending": "waiting for your pick on Telegram", "selected": "picked, being applied",
                 "applied": "applied", "denied": "none of these", "expired": "expired"}.get(extra, extra)
        return "New categories suggested", f"{detail} · {state}"
    return kind, detail


def _sync_address() -> None:
    chosen = st.session_state.payment_id
    if chosen:
        st.query_params["id"] = chosen
    elif "id" in st.query_params:
        del st.query_params["id"]


def _forget() -> None:
    st.session_state.payment_id = ""
    _sync_address()


payments = load_payments()
by_id = {t["id"]: t for t in payments}
if st.session_state.get("payment_id") not in by_id:
    # Nothing picked yet (or it was deleted): open the one in the address, if any.
    wanted = st.query_params.get("id")
    st.session_state.payment_id = wanted if wanted in by_id else ""

st.selectbox("Payment", [""] + list(by_id), key="payment_id", on_change=_sync_address,
             format_func=lambda i: "Search your payments…" if not i else payment_label(by_id[i]))
_sync_address()

t = get_payment(st.session_state.payment_id) if st.session_state.payment_id else None
if not t:
    st.info("Pick a payment to see everything about it. The Queue page links here too.")
else:
    data = (t["raw_payload"] or {}).get("data", {})
    merchant = data.get("merchant") if isinstance(data.get("merchant"), dict) else {}
    counterparty = data.get("counterparty") if isinstance(data.get("counterparty"), dict) else {}
    st.subheader(f"{payee(t)} · {_fmt(float(t['amount']))}")
    st.caption(f"{t['created_at']:%A %d %B %Y, %H:%M} · {STATUS_LABELS[t['status']]} · {t['id']}")

    left, right = st.columns(2)
    with left:
        st.markdown("**From Monzo**")
        monzo = {
            "Description": data.get("description"),
            "Merchant": merchant.get("name"),
            "Merchant category": merchant.get("category"),
            "Payee": counterparty.get("name"),
            "Payee account": " ".join(x for x in (counterparty.get("sort_code"),
                                                  counterparty.get("account_number")) if x) or None,
            "Monzo category": data.get("category"),
            "Payment scheme": data.get("scheme"),
            "Currency": data.get("currency"),
            "Local amount": (f"{abs(data['local_amount']) / 100:,.2f} {data.get('local_currency', '')}"
                             if data.get("local_currency") and data.get("local_currency") != data.get("currency")
                             else None),
            "Settled": f"{t['settled_at']:%d %b %Y, %H:%M}" if t["settled_at"] else "Not yet",
            "Top-up": "Yes" if t["is_load"] else None,
        }
        st.dataframe(pd.DataFrame([(k, str(v)) for k, v in monzo.items() if v not in (None, "")],
                                  columns=["Field", "Value"]), hide_index=True, width="stretch")
    with right:
        st.markdown("**What it has now**")
        category = " › ".join(c for c in (t["llm_category"], t["llm_subcategory"]) if c)
        ours = {
            "Context": t["user_context"],
            "Category": category or None,
            "Categorised by": t["llm_model"],
            "Confidence": f"{float(t['llm_confidence']) * 100:.0f}%" if t["llm_confidence"] is not None else None,
            "Skipped": "Yes" if t["skipped"] else "No",
            "Received": f"{t['received_at']:%d %b %Y, %H:%M}",
            "Answered": f"{t['enriched_at']:%d %b %Y, %H:%M}" if t["enriched_at"] else "Not yet",
            "Categorised": f"{t['classified_at']:%d %b %Y, %H:%M}" if t["classified_at"] else "Not yet",
            "Times asked": t["times_asked"],
        }
        st.dataframe(pd.DataFrame([(k, str(v)) for k, v in ours.items() if v not in (None, "")],
                                  columns=["Field", "Value"]), hide_index=True, width="stretch")

    with st.container(border=True):
        payment_actions(t, "payment", after_delete=_forget)

    st.subheader("History")
    history = get_payment_history(t["id"])
    rows = []
    for h in history:
        what, detail = _describe(h)
        rows.append({"When": h["at"], "What": what, "Detail": detail})
    st.dataframe(
        pd.DataFrame(rows), hide_index=True, width="stretch",
        height=min(35 * (len(rows) + 1) + 2, 520),
        column_config={"When": st.column_config.DatetimeColumn("When", format="D MMM YYYY, HH:mm:ss",
                                                                width="medium")},
    )

    with st.expander("Raw payload from Monzo"):
        st.json(t["raw_payload"])
