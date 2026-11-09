"""Rules: everything the server's old Rules page did, against every payment
ever received rather than the last 30 days -- what a rule would catch (as you
type), what each saved rule has caught, and which rules catch a given payment.
Matching is check_rules' own (see dashboard_helpers), so it's never a guess."""
import html
import pandas as pd
import streamlit as st
from dashboard_helpers import (
    DIRECTION_TYPES, DIRECTION_VALUE, RULE_FIELDS, describe_action, describe_conditions, first_rule_for,
    match_types_for, rule_matches, rule_problems,
)
from database_functions import (
    add_rule, delete_rule, get_rule_test_transactions, get_rules, get_subcategories, toggle_rule, update_rule,
)
from check_rules import payload_field
from ui import _fmt, _card_rows


@st.cache_data(ttl=60, show_spinner="Loading payments for rules...")
def load_rule_transactions() -> list[dict]:
    return get_rule_test_transactions()


_RULE_DEFAULTS = {
    "rule_name": "", "rule_f1": "merchant_name", "rule_t1": "contains", "rule_v1": "",
    "rule_f2": "", "rule_t2": "contains", "rule_v2": "",
    "rule_ctx": "", "rule_skip": False, "rule_place": 0,
    "rule_editing": None, "rule_fill": "", "rule_view": None, "rule_confirm_del": None,
    "rule_flash": None, "rule_check": "",
}


def _rule_reset() -> None:
    for k, v in _RULE_DEFAULTS.items():
        if k not in ("rule_flash", "rule_view", "rule_check"):
            st.session_state[k] = v


def _rule_edit(rule: dict) -> None:
    s = st.session_state
    s.rule_name, s.rule_ctx = rule["name"], rule["auto_context"] or ""
    s.rule_f1, s.rule_t1, s.rule_v1 = rule["match_field"], rule["match_type"], rule["match_value"]
    s.rule_f2 = rule["match_field_2"] or ""
    s.rule_t2 = rule["match_type_2"] or "contains"
    s.rule_v2 = rule["match_value_2"] or ""
    s.rule_skip, s.rule_place = bool(rule["auto_skip"]), rule["subcategory_id"] or 0
    s.rule_editing, s.rule_confirm_del = rule["id"], None


def _rule_candidate() -> dict:
    """The rule the form describes right now."""
    s = st.session_state
    second = s.rule_f2 or None
    return {
        "name": s.rule_name.strip(),
        "match_field": s.rule_f1, "match_type": s.rule_t1,
        "match_value": DIRECTION_VALUE if s.rule_t1 in DIRECTION_TYPES else s.rule_v1.strip(),
        "match_field_2": second, "match_type_2": s.rule_t2 if second else None,
        "match_value_2": (DIRECTION_VALUE if s.rule_t2 in DIRECTION_TYPES else s.rule_v2.strip()) if second else None,
        "auto_context": s.rule_ctx.strip(), "auto_skip": bool(s.rule_skip), "subcategory_id": s.rule_place or None,
        "enabled": True,
    }


def _rule_save() -> None:
    rule = _rule_candidate()
    if rule_problems(rule):
        return
    if st.session_state.rule_editing:
        update_rule(st.session_state.rule_editing, rule)
        st.session_state.rule_flash = f"Saved “{rule['name']}”."
    else:
        add_rule(rule)
        st.session_state.rule_flash = f"Added “{rule['name']}”. It applies to payments from now on."
    _rule_reset()


def _rule_toggle(rule_id: int) -> None:
    toggle_rule(rule_id)


def _rule_delete(rule: dict) -> None:
    delete_rule(rule["id"])
    if st.session_state.rule_editing == rule["id"]:
        _rule_reset()
    st.session_state.rule_confirm_del = None
    st.session_state.rule_flash = f"Deleted “{rule['name']}”."


def _payee(t: dict) -> str:
    return t["merchant_name"] or t["counterparty_name"] or t["description"] or t["id"]


def _rule_txn_label(t: dict) -> str:
    return f"{t['created_at']:%d %b %Y} · {_payee(t)} · {_fmt(float(t['amount']))}"


