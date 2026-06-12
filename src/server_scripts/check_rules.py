from server_db import get_con
import re

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
    return False

def check_rules(data) -> str | None:
    con = get_con()
    rules = con.execute(
        "SELECT id, name, match_field, match_type, match_value, auto_context, enabled, match_field_2, match_type_2, match_value_2 FROM rules WHERE enabled = TRUE"
    ).fetchall()

    for rule in rules:
        _, _, match_field, match_type, match_value, auto_context, _, match_field_2, match_type_2, match_value_2 = rule

        field_value = _extract_field(data, match_field)
        if field_value is None or not _matches(field_value, match_type, match_value):
            continue

        if match_field_2 and match_type_2 and match_value_2:
            field_value_2 = _extract_field(data, match_field_2)
            if field_value_2 is None or not _matches(field_value_2, match_type_2, match_value_2):
                continue

        return auto_context

    return None

