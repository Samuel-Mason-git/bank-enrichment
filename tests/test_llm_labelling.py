import json
import pytest
from unittest.mock import MagicMock

from llm_labelling import (
    _format_transaction,
    _payload_facts,
    _warn_if_truncated,
    _extract_json,
    _pass0_prompt,
    _pass1_prompt,
    _pass2_prompt,
    match_existing,
    classify_parents,
    classify_subcategories,
)


def _txn(**kwargs):
    defaults = {
        "id": "tx_001",
        "amount": -12.34,
        "merchant_name": "Caffe Nero",
        "description": "Coffee",
        "user_context": "morning coffee",
        "monzo_category": "eating_out",
    }
    return {**defaults, **kwargs}


class TestFormatTransaction:
    def test_includes_id_and_amount(self):
        result = _format_transaction(_txn())
        assert "tx_001" in result
        assert "12.34" in result

    def test_negative_amount_shows_money_out(self):
        assert "money out" in _format_transaction(_txn(amount=-10.0))

    def test_positive_amount_shows_money_in(self):
        assert "money in" in _format_transaction(_txn(amount=100.0))

    def test_includes_merchant_when_present(self):
        assert "Caffe Nero" in _format_transaction(_txn())

    def test_omits_merchant_when_absent(self):
        result = _format_transaction({"id": "tx_001", "amount": -5.0, "merchant_name": None})
        assert "Merchant:" not in result

    def test_includes_description_when_present(self):
        assert "Coffee" in _format_transaction(_txn())

    def test_includes_user_context_when_present(self):
        assert "morning coffee" in _format_transaction(_txn())

    def test_omits_missing_optional_fields(self):
        result = _format_transaction({"id": "tx_001", "amount": -5.0})
        assert "Context:" not in result
        assert "Description:" not in result
        assert "Merchant:" not in result


class TestExtractJson:
    def test_clean_array(self):
        result = _extract_json('[{"id": "tx_1", "category": "Food"}]')
        assert result == [{"id": "tx_1", "category": "Food"}]

    def test_empty_array(self):
        assert _extract_json("[]") == []

    def test_extracts_from_surrounding_text(self):
        raw = 'Here is the result:\n[{"id": "tx_1"}]\nThat is all.'
        assert _extract_json(raw) == [{"id": "tx_1"}]

    def test_raises_when_no_array_found(self):
        with pytest.raises(ValueError):
            _extract_json("no json here")

    def test_raises_on_malformed_json(self):
        with pytest.raises(ValueError):
            _extract_json("[{broken}]")

    def test_multiple_items(self):
        result = _extract_json('[{"id": "a"}, {"id": "b"}]')
        assert len(result) == 2


class TestPass0Prompt:
    def test_includes_subcategory_names(self):
        subs = [{"name": "Coffee Shops", "parent_name": "Food & Drink"}]
        prompt = _pass0_prompt([_txn()], subs)
        assert "Coffee Shops" in prompt
        assert "Food & Drink" in prompt

    def test_includes_transaction_id(self):
        subs = [{"name": "Coffee Shops", "parent_name": "Food & Drink"}]
        prompt = _pass0_prompt([_txn()], subs)
        assert "tx_001" in prompt

    def test_multiple_transactions_all_included(self):
        txns = [_txn(id=f"tx_{i:03d}") for i in range(3)]
        subs = [{"name": "Groceries", "parent_name": "Food & Drink"}]
        prompt = _pass0_prompt(txns, subs)
        for t in txns:
            assert t["id"] in prompt


class TestPass1Prompt:
    def test_includes_existing_parent_names(self):
        parents = [{"name": "Transport", "transaction_count": 5}]
        prompt = _pass1_prompt([_txn()], parents)
        assert "Transport" in prompt
        assert "5" in prompt

    def test_no_parents_mentions_creating(self):
        prompt = _pass1_prompt([_txn()], [])
        assert "No parent categories exist yet" in prompt

    def test_includes_transaction_id(self):
        prompt = _pass1_prompt([_txn()], [])
        assert "tx_001" in prompt


class TestPass2Prompt:
    def test_includes_parent_name(self):
        subs = [{"name": "Coffee Shops", "parent_name": "Food & Drink", "transaction_count": 3}]
        prompt = _pass2_prompt([_txn()], "Food & Drink", subs, ["Food & Drink", "Transport"])
        assert "Food & Drink" in prompt

    def test_includes_existing_subcategories(self):
        subs = [{"name": "Coffee Shops", "parent_name": "Food & Drink", "transaction_count": 3}]
        prompt = _pass2_prompt([_txn()], "Food & Drink", subs, ["Food & Drink"])
        assert "Coffee Shops" in prompt

    def test_forbidden_parent_names_listed(self):
        prompt = _pass2_prompt([_txn()], "Food & Drink", [], ["Food & Drink", "Transport"])
        assert "Transport" in prompt  # must appear in forbidden list

    def test_no_existing_subs_mentions_creating(self):
        prompt = _pass2_prompt([_txn()], "Food & Drink", [], ["Food & Drink"])
        assert "No subcategories" in prompt


