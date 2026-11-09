"""Overview: this period's income, spending, saving and investing, against the period before."""
import pandas as pd
import plotly.express as px
import streamlit as st
from dashboard_helpers import ROLE_METRICS, pct_delta, role_breakdown
from ui import _fmt, _delta_badge, _colour_range, _section, _card_rows, row_colors
from page_context import shared

filtered, prev_filtered = shared('filtered', 'prev_filtered')


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

# "saving"/"investment" net their two sides together (a deposit and a
# later withdrawal are the same pot or account, so summing them gives a
# true "how much extra is tucked away" figure). "transfer"/"excluded" stay
# direction-restricted (outgoing only, with the incoming side as its own
# card below) because that bucket can mix genuinely unrelated inbound and
# outbound movements (a refund in, an unrelated bill out) -- summing every
# sign together there would let them cancel into a number that looks
# meaningful but isn't.
income_mask = (filtered["role"] == "income") & (filtered["amount"] > 0)
spend_mask = (filtered["role"] == "spend") & (filtered["amount"] < 0)
invest_mask = filtered["role"] == "investment"
saving_mask = filtered["role"] == "saving"
excluded_mask = filtered["role"].isin(["transfer", "excluded"]) & (filtered["amount"] < 0)
excluded_in_mask = filtered["role"].isin(["transfer", "excluded"]) & (filtered["amount"] > 0)

actual_income = filtered.loc[income_mask, "amount"].sum()
actual_spend = abs(filtered.loc[spend_mask, "amount"].sum())
invested = -filtered.loc[invest_mask, "amount"].sum()
saved = -filtered.loc[saving_mask, "amount"].sum()
excluded_total = abs(filtered.loc[excluded_mask, "amount"].sum())
excluded_in = filtered.loc[excluded_in_mask, "amount"].sum()

actual_net = actual_income - actual_spend
actual_savings_rate = (actual_net / actual_income * 100) if actual_income > 0 else None

prev_income_mask = (prev_filtered["role"] == "income") & (prev_filtered["amount"] > 0)
prev_spend_mask = (prev_filtered["role"] == "spend") & (prev_filtered["amount"] < 0)
prev_invest_mask = prev_filtered["role"] == "investment"
prev_saving_mask = prev_filtered["role"] == "saving"
prev_excluded_mask = prev_filtered["role"].isin(["transfer", "excluded"]) & (prev_filtered["amount"] < 0)
prev_excluded_in_mask = prev_filtered["role"].isin(["transfer", "excluded"]) & (prev_filtered["amount"] > 0)

prev_actual_income = prev_filtered.loc[prev_income_mask, "amount"].sum()
prev_actual_spend = abs(prev_filtered.loc[prev_spend_mask, "amount"].sum())
prev_invested = -prev_filtered.loc[prev_invest_mask, "amount"].sum()
prev_saved = -prev_filtered.loc[prev_saving_mask, "amount"].sum()
prev_excluded_total = abs(prev_filtered.loc[prev_excluded_mask, "amount"].sum())
prev_excluded_in = prev_filtered.loc[prev_excluded_in_mask, "amount"].sum()
prev_actual_net = prev_actual_income - prev_actual_spend
prev_actual_savings_rate = (prev_actual_net / prev_actual_income * 100) if prev_actual_income > 0 else None

actual_income_d = pct_delta(actual_income, prev_actual_income)
actual_spend_d = pct_delta(actual_spend, prev_actual_spend)
invested_d = pct_delta(invested, prev_invested)
saved_d = pct_delta(saved, prev_saved)
excluded_d = pct_delta(excluded_total, prev_excluded_total)
excluded_in_d = pct_delta(excluded_in, prev_excluded_in)
actual_net_d = pct_delta(actual_net, prev_actual_net)
actual_sr_d = (
    round(actual_savings_rate - prev_actual_savings_rate, 1)
    if actual_savings_rate is not None and prev_actual_savings_rate is not None else None
)

