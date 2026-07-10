"""Pure, Streamlit-free logic used by dashboard.py — kept separate so it can
be unit tested directly without spinning up a Streamlit app or a database."""

import pandas as pd

FREQ_MONTHLY = {"weekly": 52 / 12, "fortnightly": 26 / 12, "monthly": 1, "annual": 1 / 12}

ROLE_METRICS = {
    "Spend": ("spend",),
    "Income": ("income",),
    "Invested": ("investment",),
    "Transferred / Excluded": ("transfer", "excluded"),
}


def role_breakdown(df: pd.DataFrame, roles: tuple, group_col: str, by_month: bool = False,
                    sign: str = "any") -> pd.DataFrame:
    """Rows whose role is in `roles`, grouped by group_col ('llm_category' or
    'llm_subcategory') and optionally also by month, summed as absolute £
    amounts. Missing group_col values become 'Unclassified'. Sorted highest
    total first unless grouped by month, where month order is kept instead.

    sign="negative"/"positive" restricts to outgoing/incoming transactions
    before summing -- roles like "transfer" are bidirectional (an Inbound and
    an Outbound Transfer subcategory can both carry that role), so summing
    every sign together would let them cancel out instead of reporting a
    clean, single-direction total."""
    label = "Subcategory" if group_col == "llm_subcategory" else "Category"
    subset = df[df["role"].isin(roles)]
    if sign == "negative":
        subset = subset[subset["amount"] < 0]
    elif sign == "positive":
        subset = subset[subset["amount"] > 0]
    subset = subset.copy()
    if subset.empty:
        return pd.DataFrame(columns=(["Month", label, "Amount"] if by_month else [label, "Amount"]))
    subset[group_col] = subset[group_col].fillna("Unclassified")
    group_cols = ["month", group_col] if by_month else [group_col]
    result = (
        subset.groupby(group_cols)["amount"]
        .sum().abs().reset_index()
        .rename(columns={group_col: label, "amount": "Amount", "month": "Month"})
    )
    if not by_month:
        result = result.sort_values("Amount", ascending=False).reset_index(drop=True)
    return result


def pct_delta(current: float, previous: float) -> float | None:
    if previous == 0:
        return None
    return round((current - previous) / abs(previous) * 100, 1)


def merchant_display_name(df: pd.DataFrame) -> pd.Series:
    """The best available payee identifier for each transaction --
    merchant_name first, falling back to counterparty_name. Many direct
    debits and bank transfers never populate merchant_name at all (e.g. a
    subscription paid by direct debit rather than card), so relying on
    merchant_name alone silently drops those transactions from any
    merchant-based grouping or matching."""
    if "counterparty_name" in df.columns:
        return df["merchant_name"].fillna(df["counterparty_name"])
    return df["merchant_name"]


def detect_subscriptions(
    df: pd.DataFrame, confirmed_names: set, reference_date: pd.Timestamp | None = None
) -> list[dict]:
    """reference_date defaults to now. Two occurrences spaced ~7 days apart is
    weak evidence of a real weekly habit -- it's just as consistent with two
    coincidental one-off visits -- so faster cadences require more supporting
    occurrences before being suggested. A candidate is also only suggested if
    its most recent transaction is recent enough for its detected cadence: a
    "weekly" pattern whose last hit was months ago is a lapsed one-off, not
    something to suggest adding as an active subscription right now."""
    if reference_date is None:
        reference_date = pd.Timestamp.now()
    min_occurrences = {"weekly": 4, "fortnightly": 4, "monthly": 3, "annual": 2}
    candidates = []
    spend = df[(df["amount"] < 0) & (df["skipped"] != True)].copy()
    spend["merchant_display"] = merchant_display_name(spend)
    spend = spend[spend["merchant_display"].notna()]
    for merchant, group in spend.groupby("merchant_display"):
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
        last_seen_days = (reference_date - dates.max()).days
        for freq, target, tolerance in [
            ("weekly", 7, 2), ("fortnightly", 14, 3),
            ("monthly", 30, 6), ("annual", 365, 30),
        ]:
            if len(dates) < min_occurrences[freq]:
                continue
            if abs(mean_gap - target) <= tolerance and cv < 0.4 and last_seen_days <= target * 2:
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


def sanitize_classification_edit(new_category: str | None, new_subcategory: str | None) -> tuple[str | None, str | None]:
    """A subcategory can't exist without a parent category — if the category
    is cleared, the subcategory must be cleared too, otherwise the saved
    label would be invisible to every category-based view (Overview,
    Category Drill-Down), which all group/filter by category first."""
    if not new_category:
        return None, None
    return new_category, (new_subcategory or None)


def should_deactivate_subscription(sub: dict, df: pd.DataFrame, cutoff: pd.Timestamp) -> bool:
    """A subscription auto-deactivates if no matching spend transaction has
    landed since `cutoff`. Matches by literal substring — NOT regex, since
    merchant/subscription names are free text and may contain characters
    (parentheses, periods, etc.) that would otherwise be misinterpreted as
    regex syntax or silently produce wrong matches. Matches against
    merchant_name OR counterparty_name (see merchant_display_name) -- a real,
    still-active subscription paid via a counterparty-only transaction would
    otherwise never find a match and get auto-deactivated every render."""
    match_name = sub["merchant_name"] or sub["name"]
    display = merchant_display_name(df)
    last_txn = df[
        display.str.contains(match_name, case=False, na=False, regex=False) &
        (df["amount"] < 0)
    ]["created_at"].max()
    return pd.isna(last_txn) or last_txn < cutoff