class TestMatchExisting:
    def test_returns_empty_when_no_subcategories(self):
        client = MagicMock()
        result = match_existing(client, [_txn()], [])
        assert result == {}
        client.messages.create.assert_not_called()

    def test_returns_matched_transactions(self):
        client = MagicMock()
        client.messages.create.return_value.content = [MagicMock(
            text='[{"id": "tx_001", "category": "Food & Drink", "subcategory": "Coffee Shops"}]'
        )]
        subs = [{"name": "Coffee Shops", "parent_name": "Food & Drink"}]
        result = match_existing(client, [_txn()], subs)
        assert result == {"tx_001": {"category": "Food & Drink", "subcategory": "Coffee Shops"}}

    def test_excludes_null_matches(self):
        client = MagicMock()
        client.messages.create.return_value.content = [MagicMock(
            text='[{"id": "tx_001", "category": null, "subcategory": null}]'
        )]
        subs = [{"name": "Coffee Shops", "parent_name": "Food & Drink"}]
        result = match_existing(client, [_txn()], subs)
        assert result == {}

    def test_returns_empty_on_api_error(self):
        client = MagicMock()
        client.messages.create.side_effect = Exception("API error")
        subs = [{"name": "Coffee", "parent_name": "Food"}]
        result = match_existing(client, [_txn()], subs)
        assert result == {}


class TestClassifyParents:
    def test_returns_id_to_category_map(self):
        client = MagicMock()
        client.messages.create.return_value.content = [MagicMock(
            text='[{"id": "tx_001", "category": "Food & Drink"}]'
        )]
        result = classify_parents(client, [_txn()], [])
        assert result == {"tx_001": "Food & Drink"}

    def test_returns_empty_on_api_error(self):
        client = MagicMock()
        client.messages.create.side_effect = Exception("timeout")
        result = classify_parents(client, [_txn()], [])
        assert result == {}


class TestClassifySubcategories:
    def test_returns_id_to_subcategory_map(self):
        client = MagicMock()
        client.messages.create.return_value.content = [MagicMock(
            text='[{"id": "tx_001", "subcategory": "Coffee Shops"}]'
        )]
        result = classify_subcategories(client, [_txn()], "Food & Drink", [], ["Food & Drink"])
        assert result == {"tx_001": "Coffee Shops"}

    def test_returns_empty_on_api_error(self):
        client = MagicMock()
        client.messages.create.side_effect = Exception("timeout")
        result = classify_subcategories(client, [_txn()], "Food & Drink", [], [])
        assert result == {}


def _payload_txn(merchant=None, currency="GBP", local_currency="GBP", local_amount=-500, **kw):
    data = {"currency": currency, "local_currency": local_currency, "local_amount": local_amount}
    if merchant is not None:
        data["merchant"] = merchant
    return {"id": "tx_p", "amount": -5.0, "raw_payload": json.dumps({"data": data}), **kw}


class TestPayloadFacts:
    def test_in_person_purchase_reports_where_the_user_was(self):
        facts = _payload_facts(_payload_txn(
            merchant={"online": False, "atm": False,
                      "address": {"city": "Malaga", "country": "ESP"}}))
        assert "Purchased in person in: Malaga, ESP" in facts

    def test_online_purchase_never_reports_a_location(self):
        """The address on an online merchant is its registered office. Reporting
        it would turn a subscription billed from San Francisco into a trip to
        California -- the exact false positive this data is meant to prevent."""
        facts = _payload_facts(_payload_txn(
            merchant={"online": True, "atm": False,
                      "address": {"city": "San Francisco", "country": "USA"}}))
        assert not any("San Francisco" in f or "USA" in f for f in facts)
        assert any("online purchase" in f for f in facts)

    def test_atm_withdrawal_is_labelled(self):
        facts = _payload_facts(_payload_txn(
            merchant={"atm": True, "online": False, "address": {"city": "Leeds", "country": "GBR"}}))
        assert "Type: ATM cash withdrawal" in facts

    def test_foreign_currency_is_reported_with_the_local_amount(self):
        facts = _payload_facts(_payload_txn(
            merchant={"online": False, "atm": False, "address": {"city": "Nerja", "country": "ESP"}},
            local_currency="EUR", local_amount=-1300))
        assert "Charged in EUR (13.00 EUR)" in facts

    def test_home_currency_is_not_mentioned(self):
        """Saying 'charged in GBP' on every domestic transaction would be noise."""
        facts = _payload_facts(_payload_txn(
            merchant={"online": False, "atm": False, "address": {"city": "Leeds", "country": "GBR"}}))
        assert not any("Charged in" in f for f in facts)

    def test_transaction_with_no_merchant_still_reports_currency(self):
        """Direct debits and transfers have no merchant block at all."""
        facts = _payload_facts(_payload_txn(local_currency="EUR", local_amount=-2000))
        assert facts == ["Charged in EUR (20.00 EUR)"]

    def test_missing_payload_yields_nothing(self):
        assert _payload_facts({"id": "tx_1", "amount": -5.0}) == []

    def test_unparseable_payload_yields_nothing_rather_than_raising(self):
        """A bad payload must cost the prompt some detail, never abort a batch."""
        assert _payload_facts({"raw_payload": "{not json"}) == []
        assert _payload_facts({"raw_payload": 12345}) == []

    def test_payload_without_a_data_block_yields_nothing(self):
        assert _payload_facts({"raw_payload": json.dumps({"type": "transaction"})}) == []

    def test_facts_reach_the_formatted_transaction(self):
        line = _format_transaction(_payload_txn(
            merchant={"online": False, "atm": False, "address": {"city": "Malaga", "country": "ESP"}},
            local_currency="EUR", local_amount=-1300))
        assert "Purchased in person in: Malaga, ESP" in line
        assert "Charged in EUR" in line


