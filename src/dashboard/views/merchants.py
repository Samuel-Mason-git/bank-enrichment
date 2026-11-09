"""Top Merchants: where the money goes, by merchant or payee."""
import plotly.express as px
import streamlit as st
from dashboard_helpers import merchant_display_name
from ui import _fmt, _colour_range, _card_rows, row_colors
from page_context import shared

filtered, = shared('filtered')


st.caption(
    "Actual Spend only — investments, savings, transfers, and refunds aren't counted as merchant spend."
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

    merch_ctrl1, merch_ctrl2, merch_ctrl3 = st.columns([3, 1, 1.4])
    with merch_ctrl1:
        merchant_search = st.text_input(
            "Search merchants", placeholder="merchant name...", key="merchant_search"
        )
    with merch_ctrl2:
        top_n_choice = st.selectbox("Show top", [10, 20, 50, "All"], index=1, key="merchant_top_n")
    with merch_ctrl3:
        rank_by = st.radio("Rank merchants by", ["Total Spend", "Frequency"],
                            horizontal=True, key="merchant_rank_by")

    if merchant_search:
        by_merchant = by_merchant[
            by_merchant["Merchant"].str.contains(merchant_search, case=False, na=False, regex=False)
        ]

    if by_merchant.empty:
        st.info("No merchants match that search.")
    else:
        rank_col = "total_spend" if rank_by == "Total Spend" else "transactions"
        by_merchant = by_merchant.sort_values(
            [rank_col, "Merchant"], ascending=[False, True]
        ).reset_index(drop=True)

        total_spend_all = by_merchant["total_spend"].sum()
        top_row = by_merchant.iloc[0]
        _card_rows([
            ("Merchants", str(len(by_merchant)), stat_color,
             "Distinct merchants matching the current search, in range.", ""),
            ("Total Spend", _fmt(total_spend_all), row_colors["Spend"],
             "Actual Spend summed across all matching merchants.", ""),
            ("Avg per Merchant", _fmt(total_spend_all / len(by_merchant)), stat_color,
             "Total Spend divided by the number of matching merchants.", ""),
            ("Top Merchant", top_row["Merchant"], stat_color,
             f"{top_row['Merchant']}: {_fmt(top_row['total_spend'])} across "
             f"{int(top_row['transactions'])} transaction(s).", ""),
        ])

        top_n = len(by_merchant) if top_n_choice == "All" else min(top_n_choice, len(by_merchant))
        top_chart = by_merchant.head(top_n).sort_values(rank_col)
        chart_title = f"Merchants by {rank_by}" if top_n >= len(by_merchant) else f"Top {top_n} Merchants by {rank_by}"
        axis_label = "Total Spend (£)" if rank_col == "total_spend" else "Transactions (#)"
        color_scale = "Reds" if rank_col == "total_spend" else "Blues"
        fig_m = px.bar(
            top_chart, x=rank_col, y="Merchant", orientation="h",
            title=chart_title,
            labels={rank_col: axis_label, "Merchant": ""},
            color=rank_col, color_continuous_scale=color_scale,
            range_color=_colour_range(top_chart[rank_col]),
        )
        if rank_col == "total_spend":
            fig_m.update_traces(hovertemplate="%{y}: £%{x:,.2f}<extra></extra>")
            fig_m.update_layout(xaxis_tickprefix="£", xaxis_tickformat=",.0f")
        else:
            fig_m.update_traces(hovertemplate="%{y}: %{x} transactions<extra></extra>")
        fig_m.update_layout(
            showlegend=False, coloraxis_showscale=False,
            height=max(400, top_n * 30 + 80),
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
            height=min(35 * (len(merch_display) + 1) + 2, 640),
            column_config={
                "Total Spend": st.column_config.NumberColumn("Total Spend", format="£%,.2f"),
                "Avg Transaction": st.column_config.NumberColumn("Avg Transaction", format="£%,.2f"),
                "Last Seen": st.column_config.DateColumn("Last Seen", format="YYYY-MM-DD"),
            },
        )