delta_direction = {
    "Income": "normal", "Spend": "inverse", "Net": "normal",
    "Savings Rate": "normal", "Invested": "normal", "Saved": "normal",
    "Transferred / Excluded": "neutral",
}
standard_tooltips = {
    "Income": "Every transaction with a positive amount, regardless of category.",
    "Spend": "Every transaction with a negative amount, regardless of category.",
    "Net": "Standard Income minus Standard Spend.",
    "Savings Rate": "Standard Net as a percentage of Standard Income.",
}
actual_static_tooltips = {
    "Net": "Actual Income minus Actual Spend.",
    "Savings Rate": "Actual Net as a percentage of Actual Income.",
}
standard_metrics = [
    ("Income", total_income, income_d, False),
    ("Spend", total_spend, spend_d, False),
    ("Net", net, net_d, False),
    ("Savings Rate", savings_rate, sr_d, True),
]

# Transferred/Excluded only shows its outgoing side by default -- but it
# can also carry an incoming side under the same role (e.g. an inbound
# transfer or a refund), which may be unrelated money rather than the same
# pot/account cycling. Surface it as its own card whenever it's nonzero
# rather than letting it net invisibly against the outgoing figure or
# vanish from the Overview entirely. Invested/Saved don't need this split
# -- they're already netted above.
excluded_label = "Transferred / Excluded (Outgoing)" if excluded_in > 0 else "Transferred / Excluded"

# (display label, color/delta-direction key, actual value, actual delta,
#  categories-mask for the tooltip, is_pct)
actual_rows = [
    ("Income", "Income", actual_income, actual_income_d, income_mask, False),
    ("Spend", "Spend", actual_spend, actual_spend_d, spend_mask, False),
    ("Net", "Net", actual_net, actual_net_d, None, False),
    ("Savings Rate", "Savings Rate", actual_savings_rate, actual_sr_d, None, True),
    ("Invested", "Invested", invested, invested_d, invest_mask, False),
    ("Saved", "Saved", saved, saved_d, saving_mask, False),
    (excluded_label, "Transferred / Excluded", excluded_total, excluded_d, excluded_mask, False),
]
if excluded_in > 0:
    actual_rows.append(("Transferred / Excluded (Incoming)", "Transferred / Excluded", excluded_in, excluded_in_d, excluded_in_mask, False))

# With nothing before the range (All time, say) there are no vs-prev
# pills at all, so the cards needn't keep room for one.
_prev_has_data = not prev_filtered.empty

st.caption(f"{len(filtered)} transactions · {int(filtered['llm_category'].isna().sum())} unclassified.")

_section(
    "Standard",
    "Every £ is counted by amount sign alone — money in is Income, "
    "money out is Spend, regardless of category or role.",
    "by amount sign",
)
_card_rows([
    (label, _fmt(std_val, is_pct), row_colors[label], standard_tooltips[label],
     _delta_badge(std_d, "pp" if is_pct else "%", delta_direction[label]))
    for label, std_val, std_d, is_pct in standard_metrics
], reserve_delta=_prev_has_data)

_section(
    "Actual",
    "Each category has a role (set in the Settings page) that decides what "
    "it counts as — e.g. refunds and transfers don't count as Income here.",
    "by category role",
)
# Income/Spend/Net/Savings Rate first, so they sit under the same cards in
# Standard; where the money went besides spending on the row below.
_card_rows([
    (label, _fmt(act_val, is_pct), row_colors[color_key],
     _categories_for(mask) if mask is not None else actual_static_tooltips[color_key],
     _delta_badge(act_d, "pp" if is_pct else "%", delta_direction[color_key]))
    for label, color_key, act_val, act_d, mask, is_pct in actual_rows
], reserve_delta=_prev_has_data)

st.divider()

# Total actual income for the filtered period — used to compute "% of Income"
# on outgoing breakdowns so you can see e.g. rent is 28% of income.
_income_total = role_breakdown(filtered, ROLE_METRICS["Income"], "llm_category", sign="positive")["Amount"].sum()

