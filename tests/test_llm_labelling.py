import pytest
from unittest.mock import MagicMock

from llm_labelling import (
    _format_transaction,
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
