import html
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))
load_dotenv(Path(__file__).parent.parent.parent / "config" / ".env")

from database_functions import (
    init_db, get_con, update_classification, update_transaction_details, upsert_parent, upsert_subcategory,
    get_subscriptions, upsert_subscription, update_subscription, toggle_subscription, delete_subscription,
    get_parents, get_subcategories, set_parent_role, set_subcategory_role, ROLES,
    backup_db, list_backups, read_backup_manifest, backup_size_bytes,
    delete_backup, BACKUPS_KEPT,
)
from dashboard_helpers import (
    FREQ_MONTHLY, pct_delta, detect_subscriptions, should_deactivate_subscription,
    sanitize_classification_edit, ROLE_METRICS, role_breakdown, merchant_display_name,
    backup_label, backup_reason, backup_coverage, format_bytes,
)

st.set_page_config(
    page_title="Bank Enrichment",
    page_icon="💳",
    layout="wide",
)


# ── Connection ─────────────────────────────────────────────────────────────────

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

@st.cache_data(ttl=60, show_spinner="Loading transactions...")
def load_transactions():
    result = con.execute("""
        SELECT
            id, amount, description, monzo_category,
            merchant_name, counterparty_name,
            created_at, user_context, skipped,
            llm_category, llm_subcategory, role
        FROM transaction_roles
        ORDER BY created_at DESC
    """)
    cols = [d[0] for d in result.description]
    df = pd.DataFrame(result.fetchall(), columns=cols)
    df["created_at"] = pd.to_datetime(df["created_at"])
    df["month"] = df["created_at"].dt.to_period("M").astype(str)
    return df


@st.cache_data(ttl=60, show_spinner="Loading taxonomy...")
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

st.sidebar.title("💳 Filters")

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

st.sidebar.markdown("#### 📅 Date Range")
preset = st.sidebar.radio("Quick Date Filter", list(presets.keys()), horizontal=True, key="date_preset")

# When the preset changes, push the new dates into the date_input's session state key
# so it actually updates (date_input ignores value= after first render)
preset_from, preset_to = presets[preset]
preset_from = max(preset_from, min_date)
preset_to = min(preset_to, max_date)

if st.session_state.get("_last_preset") != preset:
    st.session_state["_last_preset"] = preset
    st.session_state["date_range"] = (preset_from, preset_to)

if st.sidebar.button("Reset date range", help="Clear any custom dates typed below and snap back to the Quick Date Filter selected above."):
    st.session_state["date_range"] = (preset_from, preset_to)

date_range = st.sidebar.date_input(
    "Custom Date Range",
    key="date_range",
    min_value=min_date,
    max_value=max_date,
)
date_from = date_range[0] if isinstance(date_range, (list, tuple)) and len(date_range) > 0 else preset_from
date_to = date_range[1] if isinstance(date_range, (list, tuple)) and len(date_range) > 1 else preset_to

st.sidebar.divider()
st.sidebar.markdown("#### 🏷️ Categories")
selected_parents = st.sidebar.multiselect("Parent category", all_parents)

# If parent(s) selected, only show subcategories that belong to those parents
if selected_parents:
    available_subs = sorted({
        sub for p in selected_parents for sub in subs_by_parent.get(p, [])
    })
else:
    available_subs = all_subs

selected_subs = st.sidebar.multiselect("Subcategory", available_subs)

st.sidebar.divider()
st.sidebar.markdown("#### 🔎 Search & Display")
search = st.sidebar.text_input("Search", placeholder="merchant, description, context...")
show_skipped = st.sidebar.checkbox("Show skipped", value=False)
show_unclassified = st.sidebar.checkbox("Show unclassified", value=True)

st.sidebar.divider()
st.sidebar.markdown("#### 🚫 Exclude")
_all_display_names = sorted(merchant_display_name(df).dropna().unique().tolist())
exclude_merchants = st.sidebar.multiselect(
    "Exclude merchant / counterparty",
    options=_all_display_names,
    key="exclude_merchants",
    help="Hide all transactions from these merchants or counterparties in every view.",
)
_exclude_ids_raw = st.sidebar.text_area(
    "Exclude by transaction ID",
    placeholder="Paste IDs here — one per line or comma-separated",
    key="exclude_ids",
    help="Hide specific transactions by ID. Useful when there's no merchant or counterparty name to select.",
)
exclude_ids = (
    {s.strip() for s in _exclude_ids_raw.replace(",", "\n").split("\n") if s.strip()}
    if _exclude_ids_raw else set()
)

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
        filtered["description"].str.contains(search, case=False, na=False, regex=False)
        | filtered["merchant_name"].str.contains(search, case=False, na=False, regex=False)
        | filtered["user_context"].str.contains(search, case=False, na=False, regex=False)
    )
    filtered = filtered[mask]
if exclude_merchants:
    filtered = filtered[~merchant_display_name(filtered).isin(exclude_merchants)]
if exclude_ids:
    filtered = filtered[~filtered["id"].isin(exclude_ids)]

# ── Export (sidebar, after filters applied) ───────────────────────────────────

_export_cols = ["created_at", "merchant_name", "counterparty_name", "description",
                "amount", "llm_category", "llm_subcategory", "user_context"]
_export_df = filtered[[c for c in _export_cols if c in filtered.columns]].copy()
_export_df["created_at"] = _export_df["created_at"].dt.strftime("%Y-%m-%d %H:%M")
st.sidebar.divider()
st.sidebar.download_button(
    label="Export CSV",
    data=_export_df.to_csv(index=False),
    file_name=f"transactions_{date_from}_{date_to}.csv",
    mime="text/csv",
    help="Download the current filtered transactions as a CSV.",
    use_container_width=True,
)

# ── Previous period (same duration, immediately before) ────────────────────────

_period_days = (date_to - date_from).days
_prev_to = date_from - timedelta(days=1)
_prev_from = _prev_to - timedelta(days=_period_days)

prev_filtered = df[
    (df["created_at"].dt.date >= _prev_from) &
    (df["created_at"].dt.date <= _prev_to)
].copy()
if not show_skipped:
    prev_filtered = prev_filtered[prev_filtered["skipped"] != True]
if not show_unclassified:
    prev_filtered = prev_filtered[prev_filtered["llm_category"].notna()]
if selected_parents:
    prev_filtered = prev_filtered[prev_filtered["llm_category"].isin(selected_parents)]
if selected_subs:
    prev_filtered = prev_filtered[prev_filtered["llm_subcategory"].isin(selected_subs)]
if search:
    _mask = (
        prev_filtered["description"].str.contains(search, case=False, na=False, regex=False)
        | prev_filtered["merchant_name"].str.contains(search, case=False, na=False, regex=False)
        | prev_filtered["user_context"].str.contains(search, case=False, na=False, regex=False)
    )
    prev_filtered = prev_filtered[_mask]
if exclude_merchants:
    prev_filtered = prev_filtered[~merchant_display_name(prev_filtered).isin(exclude_merchants)]
if exclude_ids:
    prev_filtered = prev_filtered[~prev_filtered["id"].isin(exclude_ids)]


# ── Tabs ───────────────────────────────────────────────────────────────────────

st.title("💳 Bank Enrichment")

tab_overview, tab_time, tab_txns, tab_drill, tab_merchants, tab_subs, tab_settings = st.tabs([
    "Overview", "Over Time", "Transactions", "Category Drill-Down", "Top Merchants", "Subscriptions", "Settings"
])


# ── Tab 1: Overview ────────────────────────────────────────────────────────────

