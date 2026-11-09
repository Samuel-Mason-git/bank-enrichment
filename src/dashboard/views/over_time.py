"""Over Time: month by month, overall and per category."""
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from dashboard_helpers import ROLE_METRICS, role_breakdown
from ui import _fmt, _card_rows, row_colors
from page_context import shared

filtered, = shared('filtered')


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
        # Light, so it stands out on the dark theme (it was a navy that
        # all but vanished into the background).
        line=dict(color="rgba(236, 240, 241, 0.85)", width=3),
    ))
    fig.add_trace(go.Scatter(
        x=totals["Month"], y=totals["Rolling Avg"], mode="lines", name="3-mo Avg",
        line=dict(color="#f39c12", width=3, dash="dash"),
    ))
    # Hovering a month lists every line's £ for it, rather than one raw
    # "Month=… Amount=…" box per point.
    fig.update_traces(hovertemplate="£%{y:,.2f}")
    fig.update_layout(hovermode="x unified", yaxis_tickprefix="£", yaxis_tickformat=",.0f")

def _every_month(monthly: pd.DataFrame, group: str) -> pd.DataFrame:
    """Give each line a point for every month in range: a month a category
    had nothing in is £0, not missing -- otherwise the line skips it, and a
    category seen in one month only is a lone dot with no line at all."""
    if monthly.empty:
        return monthly
    months = sorted(filtered["month"].unique())
    grid = pd.MultiIndex.from_product([months, monthly[group].unique()], names=["Month", group])
    return (monthly.set_index(["Month", group])["Amount"]
            .reindex(grid, fill_value=0).reset_index())

def _time_chart_pair(key_prefix: str, metric: str, sign: str):
    """Monthly category + subcategory trend lines for one metric, restricted
    to one direction of money flow. This is a per-category breakdown (which
    categories drove deposits vs. withdrawals, which drove inbound vs.
    outbound transfers), not a summary total -- unlike Overview's single
    Invested/Saved figure (netted, since a deposit and a later withdrawal
    are the same pot/account), showing both directions as one netted line
    per category here would hide which side actually moved, so each
    direction gets its own chart. Lines (not stacked bars) since a
    per-category trend is what's being compared here, and a stack makes
    any one category's own trajectory hard to isolate."""
    metric_time = filtered[filtered["role"].isin(ROLE_METRICS[metric])]
    metric_time = metric_time[metric_time["amount"] < 0] if sign == "negative" else metric_time[metric_time["amount"] > 0]
    if metric_time.empty:
        st.info(f"No {metric.lower()} in range.")
        return
    monthly = _every_month(role_breakdown(
        metric_time, ROLE_METRICS[metric], "llm_category", by_month=True, sign=sign), "Category")
    available_cats = sorted(metric_time[metric_time["llm_category"].notna()]["llm_category"].unique())
    # With one category (Income is all "Income") its chart would be a
    # single line, and picking which category's subcategories to see no
    # choice at all: the subcategory chart gets the full width instead.
    single = monthly["Category"].nunique() <= 1
    if single:
        col2 = st.container()
    else:
        col1, col2 = st.columns(2)
        with col1:
            st.caption("All categories")
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
        if available_cats:
            selected_cat = (available_cats[0] if single else
                            st.selectbox("View subcategories for", available_cats, key=f"{key_prefix}_sub_cat"))
            sub_data = metric_time[metric_time["llm_category"] == selected_cat]
            monthly_sub = _every_month(role_breakdown(
                sub_data, ROLE_METRICS[metric], "llm_subcategory", by_month=True, sign=sign), "Subcategory")
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

# Split by direction for the per-category breakdown charts below --
# Spend/Invested/Saved/Transferred-Excluded are "money out", Income/
# Invested/Saved/Transferred-Excluded are "money in".
time_outgoing_metrics = [m for m in ROLE_METRICS if m != "Income"]
time_incoming_metrics = [m for m in ROLE_METRICS if m != "Spend"]

