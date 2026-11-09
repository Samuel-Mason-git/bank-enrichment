"""Payment details: one payment in full -- every field Monzo sent, what it has
now, everything that's happened to it (arriving, questions, answers, labels,
reviews), and the raw payload. Answer, skip or delete it from here too.

The open payment is in the address (?id=tx_...), so a page can be bookmarked
or linked to."""
import pandas as pd
import streamlit as st

from database_functions import get_payment, get_payment_history, get_queue_transactions
from payment_actions import STATUS_LABELS, describe_event, payee, payment_actions, payment_label
from ui import _fmt


@st.cache_data(ttl=30, show_spinner="Loading payments...")
def load_payments() -> list[dict]:
    return get_queue_transactions()


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
        what, detail = describe_event(h)
        rows.append({"When": h["at"], "What": what, "Detail": detail})
    st.dataframe(
        pd.DataFrame(rows), hide_index=True, width="stretch",
        height=min(35 * (len(rows) + 1) + 2, 520),
        column_config={"When": st.column_config.DatetimeColumn("When", format="D MMM YYYY, HH:mm:ss",
                                                                width="medium")},
    )
    call_rows = [(h["call_id"], describe_event(h)[0], h["at"]) for h in history if h.get("call_id")]
    if call_rows:
        c1, c2 = st.columns([4, 1], vertical_alignment="bottom")
        picked = c1.selectbox("A call to Claude about this payment", [c[0] for c in call_rows],
                              format_func=lambda i: next(f"{at:%d %b %Y, %H:%M} · {what}"
                                                         for cid, what, at in call_rows if cid == i),
                              key=f"payment_call_{t['id']}")
        if c2.button("Open it", width="stretch", help="Everything that went in and came out, and what it cost."):
            st.session_state.ai_open_call = picked
            st.switch_page("views/ai.py")

    with st.expander("Raw payload from Monzo"):
        st.json(t["raw_payload"])
