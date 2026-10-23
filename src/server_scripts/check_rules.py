from server_db import get_con
from typing import NamedTuple
import logging
import re

log = logging.getLogger(__name__)


class RuleMatch(NamedTuple):
    context: str | None
    skip: bool
    # Both set only when the rule fixes a placement -- the local pipeline then
    # files the transaction there instead of classifying it.
    category: str | None = None
    subcategory: str | None = None

    @property
    def matched(self) -> bool:
        return bool(self.context or self.skip or self.category)


NO_MATCH = RuleMatch(None, False)


def _extract_field(data, match_field: str):
    if match_field == "merchant_name":
        return data.merchant.get("name") if data.merchant else None
    if match_field == "description":
        return data.description
    if match_field == "category":
        return data.category
    if match_field == "counterparty_name":
        return data.counterparty.get("name") if data.counterparty else None
    if match_field == "amount":
        return data.amount
    if match_field == "scheme":
        # Monzo's own classification of how the money moved, e.g.
        # 'uk_retail_pot' for a transfer to or from a pot. Matching on this is
        # exact by nature, unlike matching text that merely looks distinctive:
        # 'pot_' as a substring also appears in HOME DEPOT_, JACKPOT_ and so on.
        return getattr(data, "scheme", None)
    return None


def _matches(field_value, match_type: str, match_value: str) -> bool:
    if match_type == "exact":
        return str(field_value).lower() == match_value.lower()
    if match_type == "contains":
        return match_value.lower() in str(field_value).lower()
    if match_type == "regex":
        return bool(re.search(match_value, str(field_value), re.IGNORECASE))
    if match_type == "amount_range":
        low, high = match_value.split("-")
        return round(float(low) * 100) <= abs(int(field_value)) <= round(float(high) * 100)
    if match_type == "amount_exact":
        return abs(int(field_value)) == round(float(match_value) * 100)
    # Direction, not magnitude -- e.g. a pot transfer's description is just its
    # pot id (no useful merchant/counterparty name), and the only way to tell a
    # deposit from a withdrawal is the sign of the amount. match_value is
    # unused; these need only ever be paired with a first condition that picks
    # out which transactions to look at (scheme, description, ...).
    if match_type == "amount_negative":
        return int(field_value) < 0
    if match_type == "amount_positive":
        return int(field_value) > 0
    return False

def check_rules(data) -> RuleMatch:
    con = get_con()
    rules = con.execute(
        "SELECT id, name, match_field, match_type, match_value, auto_context, enabled, match_field_2, match_type_2, match_value_2, auto_skip, category, subcategory FROM rules WHERE enabled = TRUE"
    ).fetchall()

    for rule in rules:
        rule_id, name, match_field, match_type, match_value, auto_context, _, match_field_2, match_type_2, match_value_2, auto_skip, category, subcategory = rule

        try:
            field_value = _extract_field(data, match_field)
            if field_value is None or not _matches(field_value, match_type, match_value):
                continue

            if match_field_2 and match_type_2 and match_value_2:
                field_value_2 = _extract_field(data, match_field_2)
                if field_value_2 is None or not _matches(field_value_2, match_type_2, match_value_2):
                    continue
        except Exception as e:
            log.warning(f"Rule {rule_id} ('{name}') failed to evaluate and was skipped: {e}")
            continue

        # Only a complete pair is a placement -- a category on its own would
        # file the transaction under a parent with no subcategory.
        if category and subcategory:
            return RuleMatch(auto_context or None, bool(auto_skip), category, subcategory)
        return RuleMatch(auto_context or None, bool(auto_skip))

    return NO_MATCH

