import json
import pytest
from unittest.mock import MagicMock

import llm_labelling as ll
import category_proposals as cp
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

    def test_no_denied_names_adds_no_block(self):
        prompt = _pass1_prompt([_txn()], [])
        assert "already declined" not in prompt

    def test_denied_parent_names_are_listed_and_forbidden(self):
        prompt = _pass1_prompt([_txn()], [], denied_parent_names={"Tax"})
        assert "already declined creating these category names" in prompt
        assert "Tax" in prompt


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

    def test_no_denied_names_adds_no_block(self):
        prompt = _pass2_prompt([_txn()], "Food & Drink", [], ["Food & Drink"])
        assert "already declined" not in prompt

    def test_denied_sub_names_are_listed_and_forbidden(self):
        prompt = _pass2_prompt([_txn()], "Food & Drink", [], ["Food & Drink"], denied_sub_names={"Self Assessment"})
        assert "already declined creating these subcategory names" in prompt
        assert "Self Assessment" in prompt


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

    def test_denied_parent_names_reach_the_prompt(self):
        client = MagicMock()
        client.messages.create.return_value.content = [MagicMock(text='[]')]
        classify_parents(client, [_txn()], [], denied_parent_names={"Tax"})
        sent_prompt = client.messages.create.call_args.kwargs["messages"][0]["content"]
        assert "Tax" in sent_prompt and "already declined" in sent_prompt


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


