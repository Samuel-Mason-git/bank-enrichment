import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))
load_dotenv(Path(__file__).parent.parent.parent / "config" / ".env")

from database_functions import (
    init_db, get_con, update_classification, upsert_parent, upsert_subcategory,
    get_subscriptions, upsert_subscription, toggle_subscription, delete_subscription,
)

st.set_page_config(
    page_title="Bank Enrichment",
    page_icon="💳",
    layout="wide",
)


# ── Connection ─────────────────────────────────────────────────────────────────

@st.cache_resource
def connect():
    init_db()
    return get_con()


con = connect()


# ── Helpers ────────────────────────────────────────────────────────────────────

def _query(sql: str, params: list = []) -> list[dict]:
    """Run a query against the cached con and return list of dicts."""
    result = con.execute(sql, params)
    cols = [d[0] for d in result.description]
    return [dict(zip(cols, row)) for row in result.fetchall()]


# ── Load data ──────────────────────────────────────────────────────────────────

@st.cache_data(ttl=60)
def load_transactions():
    result = con.execute("""
        SELECT
            id, amount, description, monzo_category,
            merchant_name, counterparty_name,
            created_at, user_context, skipped,
            llm_category, llm_subcategory
        FROM transactions
        ORDER BY created_at DESC
    """)
    cols = [d[0] for d in result.description]
    df = pd.DataFrame(result.fetchall(), columns=cols)
    df["created_at"] = pd.to_datetime(df["created_at"])
    df["month"] = df["created_at"].dt.to_period("M").astype(str)
    return df


@st.cache_data(ttl=60)
def load_taxonomy():
    parents = con.execute("SELECT name FROM parent_categories ORDER BY name").fetchall()
    subs = con.execute("""
        SELECT s.name, p.name AS parent_name
        FROM subcategories s
        JOIN parent_categories p ON p.id = s.parent_id
        ORDER BY s.name
    """).fetchall()
    all_parent_names = [r[0] for r in parents]
    # {parent_name: [sub_name, ...]}
    subs_by_parent = {}
    for sub_name, parent_name in subs:
        subs_by_parent.setdefault(parent_name, []).append(sub_name)
    all_sub_names = [r[0] for r in subs]
    return all_parent_names, all_sub_names, subs_by_parent


df = load_transactions()
all_parents, all_subs, subs_by_parent = load_taxonomy()

# ── Sidebar filters ────────────────────────────────────────────────────────────

st.sidebar.title("Filters")

if not df.empty:
    min_date = df["created_at"].min().date()
    max_date = df["created_at"].max().date()
else:
    min_date = date.today() - timedelta(days=365)
    max_date = date.today()

today = date.today()
presets = {
    "All time":   (min_date, max_date),
    "YTD":        (date(today.year, 1, 1), today),
    "12 months":  (today - timedelta(days=365), today),
    "6 months":   (today - timedelta(days=183), today),
    "3 months":   (today - timedelta(days=91), today),
    "This month": (today.replace(day=1), today),
}

preset = st.sidebar.radio("Quick Date Filter", list(presets.keys()), horizontal=True, index=0)
preset_from, preset_to = presets[preset]
preset_from = max(preset_from, min_date)
preset_to = min(preset_to, max_date)

date_range = st.sidebar.date_input(
    "Custom Date Range",
    value=(preset_from, preset_to),
    min_value=min_date,
    max_value=max_date,
)
date_from = date_range[0] if len(date_range) > 0 else preset_from
date_to = date_range[1] if len(date_range) > 1 else preset_to

selected_parents = st.sidebar.multiselect("Parent category", all_parents)

# If parent(s) selected, only show subcategories that belong to those parents
if selected_parents:
    available_subs = sorted({
        sub for p in selected_parents for sub in subs_by_parent.get(p, [])
    })
else:
    available_subs = all_subs