with tab_overview:
    spend_df = filtered[filtered["amount"] < 0]
    income_df = filtered[filtered["amount"] > 0]
    total_spend = abs(spend_df["amount"].sum())
    total_income = income_df["amount"].sum()
    net = total_income - total_spend
    savings_rate = (net / total_income * 100) if total_income > 0 else None

    prev_spend_df = prev_filtered[prev_filtered["amount"] < 0]
    prev_income_df = prev_filtered[prev_filtered["amount"] > 0]
    prev_spend = abs(prev_spend_df["amount"].sum())
    prev_income = prev_income_df["amount"].sum()
    prev_net = prev_income - prev_spend
    prev_savings_rate = (prev_net / prev_income * 100) if prev_income > 0 else None

    spend_d = pct_delta(total_spend, prev_spend)
    income_d = pct_delta(total_income, prev_income)
    net_d = pct_delta(net, prev_net)
    sr_d = (
        round(savings_rate - prev_savings_rate, 1)
        if savings_rate is not None and prev_savings_rate is not None else None
    )

    def _categories_for(role_mask) -> str:
        """Parent category names by default (keeps 'Spend', which spans most
        of the taxonomy, to a short list) -- only drills into individual
        Parent:Subcategory names for a parent whose subcategories are split
        across roles (e.g. Income, where Refunds is Excluded but the rest
        counts as Income), so the mixed part is actually identifiable."""
        subset = filtered.loc[role_mask, ["llm_category", "llm_subcategory"]].dropna(subset=["llm_category"])
        if subset.empty:
            return "No categories set for this role yet"
        labels = []
        for parent, group in subset.groupby("llm_category"):
            subs_in_role = set(group["llm_subcategory"].dropna())
            all_subs_for_parent = set(
                filtered.loc[filtered["llm_category"] == parent, "llm_subcategory"].dropna()
            )
            if subs_in_role and subs_in_role < all_subs_for_parent:
                labels.extend(f"{parent}:{s}" for s in subs_in_role)
            else:
                labels.append(parent)
        labels = sorted(labels)
        if len(labels) > 10:
            labels = labels[:10] + [f"+{len(labels) - 10} more"]
        return ", ".join(labels)

    # Roles like "transfer"/"investment" are bidirectional (e.g. Transfers has
    # both an Inbound and an Outbound subcategory) -- restricting each mask to
    # its expected direction (income=incoming, everything else=outgoing) means
    # summing signed amounts can never let an inflow cancel an outflow.
    income_mask = (filtered["role"] == "income") & (filtered["amount"] > 0)
    spend_mask = (filtered["role"] == "spend") & (filtered["amount"] < 0)
    invest_mask = (filtered["role"] == "investment") & (filtered["amount"] < 0)
    excluded_mask = filtered["role"].isin(["transfer", "excluded"]) & (filtered["amount"] < 0)

    actual_income = filtered.loc[income_mask, "amount"].sum()
    actual_spend = abs(filtered.loc[spend_mask, "amount"].sum())
    invested = abs(filtered.loc[invest_mask, "amount"].sum())
    excluded_total = abs(filtered.loc[excluded_mask, "amount"].sum())

    actual_net = actual_income - actual_spend
    actual_savings_rate = (actual_net / actual_income * 100) if actual_income > 0 else None

    prev_income_mask = (prev_filtered["role"] == "income") & (prev_filtered["amount"] > 0)
    prev_spend_mask = (prev_filtered["role"] == "spend") & (prev_filtered["amount"] < 0)
    prev_invest_mask = (prev_filtered["role"] == "investment") & (prev_filtered["amount"] < 0)
    prev_excluded_mask = prev_filtered["role"].isin(["transfer", "excluded"]) & (prev_filtered["amount"] < 0)

    prev_actual_income = prev_filtered.loc[prev_income_mask, "amount"].sum()
    prev_actual_spend = abs(prev_filtered.loc[prev_spend_mask, "amount"].sum())
    prev_invested = abs(prev_filtered.loc[prev_invest_mask, "amount"].sum())
    prev_excluded_total = abs(prev_filtered.loc[prev_excluded_mask, "amount"].sum())
    prev_actual_net = prev_actual_income - prev_actual_spend
    prev_actual_savings_rate = (prev_actual_net / prev_actual_income * 100) if prev_actual_income > 0 else None

    actual_income_d = pct_delta(actual_income, prev_actual_income)
    actual_spend_d = pct_delta(actual_spend, prev_actual_spend)
    invested_d = pct_delta(invested, prev_invested)
    excluded_d = pct_delta(excluded_total, prev_excluded_total)
    actual_net_d = pct_delta(actual_net, prev_actual_net)
    actual_sr_d = (
        round(actual_savings_rate - prev_actual_savings_rate, 1)
        if actual_savings_rate is not None and prev_actual_savings_rate is not None else None
    )

    def _fmt(v, is_pct=False):
        if v is None:
            return "—"
        return f"{v:.1f}%" if is_pct else f"£{v:.2f}"

    def _info_icon(tooltip: str) -> str:
        return (
            f'<span title="{html.escape(tooltip)}" '
            f'style="cursor:help;opacity:0.6;margin-left:5px;font-size:0.72rem;">ⓘ</span>'
        )

    def _delta_badge(delta_value, suffix, direction: str) -> str:
        """A small colored pill for the vs-prev change -- a card within the
        card. direction picks which way is "good": "normal" = up is good
        (Income, Net, Savings Rate, Invested), "inverse" = up is bad (Spend --
        spending more is a bad thing, unlike everything else going up),
        "neutral" = no good/bad direction at all (Transferred/Excluded is just
        money moving, not a gain or a loss)."""
        if delta_value is None:
            return ""
        text = f"{delta_value:+.1f}{suffix} vs prev"
        if direction == "neutral":
            bg, fg = "rgba(149, 165, 166, 0.25)", "rgba(210, 214, 215, 0.95)"
        else:
            favorable = (delta_value >= 0) if direction == "normal" else (delta_value <= 0)
            bg, fg = (
                ("rgba(46, 204, 113, 0.25)", "rgba(150, 232, 181, 0.95)") if favorable
                else ("rgba(231, 76, 60, 0.25)", "rgba(243, 158, 148, 0.95)")
            )
        return (
            f'<div style="margin-top:6px;"><span style="display:inline-block;background:{bg};'
            f'color:{fg};border-radius:6px;padding:2px 8px;font-size:0.7rem;font-weight:600;">'
            f'{html.escape(text)}</span></div>'
        )

    def _card(label, value, color, tooltip=None, delta_html=""):
        """A colored div standing in for a metric card -- st.metric can't take
        a background color, and st.dataframe can't do a real per-cell hover,
        so this is plain HTML: a visible ⓘ icon with its own native `title`
        tooltip (hover, no JS) rather than a silent hover-anywhere-on-the-card,
        plus a CSS background tint matching the row's color."""
        icon = _info_icon(tooltip) if tooltip else ""
        return (
            f'<div style="background:{color};border-radius:10px;'
            f'padding:12px 14px;min-height:82px;margin-bottom:12px;">'
            f'<div style="font-size:0.78rem;color:rgba(255,255,255,0.65);font-weight:500;">{html.escape(label)}{icon}</div>'
            f'<div style="font-size:1.45rem;font-weight:700;margin-top:4px;">{html.escape(value)}</div>'
            f'{delta_html}</div>'
        )

    row_colors = {
        "Income": "rgba(46, 204, 113, 0.14)",
        "Spend": "rgba(231, 76, 60, 0.14)",
        "Net": "rgba(52, 152, 219, 0.16)",
        "Savings Rate": "rgba(52, 152, 219, 0.16)",
        "Invested": "rgba(155, 89, 182, 0.14)",
        "Transferred / Excluded": "rgba(149, 165, 166, 0.14)",
    }
    delta_direction = {
        "Income": "normal", "Spend": "inverse", "Net": "normal",
        "Savings Rate": "normal", "Invested": "normal", "Transferred / Excluded": "neutral",
    }
    standard_tooltips = {
        "Income": "Every transaction with a positive amount, regardless of category.",
        "Spend": "Every transaction with a negative amount, regardless of category.",
        "Net": "Standard Income minus Standard Spend.",
        "Savings Rate": "Standard Net as a percentage of Standard Income.",
        "Invested": "Not tracked separately here — investment transactions are just counted within Standard Income/Spend by amount sign.",
        "Transferred / Excluded": "Not tracked separately here — transfers and refunds are just counted within Standard Income/Spend by amount sign.",
    }
    actual_static_tooltips = {
        "Net": "Actual Income minus Actual Spend.",
        "Savings Rate": "Actual Net as a percentage of Actual Income.",
    }
    # (label, standard value, standard delta, actual value, actual delta,
    #  categories-mask for the Actual card's tooltip, is_pct, color)
    metrics_row = [
        ("Income", total_income, income_d, actual_income, actual_income_d, income_mask, False),
        ("Spend", total_spend, spend_d, actual_spend, actual_spend_d, spend_mask, False),
        ("Net", net, net_d, actual_net, actual_net_d, None, False),
        ("Savings Rate", savings_rate, sr_d, actual_savings_rate, actual_sr_d, None, True),
        ("Invested", None, None, invested, invested_d, invest_mask, False),
        ("Transferred / Excluded", None, None, excluded_total, excluded_d, excluded_mask, False),
    ]

    st.caption(f"{len(filtered)} transactions · {int(filtered['llm_category'].isna().sum())} unclassified.")

    st.markdown(
        "**Standard**" + _info_icon(
            "Every £ is counted by amount sign alone — money in is Income, "
            "money out is Spend, regardless of category or role."
        ),
        unsafe_allow_html=True,
    )
    std_cols = st.columns(6)
    for col, (label, std_val, std_d, _, _, _, is_pct) in zip(std_cols, metrics_row):
        badge = _delta_badge(std_d, "pp" if is_pct else "%", delta_direction[label])
        with col:
            st.markdown(
                _card(label, _fmt(std_val, is_pct), row_colors[label], tooltip=standard_tooltips[label], delta_html=badge),
                unsafe_allow_html=True,
            )

    st.markdown(
        "**Actual**" + _info_icon(
            "Each category has a role (set in the Settings tab) that decides what "
            "it counts as — e.g. refunds and transfers don't count as Income here."
        ),
        unsafe_allow_html=True,
    )
    act_cols = st.columns(6)
    for col, (label, _, _, act_val, act_d, mask, is_pct) in zip(act_cols, metrics_row):
        badge = _delta_badge(act_d, "pp" if is_pct else "%", delta_direction[label])
        tooltip = _categories_for(mask) if mask is not None else actual_static_tooltips[label]
        with col:
            st.markdown(
                _card(label, _fmt(act_val, is_pct), row_colors[label], tooltip=tooltip, delta_html=badge),
                unsafe_allow_html=True,
            )

    st.divider()

    # Total actual income for the filtered period — used to compute "% of Income"
    # on outgoing breakdowns so you can see e.g. rent is 28% of income.
    _income_total = role_breakdown(filtered, ROLE_METRICS["Income"], "llm_category", sign="positive")["Amount"].sum()

    def _breakdown_chart(col, key_prefix: str, title: str, metric: str, color_scale: str, sign: str):
        with col:
            group_by = st.radio(
                "Group by", ["Category", "Subcategory"],
                key=f"{key_prefix}_group_by", horizontal=True,
            )
            group_col = "llm_subcategory" if group_by == "Subcategory" else "llm_category"
            breakdown = role_breakdown(filtered, ROLE_METRICS[metric], group_col, sign=sign).sort_values("Amount", ascending=True)
            if not breakdown.empty:
                fig = px.bar(breakdown, x="Amount", y=group_by, orientation="h",
                             title=f"{title} by {group_by}", labels={"Amount": f"{title} (£)"},
                             color="Amount", color_continuous_scale=color_scale)
                fig.update_layout(showlegend=False, coloraxis_showscale=False,
                                  height=max(300, len(breakdown) * 45),
                                  margin=dict(l=0, r=0, t=40, b=0))
                st.plotly_chart(fig, width="stretch", key=f"{key_prefix}_chart")
                total = breakdown["Amount"].sum()
                if total > 0:
                    pct_table = breakdown.sort_values("Amount", ascending=False).copy()
                    pct_table["% of total"] = (pct_table["Amount"] / total * 100).round(1).astype(str) + "%"
                    if sign == "negative" and _income_total > 0:
                        pct_table["% of income"] = (pct_table["Amount"] / _income_total * 100).round(1).astype(str) + "%"
                    pct_table["Amount"] = pct_table["Amount"].map("£{:.2f}".format)
                    st.dataframe(pct_table, hide_index=True, width="stretch")
            else:
                st.info(f"No {title.lower()} in range.")

    # Two charts, split by direction of money flow. Spend/Invested/Transferred
    # are bidirectional roles under the hood (e.g. Transfers has both an
    # Inbound and an Outbound subcategory) -- Outgoings restricts to the
    # negative (money-out) side of each, Incomings to the positive side, so
    # neither chart can blend an inflow and an outflow into one misleading bar.
    outgoing_metrics = [m for m in ROLE_METRICS if m != "Income"]
    incoming_metrics = [m for m in ROLE_METRICS if m != "Spend"]
    chart_col1, chart_col2 = st.columns(2)
    with chart_col1:
        outgoing_metric = st.selectbox("Outgoings metric", outgoing_metrics, key="overview_outgoing_metric")
    with chart_col2:
        incoming_metric = st.selectbox("Incomings metric", incoming_metrics, key="overview_incoming_metric")
    _breakdown_chart(chart_col1, "overview_outgoing", outgoing_metric, outgoing_metric, "Reds", sign="negative")
    _breakdown_chart(chart_col2, "overview_incoming", incoming_metric, incoming_metric, "Greens", sign="positive")

    st.divider()
    with st.expander("Monthly breakdown", expanded=False):
        if not filtered.empty and "created_at" in filtered.columns:
            _mdf = filtered.copy()
            _mdf["_month"] = pd.to_datetime(_mdf["created_at"]).dt.to_period("M").astype(str)
            _spend_roles = ROLE_METRICS.get("Spend", [])
            _income_roles = ROLE_METRICS.get("Income", [])
            _invest_roles = ROLE_METRICS.get("Invested", [])
            _mdf["_income"] = _mdf.apply(
                lambda r: r["amount"] if r["role"] in _income_roles and r["amount"] > 0 else 0, axis=1)
            _mdf["_spend"] = _mdf.apply(
                lambda r: abs(r["amount"]) if r["role"] in _spend_roles and r["amount"] < 0 else 0, axis=1)
            _mdf["_invested"] = _mdf.apply(
                lambda r: abs(r["amount"]) if r["role"] in _invest_roles and r["amount"] < 0 else 0, axis=1)
            _monthly = (
                _mdf.groupby("_month")
                .agg(_income=("_income", "sum"), _spend=("_spend", "sum"), _invested=("_invested", "sum"))
                .reset_index()
                .sort_values("_month", ascending=False)
            )
            _monthly["Net"] = (_monthly["_income"] - _monthly["_spend"]).round(2)
            _monthly["Savings Rate"] = _monthly.apply(
                lambda r: f"{(r['Net'] / r['_income'] * 100):.1f}%" if r["_income"] > 0 else "—", axis=1)
            _monthly = _monthly.rename(columns={
                "_month": "Month", "_income": "Income (£)",
                "_spend": "Spend (£)", "_invested": "Invested (£)",
            })
            _monthly["Income (£)"] = _monthly["Income (£)"].map("£{:.2f}".format)
            _monthly["Spend (£)"] = _monthly["Spend (£)"].map("£{:.2f}".format)
            _monthly["Invested (£)"] = _monthly["Invested (£)"].map("£{:.2f}".format)
            _monthly["Net"] = _monthly["Net"].map(lambda v: f"£{v:.2f}")
            st.dataframe(
                _monthly[["Month", "Income (£)", "Spend (£)", "Invested (£)", "Net", "Savings Rate"]],
                hide_index=True, width="stretch",
            )
        else:
            st.info("No data in current filter range.")