def _breakdown_chart(col, key_prefix: str, title: str, metric: str, color_scale: str, sign: str):
    with col:
        # A choice of grouping only when there's a choice: with one
        # category (Income is all "Income") grouping by category is a
        # single bar, so it goes straight to the subcategories.
        by_category = role_breakdown(filtered, ROLE_METRICS[metric], "llm_category", sign=sign)
        if len(by_category) > 1:
            group_by = st.radio(
                "Group by", ["Category", "Subcategory"],
                key=f"{key_prefix}_group_by", horizontal=True,
            )
        else:
            group_by = "Subcategory"
        if group_by == "Category":
            breakdown = by_category
        else:
            breakdown = role_breakdown(filtered, ROLE_METRICS[metric], "llm_subcategory", sign=sign)
        breakdown = breakdown.sort_values("Amount", ascending=True)
        if not breakdown.empty:
            fig = px.bar(breakdown, x="Amount", y=group_by, orientation="h",
                         title=f"{title} by {group_by}", labels={"Amount": f"{title} (£)", group_by: ""},
                         color="Amount", color_continuous_scale=color_scale,
                         range_color=_colour_range(breakdown["Amount"]))
            fig.update_traces(hovertemplate="%{y}: £%{x:,.2f}<extra></extra>")
            fig.update_layout(showlegend=False, coloraxis_showscale=False,
                              height=max(300, len(breakdown) * 34 + 80),
                              xaxis_tickprefix="£", xaxis_tickformat=",.0f",
                              margin=dict(l=0, r=0, t=40, b=0))
            st.plotly_chart(fig, width="stretch", key=f"{key_prefix}_chart")
            total = breakdown["Amount"].sum()
            if total > 0:
                pct_table = breakdown.sort_values("Amount", ascending=False).copy()
                pct_table["% of total"] = pct_table["Amount"] / total * 100
                columns = {
                    "Amount": st.column_config.NumberColumn("Amount", format="£%,.2f"),
                    "% of total": st.column_config.NumberColumn("% of total", format="%.1f%%"),
                }
                if sign == "negative" and _income_total > 0:
                    pct_table["% of income"] = pct_table["Amount"] / _income_total * 100
                    columns["% of income"] = st.column_config.NumberColumn(
                        "% of income", format="%.1f%%",
                        help="This much of the period's Actual Income.")
                st.dataframe(pct_table, hide_index=True, width="stretch", column_config=columns,
                             height=min(35 * (len(pct_table) + 1) + 2, 600))
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
        _saving_roles = ROLE_METRICS.get("Saved", [])
        _mdf["_income"] = _mdf.apply(
            lambda r: r["amount"] if r["role"] in _income_roles and r["amount"] > 0 else 0, axis=1)
        _mdf["_spend"] = _mdf.apply(
            lambda r: abs(r["amount"]) if r["role"] in _spend_roles and r["amount"] < 0 else 0, axis=1)
        _mdf["_invested"] = _mdf.apply(
            lambda r: -r["amount"] if r["role"] in _invest_roles else 0, axis=1)
        _mdf["_saved"] = _mdf.apply(
            lambda r: -r["amount"] if r["role"] in _saving_roles else 0, axis=1)
        _monthly = (
            _mdf.groupby("_month")
            .agg(_income=("_income", "sum"), _spend=("_spend", "sum"),
                 _invested=("_invested", "sum"), _saved=("_saved", "sum"))
            .reset_index()
            .sort_values("_month", ascending=False)
        )
        _monthly["Net"] = (_monthly["_income"] - _monthly["_spend"]).round(2)
        _per_income = _monthly["_income"].where(_monthly["_income"] > 0)
        _monthly["Savings Rate"] = _monthly["Net"] / _per_income * 100
        _monthly["Saved Rate"] = _monthly["_saved"] / _per_income * 100
        _monthly = _monthly.rename(columns={
            "_month": "Month", "_income": "Income",
            "_spend": "Spend", "_invested": "Invested", "_saved": "Saved",
        })
        _money = lambda label, help=None: st.column_config.NumberColumn(label, format="£%,.2f", help=help)
        st.dataframe(
            _monthly[["Month", "Income", "Spend", "Invested", "Saved",
                      "Net", "Savings Rate", "Saved Rate"]],
            hide_index=True, width="stretch",
            column_config={
                "Income": _money("Income"), "Spend": _money("Spend"),
                "Invested": _money("Invested", "Net into investments; negative = taken back out."),
                "Saved": _money("Saved", "Net into savings pots; negative = taken back out."),
                "Net": _money("Net", "Income minus Spend."),
                "Savings Rate": st.column_config.NumberColumn(
                    "Savings Rate", format="%.1f%%", help="Net as a share of Income."),
                "Saved Rate": st.column_config.NumberColumn(
                    "Saved Rate", format="%.1f%%", help="Saved as a share of Income."),
            },
        )
    else:
        st.info("No data in current filter range.")