def _rule_txn_table(txns: list[dict], extra: dict[str, callable] | None = None) -> None:
    """Payments as a table: what they are and what they have now."""
    rows = []
    for t in txns:
        category = " › ".join(c for c in (t["llm_category"], t["llm_subcategory"]) if c)
        row = {
            "Date": t["created_at"], "Payee": _payee(t), "Amount": float(t["amount"]),
            "Context now": t["user_context"] or "",
            "Category now": "Skipped" if t["skipped"] else category,
        }
        for column, value_of in (extra or {}).items():
            row[column] = value_of(t)
        rows.append(row)
    st.dataframe(
        pd.DataFrame(rows), hide_index=True, width="stretch",
        height=min(35 * (len(rows) + 1) + 2, 420),
        column_config={
            "Date": st.column_config.DatetimeColumn("Date", format="D MMM YYYY", width="small"),
            "Amount": st.column_config.NumberColumn("Amount", format="£%,.2f", width="small"),
        },
    )


def _fill_from_payment() -> None:
    """Start a rule from a real payment: match on its most telling field (a
    merchant beats a payee, which beats the free-text description)."""
    t = rule_txn_by_id.get(st.session_state.rule_fill)
    if not t:
        return
    data = t["data"] or {}
    field, value = "description", payload_field(data, "description") or ""
    for candidate in ("merchant_name", "counterparty_name"):
        if payload_field(data, candidate):
            field, value = candidate, payload_field(data, candidate)
            break
    s = st.session_state
    s.rule_f1, s.rule_t1, s.rule_v1 = field, "exact", value
    if not s.rule_name:
        s.rule_name = value
    if not s.rule_ctx and t["user_context"] and t["user_context"] != "Skipped":
        s.rule_ctx = t["user_context"]

for _k, _v in _RULE_DEFAULTS.items():
    if _k not in st.session_state:
        st.session_state[_k] = _v

st.caption(
    "Rules run on each payment as it arrives, before anything asks you about it. The first switched-on "
    "rule that matches (from the top of the list) gives it context, skips it, or files it straight into a "
    "category. They only act on new payments; the previews below show what they'd have caught so far."
)
if st.session_state.rule_flash:
    st.success(st.session_state.rule_flash)
    st.session_state.rule_flash = None

rules = get_rules()
rule_txns = load_rule_transactions()
rule_txn_by_id = {t["id"]: t for t in rule_txns}
rule_by_id = {r["id"]: r for r in rules}

# Each rule's matches, and which rule each payment would get (first wins).
rule_all_matches: dict[int, list[dict]] = {r["id"]: [] for r in rules}
rule_applied: dict[int, list[dict]] = {r["id"]: [] for r in rules}
applied_rule_of: dict[str, dict] = {}
for t in rule_txns:
    for r in rules:
        if rule_matches(r, t["data"]):
            rule_all_matches[r["id"]].append(t)
            if r["enabled"] and t["id"] not in applied_rule_of:
                applied_rule_of[t["id"]] = r
                rule_applied[r["id"]].append(t)

rules_on = [r for r in rules if r["enabled"]]
idle = [r for r in rules_on if not rule_applied[r["id"]]]
rule_stat_color = "rgba(127, 140, 141, 0.16)"
_card_rows([
    ("Rules switched on", f"{len(rules_on)} of {len(rules)}", rule_stat_color,
     "Paused rules are kept but never applied.", ""),
    ("Payments caught", f"{len(applied_rule_of)}", rule_stat_color,
     "Payments so far that a switched-on rule matches -- what the rules would have done to them.", ""),
    ("Share caught", f"{len(applied_rule_of) / len(rule_txns) * 100:.1f}%" if rule_txns else "—",
     rule_stat_color, f"Of all {len(rule_txns)} payments ever received.", ""),
    ("Rules catching nothing", str(len(idle)), rule_stat_color,
     "Switched-on rules that match no payment so far, or only ones an earlier rule already catches.", ""),
])

# ── Add / edit ─────────────────────────────────────────────────────────────
editing = rule_by_id.get(st.session_state.rule_editing)
if st.session_state.rule_editing and not editing:  # deleted meanwhile
    st.session_state.rule_editing = None

live_subs = get_subcategories()
place_label = {s["id"]: f"{s['parent_name']} › {s['name']}" for s in live_subs}
if editing and editing["subcategory_id"] and editing["subcategory_id"] not in place_label:
    place_label[editing["subcategory_id"]] = (
        f"{editing['category']} › {editing['subcategory']} (archived, ignored)")
if st.session_state.rule_place and st.session_state.rule_place not in place_label:
    st.session_state.rule_place = 0

