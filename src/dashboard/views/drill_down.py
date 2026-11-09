"""Category Drill-Down: one category's subcategories, merchants and payments."""
import plotly.express as px
import streamlit as st
from dashboard_helpers import merchant_display_name, role_breakdown
from ui import _fmt, _colour_range, _card_rows, row_colors
from page_context import shared

filtered, = shared('filtered')


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

    # "saving"/"investment" net their two sides together here too (same
    # pot/account cycling, matching how Overview computes these same
    # totals). "transfer"/"excluded" stay direction-restricted (outgoing
    # only, incoming as its own card below) since that bucket can mix
    # genuinely unrelated inbound and outbound movements.
    role_sign = {"income": "positive", "spend": "negative", "investment": "negative",
                 "saving": "negative", "transfer": "negative", "excluded": "negative"}
    income_cat = cat_df.loc[(cat_df["role"] == "income") & (cat_df["amount"] > 0), "amount"].sum()
    spend_cat = abs(cat_df.loc[(cat_df["role"] == "spend") & (cat_df["amount"] < 0), "amount"].sum())
    invested_cat = -cat_df.loc[cat_df["role"] == "investment", "amount"].sum()
    saved_cat = -cat_df.loc[cat_df["role"] == "saving", "amount"].sum()
    excluded_cat = abs(cat_df.loc[cat_df["role"].isin(["transfer", "excluded"]) & (cat_df["amount"] < 0), "amount"].sum())
    net_cat = income_cat - spend_cat

    # Transferred-Excluded only shows its outgoing side above -- but it
    # can also carry an incoming side under the same role (e.g. an
    # inbound transfer/refund), which may be unrelated money rather than
    # the same pot/account cycling. Surface it as its own card whenever
    # it's nonzero so the cards never disagree with what the charts are
    # showing. Invested/Saved don't need this -- they're already netted
    # above.
    excluded_in_cat = cat_df.loc[cat_df["role"].isin(["transfer", "excluded"]) & (cat_df["amount"] > 0), "amount"].sum()

    stat_color = "rgba(127, 140, 141, 0.16)"
    _card_rows([
        ("Transactions", str(len(cat_df)), stat_color,
         "Transactions in this category, in the selected range.", ""),
        ("Subcategories", str(int(cat_df["llm_subcategory"].nunique())), stat_color,
         "Distinct subcategories used within this category.", ""),
        ("Net", _fmt(net_cat), row_colors["Net"],
         "Actual Income minus Actual Spend for this category.", ""),
    ], per_row=3)

    # (display label, color key into row_colors, value, tooltip)
    excluded_label = "Transferred / Excluded (Outgoing)" if excluded_in_cat > 0 else "Transferred / Excluded"
    role_cards = [
        ("Income", "Income", income_cat,
         "Every transaction in this category whose role is Income (incoming side only)."),
        ("Spend", "Spend", spend_cat,
         "Every transaction in this category whose role is Spend (outgoing side only)."),
        ("Invested", "Invested", invested_cat,
         "Net money moved into investment categories here (contributions minus withdrawals)."),
        ("Saved", "Saved", saved_cat,
         "Net money moved into savings categories here (deposits minus withdrawals)."),
        (excluded_label, "Transferred / Excluded", excluded_cat,
         "Money moved to transfer or excluded categories here (outgoing side only)."),
    ]
    if excluded_in_cat > 0:
        role_cards.append(("Transferred / Excluded (Incoming)", "Transferred / Excluded", excluded_in_cat,
            "Money that came in through transfer or excluded categories here (e.g. a refund or an inbound "
            "transfer) -- kept separate so it can't cancel out the outgoing figure."))

    # Income and Spend always (a category is one or the other, and £0 says
    # so); the rest only when this category actually has some, rather than
    # a row of £0.00 cards for roles it doesn't use.
    role_cards = [c for c in role_cards if c[0] in ("Income", "Spend") or c[2]]
    _card_rows([(label, _fmt(value), row_colors[color_key], tooltip, "")
                for label, color_key, value, tooltip in role_cards])

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
                   "saving": "Saved", "transfer": "Transferred", "excluded": "Excluded"}
    role_colors = {"income": "Greens", "spend": "Reds", "investment": "Purples",
                   "saving": "Teal", "transfer": "Blues", "excluded": "Greys"}
    bidirectional = {"income": False, "spend": False, "investment": True,
                      "saving": True, "transfer": True, "excluded": True}
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

    # At most two charts side by side, so a category with three or four
    # roles doesn't squeeze each into a sliver.
    for start in range(0, len(chart_specs), 2):
        row = chart_specs[start:start + 2]
        chart_cols = st.columns(2 if len(chart_specs) > 1 else 1)
        for col, (role, sign, chart_label) in zip(chart_cols, row):
            with col:
                breakdown = role_breakdown(
                    cat_df, (role,), "llm_subcategory", sign=sign
                ).sort_values("Amount", ascending=True)
                fig = px.bar(breakdown, x="Amount", y="Subcategory", orientation="h",
                             title=f"{chart_label} by Subcategory",
                             labels={"Amount": f"{chart_label} (£)", "Subcategory": ""},
                             color="Amount", color_continuous_scale=role_colors[role],
                             range_color=_colour_range(breakdown["Amount"]))
                fig.update_traces(hovertemplate="%{y}: £%{x:,.2f}<extra></extra>")
                fig.update_layout(showlegend=False, coloraxis_showscale=False,
                                  height=max(250, len(breakdown) * 34 + 80),
                                  xaxis_tickprefix="£", xaxis_tickformat=",.0f",
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
        # Numbers stay numbers (so the columns sort properly), formatted
        # in the table; a role a subcategory has none of shows blank.
        summary_columns = {
            "Avg Transaction": st.column_config.NumberColumn("Avg Transaction", format="£%,.2f"),
            "Last Transaction": st.column_config.DateColumn("Last Transaction", format="YYYY-MM-DD"),
            share_label: st.column_config.NumberColumn(share_label, format="%.1f%%"),
        }
        for _, _, chart_label in chart_specs:
            sub_summary[chart_label] = sub_summary[chart_label].where(sub_summary[chart_label] > 0)
            summary_columns[chart_label] = st.column_config.NumberColumn(chart_label, format="£%,.2f")

        st.subheader("Subcategory Summary")
        st.dataframe(sub_summary, width="stretch", hide_index=True, column_config=summary_columns)

    st.subheader("Transactions")
    display_cat = cat_df[["created_at", "amount", "merchant_name", "description",
                           "user_context", "llm_subcategory"]].copy()
    # merchant_name is blank for direct debits/bank transfers, which
    # populate counterparty_name instead -- without the fallback this
    # column reads empty for every bill paid that way.
    display_cat["merchant_name"] = merchant_display_name(cat_df)
    display_cat.columns = ["Date", "Amount", "Merchant / payee", "Description", "Context", "Subcategory"]
    st.dataframe(
        display_cat, width="stretch", hide_index=True,
        height=min(35 * (len(display_cat) + 1) + 2, 640),
        column_config={
            "Date": st.column_config.DateColumn("Date", format="YYYY-MM-DD"),
            "Amount": st.column_config.NumberColumn("Amount", format="£%,.2f"),
        },
    )