selected_subs = st.sidebar.multiselect("Subcategory", available_subs)
search = st.sidebar.text_input("Search", placeholder="merchant, description, context...")
show_skipped = st.sidebar.checkbox("Show skipped", value=False)
show_unclassified = st.sidebar.checkbox("Show unclassified", value=True)

st.sidebar.divider()
if st.sidebar.button("Refresh data"):
    st.cache_data.clear()
    st.rerun()

# ── Apply filters ──────────────────────────────────────────────────────────────

filtered = df.copy()
filtered = filtered[
    (filtered["created_at"].dt.date >= date_from) &
    (filtered["created_at"].dt.date <= date_to)
]
if not show_skipped:
    filtered = filtered[filtered["skipped"] != True]
if not show_unclassified:
    filtered = filtered[filtered["llm_category"].notna()]
if selected_parents:
    filtered = filtered[filtered["llm_category"].isin(selected_parents)]
if selected_subs:
    filtered = filtered[filtered["llm_subcategory"].isin(selected_subs)]
if search:
    mask = (
        filtered["description"].str.contains(search, case=False, na=False)
        | filtered["merchant_name"].str.contains(search, case=False, na=False)
        | filtered["user_context"].str.contains(search, case=False, na=False)
    )
    filtered = filtered[mask]

# ── Tabs ───────────────────────────────────────────────────────────────────────

st.title("💳 Bank Enrichment")

tab_overview, tab_time, tab_txns, tab_drill, tab_subs, tab_taxonomy = st.tabs([
    "Overview", "Spending Over Time", "Transactions", "Category Drill-Down", "Subscriptions", "Taxonomy"
])


# ── Tab 1: Overview ────────────────────────────────────────────────────────────

with tab_overview:
    spend_df = filtered[filtered["amount"] < 0]
    income_df = filtered[filtered["amount"] > 0]
    total_spend = abs(spend_df["amount"].sum())
    total_income = income_df["amount"].sum()
    net = total_income - total_spend

    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Transactions", len(filtered))
    col2.metric("Total Spend", f"£{total_spend:.2f}")
    col3.metric("Total Income", f"£{total_income:.2f}")
    col4.metric("Net", f"£{net:.2f}", delta=round(net, 2), delta_color="normal")
    col5.metric("Unclassified", int(filtered["llm_category"].isna().sum()))

    st.divider()

    chart_col1, chart_col2 = st.columns(2)

    with chart_col1:
        classified_spend = spend_df[spend_df["llm_category"].notna()]
        if not classified_spend.empty:
            by_cat = (
                classified_spend.groupby("llm_category")["amount"]
                .sum().abs().reset_index()
                .rename(columns={"llm_category": "Category", "amount": "Amount"})
                .sort_values("Amount", ascending=True)
            )
            fig = px.bar(by_cat, x="Amount", y="Category", orientation="h",
                         title="Spend by Category", labels={"Amount": "Total Spend (£)"},
                         color="Amount", color_continuous_scale="Reds")
            fig.update_layout(showlegend=False, coloraxis_showscale=False,
                              height=max(300, len(by_cat) * 45),
                              margin=dict(l=0, r=0, t=40, b=0))
            st.plotly_chart(fig, width="stretch")
        else:
            st.info("No classified spend in range.")

    with chart_col2:
        classified_income = income_df[income_df["llm_category"].notna()]
        if not classified_income.empty:
            by_cat_in = (
                classified_income.groupby("llm_category")["amount"]
                .sum().reset_index()
                .rename(columns={"llm_category": "Category", "amount": "Amount"})
                .sort_values("Amount", ascending=True)
            )
            fig2 = px.bar(by_cat_in, x="Amount", y="Category", orientation="h",
                          title="Income by Category", labels={"Amount": "Total Income (£)"},
                          color="Amount", color_continuous_scale="Greens")
            fig2.update_layout(showlegend=False, coloraxis_showscale=False,
                               height=max(300, len(by_cat_in) * 45),
                               margin=dict(l=0, r=0, t=40, b=0))
            st.plotly_chart(fig2, width="stretch")
        else:
            st.info("No classified income in range.")


