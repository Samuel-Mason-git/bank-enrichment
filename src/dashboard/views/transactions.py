"""Transactions: highlights and the editable table of every payment in the filters."""
import pandas as pd
import streamlit as st
from datetime import datetime
from dashboard_helpers import merchant_display_name, sanitize_classification_edit
from database_functions import update_classification, update_transaction_details, upsert_parent, upsert_subcategory
from ui import _fmt, _card_rows
from page_context import shared

filtered, all_parents, subs_by_parent = shared('filtered', 'all_parents', 'subs_by_parent')


if not filtered.empty:
    st.subheader("Highlights")
    st.caption("Just for fun — not part of your financial totals elsewhere in the app.")
    fun_color = "rgba(127, 140, 141, 0.16)"
    # merchant_name alone misses every direct debit/bank transfer (rent,
    # utility bills, HMRC-style payments), which populate counterparty_name
    # instead -- these cards would otherwise silently exclude them.
    fun_merchant = merchant_display_name(filtered)
    merchant_counts = fun_merchant.dropna().value_counts()
    biggest_idx = filtered["amount"].abs().idxmax()
    biggest_row = filtered.loc[biggest_idx]
    biggest_merchant = fun_merchant.loc[biggest_idx]
    # pd.notna(), not a plain truthiness check: a missing merchant/payee
    # here is NaN, not "" or None, and `nan or x` is True in Python (NaN
    # is truthy) -- the same trap that broke clearing a transaction's
    # category in the Reclassify editor.
    biggest_label = biggest_merchant if pd.notna(biggest_merchant) else (biggest_row["description"] or "Unknown")
    day_counts = filtered["created_at"].dt.day_name().value_counts()

    fun_cards = [
        ("Transactions", str(len(filtered)),
         "Total transactions in the selected range and filters."),
        ("Unique Merchants", str(fun_merchant.nunique()),
         "Distinct merchant/payee names seen in the selected range."),
        ("Most Frequent Merchant",
         merchant_counts.index[0] if not merchant_counts.empty else "—",
         f"Appears {merchant_counts.iloc[0]} times." if not merchant_counts.empty else "No merchant data."),
        ("Biggest Transaction", _fmt(abs(biggest_row["amount"])),
         f"{biggest_label} on {biggest_row['created_at'].date()}."),
        ("Avg Transaction Size", _fmt(filtered['amount'].abs().mean()),
         "Mean absolute amount across every transaction in range."),
        ("Busiest Day", day_counts.index[0] if not day_counts.empty else "—",
         f"{day_counts.iloc[0]} transactions fall on a {day_counts.index[0]}." if not day_counts.empty else ""),
    ]
    _card_rows([(label, value, fun_color, tooltip, "") for label, value, tooltip in fun_cards], per_row=3)

    st.divider()

st.subheader(f"Edit transactions ({len(filtered)})")
st.caption(
    "Pick Category/Subcategory from your existing taxonomy to fix a misclassification. "
    "To add a brand-new category or subcategory, use the Settings page first."
)

edit_df = filtered[["created_at", "amount", "merchant_name", "description",
                     "user_context", "llm_category", "llm_subcategory", "id"]].copy()
# Direct debits and transfers name a payee, not a merchant: shown here the
# same way as every other tab, rather than as a blank.
edit_df["merchant_name"] = merchant_display_name(filtered)
edit_df["llm_category"] = edit_df["llm_category"].fillna("")
edit_df["llm_subcategory"] = edit_df["llm_subcategory"].fillna("")
edit_df["user_context"] = edit_df["user_context"].fillna("")
edit_df = edit_df.rename(columns={
    "created_at": "Date", "amount": "Amount", "merchant_name": "Merchant",
    "description": "Description", "user_context": "Context",
    "llm_category": "Category", "llm_subcategory": "Subcategory",
}).reset_index(drop=True)

