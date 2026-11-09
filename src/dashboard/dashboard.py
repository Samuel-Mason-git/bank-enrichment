"""The dashboard's entry point: `streamlit run src/dashboard/dashboard.py`.

Loads the transactions, draws the sidebar's filters (shared by every page),
then runs the page picked in the navigation. Each page is its own script in
views/ and gets the filtered data through page_context.shared(). Only the open
page runs on each click, rather than every tab's code as before."""
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))
# The pipeline's database library: the dashboard reads the same database the same way.
sys.path.insert(0, str(Path(__file__).parent.parent / "pipeline"))
# The server's rule matching (check_rules), so the Rules page judges a match
# exactly as the webhook does. Last on the path: nothing there shadows ours.
sys.path.append(str(Path(__file__).parent.parent / "server"))
load_dotenv(Path(__file__).parent.parent.parent / "config" / ".env")
# Changes made here are recorded as the dashboard's (settings_changes).
os.environ.setdefault("DB_APP_NAME", "dashboard")

from database_functions import init_db, get_con
from dashboard_helpers import merchant_display_name
from page_context import share

st.set_page_config(
    page_title="Bank Enrichment",
    page_icon="💳",
    layout="wide",
)


# ── Connection ─────────────────────────────────────────────────────────────────

init_db()

# ── Load data ──────────────────────────────────────────────────────────────────

@st.cache_data(ttl=60, show_spinner="Loading transactions...")
def load_transactions():
    result = get_con().execute("""
        SELECT
            id, amount, description, monzo_category,
            merchant_name, counterparty_name,
            created_at, user_context, skipped,
            llm_category, llm_subcategory, role
        FROM transaction_roles
        ORDER BY created_at DESC
    """)
    cols = [c.name for c in result.description]
    df = pd.DataFrame(result.fetchall(), columns=cols)
    # Postgres NUMERIC arrives as Decimal, which pandas keeps as plain objects:
    # Plotly then reads amounts as labels, not numbers (a bar chart's colour
    # scale came out as one random colour per bar). Floats are plenty for £.
    df["amount"] = df["amount"].astype(float)
    df["created_at"] = pd.to_datetime(df["created_at"])
    df["month"] = df["created_at"].dt.to_period("M").astype(str)
    return df


@st.cache_data(ttl=60, show_spinner="Loading taxonomy...")
def load_taxonomy():
    parents = get_con().execute("SELECT name FROM parent_categories WHERE archived_at IS NULL ORDER BY name").fetchall()
    subs = get_con().execute("""
        SELECT s.name, p.name AS parent_name
        FROM subcategories s
        JOIN parent_categories p ON p.id = s.parent_id
        WHERE s.archived_at IS NULL AND p.archived_at IS NULL
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

# When the preset changes, push the new dates into the date inputs' session state
# keys so they actually update (date_input ignores value= after first render)
preset_from, preset_to = presets[preset]
# Clamped into [min_date, max_date] on BOTH ends, not just the obvious one:
# "This month"/"YTD" compute preset_from from today's real-world date, which
# silently drifts past max_date (the latest transaction actually on record)
# every time the calendar rolls into a new month/year before that month's
# first transaction has landed -- date_input then rejects a default value
# outside its own min/max and crashes the whole dashboard.
preset_from = min(max(preset_from, min_date), max_date)
preset_to = max(min(preset_to, max_date), min_date)

if st.session_state.get("_last_preset") != preset:
    st.session_state["_last_preset"] = preset
    st.session_state["date_from"] = preset_from
    st.session_state["date_to"] = preset_to

if st.sidebar.button("Reset date range", help="Clear any custom dates typed below and snap back to the Quick Date Filter selected above."):
    st.session_state["date_from"] = preset_from
    st.session_state["date_to"] = preset_to

# Two separate boxes rather than one combined-range date_input: Streamlit's range
# picker can visually close or reset after the first click, before a second date
# is chosen, which looks exactly like the whole filter breaking. Plain date_input
# doesn't have that failure mode, at the cost of not enforcing start <= end itself
# -- handled below by sorting rather than fighting the widget over it.
col_from, col_to = st.sidebar.columns(2)
picked_from = col_from.date_input("Start date", key="date_from", min_value=min_date, max_value=max_date)
picked_to = col_to.date_input("End date", key="date_to", min_value=min_date, max_value=max_date)
date_from, date_to = min(picked_from, picked_to), max(picked_from, picked_to)

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
        # Direct debits and bank transfers -- rent, utility bills, HMRC-style
        # payments -- populate counterparty_name instead of merchant_name, so
        # without this a search for "Octopus Energy" or "HMRC" finds nothing.
        | filtered["counterparty_name"].str.contains(search, case=False, na=False, regex=False)
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
        | prev_filtered["counterparty_name"].str.contains(search, case=False, na=False, regex=False)
        | prev_filtered["user_context"].str.contains(search, case=False, na=False, regex=False)
    )
    prev_filtered = prev_filtered[_mask]
if exclude_merchants:
    prev_filtered = prev_filtered[~merchant_display_name(prev_filtered).isin(exclude_merchants)]
if exclude_ids:
    prev_filtered = prev_filtered[~prev_filtered["id"].isin(exclude_ids)]



# ── Pages ──────────────────────────────────────────────────────────────────────

share(df=df, filtered=filtered, prev_filtered=prev_filtered, all_parents=all_parents, all_subs=all_subs,
      subs_by_parent=subs_by_parent, date_from=date_from, date_to=date_to)

_views = Path(__file__).parent / "views"
# Grouped, so the top bar holds three menus rather than a dozen tabs.
page = st.navigation({
    "Your money": [
        st.Page(_views / "overview.py", title="Overview", default=True),
        st.Page(_views / "over_time.py", title="Over Time"),
        st.Page(_views / "transactions.py", title="Transactions"),
        st.Page(_views / "drill_down.py", title="Category Drill-Down"),
        st.Page(_views / "merchants.py", title="Top Merchants"),
        st.Page(_views / "subscriptions.py", title="Subscriptions"),
    ],
    "Manage": [
        st.Page(_views / "queue.py", title="Queue"),
        st.Page(_views / "payment.py", title="Payment details"),
        st.Page(_views / "rules.py", title="Rules"),
        st.Page(_views / "settings.py", title="Settings"),
    ],
    "Behind the scenes": [
        st.Page(_views / "activity.py", title="Activity"),
        st.Page(_views / "ai.py", title="AI usage"),
    ],
}, position="top")

st.title("💳 Bank Enrichment")
page.run()
