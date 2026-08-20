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
    _pass_regenerate_prompt,
    match_existing,
    classify_parents,
    classify_subcategories,
    propose_regenerated_options,
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
        assert "already declined creating these exact category names" in prompt
        assert "Tax" in prompt

    def test_denial_does_not_nudge_toward_an_existing_category(self):
        """Regression test: rejecting a proposed new name is not evidence an
        existing category is the right answer -- the retry must judge the fit
        on its own merits, not be pushed toward "existing" just because a new
        idea was declined."""
        prompt = _pass1_prompt([_txn()], [], denied_parent_names={"Tax"})
        assert "NOT a signal to prefer an existing category" in prompt
        assert "Prefer an existing category" not in prompt

    def test_new_parent_guidance_is_neutral_not_penalising(self):
        """Regression test: the old wording ("exhaust existing options first")
        read as a soft penalty against ever proposing a new parent, which is
        how a tax payment ended up nested under "Professional Services"
        instead of getting its own "Tax" parent."""
        prompt = _pass1_prompt([_txn()], [{"name": "Professional Services", "transaction_count": 3}])
        assert "NOT a worse answer than a stretch-fit" in prompt
        assert "exhaust existing options first" not in prompt


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
        assert "already declined creating these exact subcategory names" in prompt
        assert "Self Assessment" in prompt

    def test_denial_does_not_nudge_toward_an_existing_subcategory(self):
        prompt = _pass2_prompt([_txn()], "Food & Drink", [], ["Food & Drink"], denied_sub_names={"Self Assessment"})
        assert "NOT a signal to prefer an existing subcategory" in prompt
        assert "Prefer an existing subcategory" not in prompt


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
    -- offering up to a few candidate placements -- instead of being created
    on the spot."""

    def _stub_llm(self, monkeypatch, parent_by_id, subcategory_by_id, alternatives=None):
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
        monkeypatch.setattr(
            ll, "propose_alternatives",
            lambda client, groups, parents, subcategories: (alternatives or {}),
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

    def _options(self, db, proposal_id):
        import json
        row = db.execute("SELECT options FROM category_proposals WHERE id = ?", [proposal_id]).fetchone()
        return json.loads(row[0])

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

        status = db.execute("SELECT status FROM category_proposals WHERE id = ?", [tax[2]]).fetchone()[0]
        assert status == "pending"
        options = self._options(db, tax[2])
        assert options == [{
            "parent_name": "Tax", "subcategory_name": "Self Assessment",
            "parent_is_new": True, "rationale": "Best fit based on the transaction details.",
        }]
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
        options = self._options(db, tax[1])
        assert options == [{
            "parent_name": "Bills & Utilities", "subcategory_name": "Council Tax",
            "parent_is_new": False, "rationale": "Best fit based on the transaction details.",
        }]

    def test_alternatives_are_added_after_the_primary_pick(self, db, monkeypatch):
        """The whole point of the multi-option card: a stretch-fit into an
        existing parent and a genuinely new parent can both be offered, so the
        user decides instead of the classifier committing to one guess."""
        self._seed(db)
        self._stub_llm(
            monkeypatch,
            parent_by_id={"txn_bill": "Bills & Utilities", "txn_tax": "Tax"},
            subcategory_by_id={"txn_bill": "Electricity", "txn_tax": "Self Assessment"},
            alternatives={0: [
                {"parent_name": "Bills & Utilities", "subcategory_name": "Council Tax", "rationale": "Stretch-fit into an existing bucket."},
            ]},
        )
        monkeypatch.setattr(cp, "server_supports_proposals", lambda: True)
        monkeypatch.setattr(cp, "sync_new_proposals", lambda ids: None)

        ll.run()

        tax_lock = db.execute(
            "SELECT pending_category_proposal_id FROM transactions WHERE id = 'txn_tax'"
        ).fetchone()[0]
        options = self._options(db, tax_lock)
        assert len(options) == 2
        assert options[0]["parent_name"] == "Tax"
        assert options[1] == {
            "parent_name": "Bills & Utilities", "subcategory_name": "Council Tax",
            # Bills & Utilities already exists in the seeded taxonomy, so this
            # must be computed from the actual taxonomy, not trusted blindly.
            "parent_is_new": False, "rationale": "Stretch-fit into an existing bucket.",
        }

    def test_alternatives_are_capped_at_max_options_and_deduped(self, db, monkeypatch):
        self._seed(db)
        self._stub_llm(
            monkeypatch,
            parent_by_id={"txn_bill": "Bills & Utilities", "txn_tax": "Tax"},
            subcategory_by_id={"txn_bill": "Electricity", "txn_tax": "Self Assessment"},
            alternatives={0: [
                {"parent_name": "Tax", "subcategory_name": "Self Assessment", "rationale": "Duplicate of the primary -- must be dropped."},
                {"parent_name": "Bills & Utilities", "subcategory_name": "Council Tax", "rationale": "A real alternative."},
                {"parent_name": "Miscellany", "subcategory_name": "One-offs", "rationale": "A second real alternative."},
                {"parent_name": "Government", "subcategory_name": "Other Payments", "rationale": "A third real alternative -- past MAX_OPTIONS, must be dropped."},
            ]},
        )
        monkeypatch.setattr(cp, "server_supports_proposals", lambda: True)
        monkeypatch.setattr(cp, "sync_new_proposals", lambda ids: None)

        ll.run()

        tax_lock = db.execute(
            "SELECT pending_category_proposal_id FROM transactions WHERE id = 'txn_tax'"
        ).fetchone()[0]
        options = self._options(db, tax_lock)
        assert len(options) == ll.MAX_OPTIONS
        names = [(o["parent_name"], o["subcategory_name"]) for o in options]
        assert names == [("Tax", "Self Assessment"), ("Bills & Utilities", "Council Tax"), ("Miscellany", "One-offs")]

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
        import json
        self._seed(db)
        db.execute(
            "INSERT INTO category_proposals (id, options, status, proposed_at) VALUES (1, ?, 'denied', NOW())",
            [json.dumps([{"parent_name": "Tax", "subcategory_name": "Self Assessment", "parent_is_new": True, "rationale": "x"}])],
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
        monkeypatch.setattr(ll, "propose_alternatives", lambda client, groups, parents, subcategories: {})
        monkeypatch.setattr(ll, "anthropic", type("M", (), {"Anthropic": lambda **kw: None}))
        monkeypatch.setattr(cp, "server_supports_proposals", lambda: True)
        monkeypatch.setattr(cp, "sync_new_proposals", lambda ids: None)

        ll.run()

        assert seen_denied_parents == [{"tax"}]
        row = db.execute(
            "SELECT options FROM category_proposals WHERE status = 'pending'"
        ).fetchone()
        options = json.loads(row[0])
        assert [(o["parent_name"], o["subcategory_name"]) for o in options] == [("Miscellaneous", "One-off Payments")], \
            "a different new name must still be proposed, not silently forced into an existing category"


class TestPassRegeneratePrompt:
    def test_includes_previous_options_examples_and_taxonomy(self):
        previous = [{"parent_name": "Tax", "subcategory_name": "Self Assessment", "parent_is_new": True, "rationale": "x"}]
        parents = [{"name": "Bills & Utilities", "transaction_count": 5}]
        subs = [{"name": "Council Tax", "parent_name": "Bills & Utilities", "transaction_count": 3}]
        prompt = _pass_regenerate_prompt(previous, ["HMRC payment"], parents, subs)
        assert "Tax › Self Assessment" in prompt
        assert "HMRC payment" in prompt
        assert "Bills & Utilities" in prompt and "Council Tax" in prompt
        assert "do not repeat them" in prompt


class TestProposeRegeneratedOptions:
    def _client(self, payload):
        client = MagicMock()
        client.messages.create.return_value.content = [MagicMock(text=json.dumps(payload))]
        return client

    def test_returns_new_options_with_parent_is_new_computed_from_taxonomy(self):
        """Never trust the model's own claim about novelty -- compute it from
        the actual taxonomy, same as the primary pick and Pass 3 alternatives."""
        previous = [{"parent_name": "Tax", "subcategory_name": "Self Assessment", "parent_is_new": True, "rationale": "x"}]
        parents = [{"name": "Bills & Utilities", "transaction_count": 5}]
        client = self._client([
            {"parent_name": "Bills & Utilities", "subcategory_name": "Government Payments", "rationale": "Different angle."},
        ])
        options = propose_regenerated_options(client, previous, ["HMRC"], parents, [])
        assert options == [{
            "parent_name": "Bills & Utilities", "subcategory_name": "Government Payments",
            "parent_is_new": False, "rationale": "Different angle.",
        }]

    def test_a_repeat_of_a_previous_option_is_dropped(self):
        previous = [{"parent_name": "Tax", "subcategory_name": "Self Assessment", "parent_is_new": True, "rationale": "x"}]
        client = self._client([
            {"parent_name": "tax", "subcategory_name": "self assessment", "rationale": "Same idea reworded."},
            {"parent_name": "Government", "subcategory_name": "Tax Returns", "rationale": "Genuinely new."},
        ])
        options = propose_regenerated_options(client, previous, [], [], [])
        assert len(options) == 1
        assert options[0]["parent_name"] == "Government"

    def test_capped_at_max_options(self):
        results = [{"parent_name": f"P{i}", "subcategory_name": f"S{i}", "rationale": "x"} for i in range(5)]
        client = self._client(results)
        options = propose_regenerated_options(client, [], [], [], [])
        assert len(options) == ll.MAX_OPTIONS

    def test_returns_empty_on_api_error(self):
        client = MagicMock()
        client.messages.create.side_effect = Exception("boom")
        assert propose_regenerated_options(client, [], [], [], []) == []


class TestRegenerateCategoryProposals:
    """End-to-end: a "Try again" tap has already been recorded as a denied,
    regenerate_requested proposal with its transactions still locked (see
    category_proposals.collect_decisions()) -- this is the step that turns
    that into a fresh proposal."""

    def _seed_denied_for_regeneration(self, db, txn_id="tx_1", old_options=None):
        db.execute(
            """INSERT INTO transactions (id, amount, currency, user_context, skipped)
               VALUES (?, -3000.0, 'GBP', 'HMRC payment', FALSE)""",
            [txn_id],
        )
        options = old_options or [{"parent_name": "Tax", "subcategory_name": "Self Assessment", "parent_is_new": True, "rationale": "x"}]
        old_id, _ = cp.register_group(options, [txn_id])
        db.execute("UPDATE category_proposals SET status = 'denied', regenerate_requested = TRUE WHERE id = ?", [old_id])
        return old_id

    def test_creates_a_fresh_proposal_and_relocks_the_transactions(self, db, monkeypatch):
        old_id = self._seed_denied_for_regeneration(db)
        new_options = [{"parent_name": "Professional Services", "subcategory_name": "Tax Filing", "parent_is_new": False, "rationale": "Different angle."}]
        monkeypatch.setattr(ll, "propose_regenerated_options", lambda client, previous, examples, parents, subs: new_options)
        monkeypatch.setattr(ll, "anthropic", type("M", (), {"Anthropic": lambda **kw: None}))
        synced = []
        monkeypatch.setattr(cp, "sync_new_proposals", lambda ids: synced.extend(ids))

        count = ll.regenerate_category_proposals()

        assert count == 1
        new_id = db.execute("SELECT id FROM category_proposals WHERE status = 'pending'").fetchone()[0]
        assert new_id != old_id
        assert json.loads(db.execute(
            "SELECT options FROM category_proposals WHERE id = ?", [new_id]).fetchone()[0]) == new_options
        assert db.execute(
            "SELECT pending_category_proposal_id FROM transactions WHERE id = 'tx_1'"
        ).fetchone()[0] == new_id
        assert synced == [new_id]

    def test_nothing_pending_is_a_no_op(self, db):
        assert ll.regenerate_category_proposals() == 0

    def test_no_claude_secret_returns_zero(self, db, monkeypatch):
        self._seed_denied_for_regeneration(db)
        monkeypatch.setattr(ll, "CLAUDE_SECRET", None)
        assert ll.regenerate_category_proposals() == 0

    def test_the_model_finding_nothing_new_leaves_the_transaction_locked_to_the_old_proposal(self, db, monkeypatch):
        old_id = self._seed_denied_for_regeneration(db)
        monkeypatch.setattr(ll, "propose_regenerated_options", lambda *a, **k: [])
        monkeypatch.setattr(ll, "anthropic", type("M", (), {"Anthropic": lambda **kw: None}))

        count = ll.regenerate_category_proposals()

        assert count == 0
        assert db.execute(
            "SELECT pending_category_proposal_id FROM transactions WHERE id = 'tx_1'"
        ).fetchone()[0] == old_id


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