st.subheader(f"Edit rule: {editing['name']}" if editing else "Add a rule")
with st.container(border=True):
    if not editing:
        st.selectbox(
            "Start from a payment (optional)", [""] + [t["id"] for t in rule_txns],
            key="rule_fill", on_change=_fill_from_payment,
            format_func=lambda i: "Search your payments…" if not i else _rule_txn_label(rule_txn_by_id[i]),
            help="Fills in the condition from that payment's merchant, payee or description.",
        )

    c1, c2 = st.columns(2)
    c1.text_input("Name", key="rule_name", placeholder="e.g. Gym membership")
    c2.text_input("Context to give it", key="rule_ctx", placeholder="e.g. Monthly gym membership",
                  help="Written as the payment's context, so it isn't asked about.")

    def _condition_row(label: str, f_key: str, t_key: str, v_key: str, optional: bool) -> None:
        cf, ct, cv = st.columns([1.2, 1.2, 2])
        fields = ([""] if optional else []) + list(RULE_FIELDS)
        cf.selectbox(label, fields, key=f_key,
                     format_func=lambda f: "— no second condition —" if f == "" else RULE_FIELDS[f])
        field = st.session_state[f_key]
        if not field:
            return
        types = match_types_for(field)
        if st.session_state[t_key] not in types:
            st.session_state[t_key] = next(iter(types))
        ct.selectbox("Match", list(types), key=t_key, format_func=types.get)
        match_type = st.session_state[t_key]
        if match_type in DIRECTION_TYPES:
            st.session_state[v_key] = DIRECTION_VALUE
            cv.text_input("Value", key=v_key, disabled=True, help="Only whether money went in or out.")
            return
        if st.session_state[v_key] == DIRECTION_VALUE:
            st.session_state[v_key] = ""
        placeholder = {"amount_range": "e.g. 5-20 (pounds, either direction)",
                       "amount_exact": "e.g. 9.99"}.get(match_type, "e.g. PureGym")
        cv.text_input("Value", key=v_key, placeholder=placeholder,
                      help="Not case-sensitive. Amounts are in pounds and ignore the sign.")

    _condition_row("When", "rule_f1", "rule_t1", "rule_v1", optional=False)
    _condition_row("And (optional)", "rule_f2", "rule_t2", "rule_v2", optional=True)

    p1, p2 = st.columns([3, 1], vertical_alignment="bottom")
    p1.selectbox("File it under (optional)", [0] + list(place_label), key="rule_place",
                 format_func=lambda i: "— let the classifier decide —" if not i else place_label[i],
                 help="Files matches straight into this category, skipping the classifier.")
    p2.checkbox("Skip it", key="rule_skip", help="Leaves the payment out of everything, and never asks.")

    candidate = _rule_candidate()
    problems = rule_problems(candidate)
    b1, b2, b3 = st.columns([1, 1, 4], vertical_alignment="center")
    b1.button("Save changes" if editing else "Add rule", type="primary", disabled=bool(problems),
              on_click=_rule_save, width="stretch")
    b2.button("Cancel" if editing else "Clear", on_click=_rule_reset, width="stretch")
    if problems and (candidate["name"] or candidate["match_value"] or editing):
        b3.caption("To save: " + " ".join(problems))

# ── What it would catch ────────────────────────────────────────────────────
condition_ok = not [p for p in problems if "condition" in p]
if condition_ok and candidate["match_value"]:
    caught = [t for t in rule_txns if rule_matches(candidate, t["data"])]
    before_id = editing["id"] if editing else None
    earlier = {t["id"]: first_rule_for(rules, t["data"], before_id) for t in caught}
    if editing:
        earlier = {k: (v if v and v["id"] != editing["id"] else None) for k, v in earlier.items()}
    taken = [t for t in caught if earlier[t["id"]]]
    spent = -sum(float(t["amount"]) for t in caught if t["amount"] < 0)
    received = sum(float(t["amount"]) for t in caught if t["amount"] > 0)

    if not caught:
        st.info(f"This rule wouldn't have caught any of your {len(rule_txns)} payments so far.")
    else:
        summary = (f"**Would have caught {len(caught)}** of your {len(rule_txns)} payments "
                   f"(latest {caught[0]['created_at']:%d %b %Y})")
        money = " · ".join(x for x in (f"{_fmt(spent)} out" if spent else "",
                                        f"{_fmt(received)} in" if received else "") if x)
        st.markdown(summary + (f" · {money}" if money else ""))
        if editing:
            saved = {t["id"] for t in rule_all_matches[editing["id"]]}
            now_ids = {t["id"] for t in caught}
            gained, lost = len(now_ids - saved), len(saved - now_ids)
            if gained or lost:
                st.caption(f"Compared with the saved version: {gained} more, {lost} fewer.")
        if taken:
            st.warning(f"{len(taken)} of these are caught first by an earlier rule, so this one wouldn't "
                       "apply to them (see the last column).")
        _rule_txn_table(caught, {"Caught first by": lambda t: earlier[t["id"]]["name"] if earlier[t["id"]] else ""})

