"""The Rules tab's pure logic (dashboard_helpers): describing, validating and
matching rules -- matching through the server's own check_rules."""
from types import SimpleNamespace

import pytest

from check_rules import _extract_field, payload_field
from dashboard_helpers import (
    describe_action, describe_condition, describe_conditions, first_rule_for, rule_matches, rule_problems,
)


def _rule(**overrides) -> dict:
    rule = {"id": 1, "name": "Tesco", "match_field": "merchant_name", "match_type": "contains",
            "match_value": "tesco", "match_field_2": None, "match_type_2": None, "match_value_2": None,
            "auto_context": "Groceries", "auto_skip": False, "subcategory_id": None, "enabled": True}
    return {**rule, **overrides}


TESCO = {"amount": -1250, "description": "TESCO STORES 1234", "category": "groceries",
         "merchant": {"name": "Tesco"}, "counterparty": {}, "scheme": "mastercard"}
POT = {"amount": 5000, "description": "pot_0001", "category": "general",
       "merchant": None, "counterparty": None, "scheme": "uk_retail_pot"}


class TestPayloadFieldMatchesTheWebhook:
    """The dashboard reads stored payloads; the webhook reads Monzo's parsed
    data. Both must see the same field values, or a preview would lie."""

    @pytest.mark.parametrize("data", [TESCO, POT])
    @pytest.mark.parametrize("field", ["merchant_name", "counterparty_name", "description",
                                       "category", "amount", "scheme"])
    def test_same_value_as_extract_field(self, data, field):
        parsed = SimpleNamespace(**data)
        assert payload_field(data, field) == _extract_field(parsed, field)


class TestRuleMatches:
    def test_text_condition(self):
        assert rule_matches(_rule(), TESCO)
        assert not rule_matches(_rule(), POT)

    def test_both_conditions_must_hold(self):
        out = _rule(match_field="scheme", match_type="exact", match_value="uk_retail_pot",
                    match_field_2="amount", match_type_2="amount_negative", match_value_2="any")
        into = {**out, "match_type_2": "amount_positive"}
        assert not rule_matches(out, POT)
        assert rule_matches(into, POT)

    def test_amount_range_ignores_sign(self):
        assert rule_matches(_rule(match_field="amount", match_type="amount_range", match_value="10-15"), TESCO)

    def test_broken_rule_matches_nothing(self):
        assert not rule_matches(_rule(match_type="regex", match_value="(unclosed"), TESCO)
        assert not rule_matches(_rule(match_field="amount", match_type="amount_range", match_value="cheap"), TESCO)

    def test_missing_payload_matches_nothing(self):
        assert not rule_matches(_rule(), None)


class TestFirstRuleFor:
    def test_first_enabled_match_wins(self):
        rules = [_rule(id=1, enabled=False), _rule(id=2, name="Second"), _rule(id=3, name="Third")]
        assert first_rule_for(rules, TESCO)["id"] == 2

    def test_only_rules_before_an_id(self):
        rules = [_rule(id=1, match_value="nothing like it"), _rule(id=2)]
        assert first_rule_for(rules, TESCO, before_id=2) is None
        assert first_rule_for(rules, TESCO, before_id=3)["id"] == 2


class TestRuleProblems:
    def test_a_complete_rule_has_none(self):
        assert rule_problems(_rule()) == []

    def test_needs_a_name_and_a_value(self):
        problems = rule_problems(_rule(name=" ", match_value=""))
        assert any("name" in p for p in problems)
        assert any("needs a value" in p for p in problems)

    def test_has_to_do_something(self):
        assert any("do something" in p for p in rule_problems(_rule(auto_context="")))
        assert rule_problems(_rule(auto_context="", auto_skip=True)) == []
        assert rule_problems(_rule(auto_context="", subcategory_id=7)) == []

    @pytest.mark.parametrize("value", ["20-5", "5", "a-b"])
    def test_bad_range(self, value):
        assert rule_problems(_rule(match_field="amount", match_type="amount_range", match_value=value))

    def test_bad_regex(self):
        assert any("regex" in p for p in rule_problems(_rule(match_type="regex", match_value="(")))

    def test_direction_needs_no_value(self):
        assert rule_problems(_rule(match_field="amount", match_type="amount_negative", match_value="any")) == []

    def test_second_condition_checked_too(self):
        problems = rule_problems(_rule(match_field_2="description", match_type_2="contains", match_value_2=""))
        assert any("second condition" in p for p in problems)


class TestDescriptions:
    def test_conditions(self):
        assert describe_condition("merchant_name", "contains", "tesco") == 'Merchant contains "tesco"'
        assert describe_condition("amount", "amount_range", "5-20") == "Amount is between £5–£20"
        assert describe_condition("amount", "amount_negative", "any") == "Amount is money out"
        both = _rule(match_field_2="amount", match_type_2="amount_exact", match_value_2="9.99")
        assert describe_conditions(both) == 'Merchant contains "tesco" and Amount is exactly £9.99'

    def test_action(self):
        rule = _rule(auto_skip=True, category="Bills", subcategory="Phone")
        assert describe_action(rule) == 'Skip · → Bills › Phone · context "Groceries"'
        assert "archived" in describe_action({**rule, "placement_live": False})
        assert describe_action(_rule(auto_context="")) == "Nothing"
