"""Settings: categories and their roles, and backups."""
import pandas as pd
import streamlit as st
from pathlib import Path
from database_functions import (
    BACKUPS_KEPT, ROLES, backup_db, backup_dir, backup_size_bytes, delete_backup, delete_parent,
    delete_subcategory, edit_subcategory, get_parents, get_subcategories, list_backups, move_all_transactions,
    read_backup_manifest, rename_parent, set_parent_role, set_subcategory_role, upsert_parent,
    upsert_subcategory, wipe_parent_labels, wipe_taxonomy,
)
from dashboard_helpers import (
    backup_coverage, backup_label, backup_reason, format_bytes, merchant_display_name,
)
from ui import _query, _card_rows
from page_context import shared

df, = shared('df')


st.caption("Manage categories and set each one's Role, which controls what counts as Income, Spend, Investment, Transfer, or is Excluded on the Overview and in Telegram summaries.")

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

# ── Backups ────────────────────────────────────────────────────────────────
st.divider()
_backups = list_backups()
with st.expander(
    f"💾 Backups ({len(_backups)})"
    + (f" — last {backup_label(_backups[0])}" if _backups else " — none yet")
):
    st.caption(
        "The database is backed up every night wherever it lives (for this setup, the "
        "data box). This is only a copy you take yourself before trying something: every "
        f"table saved as a Parquet file in a folder under `{backup_dir()}` (set BACKUP_DIR "
        f"to change it). The most recent {BACKUPS_KEPT} are kept, and any can be deleted below. "
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

# ── Danger zone: wipe entire taxonomy ──────────────────────────────────────
st.divider()
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

    if st.button("Permanently wipe entire taxonomy", type="primary", disabled=not wipe_ready):
        wipe_taxonomy()
        st.session_state.wipe_taxonomy_ack = False
        st.session_state.wipe_taxonomy_text = ""
        st.cache_data.clear()
        st.success("Taxonomy wiped. Re-run the pipeline to re-seed the default taxonomy and re-classify.")
        st.rerun()