# ── Tab 2: Over Time ────────────────────────────────────────────────────────────

with tab_time:
    st.caption(
        "All figures below are Actual — categorized by the roles set in the Settings "
        "tab (e.g. refunds/transfers excluded from Income). See Overview for the "
        "Standard vs Actual distinction."
    )

    def _add_total_and_trend(fig, monthly_df: pd.DataFrame):
        """Overlay a Total line and its 3-month rolling average -- individual
        months are noisy (one big one-off purchase can skew a month), and
        since these are separate per-category/subcategory lines rather than a
        stack, there's no other line on the chart showing the combined total
        to read a real trend off of."""
        totals = monthly_df.groupby("Month")["Amount"].sum().reset_index().sort_values("Month")
        totals["Rolling Avg"] = totals["Amount"].rolling(3, min_periods=1).mean()
        fig.add_trace(go.Scatter(
            x=totals["Month"], y=totals["Amount"], mode="lines", name="Total",
            line=dict(color="#2c3e50", width=3),
        ))
        fig.add_trace(go.Scatter(
            x=totals["Month"], y=totals["Rolling Avg"], mode="lines", name="3-mo Avg",
            line=dict(color="#f39c12", width=3, dash="dash"),
        ))

    def _time_chart_pair(key_prefix: str, metric: str, sign: str):
        """Monthly category + subcategory trend lines for one metric, restricted
        to one direction of money flow -- roles like Invested/Transferred are
        bidirectional under the hood (e.g. Transfers has an Inbound and an
        Outbound subcategory), so without this restriction a month with both
        would net together and understate the true amount, the same
        sum-then-abs cancellation bug fixed on the Overview tab. Lines (not
        stacked bars) since a per-category trend is what's being compared here,
        and a stack makes any one category's own trajectory hard to isolate."""
        metric_time = filtered[filtered["role"].isin(ROLE_METRICS[metric])]
        metric_time = metric_time[metric_time["amount"] < 0] if sign == "negative" else metric_time[metric_time["amount"] > 0]
        if metric_time.empty:
            st.info(f"No {metric.lower()} in range.")
            return
        col1, col2 = st.columns(2)
        with col1:
            st.caption("All categories")
            monthly = role_breakdown(metric_time, ROLE_METRICS[metric], "llm_category", by_month=True, sign=sign)
            category_order = monthly.groupby("Category")["Amount"].sum().sort_values(ascending=False).index.tolist()
            fig = px.line(monthly, x="Month", y="Amount", color="Category", markers=True,
                          title=f"Monthly {metric} by Category",
                          labels={"Amount": f"{metric} (£)"},
                          category_orders={"Category": category_order})
            _add_total_and_trend(fig, monthly)
            # "Month" is a monthly bin ("2026-07"), not a specific day -- without
            # forcing a category axis, Plotly auto-detects it as a date and
            # snaps every point to the 1st, implying a precision the data
            # doesn't have (it's a whole month's total, not a day-1 reading).
            fig.update_layout(xaxis_tickangle=-45, margin=dict(t=40), height=380,
                              xaxis_type="category", xaxis_categoryorder="category ascending")
            st.plotly_chart(fig, width="stretch", key=f"{key_prefix}_category_chart")

        with col2:
            available_cats = sorted(metric_time[metric_time["llm_category"].notna()]["llm_category"].unique())
            if available_cats:
                selected_cat = st.selectbox("View subcategories for", available_cats, key=f"{key_prefix}_sub_cat")
                sub_data = metric_time[metric_time["llm_category"] == selected_cat]
                monthly_sub = role_breakdown(sub_data, ROLE_METRICS[metric], "llm_subcategory", by_month=True, sign=sign)
                if not monthly_sub.empty:
                    sub_order = monthly_sub.groupby("Subcategory")["Amount"].sum().sort_values(ascending=False).index.tolist()
                    fig_sub = px.line(monthly_sub, x="Month", y="Amount", color="Subcategory", markers=True,
                                      title=f"{selected_cat} — Monthly {metric} by Subcategory",
                                      labels={"Amount": f"{metric} (£)"},
                                      category_orders={"Subcategory": sub_order})
                    _add_total_and_trend(fig_sub, monthly_sub)
                    fig_sub.update_layout(xaxis_tickangle=-45, margin=dict(t=40), height=380,
                                          xaxis_type="category", xaxis_categoryorder="category ascending")
                    st.plotly_chart(fig_sub, width="stretch", key=f"{key_prefix}_subcategory_chart")
                else:
                    st.info("No subcategory data for this category in the selected range.")
            else:
                st.info(f"No classified {metric.lower()} in range.")

    # Split by direction, mirroring Overview's Outgoings/Incomings cards --
    # Spend/Invested/Transferred-Excluded are "money out", Income/Invested/
    # Transferred-Excluded are "money in".
    time_outgoing_metrics = [m for m in ROLE_METRICS if m != "Income"]
    time_incoming_metrics = [m for m in ROLE_METRICS if m != "Spend"]

    if not filtered.empty:
        monthly_spend = filtered[(filtered["role"] == "spend") & (filtered["amount"] < 0)].groupby("month")["amount"].sum().abs().rename("Spend")
        monthly_income = filtered[(filtered["role"] == "income") & (filtered["amount"] > 0)].groupby("month")["amount"].sum().rename("Income")
        monthly_invested = filtered[(filtered["role"] == "investment") & (filtered["amount"] < 0)].groupby("month")["amount"].sum().abs().rename("Invested")
        monthly_transferred = filtered[filtered["role"].isin(["transfer", "excluded"]) & (filtered["amount"] < 0)].groupby("month")["amount"].sum().abs().rename("Transferred")
        monthly_totals_raw = (
            pd.concat([monthly_spend, monthly_income, monthly_invested, monthly_transferred], axis=1)
            .fillna(0).reset_index()
            .rename(columns={"month": "Month"})
            .sort_values("Month")
        )
        # amount columns come back as Decimal via DuckDB -- pct_change() divides
        # raw values with no zero-guard, and Decimal division by zero raises
        # (unlike float, which would just produce inf/nan), so cast to float
        # before any of the derived-ratio columns below.
        for _col in ["Spend", "Income", "Invested", "Transferred"]:
            monthly_totals_raw[_col] = monthly_totals_raw[_col].astype(float)
        monthly_totals_raw["Net"] = monthly_totals_raw["Income"] - monthly_totals_raw["Spend"]
        monthly_totals_raw["Savings Rate"] = (
            monthly_totals_raw["Net"] / monthly_totals_raw["Income"].replace(0, float("nan")) * 100
        ).round(1)
        monthly_totals_raw["Investment Rate"] = (
            monthly_totals_raw["Invested"] / monthly_totals_raw["Income"].replace(0, float("nan")) * 100
        ).round(1)
        # A previous month of exactly £0 makes pct_change() produce +inf rather
        # than a crash now that the Decimal cast above is in place -- but
        # "infinite% increase" is a worse read than just leaving it blank.
        monthly_totals_raw["Spend MoM"] = (
            monthly_totals_raw["Spend"].pct_change().mul(100).replace([float("inf"), float("-inf")], float("nan")).round(1)
        )
        monthly_totals_raw["Income MoM"] = (
            monthly_totals_raw["Income"].pct_change().mul(100).replace([float("inf"), float("-inf")], float("nan")).round(1)
        )

        st.subheader("Averages")
        st.caption(f"Mean across the {len(monthly_totals_raw)} month(s) in range.")
        avg_values = {
            "Income": (monthly_totals_raw["Income"].mean(), False),
            "Spend": (monthly_totals_raw["Spend"].mean(), False),
            "Net": (monthly_totals_raw["Net"].mean(), False),
            "Savings Rate": (monthly_totals_raw["Savings Rate"].mean(), True),
            "Invested": (monthly_totals_raw["Invested"].mean(), False),
            "Transferred / Excluded": (monthly_totals_raw["Transferred"].mean(), False),
        }
        avg_cols = st.columns(6)
        for col, (label, (val, is_pct)) in zip(avg_cols, avg_values.items()):
            with col:
                st.markdown(
                    _card(f"Avg {label}", _fmt(val, is_pct), row_colors[label],
                          tooltip=f"Average monthly {label.lower()} across the selected range."),
                    unsafe_allow_html=True,
                )

        st.divider()
        st.subheader("Savings & Investing Over Time")

        sr_chart = monthly_totals_raw[monthly_totals_raw["Income"] > 0].copy()
        if len(sr_chart) > 1:
            sr_chart["Actual Savings"] = sr_chart["Income"] - sr_chart["Spend"]
            fig_sr = px.line(sr_chart, x="Month", y="Savings Rate",
                             labels={"Savings Rate": "Rate (%)"},
                             markers=True)
            fig_sr.add_hline(y=0, line_dash="dash", line_color="red", opacity=0.4,
                             annotation_text="break even", annotation_position="bottom right")
            fig_sr.update_traces(name="Savings Rate (%)", line_color="#2ecc71", marker_color="#2ecc71")
            fig_sr.add_trace(go.Scatter(
                x=sr_chart["Month"], y=sr_chart["Investment Rate"], name="Investment Rate (%)",
                mode="lines+markers", line=dict(color="#9b59b6"),
            ))
            fig_sr.add_trace(go.Scatter(
                x=sr_chart["Month"], y=sr_chart["Actual Savings"], name="Actual Savings (£)",
                mode="lines+markers", line=dict(color="#3498db"), yaxis="y2",
            ))
            fig_sr.update_layout(
                xaxis_tickangle=-45, margin=dict(t=20, b=0), yaxis_ticksuffix="%",
                yaxis2=dict(title="Actual Savings (£)", overlaying="y", side="right"),
                legend=dict(orientation="h", yanchor="bottom", y=1.02),
                xaxis_type="category", xaxis_categoryorder="category ascending",
            )
            st.plotly_chart(fig_sr, width="stretch", key="savings_rate_chart")
        else:
            st.info("Need at least two months with income to chart savings.")

        invested_chart = monthly_totals_raw[monthly_totals_raw["Invested"] > 0]
        if len(invested_chart) > 1:
            fig_inv = px.line(invested_chart, x="Month", y="Invested",
                              labels={"Invested": "Invested (£)"}, markers=True)
            fig_inv.update_traces(line_color="#9b59b6", marker_color="#9b59b6")
            fig_inv.update_layout(xaxis_tickangle=-45, margin=dict(t=20, b=0),
                                  xaxis_type="category", xaxis_categoryorder="category ascending")
            st.plotly_chart(fig_inv, width="stretch", key="invested_over_time_chart")
    else:
        st.info("No transactions in the selected range.")

    st.divider()
    st.subheader("Spending Over Time")
    time_outgoing_metric = st.selectbox("Metric", time_outgoing_metrics, key="time_outgoing_metric")
    _time_chart_pair("time_outgoing", time_outgoing_metric, sign="negative")

    st.divider()
    st.subheader("Income Over Time")
    time_incoming_metric = st.selectbox("Metric", time_incoming_metrics, key="time_incoming_metric")
    _time_chart_pair("time_incoming", time_incoming_metric, sign="positive")

    if not filtered.empty:
        st.divider()
        with st.expander("Monthly Totals (Actual)"):
            monthly_totals = monthly_totals_raw.sort_values("Month", ascending=False)
            st.dataframe(
                monthly_totals[[
                    "Month", "Income", "Spend", "Net", "Savings Rate",
                    "Invested", "Investment Rate", "Transferred", "Income MoM", "Spend MoM",
                ]],
                width="stretch", hide_index=True,
                column_config={
                    "Month": st.column_config.Column(
                        "Month", help="Calendar month, based on the selected date range."),
                    "Income": st.column_config.NumberColumn(
                        "Income", format="£%.2f",
                        help="Actual Income — the incoming (positive) side of every Income-role category."),
                    "Spend": st.column_config.NumberColumn(
                        "Spend", format="£%.2f",
                        help="Actual Spend — the outgoing (negative) side of every Spend-role category."),
                    "Net": st.column_config.NumberColumn(
                        "Net", format="£%.2f", help="Income minus Spend for the month."),
                    "Savings Rate": st.column_config.NumberColumn(
                        "Savings Rate", format="%.1f%%",
                        help="Net as a percentage of Income. Blank when there was no income that month."),
                    "Invested": st.column_config.NumberColumn(
                        "Invested", format="£%.2f",
                        help="Money moved into investment categories (outgoing side only, so a "
                             "withdrawal can't cancel out a contribution)."),
                    "Investment Rate": st.column_config.NumberColumn(
                        "Investment Rate", format="%.1f%%",
                        help="Invested as a percentage of Income. Blank when there was no income that month."),
                    "Transferred": st.column_config.NumberColumn(
                        "Transferred", format="£%.2f",
                        help="Money moved to transfer or excluded categories (outgoing side only)."),
                    "Income MoM": st.column_config.NumberColumn(
                        "Income MoM", format="%+.1f%%", help="Change in Income vs the previous month."),
                    "Spend MoM": st.column_config.NumberColumn(
                        "Spend MoM", format="%+.1f%%", help="Change in Spend vs the previous month."),
                },
            )


