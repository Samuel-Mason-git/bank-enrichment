"""Subscriptions: what you pay for regularly, and suggestions from recurring payments."""
import html
import pandas as pd
import streamlit as st
from dashboard_helpers import FREQ_MONTHLY, detect_subscriptions, merchant_display_name, should_deactivate_subscription
from database_functions import delete_subscription, get_subscriptions, toggle_subscription, update_subscription, upsert_subscription
from ui import _fmt, _card_rows, row_colors
from page_context import shared

df, = shared('df')


subs = get_subscriptions()
confirmed_names = {s["name"] for s in subs}

# Auto-inactive subscriptions with no matching transaction in last 2 months.
# Guard with session_state so this only fires once per browser session —
# running it on every rerun would immediately re-disable anything the user
# just manually enabled, and could cascade-disable on each button click.
if "subs_auto_deactivated" not in st.session_state:
    two_months_ago = (pd.Timestamp.now() - pd.DateOffset(months=2))
    for s in subs:
        if not s["active"]:
            continue
        if should_deactivate_subscription(s, df, two_months_ago):
            toggle_subscription(s["id"])
    st.session_state.subs_auto_deactivated = True
    subs = get_subscriptions()
    confirmed_names = {s["name"] for s in subs}

# ── KPI cards ──────────────────────────────────────────────────────────────
active_subs = [s for s in subs if s["active"]]
monthly_cost = sum(s["amount"] * FREQ_MONTHLY[s["frequency"]] for s in active_subs)
annual_cost = monthly_cost * 12

kpi_color = "rgba(127, 140, 141, 0.16)"
_card_rows([
    ("Active Subscriptions", str(len(active_subs)), kpi_color,
     f"Subscriptions not marked Inactive ({len(subs) - len(active_subs)} inactive).", ""),
    ("Est. Monthly Cost", _fmt(monthly_cost), row_colors["Spend"],
     "Sum of every active subscription's amount, normalized to a monthly rate.", ""),
    ("Est. Annual Cost", _fmt(annual_cost), row_colors["Spend"],
     "Est. Monthly Cost x 12.", ""),
], per_row=3)

st.divider()

# ── Add manually ───────────────────────────────────────────────────────────
st.subheader("Add Subscription")
# merchant_name is often blank for direct debits/bank transfers (which
# report counterparty_name instead), so falling back to that here means a
# subscription paid that way can still be matched up to its transactions.
known_merchants = sorted(merchant_display_name(df).dropna().unique().tolist())
with st.form("add_sub_form"):
    c1, c2 = st.columns([2, 2])
    sub_name = c1.text_input("Display Name", placeholder="e.g. Netflix")
    sub_merchant = c2.selectbox("Merchant Name (from transactions)", [""] + known_merchants)
    c3, c4, c5 = st.columns([1, 1, 1])
    sub_amount = c3.number_input("Amount (£)", min_value=0.01, step=0.01, format="%.2f")
    sub_freq = c4.selectbox("Frequency", ["monthly", "weekly", "fortnightly", "annual"])
    c5.markdown("<br>", unsafe_allow_html=True)
    submitted = st.form_submit_button("Add", type="primary", use_container_width=True)
    if submitted and sub_name.strip():
        if sub_name.strip().lower() in {n.lower() for n in confirmed_names}:
            st.error(f'"{sub_name.strip()}" already exists — edit it below with ✏️ instead of adding a duplicate.')
        else:
            upsert_subscription(sub_name.strip(), sub_amount, sub_freq, sub_merchant or None)
            st.cache_data.clear()
            st.rerun()

# ── Confirmed subscriptions ────────────────────────────────────────────────
if "edit_sub_id" not in st.session_state:
    st.session_state.edit_sub_id = None

_sub_widths = [3, 1.4, 1.2, 0.6, 1, 0.6]

def _sub_header(*labels):
    cols = st.columns(_sub_widths if len(labels) == 6 else [3, 1.4, 1.2, 2.2])
    for col, label in zip(cols, labels):
        col.caption(label)