if not filtered.empty:
    monthly_spend = filtered[(filtered["role"] == "spend") & (filtered["amount"] < 0)].groupby("month")["amount"].sum().abs().rename("Spend")
    monthly_income = filtered[(filtered["role"] == "income") & (filtered["amount"] > 0)].groupby("month")["amount"].sum().rename("Income")
    monthly_invested = filtered[filtered["role"] == "investment"].groupby("month")["amount"].sum().mul(-1).rename("Invested")
    monthly_saved = filtered[filtered["role"] == "saving"].groupby("month")["amount"].sum().mul(-1).rename("Saved")
    monthly_transferred = filtered[filtered["role"].isin(["transfer", "excluded"]) & (filtered["amount"] < 0)].groupby("month")["amount"].sum().abs().rename("Transferred")
    monthly_totals_raw = (
        pd.concat([monthly_spend, monthly_income, monthly_invested, monthly_saved, monthly_transferred], axis=1)
        .fillna(0).reset_index()
        .rename(columns={"month": "Month"})
        .sort_values("Month")
    )
    # amount columns come back as Decimal -- pct_change() divides
    # raw values with no zero-guard, and Decimal division by zero raises
    # (unlike float, which would just produce inf/nan), so cast to float
    # before any of the derived-ratio columns below.
    for _col in ["Spend", "Income", "Invested", "Saved", "Transferred"]:
        monthly_totals_raw[_col] = monthly_totals_raw[_col].astype(float)
    monthly_totals_raw["Net"] = monthly_totals_raw["Income"] - monthly_totals_raw["Spend"]
    monthly_totals_raw["Savings Rate"] = (
        monthly_totals_raw["Net"] / monthly_totals_raw["Income"].replace(0, float("nan")) * 100
    ).round(1)
    monthly_totals_raw["Investment Rate"] = (
        monthly_totals_raw["Invested"] / monthly_totals_raw["Income"].replace(0, float("nan")) * 100
    ).round(1)
    # Not plotted alongside Savings Rate/Investment Rate on the combined
    # chart below (that chart is already three lines on two axes) -- it's
    # still computed and shown in the Monthly Totals table, same as
    # Investment Rate itself only ever appears as a chart line and nowhere
    # else as a number.
    monthly_totals_raw["Saved Rate"] = (
        monthly_totals_raw["Saved"] / monthly_totals_raw["Income"].replace(0, float("nan")) * 100
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
        "Saved": (monthly_totals_raw["Saved"].mean(), False),
        "Transferred / Excluded": (monthly_totals_raw["Transferred"].mean(), False),
    }
    _card_rows([
        (f"Avg {label}", _fmt(val, is_pct), row_colors[label],
         f"Average monthly {label.lower()} across the selected range.", "")
        for label, (val, is_pct) in avg_values.items()
    ])

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
        # showlegend: px.line leaves a lone line out of the legend, so
        # without it this one was an unlabelled green line.
        fig_sr.update_traces(name="Savings Rate (%)", line_color="#2ecc71", marker_color="#2ecc71",
                             showlegend=True, hovertemplate="%{y:.1f}%")
        fig_sr.add_trace(go.Scatter(
            x=sr_chart["Month"], y=sr_chart["Investment Rate"], name="Investment Rate (%)",
            mode="lines+markers", line=dict(color="#9b59b6"), hovertemplate="%{y:.1f}%",
        ))
        # Income minus Spend -- what the Overview calls Net, so it's called
        # that here too (it isn't what went into savings pots; that's Saved).
        fig_sr.add_trace(go.Scatter(
            x=sr_chart["Month"], y=sr_chart["Actual Savings"], name="Net (£, right axis)",
            mode="lines+markers", line=dict(color="#3498db"), yaxis="y2",
            hovertemplate="£%{y:,.2f}",
        ))
        fig_sr.update_layout(
            xaxis_tickangle=-45, margin=dict(t=20, b=0), yaxis_ticksuffix="%",
            hovermode="x unified",
            yaxis2=dict(title="Net (£)", overlaying="y", side="right",
                        tickprefix="£", tickformat=",.0f", showgrid=False),
            legend=dict(orientation="h", yanchor="bottom", y=1.02),
            xaxis_type="category", xaxis_categoryorder="category ascending",
        )
        st.plotly_chart(fig_sr, width="stretch", key="savings_rate_chart")
    else:
        st.info("Need at least two months with income to chart savings.")

    def _net_flow_chart(column: str, color: str, key: str):
        """Each month's net into savings or investments, every month in
        range: one that took money back out is a bar below zero, not a
        month left off the chart. Its own chart rather than a fourth line
        on the combined one above, which already has three on two axes."""
        if len(monthly_totals_raw) < 2 or not monthly_totals_raw[column].any():
            return
        values = monthly_totals_raw[column]
        fig = go.Figure(go.Bar(
            x=monthly_totals_raw["Month"], y=values, name=column,
            marker_color=[color if v >= 0 else "#e74c3c" for v in values],
            hovertemplate="%{x}: £%{y:,.2f}<extra></extra>",
        ))
        fig.add_hline(y=0, line_color="rgba(255,255,255,0.3)")
        fig.update_layout(
            title=f"{column} each month (net; below zero = taken back out)",
            yaxis_title=f"{column} (£)", yaxis_tickprefix="£", xaxis_tickangle=-45,
            margin=dict(t=40, b=0), height=320,
            xaxis_type="category", xaxis_categoryorder="category ascending",
        )
        st.plotly_chart(fig, width="stretch", key=key)

    _net_flow_chart("Invested", "#9b59b6", "invested_over_time_chart")
    _net_flow_chart("Saved", "#2ab0a0", "saved_over_time_chart")
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
                "Invested", "Investment Rate", "Saved", "Saved Rate",
                "Transferred", "Income MoM", "Spend MoM",
            ]],
            width="stretch", hide_index=True,
            column_config={
                "Month": st.column_config.Column(
                    "Month", help="Calendar month, based on the selected date range."),
                "Income": st.column_config.NumberColumn(
                    "Income", format="£%,.2f",
                    help="Actual Income — the incoming (positive) side of every Income-role category."),
                "Spend": st.column_config.NumberColumn(
                    "Spend", format="£%,.2f",
                    help="Actual Spend — the outgoing (negative) side of every Spend-role category."),
                "Net": st.column_config.NumberColumn(
                    "Net", format="£%,.2f", help="Income minus Spend for the month."),
                "Savings Rate": st.column_config.NumberColumn(
                    "Savings Rate", format="%.1f%%",
                    help="Net as a percentage of Income. Blank when there was no income that month."),
                "Invested": st.column_config.NumberColumn(
                    "Invested", format="£%,.2f",
                    help="Net money moved into investment categories (contributions minus withdrawals)."),
                "Investment Rate": st.column_config.NumberColumn(
                    "Investment Rate", format="%.1f%%",
                    help="Invested as a percentage of Income. Blank when there was no income that month."),
                "Saved": st.column_config.NumberColumn(
                    "Saved", format="£%,.2f",
                    help="Net money moved into savings categories (deposits minus withdrawals) -- "
                         "mechanically identical to Invested, kept separate so it isn't mixed with "
                         "genuine investing."),
                "Saved Rate": st.column_config.NumberColumn(
                    "Saved Rate", format="%.1f%%",
                    help="Saved as a percentage of Income. Blank when there was no income that month."),
                "Transferred": st.column_config.NumberColumn(
                    "Transferred", format="£%,.2f",
                    help="Money moved to transfer or excluded categories (outgoing side only)."),
                "Income MoM": st.column_config.NumberColumn(
                    "Income MoM", format="%+.1f%%", help="Change in Income vs the previous month."),
                "Spend MoM": st.column_config.NumberColumn(
                    "Spend MoM", format="%+.1f%%", help="Change in Spend vs the previous month."),
            },
        )
