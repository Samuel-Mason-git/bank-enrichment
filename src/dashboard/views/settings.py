"""Settings, in tabs: categories and their roles, the unusual-payment alerts, the Ask page's
daily limit, backups, and the danger zone."""
import pandas as pd
import app_settings
import streamlit as st
from pathlib import Path
from database_functions import (
    BACKUPS_KEPT, ROLES, BackupDirError, backup_db, backup_dir, backup_size_bytes,
    backup_zip, default_backup_dir,
    delete_backup, delete_parent, reset_backup_dir, set_backup_dir,
    delete_subcategory, edit_subcategory, get_parents, get_subcategories, list_backups, move_all_transactions,
    read_backup_manifest, rename_parent, set_parent_role, set_subcategory_role, upsert_parent,
    upsert_subcategory, wipe_parent_labels, wipe_taxonomy,
)
from dashboard_helpers import (
    backup_coverage, backup_label, backup_reason, format_bytes, merchant_display_name,
)
from ui import _query, _card_rows
import finance_agent as fa
import alert_settings as AS
import app_settings
import app_config
import ask_settings as ASK
import classifier_settings as CLS
import budget_messages
import budgets as bg
from page_context import shared

df, = shared('df')


st.caption("Categories, alerts, the Ask limit, backups and the danger zone, each in its own tab.")

INHERIT = "(inherit)"
# Two queries for the whole tab: every count below comes from these,
# rather than one query per category (each of which used to rebuild every
# transaction, so this tab alone took most of a minute on every rerun).
_all_parents = get_parents()
_all_subs = get_subcategories()
role_by_parent_id = {p["id"]: p["role"] for p in _all_parents}
sub_role_info = {s["id"]: s for s in _all_subs}

# Session state for right-panel mode
for _k in ("tax_edit_sub", "tax_view_sub", "tax_edit_parent", "tax_del_parent", "tax_wipe_parent", "tax_del_sub"):
    if _k not in st.session_state:
        st.session_state[_k] = None

# Every live parent with its counts (used by both columns), busiest first.
subs_by_parent_id: dict[int, list[dict]] = {}
for s in _all_subs:  # already busiest-first within each parent
    subs_by_parent_id.setdefault(s["parent_id"], []).append(
        {"id": s["id"], "name": s["name"], "txn_count": s["transaction_count"]})
tax_parents = [
    {"id": p["id"], "name": p["name"], "txn_count": p["transaction_count"],
     "sub_count": len(subs_by_parent_id.get(p["id"], []))}
    for p in _all_parents
]
tax_parent_names = [p["name"] for p in tax_parents]

def _alert_labels(th: dict) -> dict:
    """The instant alerts, named with the thresholds in use."""
    return {
        "early": f"Halfway ({round(th['early'] * 100)}% used)",
        "near": f"Nearly used up ({round(th['near'] * 100)}% used)",
        "over": "Over budget",
        "pace": f"Off pace (from day {th['pace_from']}, heading for {round(th['pace_over'] * 100)}% of the budget)",
        "week": (f"This week over £{th['week_limit']:,.0f}" if th["week_limit"] else "This week over a limit (set below)"),
    }