# ── Tab 3: Transactions ────────────────────────────────────────────────────────

with tab_txns:
    st.subheader(f"{len(filtered)} transactions")

    if not filtered.empty:
        st.caption("Just for fun — not part of your financial totals elsewhere in the app.")
        fun_color = "rgba(127, 140, 141, 0.16)"
        merchant_counts = filtered["merchant_name"].dropna().value_counts()
        biggest_idx = filtered["amount"].abs().idxmax()
        biggest_row = filtered.loc[biggest_idx]
        day_counts = filtered["created_at"].dt.day_name().value_counts()

        fun_cards = [
            ("Transactions", str(len(filtered)),
             "Total transactions in the selected range and filters."),
            ("Unique Merchants", str(filtered["merchant_name"].nunique()),
             "Distinct merchant names seen in the selected range."),
            ("Most Frequent Merchant",
             merchant_counts.index[0] if not merchant_counts.empty else "—",
             f"Appears {merchant_counts.iloc[0]} times." if not merchant_counts.empty else "No merchant data."),
            ("Biggest Transaction", f"£{abs(biggest_row['amount']):.2f}",
             f"{biggest_row['merchant_name'] or biggest_row['description'] or 'Unknown'} "
             f"on {biggest_row['created_at'].date()}."),
            ("Avg Transaction Size", f"£{filtered['amount'].abs().mean():.2f}",
             "Mean absolute amount across every transaction in range."),
            ("Busiest Day", day_counts.index[0] if not day_counts.empty else "—",
             f"{day_counts.iloc[0]} transactions fall on a {day_counts.index[0]}." if not day_counts.empty else ""),
        ]
        fun_cols = st.columns(6)
        for col, (label, value, tooltip) in zip(fun_cols, fun_cards):
            with col:
                st.markdown(_card(label, value, fun_color, tooltip=tooltip), unsafe_allow_html=True)

        st.divider()

    st.caption(
        "Pick Category/Subcategory from your existing taxonomy to fix a misclassification. "
        "To add a brand-new category or subcategory, use the Settings tab first."
    )

    edit_df = filtered[["id", "created_at", "amount", "merchant_name", "description",
                         "user_context", "llm_category", "llm_subcategory"]].copy()
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
            "id": st.column_config.Column("ID", disabled=True, width="small"),
            "Date": st.column_config.DateColumn(
                "Date", format="YYYY-MM-DD", width="small",
                help="The time-of-day from the original transaction is kept as-is -- only the date changes."),
            "Amount": st.column_config.NumberColumn("Amount", disabled=True, format="£%.2f", width="small"),
            "Merchant": st.column_config.Column(disabled=True),
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