# ── Tab 2: Spending Over Time ──────────────────────────────────────────────────

with tab_time:
    spend_time = filtered[filtered["amount"] < 0].copy()

    if not spend_time.empty:
        spend_time["Category"] = spend_time["llm_category"].fillna("Unclassified")
        monthly = (
            spend_time.groupby(["month", "Category"])["amount"]
            .sum().abs().reset_index()
            .rename(columns={"amount": "Spend", "month": "Month"})
        )
        fig = px.bar(monthly, x="Month", y="Spend", color="Category",
                     title="Monthly Spend by Category",
                     labels={"Spend": "Total Spend (£)"}, barmode="stack")
        fig.update_layout(xaxis_tickangle=-45, margin=dict(t=40))
        st.plotly_chart(fig, width="stretch")

        st.subheader("Monthly Totals")
        income_time = filtered[filtered["amount"] > 0]
        monthly_spend = spend_time.groupby("month")["amount"].sum().abs().rename("Spend")
        monthly_income = income_time.groupby("month")["amount"].sum().rename("Income")
        monthly_totals = (
            pd.concat([monthly_spend, monthly_income], axis=1)
            .fillna(0).reset_index()
            .rename(columns={"month": "Month"})
            .sort_values("Month", ascending=False)
        )
        monthly_totals["Net"] = monthly_totals["Income"] - monthly_totals["Spend"]
        monthly_totals["Spend"] = monthly_totals["Spend"].map("£{:.2f}".format)
        monthly_totals["Income"] = monthly_totals["Income"].map("£{:.2f}".format)
        monthly_totals["Net"] = monthly_totals["Net"].map("£{:.2f}".format)
        st.dataframe(monthly_totals, width="stretch", hide_index=True)
    else:
        st.info("No spend transactions in the selected range.")


# ── Tab 3: Transactions ────────────────────────────────────────────────────────

with tab_txns:
    st.subheader(f"{len(filtered)} transactions")
    st.caption("Edit Category or Subcategory cells directly — type any value including new labels. Click Save to apply.")

    edit_df = filtered[["id", "created_at", "amount", "merchant_name", "description",
                         "user_context", "llm_category", "llm_subcategory"]].copy()
    edit_df["created_at"] = edit_df["created_at"].dt.strftime("%Y-%m-%d")
    edit_df["amount"] = edit_df["amount"].map("£{:.2f}".format)
    edit_df = edit_df.rename(columns={
        "created_at": "Date", "amount": "Amount", "merchant_name": "Merchant",
        "description": "Description", "user_context": "Context",
        "llm_category": "Category", "llm_subcategory": "Subcategory",
    }).reset_index(drop=True)

    edited = st.data_editor(
        edit_df,
        column_config={
            "id": st.column_config.Column("ID", disabled=True, width="small"),
            "Date": st.column_config.Column(disabled=True, width="small"),
            "Amount": st.column_config.Column(disabled=True, width="small"),
            "Merchant": st.column_config.Column(disabled=True),
            "Description": st.column_config.Column(disabled=True),
            "Context": st.column_config.Column(disabled=True),
            "Category": st.column_config.TextColumn("Category", width="medium"),
            "Subcategory": st.column_config.TextColumn("Subcategory", width="medium"),
        },
        width="stretch",
        hide_index=True,
        num_rows="fixed",
    )

    if st.button("Save changes", type="primary"):
        changes = 0
        for i in range(len(edit_df)):
            orig_cat = edit_df.at[i, "Category"]
            orig_sub = edit_df.at[i, "Subcategory"]
            new_cat = edited.at[i, "Category"]
            new_sub = edited.at[i, "Subcategory"]
            txn_id = edit_df.at[i, "id"]
            if orig_cat != new_cat or orig_sub != new_sub:
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
        if changes:
            st.success(f"Saved {changes} change(s).")
            st.cache_data.clear()
            st.rerun()
        else:
            st.info("No changes detected.")