def _budget_telegram_settings(mb_now: bg.MonthBudgets) -> None:
    st.caption("Sent by the worker after its runs. Its first run each day is at 08:00, so a morning message is "
               "there when you wake up. Each alert comes at most once per category per month. Nothing is sent "
               "while there are no budgets.")
    current = bg.digest_settings()
    with st.form("budget_telegram_form"):
        mode = st.selectbox("How often to get the budget message (text and a chart of bars)",
                            list(bg.DIGEST_MODES), format_func=bg.DIGEST_MODES.get,
                            index=list(bg.DIGEST_MODES).index(current["mode"]), key="budget_mode")
        days, monthday = [], current["monthday"]
        if mode == "days":
            day_names = st.multiselect("On these days", bg.WEEKDAYS,
                                       default=[bg.WEEKDAYS[d] for d in current["days"]] or [bg.WEEKDAYS[0]],
                                       key="budget_days")
            days = [bg.WEEKDAYS.index(n) for n in day_names]
        elif mode == "monthly":
            monthday = st.number_input("On this day of the month (1 to 28, e.g. your payday)", min_value=1,
                                       max_value=28, value=int(current["monthday"]), step=1, key="budget_monthday")
        th = bg.thresholds()
        labels = _alert_labels(th)
        wanted = st.multiselect("Instant alerts", list(labels), default=sorted(bg.alerts_wanted()),
                                format_func=labels.get, key="budget_alert_kinds")
        c1, c2, c3 = st.columns(3)
        early_pct = c1.number_input("Halfway warning at (%)", min_value=10, max_value=90,
                                    value=round(th["early"] * 100), step=5, key="budget_early")
        near_pct = c2.number_input("Nearly used up at (%)", min_value=50, max_value=100,
                                   value=round(th["near"] * 100), step=5, key="budget_near")
        pace_over = c3.number_input("Off pace when heading for (% of budget)", min_value=100, max_value=200,
                                    value=round(th["pace_over"] * 100), step=5, key="budget_pace_over")
        c4, c5 = st.columns(2)
        pace_from = c4.number_input("Pace alerts start on day", min_value=1, max_value=28,
                                    value=int(th["pace_from"]), step=1, key="budget_pace_from")
        week_limit = c5.number_input("Weekly limit across the budgets (£, 0 = off)", min_value=0,
                                     max_value=100_000, value=int(th["week_limit"]), step=10, key="budget_week_limit")
        q1, q2 = st.columns(2)
        quiet_from = q1.selectbox("Quiet from", list(range(24)), index=int(th["quiet_from"]),
                                  format_func=lambda h: f"{h:02d}:00", key="budget_quiet_from")
        quiet_to = q2.selectbox("Until", list(range(24)), index=int(th["quiet_to"]),
                                format_func=lambda h: f"{h:02d}:00", key="budget_quiet_to")
        st.caption("Alerts wait during quiet hours and go out at the first run after them. The worker runs "
                   "08:00 to 22:00, so a 22:00 to 08:00 window holds them until morning. The same hour for both "
                   "means no quiet hours.")
        budgeted = {s_.parent_id: s_.category for s_ in mb_now.statuses}
        muted_now = bg.muted_parents()
        muted_names = st.multiselect("Categories with no alerts", list(budgeted.values()),
                                     default=[budgeted[p] for p in budgeted if p in muted_now],
                                     key="budget_muted")
        if st.form_submit_button("Save", type="primary"):
            try:
                bg.set_digest(mode, days=days, monthday=int(monthday))
                bg.set_alerts_wanted(wanted)
                bg.set_muted_parents([p for p, name in budgeted.items() if name in muted_names])
                for setting, value in [(bg.EARLY_PCT, early_pct), (bg.NEAR_PCT, near_pct),
                                       (bg.PACE_OVER_PCT, pace_over), (bg.PACE_FROM, pace_from),
                                       (bg.WEEK_LIMIT, week_limit), (bg.QUIET_FROM, quiet_from),
                                       (bg.QUIET_TO, quiet_to)]:
                    app_settings.save(setting, int(value))
                st.success("Saved: " + bg.DIGEST_MODES[mode].lower()
                           + (f" ({', '.join(bg.WEEKDAYS[d] for d in sorted(days))})" if mode == "days" else "")
                           + (f" on day {int(monthday)}" if mode == "monthly" else "")
                           + ". Alerts: " + (", ".join(labels[k].lower() for k in wanted) or "none") + ".")
            except (bg.BudgetError, ValueError) as e:
                st.error(str(e))
    if st.button("Send me the budget message now", key="budget_send_now", disabled=not mb_now.statuses):
        if budget_messages.send_digest(mb_now):
            st.success("Sent to Telegram. It doesn't count as today's message, so the usual one still comes.")
        else:
            st.error("Telegram didn't take it: check TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in config/.env.")


tab_categories, tab_storage, tab_classification, tab_budgets, tab_alerts, tab_ask, tab_backups, tab_danger = st.tabs(
    ["Categories", "Storage", "Classification", "Budgets", "Alerts", "Ask", "Backups", "Danger zone"]
)

