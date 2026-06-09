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

preset = st.sidebar.radio("Quick Date Filter", list(presets.keys()), horizontal=True, key="date_preset")

# When the preset changes, push the new dates into the date_input's session state key
# so it actually updates (date_input ignores value= after first render)
preset_from, preset_to = presets[preset]
preset_from = max(preset_from, min_date)
preset_to = min(preset_to, max_date)

if st.session_state.get("_last_preset") != preset:
    st.session_state["_last_preset"] = preset
    st.session_state["date_range"] = (preset_from, preset_to)

date_range = st.sidebar.date_input(
    "Custom Date Range",
    key="date_range",
    min_value=min_date,
    max_value=max_date,
)
date_from = date_range[0] if isinstance(date_range, (list, tuple)) and len(date_range) > 0 else preset_from
date_to = date_range[1] if isinstance(date_range, (list, tuple)) and len(date_range) > 1 else preset_to

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
    edit_df["clear"] = False
    edit_df = edit_df.rename(columns={
        "created_at": "Date", "amount": "Amount", "merchant_name": "Merchant",
        "description": "Description", "user_context": "Context",
        "llm_category": "Category", "llm_subcategory": "Subcategory",
        "clear": "Clear",
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
            "Clear": st.column_config.CheckboxColumn("Clear", width="small"),
        },
        width="stretch",
        hide_index=True,
        num_rows="fixed",
    )

    btn_col1, btn_col2 = st.columns([1, 1])
    with btn_col1:
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
    with btn_col2:
        if st.button("Clear selected labels"):
            to_clear = [edit_df.at[i, "id"] for i in range(len(edit_df)) if edited.at[i, "Clear"]]
            if to_clear:
                placeholders = ", ".join("?" * len(to_clear))
                con.execute(
                    f"""UPDATE transactions
                        SET llm_category = NULL, llm_subcategory = NULL,
                            classified_at = NULL, llm_model = NULL, llm_confidence = NULL
                        WHERE id IN ({placeholders})""",
                    to_clear,
                )
                st.success(f"Cleared labels for {len(to_clear)} transaction(s).")
                st.cache_data.clear()
                st.rerun()
            else:
                st.info("No rows checked.")


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
    # Session state for right-panel mode
    for _k in ("tax_edit_sub", "tax_view_sub", "tax_edit_parent", "tax_del_parent", "tax_wipe_parent"):
        if _k not in st.session_state:
            st.session_state[_k] = None

    # Load all parents (used by both columns)
    tax_parents = _query("""
        SELECT p.id, p.name,
               COUNT(DISTINCT s.id) AS sub_count,
               COUNT(DISTINCT t.id) AS txn_count
        FROM parent_categories p
        LEFT JOIN subcategories s ON s.parent_id = p.id
        LEFT JOIN transactions t ON t.llm_category = p.name
        GROUP BY p.id, p.name
        ORDER BY txn_count DESC
    """)
    tax_parent_names = [p["name"] for p in tax_parents]

    tax_left, tax_right = st.columns([2, 3])

    # ── Left: tree of parents + subcategories ──────────────────────────────────
    with tax_left:
        st.subheader("Categories")

        with st.form("tax_add_parent"):
            c1, c2 = st.columns([3, 1])
            with c1:
                new_pname_input = st.text_input("new_parent", placeholder="New parent category", label_visibility="collapsed")
            with c2:
                add_p_btn = st.form_submit_button("+ Add", use_container_width=True)
            if add_p_btn and new_pname_input.strip():
                upsert_parent(new_pname_input.strip())
                st.cache_data.clear()
                st.rerun()

        st.divider()

        for parent in tax_parents:
            pid = parent["id"]
            pname = parent["name"]

            subs_for_parent = _query("""
                SELECT s.id, s.name, COUNT(t.id) AS txn_count
                FROM subcategories s
                LEFT JOIN transactions t ON t.llm_subcategory = s.name AND t.llm_category = ?
                WHERE s.parent_id = ?
                GROUP BY s.id, s.name
                ORDER BY txn_count DESC
            """, [pname, pid])

            active_sub_ids = [
                s["id"] for s in subs_for_parent
                if s["id"] in (st.session_state.tax_edit_sub, st.session_state.tax_view_sub)
            ]
            is_active_parent = pid in (st.session_state.tax_edit_parent, st.session_state.tax_del_parent, st.session_state.tax_wipe_parent)

            label = f"**{pname}** — {parent['txn_count']} txns · {parent['sub_count']} subcats"
            with st.expander(label, expanded=bool(active_sub_ids or is_active_parent)):

                # Parent action buttons
                pb1, pb2, pb3 = st.columns(3)
                with pb1:
                    if st.button("✏️ Rename", key=f"ep_{pid}", use_container_width=True):
                        st.session_state.tax_edit_parent = pid
                        st.session_state.tax_del_parent = None
                        st.session_state.tax_wipe_parent = None
                        st.session_state.tax_edit_sub = None
                        st.session_state.tax_view_sub = None
                with pb2:
                    if st.button("🧹 Wipe labels", key=f"wp_{pid}", use_container_width=True):
                        st.session_state.tax_wipe_parent = pid
                        st.session_state.tax_edit_parent = None
                        st.session_state.tax_del_parent = None
                        st.session_state.tax_edit_sub = None
                        st.session_state.tax_view_sub = None
                with pb3:
                    if st.button("🗑️ Delete", key=f"dp_{pid}", use_container_width=True):
                        st.session_state.tax_del_parent = pid
                        st.session_state.tax_edit_parent = None
                        st.session_state.tax_wipe_parent = None
                        st.session_state.tax_edit_sub = None
                        st.session_state.tax_view_sub = None

                if subs_for_parent:
                    st.caption("Subcategories")
                    for sub in subs_for_parent:
                        sid = sub["id"]
                        is_sel = sid in (st.session_state.tax_edit_sub, st.session_state.tax_view_sub)
                        sc1, sc2, sc3, sc4 = st.columns([4, 1, 1, 1])
                        with sc1:
                            label_text = f"**{sub['name']}**" if is_sel else sub["name"]
                            st.markdown(f"{label_text} *({sub['txn_count']})*")
                        with sc2:
                            if st.button("✏️", key=f"es_{sid}", help="Edit name/parent"):
                                st.session_state.tax_edit_sub = sid
                                st.session_state.tax_view_sub = None
                                st.session_state.tax_edit_parent = None
                                st.session_state.tax_del_parent = None
                        with sc3:
                            if st.button("🔗", key=f"vs_{sid}", help="View transactions"):
                                st.session_state.tax_view_sub = sid
                                st.session_state.tax_edit_sub = None
                                st.session_state.tax_edit_parent = None
                                st.session_state.tax_del_parent = None
                        with sc4:
                            if st.button("🗑️", key=f"ds_{sid}", help="Delete subcategory"):
                                con.execute("DELETE FROM subcategories WHERE id = ?", [sid])
                                con.execute(
                                    "UPDATE transactions SET llm_subcategory = NULL WHERE llm_subcategory = ? AND llm_category = ?",
                                    [sub["name"], pname],
                                )
                                if st.session_state.tax_edit_sub == sid:
                                    st.session_state.tax_edit_sub = None
                                if st.session_state.tax_view_sub == sid:
                                    st.session_state.tax_view_sub = None
                                st.cache_data.clear()
                                st.rerun()

                # Add subcategory form inside this parent
                with st.form(f"add_sub_{pid}"):
                    new_sub_input = st.text_input(
                        "new_sub", placeholder="New subcategory name",
                        label_visibility="collapsed", key=f"nsi_{pid}"
                    )
                    if st.form_submit_button("+ Add subcategory", use_container_width=True):
                        if new_sub_input.strip():
                            upsert_subcategory(new_sub_input.strip(), pid)
                            st.cache_data.clear()
                            st.rerun()

    # ── Right: action panel ────────────────────────────────────────────────────
    with tax_right:

        # Rename parent
        if st.session_state.tax_edit_parent:
            pid = st.session_state.tax_edit_parent
            parent_row = next((p for p in tax_parents if p["id"] == pid), None)
            if parent_row:
                st.subheader(f"Rename: {parent_row['name']}")
                with st.form("edit_parent_form"):
                    new_pname = st.text_input("New name", value=parent_row["name"])
                    c1, c2 = st.columns(2)
                    with c1:
                        save = st.form_submit_button("Save", type="primary")
                    with c2:
                        cancel = st.form_submit_button("Cancel")
                    if save and new_pname.strip():
                        if new_pname.strip() != parent_row["name"]:
                            # DuckDB FK constraint fires on any UPDATE to a referenced parent row,
                            # even non-key columns. Workaround: insert new name, re-point subs, delete old.
                            new_pid = upsert_parent(new_pname.strip())
                            con.execute("UPDATE subcategories SET parent_id = ? WHERE parent_id = ?", [new_pid, pid])
                            con.execute("UPDATE transactions SET llm_category = ? WHERE llm_category = ?", [new_pname.strip(), parent_row["name"]])
                            con.execute("DELETE FROM parent_categories WHERE id = ?", [pid])
                        st.session_state.tax_edit_parent = None
                        st.cache_data.clear()
                        st.rerun()
                    if cancel:
                        st.session_state.tax_edit_parent = None
                        st.rerun()

        # Delete parent confirmation
        elif st.session_state.tax_del_parent:
            pid = st.session_state.tax_del_parent
            parent_row = next((p for p in tax_parents if p["id"] == pid), None)
            if parent_row:
                st.subheader(f"Delete: {parent_row['name']}")
                st.warning(
                    f"This will permanently delete **{parent_row['name']}**, all its subcategories, "
                    f"and clear labels from **{parent_row['txn_count']} transaction(s)**."
                )
                c1, c2 = st.columns(2)
                with c1:
                    if st.button("Confirm delete", type="primary"):
                        con.execute("DELETE FROM subcategories WHERE parent_id = ?", [pid])
                        con.execute("DELETE FROM parent_categories WHERE id = ?", [pid])
                        con.execute("UPDATE transactions SET llm_category = NULL, llm_subcategory = NULL WHERE llm_category = ?", [parent_row["name"]])
                        st.session_state.tax_del_parent = None
                        st.cache_data.clear()
                        st.rerun()
                with c2:
                    if st.button("Cancel"):
                        st.session_state.tax_del_parent = None
                        st.rerun()

        # Wipe labels confirmation
        elif st.session_state.tax_wipe_parent:
            pid = st.session_state.tax_wipe_parent
            parent_row = next((p for p in tax_parents if p["id"] == pid), None)
            if parent_row:
                st.subheader(f"Wipe labels: {parent_row['name']}")
                st.warning(
                    f"This will clear the LLM labels from **{parent_row['txn_count']} transaction(s)** "
                    f"under **{parent_row['name']}**. The transactions and taxonomy structure are kept — "
                    f"only the category assignments are removed so they can be re-classified."
                )
                c1, c2 = st.columns(2)
                with c1:
                    if st.button("Confirm wipe", type="primary"):
                        con.execute(
                            """UPDATE transactions
                               SET llm_category = NULL, llm_subcategory = NULL,
                                   classified_at = NULL, llm_model = NULL, llm_confidence = NULL
                               WHERE llm_category = ?""",
                            [parent_row["name"]],
                        )
                        st.session_state.tax_wipe_parent = None
                        st.cache_data.clear()
                        st.rerun()
                with c2:
                    if st.button("Cancel"):
                        st.session_state.tax_wipe_parent = None
                        st.rerun()

        # Edit subcategory
        elif st.session_state.tax_edit_sub:
            sid = st.session_state.tax_edit_sub
            sub_rows = _query("""
                SELECT s.id, s.name, s.parent_id, p.name AS parent_name
                FROM subcategories s
                JOIN parent_categories p ON p.id = s.parent_id
                WHERE s.id = ?
            """, [sid])
            if sub_rows:
                sub = sub_rows[0]
                st.subheader(f"Edit: {sub['name']}")
                with st.form("edit_sub_form"):
                    new_sname = st.text_input("Name", value=sub["name"])
                    cur_idx = tax_parent_names.index(sub["parent_name"]) if sub["parent_name"] in tax_parent_names else 0
                    new_sparent = st.selectbox("Parent category", tax_parent_names, index=cur_idx)
                    c1, c2 = st.columns(2)
                    with c1:
                        save = st.form_submit_button("Save", type="primary")
                    with c2:
                        cancel = st.form_submit_button("Cancel")
                    if save:
                        name_changed = new_sname.strip() and new_sname.strip() != sub["name"]
                        parent_changed = new_sparent != sub["parent_name"]
                        if name_changed:
                            con.execute("UPDATE subcategories SET name = ? WHERE id = ?", [new_sname.strip(), sid])
                            con.execute(
                                "UPDATE transactions SET llm_subcategory = ? WHERE llm_subcategory = ? AND llm_category = ?",
                                [new_sname.strip(), sub["name"], sub["parent_name"]],
                            )
                        if parent_changed:
                            new_pid = upsert_parent(new_sparent)
                            con.execute("UPDATE subcategories SET parent_id = ? WHERE id = ?", [new_pid, sid])
                            effective = new_sname.strip() if name_changed else sub["name"]
                            con.execute(
                                "UPDATE transactions SET llm_category = ? WHERE llm_subcategory = ? AND llm_category = ?",
                                [new_sparent, effective, sub["parent_name"]],
                            )
                        st.session_state.tax_edit_sub = None
                        st.cache_data.clear()
                        st.rerun()
                    if cancel:
                        st.session_state.tax_edit_sub = None
                        st.rerun()

        # View + reassign transactions
        elif st.session_state.tax_view_sub:
            sid = st.session_state.tax_view_sub
            sub_rows = _query("""
                SELECT s.id, s.name, s.parent_id, p.name AS parent_name
                FROM subcategories s
                JOIN parent_categories p ON p.id = s.parent_id
                WHERE s.id = ?
            """, [sid])
            if sub_rows:
                sub = sub_rows[0]
                st.subheader(f"Transactions: {sub['name']}")
                st.caption(f"Under {sub['parent_name']}")

                reassign_raw = _query("""
                    SELECT s.id, s.name, p.name AS parent_name
                    FROM subcategories s
                    JOIN parent_categories p ON p.id = s.parent_id
                    WHERE s.id != ?
                    ORDER BY p.name, s.name
                """, [sid])
                reassign_map = {f"{s['name']} ({s['parent_name']})": s for s in reassign_raw}

                if reassign_map:
                    with st.form("reassign_all_form"):
                        target_key = st.selectbox("Move all transactions to:", list(reassign_map.keys()))
                        if st.form_submit_button("Move all", type="secondary"):
                            tgt = reassign_map[target_key]
                            con.execute(
                                "UPDATE transactions SET llm_category = ?, llm_subcategory = ? WHERE llm_subcategory = ? AND llm_category = ?",
                                [tgt["parent_name"], tgt["name"], sub["name"], sub["parent_name"]],
                            )
                            st.session_state.tax_view_sub = None
                            st.cache_data.clear()
                            st.rerun()

                st.divider()

                txns = _query("""
                    SELECT id, description, merchant_name, amount, created_at, user_context
                    FROM transactions
                    WHERE llm_subcategory = ? AND llm_category = ?
                    ORDER BY created_at DESC
                """, [sub["name"], sub["parent_name"]])

                if txns:
                    txns_df = pd.DataFrame(txns)
                    txns_df["amount"] = txns_df["amount"].apply(lambda x: f"£{abs(float(x)):.2f}")
                    txns_df["created_at"] = pd.to_datetime(txns_df["created_at"]).dt.strftime("%Y-%m-%d")
                    txns_df = txns_df.rename(columns={
                        "created_at": "Date", "merchant_name": "Merchant",
                        "description": "Description", "amount": "Amount", "user_context": "Context",
                    })
                    st.dataframe(
                        txns_df[["Date", "Merchant", "Description", "Amount", "Context"]],
                        use_container_width=True,
                        hide_index=True,
                    )
                    st.caption(f"{len(txns)} transaction(s)")
                else:
                    st.info("No transactions assigned to this subcategory.")

        else:
            st.info("Click ✏️ to edit, 🔗 to view transactions, or 🗑️ to delete a subcategory.")