# ── Tab 4: Category Drill-Down ─────────────────────────────────────────────────

with tab_drill:
    classified = filtered[filtered["llm_category"].notna()]

    if classified.empty:
        st.info("No classified transactions in the selected range.")
    else:
        st.caption(
            "All figures are Actual — categorized by the roles set in the Settings "
            "tab. See Overview for the Standard vs Actual distinction."
        )
        selected_cat = st.selectbox(
            "Select parent category",
            sorted(classified["llm_category"].unique()),
            key="drill_selected_category",
        )
        cat_df = classified[classified["llm_category"] == selected_cat]

        # Roles like "transfer"/"investment" are bidirectional (e.g. Transfers
        # has both an Inbound and an Outbound subcategory) -- restricting each
        # to its expected direction (income=incoming, everything else=outgoing)
        # means summing signed amounts can never let an inflow cancel an
        # outflow, matching how Overview computes these same totals.
        role_sign = {"income": "positive", "spend": "negative", "investment": "negative",
                     "transfer": "negative", "excluded": "negative"}
        income_cat = cat_df.loc[(cat_df["role"] == "income") & (cat_df["amount"] > 0), "amount"].sum()
        spend_cat = abs(cat_df.loc[(cat_df["role"] == "spend") & (cat_df["amount"] < 0), "amount"].sum())
        invested_cat = abs(cat_df.loc[(cat_df["role"] == "investment") & (cat_df["amount"] < 0), "amount"].sum())
        excluded_cat = abs(cat_df.loc[cat_df["role"].isin(["transfer", "excluded"]) & (cat_df["amount"] < 0), "amount"].sum())
        net_cat = income_cat - spend_cat

        # Invested/Transferred-Excluded only show their outgoing side above --
        # but since both can also carry an incoming side under the same role
        # (e.g. an investment withdrawal, or an inbound transfer/refund), a
        # category with ONLY incoming activity would otherwise show £0.00 here
        # while the subcategory chart below it shows a real, nonzero amount.
        # Surface the incoming side as its own card whenever it's nonzero so
        # the cards never disagree with what the charts are showing.
        invested_in_cat = cat_df.loc[(cat_df["role"] == "investment") & (cat_df["amount"] > 0), "amount"].sum()
        excluded_in_cat = cat_df.loc[cat_df["role"].isin(["transfer", "excluded"]) & (cat_df["amount"] > 0), "amount"].sum()

        stat_color = "rgba(127, 140, 141, 0.16)"
        top_cols = st.columns(3)
        with top_cols[0]:
            st.markdown(_card("Transactions", str(len(cat_df)), stat_color,
                               tooltip="Transactions in this category, in the selected range."),
                        unsafe_allow_html=True)
        with top_cols[1]:
            st.markdown(_card("Subcategories", str(int(cat_df["llm_subcategory"].nunique())), stat_color,
                               tooltip="Distinct subcategories used within this category."),
                        unsafe_allow_html=True)
        with top_cols[2]:
            st.markdown(_card("Net", f"£{net_cat:.2f}", row_colors["Net"],
                               tooltip="Actual Income minus Actual Spend for this category."),
                        unsafe_allow_html=True)

        # (display label, color key into row_colors, value, tooltip)
        invested_label = "Invested (Outgoing)" if invested_in_cat > 0 else "Invested"
        excluded_label = "Transferred / Excluded (Outgoing)" if excluded_in_cat > 0 else "Transferred / Excluded"
        role_cards = [
            ("Income", "Income", income_cat,
             "Every transaction in this category whose role is Income (incoming side only)."),
            ("Spend", "Spend", spend_cat,
             "Every transaction in this category whose role is Spend (outgoing side only)."),
            (invested_label, "Invested", invested_cat,
             "Money moved into investment categories here (outgoing side only, so a withdrawal can't cancel a contribution)."),
            (excluded_label, "Transferred / Excluded", excluded_cat,
             "Money moved to transfer or excluded categories here (outgoing side only)."),
        ]
        if invested_in_cat > 0:
            role_cards.append(("Invested (Incoming)", "Invested", invested_in_cat,
                "Money that came back out of investment categories here (e.g. a withdrawal or sale) -- "
                "kept separate so it can't cancel out the outgoing figure."))
        if excluded_in_cat > 0:
            role_cards.append(("Transferred / Excluded (Incoming)", "Transferred / Excluded", excluded_in_cat,
                "Money that came in through transfer or excluded categories here (e.g. a refund or an inbound "
                "transfer) -- kept separate so it can't cancel out the outgoing figure."))

        role_cols = st.columns(len(role_cards))
        for col, (label, color_key, value, tooltip) in zip(role_cols, role_cards):
            with col:
                st.markdown(_card(label, f"£{value:.2f}", row_colors[color_key], tooltip=tooltip),
                            unsafe_allow_html=True)

        st.divider()

        # Income/Spend are one-directional by definition, but Invested/
        # Transferred/Excluded can genuinely have both an outgoing and an
        # incoming subcategory under the same role (e.g. Transfers has both
        # an Inbound and an Outbound Transfer subcategory) -- checking only
        # one fixed direction per role would make that other side vanish from
        # this view entirely. Check both directions and chart whichever
        # actually has data, labeling which side it is whenever a role could
        # go either way.
        role_labels = {"income": "Income", "spend": "Spend", "investment": "Invested",
                       "transfer": "Transferred", "excluded": "Excluded"}
        role_colors = {"income": "Greens", "spend": "Reds", "investment": "Purples",
                       "transfer": "Blues", "excluded": "Greys"}
        bidirectional = {"income": False, "spend": False, "investment": True,
                          "transfer": True, "excluded": True}
        chart_specs = []
        for role, label in role_labels.items():
            signs = ["negative", "positive"] if bidirectional[role] else [role_sign[role]]
            for sign in signs:
                mask = (cat_df["role"] == role) & (
                    (cat_df["amount"] > 0) if sign == "positive" else (cat_df["amount"] < 0)
                )
                if abs(cat_df.loc[mask, "amount"].sum()) > 0:
                    suffix = "" if len(signs) == 1 else (" (Incoming)" if sign == "positive" else " (Outgoing)")
                    chart_specs.append((role, sign, f"{label}{suffix}"))

        if chart_specs:
            chart_cols = st.columns(len(chart_specs))
            for col, (role, sign, chart_label) in zip(chart_cols, chart_specs):
                with col:
                    breakdown = role_breakdown(
                        cat_df, (role,), "llm_subcategory", sign=sign
                    ).sort_values("Amount", ascending=True)
                    fig = px.bar(breakdown, x="Amount", y="Subcategory", orientation="h",
                                 title=f"{chart_label} by Subcategory",
                                 labels={"Amount": f"{chart_label} (£)"},
                                 color="Amount", color_continuous_scale=role_colors[role])
                    fig.update_layout(showlegend=False, coloraxis_showscale=False,
                                      height=max(250, len(breakdown) * 45),
                                      margin=dict(l=0, r=0, t=40, b=0))
                    st.plotly_chart(fig, width="stretch", key=f"drill_{selected_cat}_{role}_{sign}_chart")

        # "Unclassified" (no subcategory) is included here, same as the charts
        # above via role_breakdown -- otherwise this table's per-role totals
        # wouldn't add up to what the charts show for the same category.
        sub_summary = (
            cat_df.assign(llm_subcategory=cat_df["llm_subcategory"].fillna("Unclassified"))
            .groupby("llm_subcategory")
            .agg(Transactions=("id", "count"))
            .reset_index()
            .rename(columns={"llm_subcategory": "Subcategory"})
        )
        if not sub_summary.empty and chart_specs:
            for role, sign, chart_label in chart_specs:
                bd = role_breakdown(cat_df, (role,), "llm_subcategory", sign=sign)
                role_sums = bd.set_index("Subcategory")["Amount"]
                sub_summary[chart_label] = sub_summary["Subcategory"].map(role_sums).fillna(0)

            # A subcategory's own rows are almost always one direction, so a
            # plain per-row average/latest-date/share-of-total is a meaningful
            # "how big, how recent, how much of this category" signal without
            # needing another sign split like the £ columns above.
            grouped = cat_df.assign(
                llm_subcategory=cat_df["llm_subcategory"].fillna("Unclassified")
            ).groupby("llm_subcategory")
            avg_amount = grouped["amount"].apply(lambda s: s.astype(float).abs().mean())
            last_txn = grouped["created_at"].max()
            sub_summary["Avg Transaction"] = sub_summary["Subcategory"].map(avg_amount)
            sub_summary["Last Transaction"] = sub_summary["Subcategory"].map(last_txn)

            sort_col = chart_specs[0][2]
            total_for_share = sub_summary[sort_col].sum()
            share_label = f"% of {sort_col}"
            sub_summary[share_label] = (
                (sub_summary[sort_col] / total_for_share * 100).round(1) if total_for_share > 0 else 0.0
            )

            sub_summary = sub_summary.sort_values(sort_col, ascending=False).reset_index(drop=True)
            for _, _, chart_label in chart_specs:
                sub_summary[chart_label] = sub_summary[chart_label].map(lambda x: f"£{x:.2f}" if x > 0 else "—")
            sub_summary["Avg Transaction"] = sub_summary["Avg Transaction"].map("£{:.2f}".format)
            sub_summary["Last Transaction"] = sub_summary["Last Transaction"].dt.strftime("%Y-%m-%d")
            sub_summary[share_label] = sub_summary[share_label].map(lambda x: f"{x:.1f}%")

            st.subheader("Subcategory Summary")
            st.dataframe(sub_summary, width="stretch", hide_index=True)

        st.subheader("Transactions")
        display_cat = cat_df[["created_at", "amount", "merchant_name", "description",
                               "user_context", "llm_subcategory"]].copy()
        display_cat.columns = ["Date", "Amount", "Merchant", "Description", "Context", "Subcategory"]
        st.dataframe(
            display_cat, width="stretch", hide_index=True,
            column_config={
                "Date": st.column_config.DateColumn("Date", format="YYYY-MM-DD"),
                "Amount": st.column_config.NumberColumn("Amount", format="£%.2f"),
            },
        )