# "Focus category" narrows the editor to one category at a time so the
# Subcategory dropdown only shows that category's options (SelectboxColumn
# cannot vary options per-row, so this is the practical workaround).
_cats_in_table = sorted(set(edit_df["Category"]) - {""})
focus_cat = st.selectbox(
    "Focus category for editing subcategories",
    options=["All categories"] + _cats_in_table,
    key="txn_focus_cat",
    help="Narrows the table to one category so the Subcategory dropdown only shows relevant options.",
)
if focus_cat != "All categories":
    edit_df = edit_df[edit_df["Category"] == focus_cat].reset_index(drop=True)

# A row's current label might not be in the taxonomy anymore (renamed/
# removed elsewhere) -- include it anyway so the dropdown can still show
# the row's actual value instead of silently blanking it.
category_options = sorted(set(all_parents) | set(edit_df["Category"]) - {""})
_focused_cats = set(edit_df["Category"]) - {""}
subcategory_options = sorted(
    {sub for cat in _focused_cats for sub in subs_by_parent.get(cat, [])}
    | (set(edit_df["Subcategory"]) - {""})
)

edited = st.data_editor(
    edit_df,
    column_config={
        "id": st.column_config.Column("ID", disabled=True, width="small",
                                      help="Monzo's id, for the sidebar's Exclude by transaction ID."),
        "Date": st.column_config.DateColumn(
            "Date", format="YYYY-MM-DD", width="small",
            help="The time-of-day from the original transaction is kept as-is -- only the date changes."),
        "Amount": st.column_config.NumberColumn("Amount", disabled=True, format="£%,.2f", width="small"),
        "Merchant": st.column_config.Column("Merchant / payee", disabled=True),
        "Description": st.column_config.Column(disabled=True),
        "Context": st.column_config.TextColumn("Context", width="medium"),
        "Category": st.column_config.SelectboxColumn(
            "Category", options=[""] + category_options, width="medium"),
        "Subcategory": st.column_config.SelectboxColumn(
            "Subcategory", options=[""] + subcategory_options, width="medium"),
    },
    width="stretch",
    hide_index=True,
    num_rows="fixed",
    height=min(35 * (len(edit_df) + 1) + 2, 640),
)

if st.button("Save changes", type="primary"):
    changes = 0
    for i in range(len(edit_df)):
        txn_id = edit_df.at[i, "id"]

        orig_cat = edit_df.at[i, "Category"]
        orig_sub = edit_df.at[i, "Subcategory"]
        new_cat, new_sub = sanitize_classification_edit(
            edited.at[i, "Category"], edited.at[i, "Subcategory"])
        # Clear subcategory if it doesn't belong to the newly chosen category
        if new_cat and new_sub and new_sub not in subs_by_parent.get(new_cat, []):
            new_sub = None
        if orig_cat != (new_cat or "") or orig_sub != (new_sub or ""):
            if new_cat:
                parent_id = upsert_parent(new_cat)
                if new_sub:
                    upsert_subcategory(new_sub, parent_id)
            update_classification(
                transaction_id=txn_id,
                category=new_cat or None,
                subcategory=new_sub or None,
                confidence=None,
                model="manual",
            )
            changes += 1

        # Date only edits the calendar date -- the original time-of-day
        # (e.g. the actual moment Monzo recorded the purchase) is kept,
        # since the date picker has no way to set a time.
        orig_datetime = edit_df.at[i, "Date"]
        new_date = edited.at[i, "Date"]
        new_date = new_date.date() if hasattr(new_date, "date") else new_date
        orig_context = edit_df.at[i, "Context"]
        new_context = edited.at[i, "Context"] or ""
        date_changed = orig_datetime.date() != new_date
        context_changed = orig_context != new_context
        if date_changed or context_changed:
            new_datetime = datetime.combine(new_date, orig_datetime.time()) if date_changed else orig_datetime
            update_transaction_details(
                transaction_id=txn_id,
                created_at=new_datetime.strftime("%Y-%m-%d %H:%M:%S"),
                user_context=new_context or None,
            )
            changes += 1

    if changes:
        st.success(f"Saved {changes} change(s).")
        st.cache_data.clear()
        st.rerun()
    else:
        st.info("No changes detected.")