# ── Tab 4: Category Drill-Down ─────────────────────────────────────────────────

with tab_drill:
    classified = filtered[filtered["llm_category"].notna()]

    if classified.empty:
        st.info("No classified transactions in the selected range.")
    else:
        selected_cat = st.selectbox(
            "Select parent category",
            sorted(classified["llm_category"].unique()),
        )
        cat_df = classified[classified["llm_category"] == selected_cat]
        spend_cat = cat_df[cat_df["amount"] < 0]
        income_cat = cat_df[cat_df["amount"] > 0]
        total_spend_cat = abs(spend_cat["amount"].sum())
        total_income_cat = income_cat["amount"].sum()
        net_cat = total_income_cat - total_spend_cat

        col1, col2, col3, col4, col5 = st.columns(5)
        col1.metric("Transactions", len(cat_df))
        col2.metric("Total Spend", f"£{total_spend_cat:.2f}")
        col3.metric("Total Income", f"£{total_income_cat:.2f}")
        col4.metric("Net", f"£{net_cat:.2f}", delta=round(net_cat, 2), delta_color="normal")
        col5.metric("Subcategories", int(cat_df["llm_subcategory"].nunique()))

        st.divider()

        chart_col1, chart_col2 = st.columns(2)

        with chart_col1:
            classified_spend_cat = spend_cat[spend_cat["llm_subcategory"].notna()]
            if not classified_spend_cat.empty:
                sub_spend = (
                    classified_spend_cat.groupby("llm_subcategory")["amount"]
                    .sum().abs().reset_index()
                    .rename(columns={"llm_subcategory": "Subcategory", "amount": "Amount"})
                    .sort_values("Amount", ascending=True)
                )
                fig = px.bar(sub_spend, x="Amount", y="Subcategory", orientation="h",
                             title=f"Spend by Subcategory",
                             labels={"Amount": "Total Spend (£)"},
                             color="Amount", color_continuous_scale="Reds")
                fig.update_layout(showlegend=False, coloraxis_showscale=False,
                                  height=max(250, len(sub_spend) * 45),
                                  margin=dict(l=0, r=0, t=40, b=0))
                st.plotly_chart(fig, width="stretch")
            else:
                st.info("No spend in this category.")

        with chart_col2:
            classified_income_cat = income_cat[income_cat["llm_subcategory"].notna()]
            if not classified_income_cat.empty:
                sub_income = (
                    classified_income_cat.groupby("llm_subcategory")["amount"]
                    .sum().reset_index()
                    .rename(columns={"llm_subcategory": "Subcategory", "amount": "Amount"})
                    .sort_values("Amount", ascending=True)
                )
                fig2 = px.bar(sub_income, x="Amount", y="Subcategory", orientation="h",
                              title=f"Income by Subcategory",
                              labels={"Amount": "Total Income (£)"},
                              color="Amount", color_continuous_scale="Greens")
                fig2.update_layout(showlegend=False, coloraxis_showscale=False,
                                   height=max(250, len(sub_income) * 45),
                                   margin=dict(l=0, r=0, t=40, b=0))
                st.plotly_chart(fig2, width="stretch")
            else:
                st.info("No income in this category.")

        st.subheader("Transactions")
        display_cat = cat_df[["created_at", "amount", "merchant_name", "description",
                               "user_context", "llm_subcategory"]].copy()
        display_cat["created_at"] = display_cat["created_at"].dt.strftime("%Y-%m-%d")
        display_cat["amount"] = display_cat["amount"].map("£{:.2f}".format)
        display_cat.columns = ["Date", "Amount", "Merchant", "Description", "Context", "Subcategory"]
        st.dataframe(display_cat, width="stretch", hide_index=True)


# ── Tab 5: Subscriptions ──────────────────────────────────────────────────────