with tab_categories:
    settings_stat_color = "rgba(127, 140, 141, 0.16)"
    # Skipped transactions are never classified, so they're counted apart:
    # "Unclassified" is what's still waiting, the same as Overview's count.
    _not_skipped = df[df["skipped"] != True] if not df.empty else df
    total_txns = len(_not_skipped)
    total_classified = int(_not_skipped["llm_category"].notna().sum()) if total_txns else 0
    total_subs_all = sum(p["sub_count"] for p in tax_parents)
    classified_pct = (total_classified / total_txns * 100) if total_txns else 0.0

    _card_rows([
        ("Parent Categories", str(len(tax_parents)), settings_stat_color,
         "Total parent categories in your taxonomy.", ""),
        ("Subcategories", str(total_subs_all), settings_stat_color,
         "Total subcategories across all parent categories.", ""),
        # One decimal: 793 of 794 is 99.9%, not a "100%" beside "1 unclassified".
        ("Classified", f"{classified_pct:.1f}%", settings_stat_color,
         f"{total_classified} of {total_txns} transactions (not counting skipped ones) have a category.", ""),
        ("Unclassified", str(total_txns - total_classified), settings_stat_color,
         "Transactions not skipped and with no category yet.", ""),
    ])


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

            subs_for_parent = subs_by_parent_id.get(pid, [])

            active_sub_ids = [
                s["id"] for s in subs_for_parent
                if s["id"] in (st.session_state.tax_edit_sub, st.session_state.tax_view_sub, st.session_state.tax_del_sub)
            ]
            is_active_parent = pid in (st.session_state.tax_edit_parent, st.session_state.tax_del_parent, st.session_state.tax_wipe_parent)

            current_role = role_by_parent_id.get(pid, "spend")
            label = (f"**{pname}** · {parent['txn_count']} transactions · "
                     f"{parent['sub_count']} subcategories · counts as {current_role}")
            with st.expander(label, expanded=bool(active_sub_ids or is_active_parent)):

                new_role = st.selectbox(
                    "Role", ROLES, index=ROLES.index(current_role), key=f"role_{pid}",
                    help="What this category counts as for Income/Spend/Investment totals "
                         "on the Overview page and in the weekly/monthly Telegram summaries."
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
                        sc1, sc_role, sc2, sc3, sc4 = st.columns([3, 2, 1, 1, 1], vertical_alignment="center")
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
                                # Transactions point at the id, so they follow on their own.
                                rename_parent(pid, stripped)
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
                        delete_parent(pid)
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
                        wipe_parent_labels(pid)
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
            sub_rows = [s for s in _all_subs if s["id"] == sid]
            if sub_rows:
                sub = {**sub_rows[0], "txn_count": sub_rows[0]["transaction_count"]}
                st.subheader(f"Delete: {sub['name']}")
                st.warning(
                    f"This will permanently delete **{sub['name']}** (under {sub['parent_name']}) "
                    f"and clear labels from **{sub['txn_count']} transaction(s)**."
                )
                c1, c2 = st.columns(2)
                with c1:
                    if st.button("Confirm delete", type="primary", key="confirm_del_sub"):
                        delete_subcategory(sid)
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
                WHERE s.id = %s
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
                        # constraint violation instead of a friendly message.
                        collision = (name_changed or parent_changed) and _query(
                            "SELECT 1 AS x FROM subcategories WHERE name = %s AND parent_id = %s AND id != %s AND archived_at IS NULL",
                            [effective_name, target_parent_id, sid],
                        )
                        if collision:
                            st.error(
                                f'"{effective_name}" already exists under {new_sparent} — '
                                f"choose a different name, or delete/merge the existing one first."
                            )
                        else:
                            if name_changed or parent_changed:
                                # Its transactions follow on their own: they point at the id.
                                edit_subcategory(sid, effective_name, target_parent_id)
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
                WHERE s.id = %s
            """, [sid])
            if sub_rows:
                sub = sub_rows[0]
                st.subheader(f"Transactions: {sub['name']}")
                st.caption(f"Under {sub['parent_name']}")

                reassign_raw = _query("""
                    SELECT s.id, s.name, p.name AS parent_name
                    FROM subcategories s
                    JOIN parent_categories p ON p.id = s.parent_id
                    WHERE s.id != %s AND s.archived_at IS NULL AND p.archived_at IS NULL
                    ORDER BY p.name, s.name
                """, [sid])
                reassign_map = {f"{s['name']} ({s['parent_name']})": s for s in reassign_raw}

                if reassign_map:
                    with st.form("reassign_all_form"):
                        target_key = st.selectbox("Move all transactions to:", list(reassign_map.keys()))
                        if st.form_submit_button("Move all", type="secondary"):
                            move_all_transactions(sid, reassign_map[target_key]["id"])
                            st.session_state.tax_view_sub = None
                            st.cache_data.clear()
                            st.rerun()

                st.divider()

                txns = _query("""
                    SELECT id, description, merchant_name, counterparty_name, amount, created_at, user_context
                    FROM transactions
                    WHERE subcategory_id = %s
                    ORDER BY created_at DESC
                """, [sid])

                if txns:
                    txns_df = pd.DataFrame(txns)
                    txns_df["amount"] = txns_df["amount"].astype(float)
                    txns_df["created_at"] = pd.to_datetime(txns_df["created_at"])
                    # merchant_name is blank for direct debits/bank transfers,
                    # which populate counterparty_name instead -- a subcategory
                    # dominated by bills would otherwise show an empty Merchant
                    # column for nearly every row here.
                    txns_df["merchant_name"] = merchant_display_name(txns_df)
                    txns_df = txns_df.rename(columns={
                        "created_at": "Date", "merchant_name": "Merchant / payee",
                        "description": "Description", "amount": "Amount", "user_context": "Context",
                    })
                    st.dataframe(
                        txns_df[["Date", "Merchant / payee", "Description", "Amount", "Context"]],
                        width="stretch",
                        hide_index=True,
                        column_config={
                            "Date": st.column_config.DateColumn("Date", format="YYYY-MM-DD"),
                            "Amount": st.column_config.NumberColumn("Amount", format="£%,.2f"),
                        },
                    )
                    st.caption(f"{len(txns)} transaction(s)")
                else:
                    st.info("No transactions assigned to this subcategory.")

@st.cache_data(show_spinner=False)
def _backup_zip_cached(path: str, modified: float) -> bytes:
    """A backup as a zip, built once per backup. The modification time is in the key,
    so a backup that changes is zipped again; a rerun of the page doesn't zip it."""
    return backup_zip(Path(path))


with tab_storage:
    import export_destinations as ed
    import setup_wizard as sw_
    st.caption("Where your data is kept. These are set in config/.env, by the setup command. This page shows them and tests them.")
    st.subheader("Live database")
    st.write(f"This page reads it, so it's connected. Host `{app_config.get('DB_HOST', '')}`, "
             f"port `{app_config.get('DB_PORT', '5432')}`, database `{app_config.get('DB_NAME', '')}`. "
             "Its password isn't shown.")
    st.subheader("Bronze copy")
    st.caption("An optional raw save of everything the app receives, as plain files. The app runs without it.")
    dest = ed.load()
    info = ed.describe(dest)
    if info["kind"] == "none":
        st.info("Off. Set BRONZE_DESTINATION in config/.env, or run the setup command, to turn it on.")
    else:
        where = info["folder"] if info["kind"] == "folder" else f"{info['bucket']}/{info['prefix']} at {info['endpoint']}"
        st.write(f"Kind: **{info['kind']}**. Where: `{where}`. Keeps the newest {info['keep']} copies.")
        st.write(f"Last copy: {info['last_day'] or 'none yet'}.")
        if st.button("Test the bronze copy", key="storage_test_bronze"):
            try:
                sw_.check_destination(ed.as_answers(dest))
            except sw_.SetupError as e:
                st.error(str(e))
            else:
                st.success("The destination takes a copy.")
    st.subheader("Silver")
    st.caption("Not in use yet. When it is, it lives in a schema in the live database, next to the transactions.")

with tab_classification:
    st.subheader("Classification")
    st.caption(
        "The model that places each payment in a category (passes 1 to 3). The match against existing "
        "categories and the placement judge always use Haiku. Check a different model against your own "
        "history before you rely on it: model_eval.py compares them."
    )
    c_models = list(CLS.MODELS)
    c_saved = app_settings.get(CLS.MODEL)
    c_model = st.selectbox("Model for placing payments", c_models, index=c_models.index(c_saved),
                           format_func=CLS.MODELS.get, key="classifier_model")
    if st.button("Save classification settings", key="classifier_save"):
        try:
            app_settings.save(CLS.MODEL, c_model)
        except ValueError as e:
            st.error(str(e))
        else:
            st.success("Saved. The next classification run uses it.")

with tab_budgets:
    st.caption("When you get budget messages and alerts on Telegram. The budgets themselves are set on the Budget page.")
    st.subheader("Telegram messages and alerts")
    _budget_telegram_settings(bg.month_budgets())

with tab_alerts:
    st.caption("The Telegram messages from the hourly checks. Each can be switched off, and tuned here.")

    st.subheader("Unusual payments")
    st.caption(
        "A payment far above what its merchant usually costs. Amounts are compared on a log scale against that "
        "merchant's past payments, so a £3 coffee and a £300 shop are judged by the same measure."
    )
    u_on = st.toggle("Send unusual-payment alerts", value=bool(app_settings.get(AS.UNUSUAL_ON)), key="unusual_on")
    levels = list(AS.LEVELS)
    saved_level = app_settings.get(AS.UNUSUAL_LEVEL)
    u_level = st.radio(
        "How far a payment has to stand out", levels, index=levels.index(saved_level),
        format_func=AS.LEVEL_LABELS.get, horizontal=True, key="unusual_level", disabled=not u_on,
    )
    u_history = st.number_input(
        "Earlier payments to a merchant needed before comparing", min_value=int(AS.UNUSUAL_MIN_HISTORY.low),
        max_value=int(AS.UNUSUAL_MIN_HISTORY.high), value=int(app_settings.get(AS.UNUSUAL_MIN_HISTORY)), step=1,
        key="unusual_history", disabled=not u_on,
    )
    u_gap = st.number_input(
        "And at least this far above the usual (£)", min_value=float(AS.UNUSUAL_MIN_GAP.low),
        max_value=float(AS.UNUSUAL_MIN_GAP.high), value=float(app_settings.get(AS.UNUSUAL_MIN_GAP)), step=1.0,
        format="%.2f", key="unusual_gap", disabled=not u_on,
    )

    st.subheader("Subscriptions")
    st.caption("Checked against the subscriptions on this page, using the payments that come in.")
    p_on = st.toggle("Tell me when a subscription's price changes", value=bool(app_settings.get(AS.SUB_PRICE_ON)),
                     key="sub_price_on")
    p_pounds = st.number_input(
        "A price change counts from (£)", min_value=float(AS.SUB_PRICE_POUNDS.low),
        max_value=float(AS.SUB_PRICE_POUNDS.high), value=float(app_settings.get(AS.SUB_PRICE_POUNDS)), step=0.10,
        format="%.2f", key="sub_price_pounds", disabled=not p_on,
    )
    p_percent = st.number_input(
        "…and from (% of the amount)", min_value=float(AS.SUB_PRICE_PERCENT.low),
        max_value=float(AS.SUB_PRICE_PERCENT.high), value=float(app_settings.get(AS.SUB_PRICE_PERCENT)), step=1.0,
        format="%.1f", key="sub_price_percent", disabled=not p_on,
    )
    m_on = st.toggle("Tell me when a payment is missed", value=bool(app_settings.get(AS.SUB_MISSED_ON)), key="sub_missed_on")
    m_grace = st.number_input(
        "Days late before a missed-payment alert", min_value=int(AS.SUB_MISSED_GRACE.low),
        max_value=int(AS.SUB_MISSED_GRACE.high), value=int(app_settings.get(AS.SUB_MISSED_GRACE)), step=1,
        key="sub_grace", disabled=not m_on,
    )

    if st.button("Save alert settings", key="alerts_save"):
        try:
            app_settings.save(AS.UNUSUAL_ON, bool(u_on))
            app_settings.save(AS.UNUSUAL_LEVEL, u_level)
            app_settings.save(AS.UNUSUAL_MIN_HISTORY, int(u_history))
            app_settings.save(AS.UNUSUAL_MIN_GAP, float(u_gap))
            app_settings.save(AS.SUB_PRICE_ON, bool(p_on))
            app_settings.save(AS.SUB_PRICE_POUNDS, float(p_pounds))
            app_settings.save(AS.SUB_PRICE_PERCENT, float(p_percent))
            app_settings.save(AS.SUB_MISSED_ON, bool(m_on))
            app_settings.save(AS.SUB_MISSED_GRACE, int(m_grace))
        except ValueError as e:
            st.error(str(e))
        else:
            st.success("Saved. Changes apply from the next hourly run.")

with tab_ask:
    st.subheader("Ask")
    st.caption("The agent that answers questions about your money, on the Ask page and on Telegram with /ask.")
    a_page = st.toggle("Ask page on", value=bool(app_settings.get(ASK.PAGE_ON)), key="ask_page_on")
    a_tg = st.toggle("Ask on Telegram (/ask)", value=bool(app_settings.get(ASK.TELEGRAM_ON)), key="ask_tg_on")
    a_models = list(ASK.MODELS)
    a_model = st.selectbox("Model", a_models, index=a_models.index(app_settings.get(ASK.MODEL)),
                           format_func=ASK.MODELS.get, key="ask_model")
    st.caption("A stronger model costs more per question. The daily limit below still applies to either.")
    a_efforts = list(ASK.EFFORTS)
    a_effort = st.radio("How hard it thinks", a_efforts, index=a_efforts.index(app_settings.get(ASK.EFFORT)),
                        format_func=ASK.EFFORTS.get, horizontal=True, key="ask_effort")
    a_chat = st.number_input("Minutes after a question before a Telegram chat starts afresh",
                             min_value=int(ASK.CHAT_MINUTES.low), max_value=int(ASK.CHAT_MINUTES.high),
                             value=int(app_settings.get(ASK.CHAT_MINUTES)), step=5, key="ask_chat_minutes")
    a_prop = st.number_input("Minutes a budget suggestion on Telegram stays open",
                             min_value=int(ASK.PROPOSAL_MINUTES.low), max_value=int(ASK.PROPOSAL_MINUTES.high),
                             value=int(app_settings.get(ASK.PROPOSAL_MINUTES)), step=5, key="ask_proposal_minutes")
    if st.button("Save Ask settings", key="ask_settings_save"):
        try:
            app_settings.save(ASK.PAGE_ON, bool(a_page))
            app_settings.save(ASK.TELEGRAM_ON, bool(a_tg))
            app_settings.save(ASK.MODEL, a_model)
            app_settings.save(ASK.EFFORT, a_effort)
            app_settings.save(ASK.CHAT_MINUTES, int(a_chat))
            app_settings.save(ASK.PROPOSAL_MINUTES, int(a_prop))
        except ValueError as e:
            st.error(str(e))
        else:
            st.success("Saved. A new Telegram chat or question uses the new model; a chat already open keeps its model until it starts afresh.")
    st.divider()

    # ── Ask: the agent's daily limit ───────────────────────────────────────────
    _limit = fa.daily_limit()
    _spent = fa.spent_today()
    with st.expander(f"🤖 Ask — daily limit ${_limit:.2f} (${_spent:.4f} used today)"):
        st.caption(
            "Most the Ask page may spend on Claude in one day (London time), in US dollars as Anthropic "
            "bills. Once today's questions reach it, new ones are refused until midnight. A typical "
            "question costs well under $0.01. Changes apply straight away, even to a chat that's open."
        )
        new_limit = st.number_input("Daily limit (USD)", min_value=0.0, max_value=20.0, step=0.10,
                                    value=float(_limit), format="%.2f", key="ask_daily_limit")
        if st.button("Save limit", key="ask_daily_limit_save"):
            app_settings.save(ASK.DAILY_LIMIT, round(float(new_limit), 2))
            st.success(f"Daily limit set to ${new_limit:.2f}.")
            st.rerun()

with tab_backups:
    # ── Backups ────────────────────────────────────────────────────────────────
    _backups = list_backups()
    with st.expander(
        f"💾 Backups ({len(_backups)})"
        + (f" — last {backup_label(_backups[0])}" if _backups else " — none yet")
    ):
        st.markdown("**Where backups are saved**")
        st.caption(
            "A folder on the machine running the dashboard. On Windows with WSL, a folder on your Windows "
            "drive is /mnt/c/... (for example /mnt/c/Users/you/Backups), and it shows up in Explorer."
        )
        st.write(f"Saving to `{backup_dir()}`")
        loc_col, save_col, default_col = st.columns([4, 1, 1])
        loc = loc_col.text_input("Folder", key="backup_dir_input", placeholder=str(default_backup_dir()),
                                 label_visibility="collapsed")
        if save_col.button("Save folder", key="backup_dir_save"):
            try:
                saved = set_backup_dir(loc)
            except BackupDirError as e:
                st.error(str(e))
            else:
                st.success(f"Backups will be saved to {saved}.")
                st.rerun()
        if default_col.button("Use the default", key="backup_dir_default"):
            reset_backup_dir()
            st.rerun()
        st.caption(
            "Back up the database however you run it; this project doesn't include backups. "
            "This is only a copy you take yourself before trying something: every "
            f"table saved as a Parquet file in the folder above. The most recent {BACKUPS_KEPT} are kept, "
            "and any can be deleted below. "
            "Nothing here is ever taken automatically — and every change to a transaction is "
            "kept in its history anyway."
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
                        "Reason", help="'manual' for the button above; older copies may name "
                                       "the action they were taken before."),
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
                    "Size": st.column_config.Column("Size", help="Total size of the copy on disk."),
                    "Folder": st.column_config.Column(
                        "Folder", help="One Parquet file per table, readable by DuckDB or pandas."),
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

    st.markdown("**Download a backup**")
    st.caption("Download a backup as a zip, to keep it somewhere else.")
    if _backups:
        pick = st.selectbox("Backup to download", _backups, format_func=lambda p: p.name, key="backup_pick")
        st.download_button("Download as a zip", data=_backup_zip_cached(str(pick), pick.stat().st_mtime),
                           file_name=f"{pick.name}.zip",
                           mime="application/zip", key="backup_download")

with tab_danger:
    # ── Danger zone: wipe entire taxonomy ──────────────────────────────────────
    with st.expander("⚠️ Danger Zone"):
        st.markdown("**Wipe entire taxonomy**")

        st.warning(
            f"This removes all **{len(tax_parents)} parent categories** and "
            f"**{total_subs_all} subcategories**, and clears the LLM category/subcategory "
            f"labels from **{total_classified} of {total_txns}** transactions.\n\n"
            f"Your raw transactions and context sentences are **never touched** — only the "
            f"category structure and label assignments are removed (both stay in the "
            f"history). The default taxonomy is seeded the next time the pipeline or dashboard "
            f"starts, and transactions will need to be re-classified from scratch.\n\n"
            f"Take a copy first with **Back up now** above if you might want this back."
        )

        wipe_ack = st.checkbox("I understand this cannot be undone", key="wipe_taxonomy_ack")
        wipe_text = st.text_input(
            'Type "WIPE TAXONOMY" (exact, case-sensitive) to confirm',
            key="wipe_taxonomy_text",
            disabled=not wipe_ack,
        )
        wipe_ready = wipe_ack and wipe_text.strip() == "WIPE TAXONOMY"

        def _clear_wipe_confirmation():
            # A click callback runs before the widgets are drawn again, so the fields can be reset here.
            st.session_state.wipe_taxonomy_ack = False
            st.session_state.wipe_taxonomy_text = ""

        if st.button("Permanently wipe entire taxonomy", type="primary", disabled=not wipe_ready,
                     on_click=_clear_wipe_confirmation):
            wipe_taxonomy()
            st.cache_data.clear()
            st.success("Taxonomy wiped. Re-run the pipeline to re-seed the default taxonomy and re-classify.")
            st.rerun()