st.divider()

# ── Saved rules ────────────────────────────────────────────────────────────
st.subheader(f"Your rules ({len(rules)})")
if not rules:
    st.info("No rules yet. Add one above, or accept a suggested rule in Telegram.")
widths = [0.35, 3.1, 2.4, 1.5, 0.75, 0.95, 0.85, 0.85]
for position, r in enumerate(rules, start=1):
    applied, matched = rule_applied[r["id"]], rule_all_matches[r["id"]]
    with st.container(border=True):
        cols = st.columns(widths, vertical_alignment="center")
        cols[0].markdown(f"**{position}**")
        paused = "" if r["enabled"] else ' <span style="opacity:0.55;font-size:0.8rem;">⏸ paused</span>'
        cols[1].markdown(
            f"**{html.escape(r['name'])}**{paused}<br>"
            f'<span style="font-size:0.85rem;opacity:0.7;">{html.escape(describe_conditions(r))}</span>',
            unsafe_allow_html=True)
        cols[2].markdown(html.escape(describe_action(r)))
        if r["enabled"]:
            hit_text = f"{len(applied)} payment{'s' if len(applied) != 1 else ''}"
            last = f"last {applied[0]['created_at']:%d %b %Y}" if applied else "none so far"
            if len(matched) > len(applied):
                last += f" · {len(matched) - len(applied)} taken by earlier rules"
        else:
            hit_text, last = f"would catch {len(matched)}", "paused"
        cols[3].markdown(f"{hit_text}<br><span style='font-size:0.8rem;opacity:0.6;'>{last}</span>",
                         unsafe_allow_html=True)
        cols[4].button("Edit", key=f"rule_edit_{r['id']}", width="stretch", on_click=_rule_edit, args=(r,))
        if cols[5].button("Matches", key=f"rule_view_{r['id']}", help="See every payment it matches", width="stretch"):
            st.session_state.rule_view = None if st.session_state.rule_view == r["id"] else r["id"]
        cols[6].button("Pause" if r["enabled"] else "Resume", key=f"rule_toggle_{r['id']}",
                       on_click=_rule_toggle, args=(r["id"],), width="stretch")
        if cols[7].button("Delete", key=f"rule_del_{r['id']}", width="stretch"):
            st.session_state.rule_confirm_del = r["id"]

    if st.session_state.rule_confirm_del == r["id"]:
        d1, d2, d3 = st.columns([3, 1, 1])
        d1.warning(f"Delete **{r['name']}**? This can't be undone (pausing keeps it).")
        d2.button("Delete", key=f"rule_del_yes_{r['id']}", type="primary", on_click=_rule_delete, args=(r,))
        if d3.button("Cancel", key=f"rule_del_no_{r['id']}"):
            st.session_state.rule_confirm_del = None
            st.rerun()

    if st.session_state.rule_view == r["id"]:
        if matched:
            _rule_txn_table(matched, {"Applied by": lambda t: applied_rule_of[t["id"]]["name"]
                                      if t["id"] in applied_rule_of else "— (no switched-on rule)"})
        else:
            st.caption("It matches none of your payments so far.")

st.divider()

# ── Check a payment ────────────────────────────────────────────────────────
st.subheader("Which rules catch a payment?")
st.caption("Pick any payment to see the fields rules can match on, and every rule that matches it, in order.")
st.selectbox("Payment", [""] + [t["id"] for t in rule_txns], key="rule_check",
             format_func=lambda i: "Search your payments…" if not i else _rule_txn_label(rule_txn_by_id[i]))
checked = rule_txn_by_id.get(st.session_state.rule_check)
if checked:
    data = checked["data"] or {}
    fields_now = {
        RULE_FIELDS[f]: (_fmt(payload_field(data, f) / 100) if f == "amount" and payload_field(data, f) is not None
                         else payload_field(data, f) or "—")
        for f in RULE_FIELDS
    }
    st.dataframe(pd.DataFrame([fields_now]), hide_index=True, width="stretch")
    hits = [r for r in rules if rule_matches(r, data)]
    if not hits:
        st.info("No rule matches this payment.")
    else:
        winner = first_rule_for(rules, data)
        for r in hits:
            if winner and r["id"] == winner["id"]:
                mark = "✅ **Applies:**"
            elif not r["enabled"]:
                mark = "⏸ Matches, but paused:"
            else:
                mark = "↪ Matches, but an earlier rule wins:"
            st.markdown(f"{mark} {html.escape(r['name'])} → {html.escape(describe_action(r))}")