class TestTruncationWarning:
    class _Resp:
        def __init__(self, stop_reason):
            self.stop_reason = stop_reason

    def test_warns_when_the_response_was_cut_off(self, caplog):
        """A truncated reply is indistinguishable from a malformed one once it
        reaches _extract_json, but the fix is different -- so it has to be named."""
        with caplog.at_level("ERROR"):
            _warn_if_truncated(self._Resp("max_tokens"), "Pass 0", 15)
        assert "truncated" in caplog.text
        assert "15 transactions" in caplog.text

    def test_silent_on_a_normal_response(self, caplog):
        with caplog.at_level("ERROR"):
            _warn_if_truncated(self._Resp("end_turn"), "Pass 0", 15)
        assert caplog.text == ""

    def test_tolerates_a_response_without_a_stop_reason(self, caplog):
        with caplog.at_level("ERROR"):
            _warn_if_truncated(object(), "Pass 0", 15)
        assert caplog.text == ""


class TestMerchantTags:
    def test_tags_are_included_and_subordinated_to_the_context(self):
        """The framing here is load-bearing, not cosmetic. Measured against real
        transactions, a softer wording ("a hint not a category") let the tags
        override the user outright: `Coffee` at McDonald's was classified
        Takeaway and `Coffee` at Greggs became Snacks, because those merchants
        are tagged that way. Telling the model to ignore the tags outright when
        the context says what was bought removed every one of those regressions."""
        facts = _payload_facts(_payload_txn(
            merchant={"online": False, "atm": False, "suggested_tags": "#food #takeaway",
                      "address": {"city": "Leeds", "country": "GBR"}}))
        tag_fact = next(f for f in facts if "#takeaway" in f)
        assert "#food #takeaway" in tag_fact
        assert "NOT what was bought" in tag_fact
        assert "ignore entirely when the context says what it was" in tag_fact

    def test_no_tag_line_when_the_merchant_has_none(self):
        facts = _payload_facts(_payload_txn(
            merchant={"online": False, "atm": False,
                      "address": {"city": "Leeds", "country": "GBR"}}))
        assert not any("product range" in f for f in facts)

    def test_tags_survive_on_online_purchases(self):
        """Location is suppressed for online merchants, but what they sell is
        still true and useful."""
        facts = _payload_facts(_payload_txn(
            merchant={"online": True, "atm": False, "suggested_tags": "#shopping",
                      "address": {"city": "San Francisco", "country": "USA"}}))
        assert any("#shopping" in f for f in facts)
        assert not any("San Francisco" in f for f in facts)


class TestCounterparty:
    def test_counterparty_is_included(self):
        """Direct debits carry the real payee here, not in merchant_name."""
        line = _format_transaction({
            "id": "tx_dd", "amount": -45.0, "merchant_name": None,
            "counterparty_name": "OCTOPUS ENERGY", "description": "89GJTS7"})
        assert "Counterparty: OCTOPUS ENERGY" in line

    def test_omitted_when_absent(self):
        line = _format_transaction({"id": "tx_1", "amount": -5.0, "merchant_name": "Tesco"})
        assert "Counterparty" not in line

    def test_shown_alongside_merchant_when_both_exist(self):
        line = _format_transaction({
            "id": "tx_1", "amount": -5.0, "merchant_name": "Tesco",
            "counterparty_name": "TESCO STORES"})
        assert "Merchant: Tesco" in line and "Counterparty: TESCO STORES" in line