FREQ_MONTHLY = {"weekly": 52/12, "fortnightly": 26/12, "monthly": 1, "annual": 1/12}

def _detect_subscriptions(df: pd.DataFrame, confirmed_names: set) -> list[dict]:
    candidates = []
    spend = df[(df["amount"] < 0) & df["merchant_name"].notna() & (df["skipped"] != True)].copy()
    for merchant, group in spend.groupby("merchant_name"):
        if merchant in confirmed_names:
            continue
        dates = group["created_at"].sort_values()
        if len(dates) < 2:
            continue
        gaps = dates.diff().dropna().dt.days.tolist()
        if not gaps:
            continue
        mean_gap = sum(gaps) / len(gaps)
        std_gap = pd.Series(gaps).std()
        cv = std_gap / mean_gap if mean_gap > 0 else 999
        for freq, target, tolerance in [
            ("weekly", 7, 2), ("fortnightly", 14, 3),
            ("monthly", 30, 6), ("annual", 365, 30),
        ]:
            if abs(mean_gap - target) <= tolerance and cv < 0.4:
                median_amount = abs(group["amount"].median())
                candidates.append({
                    "name": merchant,
                    "amount": round(median_amount, 2),
                    "frequency": freq,
                    "occurrences": len(group),
                    "monthly_cost": round(median_amount * FREQ_MONTHLY[freq], 2),
                })
                break
    return sorted(candidates, key=lambda x: x["monthly_cost"], reverse=True)


with tab_subs:
    subs = get_subscriptions()
    confirmed_names = {s["name"] for s in subs}

    # Auto-inactive subscriptions with no matching transaction in last 2 months
    two_months_ago = (pd.Timestamp.now() - pd.DateOffset(months=2))
    for s in subs:
        if not s["active"]:
            continue
        match_name = s["merchant_name"] or s["name"]
        last_txn = df[
            df["merchant_name"].str.contains(match_name, case=False, na=False) &
            (df["amount"] < 0)
        ]["created_at"].max()
        if pd.isna(last_txn) or last_txn < two_months_ago:
            toggle_subscription(s["id"])
    subs = get_subscriptions()
    confirmed_names = {s["name"] for s in subs}

    # ── KPI cards ──────────────────────────────────────────────────────────────
    active_subs = [s for s in subs if s["active"]]
    monthly_cost = sum(s["amount"] * FREQ_MONTHLY[s["frequency"]] for s in active_subs)
    annual_cost = monthly_cost * 12

    col1, col2, col3 = st.columns(3)
    col1.metric("Active Subscriptions", len(active_subs))
    col2.metric("Est. Monthly Cost", f"£{monthly_cost:.2f}")
    col3.metric("Est. Annual Cost", f"£{annual_cost:.2f}")

    st.divider()

    # ── Add manually ───────────────────────────────────────────────────────────
    st.subheader("Add Subscription")
    known_merchants = sorted(df["merchant_name"].dropna().unique().tolist())
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
            upsert_subscription(sub_name.strip(), sub_amount, sub_freq, sub_merchant or None)
            st.cache_data.clear()
            st.rerun()

    # ── Confirmed subscriptions ────────────────────────────────────────────────
    if subs:
        st.subheader("Your Subscriptions")
        for s in subs:
            mc = s["amount"] * FREQ_MONTHLY[s["frequency"]]
            col1, col2, col3, col4, col5 = st.columns([3, 1, 1, 1, 1])
            col1.markdown(f"**{s['name']}**")
            col2.markdown(f"£{s['amount']:.2f} / {s['frequency']}")
            col3.markdown(f"~£{mc:.2f}/mo")
            label = "Disable" if s["active"] else "Enable"
            if col4.button(label, key=f"toggle_{s['id']}"):
                toggle_subscription(s["id"])
                st.cache_data.clear()
                st.rerun()
            if col5.button("✕", key=f"del_{s['id']}"):
                delete_subscription(s["id"])
                st.cache_data.clear()
                st.rerun()
            if not s["active"]:
                st.caption("⏸ Inactive")

    st.divider()

    # ── Detected candidates ────────────────────────────────────────────────────
    st.subheader("Suggested Subscriptions")
    st.caption("Detected from recurring transactions — click Add to confirm.")
    candidates = _detect_subscriptions(df, confirmed_names)
    if candidates:
        for c in candidates:
            col1, col2, col3, col4 = st.columns([3, 1, 1, 1])
            col1.markdown(f"**{c['name']}**  <span style='color:#a1a1aa;font-size:0.8em'>{c['occurrences']} transactions</span>", unsafe_allow_html=True)
            col2.markdown(f"£{c['amount']:.2f} / {c['frequency']}")
            col3.markdown(f"~£{c['monthly_cost']:.2f}/mo")
            if col4.button("+ Add", key=f"add_{c['name']}"):
                upsert_subscription(c["name"], c["amount"], c["frequency"], c["name"])
                st.cache_data.clear()
                st.rerun()
    else:
        st.info("No recurring patterns detected yet — more transaction history will improve detection.")