# ── Tab 5: Top Merchants ──────────────────────────────────────────────────────

with tab_merchants:
    st.caption(
        "Actual Spend only — investments, transfers, and refunds aren't counted as merchant spend."
    )
    stat_color = "rgba(127, 140, 141, 0.16)"
    # Same direction-restriction used everywhere else: a "spend"-role category
    # can still have a stray positive-amount row (e.g. an unclassified refund),
    # and without this, total_spend/avg below would sum signed amounts before
    # taking abs(), letting it quietly cancel out real spend.
    merch_spend = filtered[(filtered["role"] == "spend") & (filtered["amount"] < 0)].copy()
    # merchant_name is often blank for direct debits/bank transfers, which
    # report counterparty_name instead -- falling back to that (and then to
    # the raw description) keeps those from all getting lumped into "Unknown".
    merch_spend["merchant_display"] = (
        merchant_display_name(merch_spend).fillna(merch_spend["description"]).fillna("Unknown")
    )

    if merch_spend.empty:
        st.info("No spend transactions in the selected range.")
    else:
        by_merchant = (
            merch_spend.groupby("merchant_display")
            .agg(
                total_spend=("amount", lambda x: abs(x.sum())),
                transactions=("id", "count"),
                avg_transaction=("amount", lambda x: abs(x.mean())),
                last_seen=("created_at", "max"),
            )
            .reset_index()
            .rename(columns={"merchant_display": "Merchant"})
            .sort_values(["total_spend", "Merchant"], ascending=[False, True])
            .reset_index(drop=True)
        )

        merch_ctrl1, merch_ctrl2 = st.columns([3, 1])
        with merch_ctrl1:
            merchant_search = st.text_input(
                "Search merchants", placeholder="merchant name...", key="merchant_search"
            )
        with merch_ctrl2:
            top_n_choice = st.selectbox("Top N", [10, 20, 50, "All"], index=1, key="merchant_top_n")

        if merchant_search:
            by_merchant = by_merchant[
                by_merchant["Merchant"].str.contains(merchant_search, case=False, na=False, regex=False)
            ]

        if by_merchant.empty:
            st.info("No merchants match that search.")
        else:
            rank_by = st.radio("Rank merchants by", ["Total Spend", "Frequency"],
                                horizontal=True, key="merchant_rank_by")
            rank_col = "total_spend" if rank_by == "Total Spend" else "transactions"
            by_merchant = by_merchant.sort_values(
                [rank_col, "Merchant"], ascending=[False, True]
            ).reset_index(drop=True)

            total_spend_all = by_merchant["total_spend"].sum()
            top_row = by_merchant.iloc[0]
            stat_cols = st.columns(4)
            with stat_cols[0]:
                st.markdown(_card("Merchants", str(len(by_merchant)), stat_color,
                                   tooltip="Distinct merchants matching the current search, in range."),
                            unsafe_allow_html=True)
            with stat_cols[1]:
                st.markdown(_card("Total Spend", f"£{total_spend_all:.2f}", row_colors["Spend"],
                                   tooltip="Actual Spend summed across all matching merchants."),
                            unsafe_allow_html=True)
            with stat_cols[2]:
                st.markdown(_card("Avg per Merchant", f"£{(total_spend_all / len(by_merchant)):.2f}", stat_color,
                                   tooltip="Total Spend divided by the number of matching merchants."),
                            unsafe_allow_html=True)
            with stat_cols[3]:
                st.markdown(_card("Top Merchant", top_row["Merchant"], stat_color,
                                   tooltip=f"£{top_row['total_spend']:.2f} across {int(top_row['transactions'])} transaction(s)."),
                            unsafe_allow_html=True)

            top_n = len(by_merchant) if top_n_choice == "All" else min(top_n_choice, len(by_merchant))
            top_chart = by_merchant.head(top_n).sort_values(rank_col)
            chart_title = f"Merchants by {rank_by}" if top_n >= len(by_merchant) else f"Top {top_n} Merchants by {rank_by}"
            axis_label = "Total Spend (£)" if rank_col == "total_spend" else "Transactions (#)"
            color_scale = "Reds" if rank_col == "total_spend" else "Blues"
            fig_m = px.bar(
                top_chart, x=rank_col, y="Merchant", orientation="h",
                title=chart_title,
                labels={rank_col: axis_label},
                color=rank_col, color_continuous_scale=color_scale,
            )
            fig_m.update_layout(
                showlegend=False, coloraxis_showscale=False,
                height=max(400, top_n * 36),
                margin=dict(l=0, r=0, t=40, b=0),
            )
            st.plotly_chart(fig_m, width="stretch", key="top_merchants_chart")

            st.subheader(f"All Merchants ({len(by_merchant)})")
            merch_display = by_merchant.rename(columns={
                "total_spend": "Total Spend",
                "transactions": "Transactions",
                "avg_transaction": "Avg Transaction",
                "last_seen": "Last Seen",
            })
            st.dataframe(
                merch_display, width="stretch", hide_index=True,
                column_config={
                    "Total Spend": st.column_config.NumberColumn("Total Spend", format="£%.2f"),
                    "Avg Transaction": st.column_config.NumberColumn("Avg Transaction", format="£%.2f"),
                    "Last Seen": st.column_config.DateColumn("Last Seen", format="YYYY-MM-DD"),
                },
            )


# ── Tab 6: Subscriptions ──────────────────────────────────────────────────────

with tab_subs:
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
    kpi_cols = st.columns(3)
    with kpi_cols[0]:
        st.markdown(_card("Active Subscriptions", str(len(active_subs)), kpi_color,
                           tooltip="Subscriptions not marked Inactive."),
                    unsafe_allow_html=True)
    with kpi_cols[1]:
        st.markdown(_card("Est. Monthly Cost", f"£{monthly_cost:.2f}", row_colors["Spend"],
                           tooltip="Sum of every active subscription's amount, normalized to a monthly rate."),
                    unsafe_allow_html=True)
    with kpi_cols[2]:
        st.markdown(_card("Est. Annual Cost", f"£{annual_cost:.2f}", row_colors["Spend"],
                           tooltip="Est. Monthly Cost x 12."),
                    unsafe_allow_html=True)

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

    if subs:
        st.subheader("Your Subscriptions")
        for s in subs:
            mc = s["amount"] * FREQ_MONTHLY[s["frequency"]]
            confirm_key = f"confirm_del_sub_{s['id']}"
            if confirm_key not in st.session_state:
                st.session_state[confirm_key] = False

            col1, col2, col3, col4, col5, col6 = st.columns([3, 1, 1, 1, 1, 1])
            col1.markdown(f"**{s['name']}**")
            col2.markdown(f"£{s['amount']:.2f} / {s['frequency']}")
            col3.markdown(f"~£{mc:.2f}/mo")
            if col4.button("✏️", key=f"edit_{s['id']}", help="Edit"):
                st.session_state.edit_sub_id = None if st.session_state.edit_sub_id == s["id"] else s["id"]
            label = "Disable" if s["active"] else "Enable"
            if col5.button(label, key=f"toggle_{s['id']}"):
                toggle_subscription(s["id"])
                st.cache_data.clear()
                st.rerun()
            if col6.button("✕", key=f"del_{s['id']}"):
                st.session_state[confirm_key] = True
            if not s["active"]:
                st.caption("⏸ Inactive")

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
    candidates = detect_subscriptions(df, confirmed_names)
    if candidates:
        for c in candidates:
            col1, col2, col3, col4 = st.columns([3, 1, 1, 1])
            col1.markdown(f"**{c['name']}**")
            col1.caption(f"{c['occurrences']} transactions")
            col2.markdown(f"£{c['amount']:.2f} / {c['frequency']}")
            col3.markdown(f"~£{c['monthly_cost']:.2f}/mo")
            if col4.button("+ Add", key=f"add_{c['name']}"):
                upsert_subscription(c["name"], c["amount"], c["frequency"], c["name"])
                st.cache_data.clear()
                st.rerun()
    else:
        st.info("No recurring patterns detected yet — more transaction history will improve detection.")