if subs:
    st.subheader("Your Subscriptions")
    _sub_header("Name", "Amount", "Per month", "", "", "")
    # Active first, so what you're paying for is at the top.
    for s in sorted(subs, key=lambda s: (not s["active"], s["name"].lower())):
        mc = s["amount"] * FREQ_MONTHLY[s["frequency"]]
        confirm_key = f"confirm_del_sub_{s['id']}"
        if confirm_key not in st.session_state:
            st.session_state[confirm_key] = False

        with st.container(border=True):
            col1, col2, col3, col4, col5, col6 = st.columns(_sub_widths, vertical_alignment="center")
            status = "" if s["active"] else ' <span style="opacity:0.55;font-size:0.8rem;">⏸ inactive</span>'
            col1.markdown(f"**{html.escape(s['name'])}**{status}", unsafe_allow_html=True)
            col2.markdown(f"{_fmt(float(s['amount']))} / {s['frequency']}")
            col3.markdown(f"~{_fmt(float(mc))}")
            if col4.button("✏️", key=f"edit_{s['id']}", help="Edit"):
                st.session_state.edit_sub_id = None if st.session_state.edit_sub_id == s["id"] else s["id"]
            label = "Disable" if s["active"] else "Enable"
            if col5.button(label, key=f"toggle_{s['id']}", use_container_width=True):
                toggle_subscription(s["id"])
                st.cache_data.clear()
                st.rerun()
            if col6.button("✕", key=f"del_{s['id']}", help="Delete"):
                st.session_state[confirm_key] = True

        if st.session_state.edit_sub_id == s["id"]:
            with st.form(f"edit_sub_form_{s['id']}"):
                ec1, ec2 = st.columns([2, 2])
                edit_name = ec1.text_input("Display Name", value=s["name"])
                merchant_options = [""] + known_merchants
                cur_merchant = s["merchant_name"] if s["merchant_name"] in merchant_options else ""
                edit_merchant = ec2.selectbox("Merchant Name", merchant_options, index=merchant_options.index(cur_merchant))
                ec3, ec4 = st.columns([1, 1])
                edit_amount = ec3.number_input("Amount (£)", min_value=0.01, step=0.01, format="%.2f", value=float(s["amount"]))
                freq_options = ["monthly", "weekly", "fortnightly", "annual"]
                edit_freq = ec4.selectbox("Frequency", freq_options, index=freq_options.index(s["frequency"]))
                ec5, ec6 = st.columns(2)
                save = ec5.form_submit_button("Save", type="primary")
                cancel = ec6.form_submit_button("Cancel")
                if save and edit_name.strip():
                    update_subscription(s["id"], edit_name.strip(), edit_amount, edit_freq, edit_merchant or None)
                    st.session_state.edit_sub_id = None
                    st.cache_data.clear()
                    st.rerun()
                if cancel:
                    st.session_state.edit_sub_id = None
                    st.rerun()

        if st.session_state[confirm_key]:
            wc1, wc2, wc3 = st.columns([3, 1, 1])
            wc1.warning(f"Delete **{s['name']}**? This cannot be undone.")
            if wc2.button("Confirm", key=f"confirm_yes_{s['id']}", type="primary"):
                delete_subscription(s["id"])
                st.session_state[confirm_key] = False
                st.cache_data.clear()
                st.rerun()
            if wc3.button("Cancel", key=f"confirm_no_{s['id']}"):
                st.session_state[confirm_key] = False
                st.rerun()

st.divider()

# ── Detected candidates ────────────────────────────────────────────────────
st.subheader("Suggested Subscriptions")
st.caption("Detected from recurring transactions — click Add to confirm.")
# A subscription you've named yourself ("WIFI Subscription") still covers
# its merchant ("EE LTD"), so that merchant isn't suggested again.
candidates = detect_subscriptions(
    df, confirmed_names | {s["merchant_name"] for s in subs if s["merchant_name"]})
if candidates:
    _sub_header("Merchant", "Typical amount", "Per month", "")
    for c in candidates:
        with st.container(border=True):
            col1, col2, col3, col4 = st.columns([3, 1.4, 1.2, 2.2], vertical_alignment="center")
            col1.markdown(f"**{html.escape(c['name'])}** "
                          f'<span style="opacity:0.55;font-size:0.8rem;">{c["occurrences"]} payments</span>',
                          unsafe_allow_html=True)
            col2.markdown(f"{_fmt(float(c['amount']))} / {c['frequency']}")
            col3.markdown(f"~{_fmt(float(c['monthly_cost']))}")
            add_clicked = col4.button("+ Add", key=f"add_{c['name']}")
        if add_clicked:
            upsert_subscription(c["name"], c["amount"], c["frequency"], c["name"])
            st.cache_data.clear()
            st.rerun()
else:
    st.info("No recurring patterns detected yet — more transaction history will improve detection.")