# ── Tab 6: Taxonomy ────────────────────────────────────────────────────────────

with tab_taxonomy:
    st.caption("Rename labels by editing the Name cell. Add rows to create new ones. Renames cascade to all transactions automatically.")

    # ── Parent categories ──────────────────────────────────────────────────────
    st.subheader("Parent Categories")

    parents_raw = _query("""
        SELECT p.id, p.name, COUNT(t.id) AS transaction_count
        FROM parent_categories p
        LEFT JOIN transactions t ON t.llm_category = p.name
        GROUP BY p.id, p.name
        ORDER BY transaction_count DESC
    """)
    parents_df = pd.DataFrame(parents_raw) if parents_raw else pd.DataFrame(columns=["id", "name", "transaction_count"])
    parents_df = parents_df.rename(columns={"name": "Name", "transaction_count": "Transactions"})
    parents_display = parents_df[["Name", "Transactions"]].reset_index(drop=True)

    edited_parents = st.data_editor(
        parents_display,
        column_config={
            "Name": st.column_config.TextColumn("Name", width="large"),
            "Transactions": st.column_config.Column("Transactions", disabled=True, width="small"),
        },
        width="stretch",
        hide_index=True,
        num_rows="dynamic",
    )

    if st.button("Save parent categories", type="primary"):
        changes = 0
        # Deletions handled separately below
        # Renames
        for i in range(min(len(parents_df), len(edited_parents))):
            old_name = parents_df.at[i, "Name"]
            new_name = str(edited_parents.at[i, "Name"] or "").strip()
            row_id = parents_df.at[i, "id"]
            if old_name and new_name and old_name != new_name:
                con.execute("UPDATE parent_categories SET name = ? WHERE id = ?", [new_name, row_id])
                con.execute("UPDATE transactions SET llm_category = ? WHERE llm_category = ?", [new_name, old_name])
                changes += 1
        # New rows
        for i in range(len(parents_df), len(edited_parents)):
            new_name = str(edited_parents.at[i, "Name"] or "").strip()
            if new_name:
                upsert_parent(new_name)
                changes += 1
        if changes:
            st.success(f"Saved {changes} change(s).")
            st.cache_data.clear()
            st.rerun()
        else:
            st.info("No changes detected.")

    if parents_df is not None and not parents_df.empty:
        with st.expander("Delete a parent category"):
            del_parent = st.selectbox("Select category to delete", parents_df["Name"].tolist(), key="del_parent")
            st.caption("⚠️ This will also delete all its subcategories and clear the label from all transactions.")
            if st.button("Delete", type="primary", key="del_parent_btn"):
                row = parents_df[parents_df["Name"] == del_parent].iloc[0]
                con.execute("DELETE FROM subcategories WHERE parent_id = ?", [int(row["id"])])
                con.execute("DELETE FROM parent_categories WHERE id = ?", [int(row["id"])])
                con.execute("UPDATE transactions SET llm_category = NULL, llm_subcategory = NULL WHERE llm_category = ?", [del_parent])
                st.cache_data.clear()
                st.rerun()

    st.divider()

    # ── Subcategories ──────────────────────────────────────────────────────────
    st.subheader("Subcategories")

    subs_raw = _query("""
        SELECT s.id, s.name, s.parent_id, p.name AS parent_name,
               COUNT(t.id) AS transaction_count
        FROM subcategories s
        JOIN parent_categories p ON p.id = s.parent_id
        LEFT JOIN transactions t ON t.llm_subcategory = s.name AND t.llm_category = p.name
        GROUP BY s.id, s.name, s.parent_id, p.name
        ORDER BY p.name, transaction_count DESC
    """)
    subs_df = pd.DataFrame(subs_raw) if subs_raw else pd.DataFrame(columns=["id", "name", "parent_id", "parent_name", "transaction_count"])
    subs_df = subs_df.rename(columns={"name": "Name", "parent_name": "Parent", "transaction_count": "Transactions"})
    subs_display = subs_df[["Name", "Parent", "Transactions"]].reset_index(drop=True)

    current_parent_names = sorted(edited_parents["Name"].dropna().tolist())

    edited_subs = st.data_editor(
        subs_display,
        column_config={
            "Name": st.column_config.TextColumn("Name", width="large"),
            "Parent": st.column_config.SelectboxColumn("Parent", options=current_parent_names, width="medium"),
            "Transactions": st.column_config.Column("Transactions", disabled=True, width="small"),
        },
        width="stretch",
        hide_index=True,
        num_rows="dynamic",
    )

    if st.button("Save subcategories", type="primary"):
        changes = 0
        # Deletions handled separately below
        # Renames
        for i in range(min(len(subs_df), len(edited_subs))):
            old_name = subs_df.at[i, "Name"]
            new_name = str(edited_subs.at[i, "Name"] or "").strip()
            old_parent = subs_df.at[i, "Parent"]
            row_id = subs_df.at[i, "id"]
            if old_name and new_name and old_name != new_name:
                con.execute("UPDATE subcategories SET name = ? WHERE id = ?", [new_name, row_id])
                con.execute(
                    "UPDATE transactions SET llm_subcategory = ? WHERE llm_subcategory = ? AND llm_category = ?",
                    [new_name, old_name, old_parent],
                )
                changes += 1
        # New rows
        for i in range(len(subs_df), len(edited_subs)):
            new_name = str(edited_subs.at[i, "Name"] or "").strip()
            new_parent = str(edited_subs.at[i, "Parent"] or "").strip()
            if new_name and new_parent:
                parent_id = upsert_parent(new_parent)
                upsert_subcategory(new_name, parent_id)
                changes += 1
        if changes:
            st.success(f"Saved {changes} change(s).")
            st.cache_data.clear()
            st.rerun()
        else:
            st.info("No changes detected.")

    if subs_df is not None and not subs_df.empty:
        with st.expander("Delete a subcategory"):
            sub_options = [f"{r['Name']} ({r['Parent']})" for _, r in subs_df.iterrows()]
            del_sub = st.selectbox("Select subcategory to delete", sub_options, key="del_sub")
            st.caption("⚠️ This will clear the subcategory label from all affected transactions.")
            if st.button("Delete", type="primary", key="del_sub_btn"):
                del_idx = sub_options.index(del_sub)
                row = subs_df.iloc[del_idx]
                con.execute("DELETE FROM subcategories WHERE id = ?", [int(row["id"])])
                con.execute(
                    "UPDATE transactions SET llm_subcategory = NULL WHERE llm_subcategory = ? AND llm_category = ?",
                    [row["Name"], row["Parent"]],
                )
                st.cache_data.clear()
                st.rerun()