# ── Tab 7: Settings ────────────────────────────────────────────────────────────

with tab_settings:
    st.caption("Manage categories and set each one's Role, which controls what counts as Income, Spend, Investment, Transfer, or is Excluded on the Overview and in Telegram summaries.")

    INHERIT = "(inherit)"
    role_by_parent_id = {p["id"]: p["role"] for p in get_parents()}
    sub_role_info = {s["id"]: s for s in get_subcategories()}

    # Session state for right-panel mode
    for _k in ("tax_edit_sub", "tax_view_sub", "tax_edit_parent", "tax_del_parent", "tax_wipe_parent", "tax_del_sub"):
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

    settings_stat_color = "rgba(127, 140, 141, 0.16)"
    total_txns = _query("SELECT COUNT(*) AS n FROM transactions")[0]["n"]
    total_classified = _query("SELECT COUNT(*) AS n FROM transactions WHERE llm_category IS NOT NULL")[0]["n"]
    total_subs_all = sum(p["sub_count"] for p in tax_parents)
    classified_pct = (total_classified / total_txns * 100) if total_txns else 0.0

    stat_cols = st.columns(4)
    with stat_cols[0]:
        st.markdown(_card("Parent Categories", str(len(tax_parents)), settings_stat_color,
                           tooltip="Total parent categories in your taxonomy."),
                    unsafe_allow_html=True)
    with stat_cols[1]:
        st.markdown(_card("Subcategories", str(total_subs_all), settings_stat_color,
                           tooltip="Total subcategories across all parent categories."),
                    unsafe_allow_html=True)
    with stat_cols[2]:
        st.markdown(_card("Classified", f"{classified_pct:.0f}%", settings_stat_color,
                           tooltip=f"{total_classified} of {total_txns} transactions have a category assigned."),
                    unsafe_allow_html=True)
    with stat_cols[3]:
        st.markdown(_card("Unclassified", str(total_txns - total_classified), settings_stat_color,
                           tooltip="Transactions with no category assigned yet."),
                    unsafe_allow_html=True)

    st.divider()

    # Only split into two columns when there's actually something to show on
    # the right -- otherwise the tree gets squeezed into 2/5 of the page width
    # for no reason while an idle "click something" message sits in the rest.
    has_active_panel = any([
        st.session_state.tax_edit_parent, st.session_state.tax_del_parent,
        st.session_state.tax_wipe_parent, st.session_state.tax_del_sub,
        st.session_state.tax_edit_sub, st.session_state.tax_view_sub,
    ])
    if has_active_panel:
        tax_left, tax_right = st.columns([2, 3])
    else:
        tax_left, tax_right = st.container(), st.container()

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
                if s["id"] in (st.session_state.tax_edit_sub, st.session_state.tax_view_sub, st.session_state.tax_del_sub)
            ]
            is_active_parent = pid in (st.session_state.tax_edit_parent, st.session_state.tax_del_parent, st.session_state.tax_wipe_parent)

            current_role = role_by_parent_id.get(pid, "spend")
            label = f"**{pname}** — {parent['txn_count']} txns · {parent['sub_count']} subcats · Role: {current_role}"
            with st.expander(label, expanded=bool(active_sub_ids or is_active_parent)):

                new_role = st.selectbox(
                    "Role", ROLES, index=ROLES.index(current_role), key=f"role_{pid}",
                    help="What this category counts as for Income/Spend/Investment totals "
                         "on the Overview tab and in the weekly/monthly Telegram summaries."
                )
                if new_role != current_role:
                    set_parent_role(pid, new_role)
                    st.cache_data.clear()
                    st.rerun()

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
                    st.caption(f"Subcategories — role defaults to {current_role} unless overridden")
                    role_options = [INHERIT, *ROLES]
                    for sub in subs_for_parent:
                        sid = sub["id"]
                        is_sel = sid in (st.session_state.tax_edit_sub, st.session_state.tax_view_sub)
                        sc1, sc_role, sc2, sc3, sc4 = st.columns([3, 2, 1, 1, 1])
                        with sc1:
                            label_text = f"**{sub['name']}**" if is_sel else sub["name"]
                            st.markdown(f"{label_text} *({sub['txn_count']})*")
                        with sc_role:
                            current_override = (sub_role_info.get(sid) or {}).get("role_override") or INHERIT
                            new_override = st.selectbox(
                                "Role override", role_options, index=role_options.index(current_override),
                                key=f"role_sub_{sid}", label_visibility="collapsed"
                            )
                            if new_override != current_override:
                                set_subcategory_role(sid, None if new_override == INHERIT else new_override)
                                st.cache_data.clear()
                                st.rerun()
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
                                st.session_state.tax_del_sub = sid
                                st.session_state.tax_edit_sub = None
                                st.session_state.tax_view_sub = None
                                st.session_state.tax_edit_parent = None
                                st.session_state.tax_del_parent = None
                                st.session_state.tax_wipe_parent = None

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
                        stripped = new_pname.strip()
                        # upsert_parent() below is a get-or-create -- renaming to a name
                        # that already belongs to a DIFFERENT parent would silently merge
                        # the two categories together with no warning at all. Block it
                        # instead, same as duplicate-name adds are already blocked elsewhere.
                        name_collision = stripped != parent_row["name"] and stripped.lower() in {
                            p["name"].lower() for p in tax_parents if p["id"] != pid
                        }
                        if name_collision:
                            st.error(f'"{stripped}" already exists as a category — rename to a unique name, or delete one of them first.')
                        else:
                            if stripped != parent_row["name"]:
                                # DuckDB FK constraint fires on any UPDATE to a referenced parent row,
                                # even non-key columns. Workaround: insert new name, re-point subs, delete old.
                                new_pid = upsert_parent(stripped)
                                con.execute("UPDATE subcategories SET parent_id = ? WHERE parent_id = ?", [new_pid, pid])
                                con.execute("UPDATE transactions SET llm_category = ? WHERE llm_category = ?", [stripped, parent_row["name"]])
                                # Carry the role override across the rename -- otherwise a
                                # custom category's role silently resets to its default.
                                con.execute(
                                    "UPDATE category_roles SET parent_id = ? WHERE parent_id = ? AND subcategory_id IS NULL",
                                    [new_pid, pid],
                                )
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
                        backup_db(f"delete-parent-{parent_row['name']}")
                        # Role overrides reference parent/subcategory ids, and ids get
                        # reused after a delete (new_id = MAX(id)+1) -- an orphaned
                        # override left behind here could later attach itself to an
                        # unrelated category that happens to reuse the same id.
                        con.execute(
                            "DELETE FROM category_roles WHERE subcategory_id IN "
                            "(SELECT id FROM subcategories WHERE parent_id = ?)", [pid]
                        )
                        con.execute("DELETE FROM category_roles WHERE parent_id = ? AND subcategory_id IS NULL", [pid])
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
                        backup_db(f"wipe-labels-{parent_row['name']}")
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

        # Delete subcategory confirmation
        elif st.session_state.tax_del_sub:
            sid = st.session_state.tax_del_sub
            sub_rows = _query("""
                SELECT s.id, s.name, p.name AS parent_name,
                       COUNT(t.id) AS txn_count
                FROM subcategories s
                JOIN parent_categories p ON p.id = s.parent_id
                LEFT JOIN transactions t ON t.llm_subcategory = s.name AND t.llm_category = p.name
                WHERE s.id = ?
                GROUP BY s.id, s.name, p.name
            """, [sid])
            if sub_rows:
                sub = sub_rows[0]
                st.subheader(f"Delete: {sub['name']}")
                st.warning(
                    f"This will permanently delete **{sub['name']}** (under {sub['parent_name']}) "
                    f"and clear labels from **{sub['txn_count']} transaction(s)**."
                )
                c1, c2 = st.columns(2)
                with c1:
                    if st.button("Confirm delete", type="primary", key="confirm_del_sub"):
                        backup_db(f"delete-subcategory-{sub['name']}")
                        con.execute("DELETE FROM category_roles WHERE subcategory_id = ?", [sid])
                        con.execute("DELETE FROM subcategories WHERE id = ?", [sid])
                        con.execute(
                            "UPDATE transactions SET llm_subcategory = NULL WHERE llm_subcategory = ? AND llm_category = ?",
                            [sub["name"], sub["parent_name"]],
                        )
                        st.session_state.tax_del_sub = None
                        st.cache_data.clear()
                        st.rerun()
                with c2:
                    if st.button("Cancel", key="cancel_del_sub"):
                        st.session_state.tax_del_sub = None
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
                        effective_name = new_sname.strip() or sub["name"]
                        name_changed = effective_name != sub["name"]
                        parent_changed = new_sparent != sub["parent_name"]
                        target_parent_id = upsert_parent(new_sparent) if parent_changed else sub["parent_id"]

                        # subcategories has a UNIQUE(name, parent_id) constraint --
                        # saving into a (name, parent) combo that's already taken by
                        # another subcategory would otherwise crash with an uncaught
                        # DuckDB constraint violation instead of a friendly message.
                        collision = (name_changed or parent_changed) and _query(
                            "SELECT 1 AS x FROM subcategories WHERE name = ? AND parent_id = ? AND id != ?",
                            [effective_name, target_parent_id, sid],
                        )
                        if collision:
                            st.error(
                                f'"{effective_name}" already exists under {new_sparent} — '
                                f"choose a different name, or delete/merge the existing one first."
                            )
                        else:
                            if name_changed:
                                con.execute("UPDATE subcategories SET name = ? WHERE id = ?", [effective_name, sid])
                                con.execute(
                                    "UPDATE transactions SET llm_subcategory = ? WHERE llm_subcategory = ? AND llm_category = ?",
                                    [effective_name, sub["name"], sub["parent_name"]],
                                )
                            if parent_changed:
                                con.execute("UPDATE subcategories SET parent_id = ? WHERE id = ?", [target_parent_id, sid])
                                con.execute(
                                    "UPDATE transactions SET llm_category = ? WHERE llm_subcategory = ? AND llm_category = ?",
                                    [new_sparent, effective_name, sub["parent_name"]],
                                )
                                # Keep the override's parent_id in sync with the move --
                                # unused by role resolution (which only keys off
                                # subcategory_id) but avoids a stale, confusing value.
                                con.execute(
                                    "UPDATE category_roles SET parent_id = ? WHERE subcategory_id = ?",
                                    [target_parent_id, sid],
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
                            backup_db(f"move-all-{sub['name']}")
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
                        width="stretch",
                        hide_index=True,
                    )
                    st.caption(f"{len(txns)} transaction(s)")
                else:
                    st.info("No transactions assigned to this subcategory.")

    # ── Backups ────────────────────────────────────────────────────────────────
    st.divider()
    _backups = list_backups()
    with st.expander(
        f"💾 Backups ({len(_backups)})"
        + (f" — last {backup_label(_backups[0])}" if _backups else " — none yet")
    ):
        st.caption(
            "A backup is taken automatically before anything irreversible — deleting a "
            "category or subcategory, wiping labels, bulk-reassigning transactions, or "
            f"wiping the taxonomy. The most recent {BACKUPS_KEPT} are kept; older ones are "
            "pruned, and any of them can be deleted by hand below. Restore one with "
            "DuckDB's `IMPORT DATABASE '<folder>'`."
        )
        if st.button("Back up now"):
            path = backup_db("manual")
            st.success(f"Backed up to {path}")
            st.rerun()

        if _backups:
            _manifests = [(p, read_backup_manifest(p)) for p in _backups]
            # Headline the newest backup's contents -- the question this section
            # exists to answer is "if I restore, what do I get back?", and that
            # is almost always about the most recent one.
            _newest_path, _newest = _manifests[0]
            if _newest:
                st.markdown(
                    f"**Latest backup** holds **{_newest.get('transactions', 0):,} transactions** "
                    f"({_newest.get('classified', 0):,} classified) across "
                    f"**{_newest.get('parent_categories', 0)} categories** / "
                    f"{_newest.get('subcategories', 0)} subcategories, plus "
                    f"{_newest.get('subscriptions', 0)} subscription(s) — "
                    f"covering {backup_coverage(_newest)}, "
                    f"{format_bytes(backup_size_bytes(_newest_path))} on disk."
                )

            st.dataframe(
                pd.DataFrame([
                    {
                        "When": backup_label(p),
                        "Reason": backup_reason(p),
                        "Transactions": m.get("transactions"),
                        "Classified": m.get("classified"),
                        "Categories": m.get("parent_categories"),
                        "Subcats": m.get("subcategories"),
                        "Covers": backup_coverage(m),
                        "Size": format_bytes(backup_size_bytes(p)),
                        "Folder": str(p),
                    }
                    for p, m in _manifests
                ]),
                hide_index=True, width="stretch",
                column_config={
                    "When": st.column_config.Column("When", help="When this backup was taken."),
                    "Reason": st.column_config.Column(
                        "Reason", help="What triggered it — 'manual' for the button above, "
                                       "otherwise the destructive action it was taken before."),
                    "Transactions": st.column_config.NumberColumn(
                        "Transactions", format="%d",
                        help="Transactions captured in this backup. Blank for a backup taken "
                             "before manifests existed, or a folder added here by hand."),
                    "Classified": st.column_config.NumberColumn(
                        "Classified", format="%d",
                        help="How many of those transactions had a category assigned at the time."),
                    "Categories": st.column_config.NumberColumn(
                        "Categories", format="%d", help="Parent categories in the taxonomy."),
                    "Subcats": st.column_config.NumberColumn(
                        "Subcats", format="%d", help="Subcategories in the taxonomy."),
                    "Covers": st.column_config.Column(
                        "Covers", help="Date range of the transactions inside this backup."),
                    "Size": st.column_config.Column("Size", help="Total size of the export on disk."),
                    "Folder": st.column_config.Column(
                        "Folder", help="Pass this to IMPORT DATABASE to restore."),
                },
            )

            # Deleting a backup throws away a safety net, so it gets the same
            # two-step confirm the rest of the app uses for destructive actions
            # rather than a single click next to a table.
            st.divider()
            if "confirm_del_backup" not in st.session_state:
                st.session_state.confirm_del_backup = None

            _chosen = st.selectbox(
                "Delete a backup",
                options=_backups,
                format_func=lambda p: f"{backup_label(p)} · {backup_reason(p)}",
                key="backup_to_delete",
                help="Backups are pruned automatically once there are more than "
                     f"{BACKUPS_KEPT}. Delete one by hand to reclaim space sooner.",
            )
            if st.button("Delete backup", key="del_backup"):
                st.session_state.confirm_del_backup = str(_chosen)

            if st.session_state.confirm_del_backup:
                _pending = Path(st.session_state.confirm_del_backup)
                _pending_manifest = read_backup_manifest(_pending)
                st.warning(
                    f"Delete the backup from **{backup_label(_pending)}** "
                    f"({backup_reason(_pending)})? It holds "
                    f"**{_pending_manifest.get('transactions', 'an unknown number of')} "
                    f"transactions** and cannot be recovered."
                    + ("\n\nThis is your only backup." if len(_backups) == 1 else "")
                )
                _dc1, _dc2 = st.columns(2)
                with _dc1:
                    if st.button("Yes, delete it", type="primary", key="confirm_del_backup_yes"):
                        delete_backup(_pending)
                        st.session_state.confirm_del_backup = None
                        st.rerun()
                with _dc2:
                    if st.button("Cancel", key="confirm_del_backup_no"):
                        st.session_state.confirm_del_backup = None
                        st.rerun()

    # ── Danger zone: wipe entire taxonomy ──────────────────────────────────────
    st.divider()
    with st.expander("⚠️ Danger Zone"):
        st.markdown("**Wipe entire taxonomy**")

        st.warning(
            f"This permanently deletes all **{len(tax_parents)} parent categories** and "
            f"**{total_subs_all} subcategories**, and clears the LLM category/subcategory "
            f"labels from **{total_classified} of {total_txns}** transactions.\n\n"
            f"Your raw transactions and context sentences are **never touched** — only the "
            f"category structure and label assignments are removed. The default taxonomy "
            f"is re-seeded automatically the next time the pipeline or dashboard starts, and "
            f"transactions will need to be re-classified from scratch.\n\n"
            f"A full backup is taken first — see the Backups section above to restore it."
        )

        wipe_ack = st.checkbox("I understand this cannot be undone", key="wipe_taxonomy_ack")
        wipe_text = st.text_input(
            'Type "WIPE TAXONOMY" (exact, case-sensitive) to confirm',
            key="wipe_taxonomy_text",
            disabled=not wipe_ack,
        )
        wipe_ready = wipe_ack and wipe_text.strip() == "WIPE TAXONOMY"

        if st.button("Permanently wipe entire taxonomy", type="primary", disabled=not wipe_ready):
            backup_db("wipe-taxonomy")
            con.execute(
                """UPDATE transactions
                   SET llm_category = NULL, llm_subcategory = NULL,
                       classified_at = NULL, llm_model = NULL, llm_confidence = NULL"""
            )
            # Without this, a role override left behind here could later attach
            # itself to an unrelated category re-seeded with the same reused id
            # (ids restart from MAX(id)+1, which is 1 again once these tables
            # are empty).
            con.execute("DELETE FROM category_roles")
            con.execute("DELETE FROM subcategories")
            con.execute("DELETE FROM parent_categories")
            st.session_state.wipe_taxonomy_ack = False
            st.session_state.wipe_taxonomy_text = ""
            st.cache_data.clear()
            st.success("Taxonomy wiped. Re-run the pipeline to re-seed the default taxonomy and re-classify.")
            st.rerun()
