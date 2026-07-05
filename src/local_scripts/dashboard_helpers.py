"""Pure, Streamlit-free logic used by dashboard.py — kept separate so it can
be unit tested directly without spinning up a Streamlit app or a database."""

import pandas as pd

FREQ_MONTHLY = {"weekly": 52 / 12, "fortnightly": 26 / 12, "monthly": 1, "annual": 1 / 12}


def pct_delta(current: float, previous: float) -> float | None:
    if previous == 0:
        return None
    return round((current - previous) / abs(previous) * 100, 1)


def detect_subscriptions(df: pd.DataFrame, confirmed_names: set) -> list[dict]:
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
    regex syntax or silently produce wrong matches."""
    match_name = sub["merchant_name"] or sub["name"]
    last_txn = df[
        df["merchant_name"].str.contains(match_name, case=False, na=False, regex=False) &
        (df["amount"] < 0)
    ]["created_at"].max()
    return pd.isna(last_txn) or last_txn < cutoff