class TestNoveltyGateHoldsNewCategoriesForApproval:
    """The bug this feature fixes: a large one-off transaction (a tax payment)
    got silently filed under an existing, unrelated category because nothing
    stopped Pass 1/2 from treating "close enough" as good enough. Now a
    genuinely new (parent, subcategory) pair is held for a Telegram decision
    instead of being created on the spot."""

    def _stub_llm(self, monkeypatch, parent_by_id, subcategory_by_id):
        monkeypatch.setattr(ll, "match_existing", lambda client, txns, subs: {})
        monkeypatch.setattr(
            ll, "classify_parents",
            lambda client, txns, parents, denied_parent_names=None: {
                t["id"]: parent_by_id[t["id"]] for t in txns
            },
        )
        monkeypatch.setattr(
            ll, "classify_subcategories",
            lambda client, txns, parent_name, subs, all_parent_names, denied_sub_names=None: {
                t["id"]: subcategory_by_id[t["id"]] for t in txns
            },
        )
        monkeypatch.setattr(ll, "anthropic", type("M", (), {"Anthropic": lambda **kw: None}))

    def _seed(self, db, parent="Bills & Utilities", sub="Electricity"):
        parent_id = db.execute(
            "INSERT INTO parent_categories (id, name, created_at) VALUES (1, ?, NOW()) RETURNING id", [parent]
        ).fetchone()[0]
        db.execute(
            "INSERT INTO subcategories (id, name, parent_id, created_at) VALUES (1, ?, ?, NOW())", [sub, parent_id]
        )
        db.execute(
            """INSERT INTO transactions (id, amount, currency, description, user_context, skipped)
               VALUES ('txn_bill', -80.0, 'GBP', 'Octopus Energy', 'Electricity bill', FALSE)"""
        )
        db.execute(
            """INSERT INTO transactions (id, amount, currency, description, user_context, skipped)
               VALUES ('txn_tax', -3000.0, 'GBP', 'HMRC', 'Self assessment tax payment', FALSE)"""
        )

    def test_a_new_category_is_held_not_created(self, db, monkeypatch):
        self._seed(db)
        self._stub_llm(
            monkeypatch,
            parent_by_id={"txn_bill": "Bills & Utilities", "txn_tax": "Tax"},
            subcategory_by_id={"txn_bill": "Electricity", "txn_tax": "Self Assessment"},
        )
        monkeypatch.setattr(cp, "server_supports_proposals", lambda: True)
        synced = []
        monkeypatch.setattr(cp, "sync_new_proposals", lambda ids: synced.extend(ids))

        ll.run()

        bill = db.execute(
            "SELECT llm_category, llm_subcategory, pending_category_proposal_id FROM transactions WHERE id = 'txn_bill'"
        ).fetchone()
        tax = db.execute(
            "SELECT llm_category, llm_subcategory, pending_category_proposal_id FROM transactions WHERE id = 'txn_tax'"
        ).fetchone()

        assert bill == ("Bills & Utilities", "Electricity", None)
        assert tax[0] is None and tax[1] is None and tax[2] is not None, \
            "the tax payment must stay unclassified and locked, not be filed anywhere"
        assert db.execute("SELECT COUNT(*) FROM parent_categories WHERE name = 'Tax'").fetchone()[0] == 0, \
            "the new category must not exist until approved"

        proposal = db.execute(
            "SELECT parent_name, parent_is_new, subcategory_name, status FROM category_proposals WHERE id = ?",
            [tax[2]],
        ).fetchone()
        assert proposal == ("Tax", True, "Self Assessment", "pending")
        assert synced == [tax[2]]

    def test_a_new_subcategory_under_an_existing_parent_is_also_held(self, db, monkeypatch):
        """Novelty at the subcategory level alone must be caught too -- not
        just a brand new parent."""
        self._seed(db)
        self._stub_llm(
            monkeypatch,
            parent_by_id={"txn_bill": "Bills & Utilities", "txn_tax": "Bills & Utilities"},
            subcategory_by_id={"txn_bill": "Electricity", "txn_tax": "Council Tax"},
        )
        monkeypatch.setattr(cp, "server_supports_proposals", lambda: True)
        monkeypatch.setattr(cp, "sync_new_proposals", lambda ids: None)

        ll.run()

        tax = db.execute(
            "SELECT llm_category, pending_category_proposal_id FROM transactions WHERE id = 'txn_tax'"
        ).fetchone()
        assert tax[0] is None and tax[1] is not None
        proposal = db.execute(
            "SELECT parent_name, parent_is_new, subcategory_name FROM category_proposals WHERE id = ?", [tax[1]]
        ).fetchone()
        assert proposal == ("Bills & Utilities", False, "Council Tax")

    def test_server_not_yet_deployed_falls_back_to_immediate_creation(self, db, monkeypatch):
        """If the gate can't be enforced, the old behaviour must still work
        rather than stranding a transaction locked with no card ever sent."""
        self._seed(db)
        self._stub_llm(
            monkeypatch,
            parent_by_id={"txn_bill": "Bills & Utilities", "txn_tax": "Tax"},
            subcategory_by_id={"txn_bill": "Electricity", "txn_tax": "Self Assessment"},
        )
        monkeypatch.setattr(cp, "server_supports_proposals", lambda: False)

        ll.run()

        tax = db.execute(
            "SELECT llm_category, llm_subcategory, pending_category_proposal_id FROM transactions WHERE id = 'txn_tax'"
        ).fetchone()
        assert tax == ("Tax", "Self Assessment", None)
        assert db.execute("SELECT COUNT(*) FROM category_proposals").fetchone()[0] == 0

    def test_a_denied_name_is_forbidden_on_the_retry_and_a_different_one_is_accepted(self, db, monkeypatch):
        self._seed(db)
        db.execute(
            """INSERT INTO category_proposals (id, parent_name, parent_is_new, subcategory_name, status, proposed_at)
               VALUES (1, 'Tax', TRUE, 'Self Assessment', 'denied', NOW())"""
        )
        seen_denied_parents = []

        def fake_classify_parents(client, txns, parents, denied_parent_names=None):
            seen_denied_parents.append(denied_parent_names)
            return {t["id"]: ("Bills & Utilities" if t["id"] == "txn_bill" else "Miscellaneous") for t in txns}

        monkeypatch.setattr(ll, "match_existing", lambda client, txns, subs: {})
        monkeypatch.setattr(ll, "classify_parents", fake_classify_parents)
        monkeypatch.setattr(
            ll, "classify_subcategories",
            lambda client, txns, parent_name, subs, all_parent_names, denied_sub_names=None: {
                t["id"]: ("Electricity" if t["id"] == "txn_bill" else "One-off Payments") for t in txns
            },
        )
        monkeypatch.setattr(ll, "anthropic", type("M", (), {"Anthropic": lambda **kw: None}))
        monkeypatch.setattr(cp, "server_supports_proposals", lambda: True)
        monkeypatch.setattr(cp, "sync_new_proposals", lambda ids: None)

        ll.run()

        assert seen_denied_parents == [{"tax"}]
        proposal = db.execute(
            "SELECT parent_name, subcategory_name FROM category_proposals WHERE status = 'pending'"
        ).fetchone()
        assert proposal == ("Miscellaneous", "One-off Payments"), \
            "a different new name must still be proposed, not silently forced into an existing category"


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
