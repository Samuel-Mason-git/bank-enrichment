"""Rules can match on Monzo's own payment scheme.

Matching a pot transfer on its description meant matching the substring 'pot_',
which also appears in HOME DEPOT_, JACKPOT_ and SPOT_. Monzo already labels the
movement itself as 'uk_retail_pot', which cannot collide with a merchant name.
"""
from types import SimpleNamespace

import pytest

from check_rules import _extract_field, _matches, check_rules


def _txn(scheme="uk_retail_pot", description="pot_0000B7cenLixHWSDlwwxKD",
         merchant=None, amount=10000, category="general"):
    return SimpleNamespace(
        description=description, category=category, amount=amount,
        merchant={"name": merchant} if merchant else None, counterparty=None,
        scheme=scheme,
    )


def _add_rule(con, match_field="scheme", match_type="exact",
              match_value="uk_retail_pot", context="Moved between Monzo pot and account",
              skip=False):
    con.execute(
        """INSERT INTO rules (id, name, match_field, match_type, match_value,
                              auto_context, enabled, auto_skip)
           VALUES (1, 'Monzo pot transfer', ?, ?, ?, ?, TRUE, ?)""",
        [match_field, match_type, match_value, context, skip])


class TestSchemeIsExtractable:
    def test_scheme_is_read_from_the_transaction(self):
        assert _extract_field(_txn(), "scheme") == "uk_retail_pot"

    def test_missing_scheme_is_none_not_an_error(self):
        """Older stored payloads and hand-built test transactions have no
        scheme, and a rule referencing it must simply not match."""
        bare = SimpleNamespace(description="x", category="general", amount=1,
                               merchant=None, counterparty=None)
        assert _extract_field(bare, "scheme") is None

    def test_an_unknown_field_is_still_none(self):
        assert _extract_field(_txn(), "not_a_field") is None


class TestPotRuleMatchesTheRightThings:
    def test_a_pot_transfer_matches(self, server_con):
        _add_rule(server_con)
        assert check_rules(_txn()) == ("Moved between Monzo pot and account", False)

    def test_a_card_payment_does_not_match(self, server_con):
        _add_rule(server_con)
        card = _txn(scheme="mastercard", description="MORRISONS MILTON KEYNES",
                    merchant="Morrisons", amount=-891)
        assert check_rules(card) == (None, False)

    def test_a_merchant_whose_name_contains_pot_does_not_match(self, server_con):
        """The whole reason for using scheme. Under a description `contains
        'pot_'` rule this would have been silently auto-enriched as a pot
        transfer and never shown to the user."""
        _add_rule(server_con)
        depot = _txn(scheme="mastercard", description="HOME DEPOT_STORE 42",
                     merchant="Home Depot", amount=-4500)
        assert check_rules(depot) == (None, False)

    def test_a_transaction_with_no_scheme_does_not_match(self, server_con):
        _add_rule(server_con)
        assert check_rules(_txn(scheme=None)) == (None, False)

    def test_the_rule_does_not_skip_so_the_transfer_stays_visible(self, server_con):
        """Skipping hides it from the dashboard entirely. The transfer role
        already keeps it out of income and spend while remaining visible."""
        _add_rule(server_con)
        _, skipped = check_rules(_txn())
        assert skipped is False


class TestDescriptionMatchingIsStillAvailable:
    def test_an_anchored_regex_on_description_also_works(self, server_con):
        """The no-deploy alternative, kept working so either approach is valid."""
        _add_rule(server_con, match_field="description", match_type="regex",
                  match_value=r"^pot_[A-Za-z0-9]+$")
        assert check_rules(_txn())[0] == "Moved between Monzo pot and account"

    def test_the_anchored_regex_rejects_a_merchant_containing_pot(self, server_con):
        _add_rule(server_con, match_field="description", match_type="regex",
                  match_value=r"^pot_[A-Za-z0-9]+$")
        assert check_rules(_txn(description="HOME DEPOT_STORE 42")) == (None, False)

    def test_an_unanchored_contains_rule_is_the_trap(self, server_con):
        """Documents why the anchor matters: this is what a naive rule does."""
        _add_rule(server_con, match_field="description", match_type="contains",
                  match_value="pot_")
        assert check_rules(_txn(description="HOME DEPOT_STORE 42"))[0] is not None
