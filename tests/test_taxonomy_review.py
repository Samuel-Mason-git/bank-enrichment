import json
from datetime import date

import pytest

import taxonomy_review as tr
from taxonomy_review import (
    build_prompt, filter_proposals, run_key,
    MIN_CLUSTER_SIZE, MIN_CLUSTER_SHARE, MAX_PROPOSALS_PER_RUN,
)


# The taxonomy the reviewer is shown. "Breakfast Out" deliberately does NOT
# exist, so a create targeting it is valid and a move to it is not.
TAXONOMY = {
    "Food & Drink": ["Lunches Out", "Groceries", "Takeaway", "Coffee Shops"],
    "Transport": ["Public Transport", "Parking"],
}


def _proposal(name="Breakfast Out", n=8, action="create",
              rationale="These are breakfasts, not lunches."):
    return {"action": action, "target_sub": name, "rationale": rationale,
            "evidence_ids": [f"tx_{i}" for i in range(n)]}


def _ids(n, offset=0):
    return {f"tx_{i}" for i in range(offset, offset + n)}


def _filter(proposals, source_size=20, valid_ids=None, taxonomy=None, **kw):
    """filter_proposals with the arguments these tests almost always want."""
    return filter_proposals(
        proposals, source_size=source_size,
        valid_ids=_ids(source_size) if valid_ids is None else valid_ids,
        taxonomy=TAXONOMY if taxonomy is None else taxonomy,
        source_parent=kw.pop("source_parent", "Food & Drink"),
        source_sub=kw.pop("source_sub", "Lunches Out"),
        **kw,
    )


class TestRunKey:
    def test_is_one_per_calendar_month(self):
        assert run_key(date(2026, 8, 13)) == "2026-08"
        assert run_key(date(2026, 8, 1)) == run_key(date(2026, 8, 31))
        assert run_key(date(2026, 9, 1)) != run_key(date(2026, 8, 31))


class TestGuardrails:
    def test_accepts_a_clear_cluster(self):
        kept = _filter([_proposal(n=8)])
        assert len(kept) == 1
        assert kept[0]["target_sub"] == "Breakfast Out"
        assert kept[0]["action"] == "create"
        assert kept[0]["evidence_count"] == 8

    def test_rejects_a_cluster_below_the_size_floor(self):
        """The breakfast case: real, coherent, but too small to reshape the
        taxonomy over. Deliberately blocked at the strict setting."""
        kept = _filter([_proposal(n=MIN_CLUSTER_SIZE - 1)])
        assert kept == []

    def test_rejects_a_cluster_that_is_too_small_a_share_of_its_parent(self):
        # 8 transactions clears the size floor, but out of 100 it is only 8%.
        kept = _filter([_proposal(n=8)], source_size=100)
        assert kept == []

    def test_rejects_a_cluster_covering_nearly_everything(self):
        """That is a rename, not a split -- and renaming is already a one-click
        job in the Settings tab."""
        kept = _filter([_proposal(n=19)])
        assert kept == []

    def test_rejects_a_name_that_already_exists(self):
        kept = _filter([_proposal(name="Groceries")])
        assert kept == []

    def test_name_collision_is_case_insensitive(self):
        kept = _filter([_proposal(name="GROCERIES")])
        assert kept == []

    def test_rejects_a_proposal_with_no_name_or_no_rationale(self):
        assert _filter([_proposal(name="  ")]) == []
        assert _filter([_proposal(rationale="")]) == []

    def test_ignores_evidence_ids_that_are_not_in_this_subcategory(self):
        """The model echoes ids back from the prompt. An id from somewhere else
        would move a transaction the user never saw on the card, so unknown ids
        are dropped -- which here drops the proposal below the size floor."""
        p = _proposal(n=8)
        p["evidence_ids"] += ["tx_from_another_category"]
        kept = _filter([p], valid_ids=_ids(4))
        assert kept == []

    def test_duplicate_evidence_ids_are_not_double_counted(self):
        """Six distinct ids repeated would otherwise look like twelve and clear
        the floor on a cluster that is really half the size."""
        p = _proposal(n=4)
        p["evidence_ids"] = p["evidence_ids"] * 3
        kept = _filter([p])
        assert kept == []

    def test_caps_the_number_of_proposals(self):
        # Each cluster uses a distinct block of ids and is 25% of a 32-row
        # parent, so every one of them clears the guardrails on its own merits
        # and only the cap can reduce the count.
        many = []
        for i in range(6):
            p = _proposal(name=f"Cluster {i}")
            p["evidence_ids"] = [f"tx_{j}" for j in range(i * 8, i * 8 + 8)]
            many.append(p)
        kept = _filter(many, source_size=32, valid_ids=_ids(48))
        assert len(kept) == MAX_PROPOSALS_PER_RUN

    def test_cap_keeps_the_largest_clusters(self):
        small, large = _proposal(name="Small", n=6), _proposal(name="Large", n=12)
        kept = _filter([small, large])
        assert kept[0]["target_sub"] == "Large"

    def test_empty_response_is_fine(self):
        assert _filter([]) == []

    def test_zero_sized_parent_does_not_divide_by_zero(self):
        assert _filter([_proposal()], source_size=0) == []

    def test_share_boundary_is_inclusive(self):
        # Exactly at the floor: 5 of 20 is 25%, which should pass on size 6+.
        kept = _filter([_proposal(n=6)], source_size=24, valid_ids=_ids(24))
        assert len(kept) == 1


class TestPrompt:
    def test_lists_every_transaction_with_its_id(self):
        rows = [{"id": "tx_a", "amount": -3.5, "merchant": "Tesco", "context": "Meal deal"},
                {"id": "tx_b", "amount": -9.0, "merchant": "Subway", "context": "Breakfast"}]
        prompt = build_prompt("Food & Drink", "Lunches Out", rows)
        assert "tx_a" in prompt and "tx_b" in prompt
        assert "Meal deal" in prompt and "Breakfast" in prompt
        assert "Lunches Out" in prompt and "Food & Drink" in prompt

    def test_states_the_size_floor_so_the_model_does_not_waste_effort(self):
        prompt = build_prompt("P", "S", [{"id": "tx_a", "amount": -1, "merchant": None, "context": None}])
        assert str(MIN_CLUSTER_SIZE) in prompt

    def test_tells_the_model_that_proposing_nothing_is_correct(self):
        prompt = build_prompt("P", "S", [{"id": "tx_a", "amount": -1, "merchant": None, "context": None}])
        assert "empty" in prompt.lower()

    def test_handles_missing_merchant_and_context(self):
        prompt = build_prompt("P", "S", [{"id": "tx_a", "amount": -1, "merchant": None, "context": None}])
        assert "tx_a" in prompt


class TestDatabaseHelpers:
    def test_reviewable_subcategories_respects_the_size_floor(self, db, monkeypatch):
        monkeypatch.setattr(tr, "MIN_SUBCATEGORY_SIZE", 3)
        for i in range(4):
            db.execute("""INSERT INTO transactions (id, amount, currency, llm_category, llm_subcategory, created_at)
                          VALUES (?, -1, 'GBP', 'Food & Drink', 'Lunches Out', NOW())""", [f"big_{i}"])
        db.execute("""INSERT INTO transactions (id, amount, currency, llm_category, llm_subcategory, created_at)
                      VALUES ('small_1', -1, 'GBP', 'Food & Drink', 'Snacks', NOW())""")
        names = [s["subcategory"] for s in tr.reviewable_subcategories()]
        assert names == ["Lunches Out"]

    def test_already_reviewed_is_false_before_any_run(self, db):
        assert tr.already_reviewed("2026-08") is False

    def test_empty_run_is_recorded_so_the_month_is_not_retried(self, db):
        tr._record_empty_run("2026-08")
        assert tr.already_reviewed("2026-08") is True
        status = db.execute("SELECT status FROM taxonomy_proposals").fetchone()[0]
        assert status == "expired", "the sentinel must never reach Telegram"

    def test_previously_proposed_names_blocks_a_denied_idea_returning(self, db):
        tr._record_empty_run("2026-07")
        db.execute("""INSERT INTO taxonomy_proposals
            (id, parent_name, source_sub, proposed_sub, rationale, evidence_ids,
             evidence_count, status, proposed_at, run_key)
            VALUES (99, 'Food & Drink', 'Lunches Out', 'Meal Deals', 'x', '[]', 8,
                    'denied', NOW(), '2026-07')""")
        assert "meal deals" in tr.previously_proposed_names()

    def test_store_proposals_persists_the_evidence(self, db):
        stored = tr.store_proposals([{
            "parent_name": "Food & Drink", "source_sub": "Lunches Out",
            "action": "create", "target_parent": "Food & Drink",
            "target_sub": "Meal Deals", "rationale": "Desk lunches.",
            "evidence_ids": ["tx_1", "tx_2"], "evidence_count": 2,
        }], "2026-08")
        assert stored[0]["id"] == 1
        row = db.execute("""SELECT proposed_sub, evidence_ids, status
                            FROM taxonomy_proposals WHERE id = 1""").fetchone()
        assert row[0] == "Meal Deals"
        assert json.loads(row[1]) == ["tx_1", "tx_2"]
        assert row[2] == "pending"


class TestPromptDiscouragesOverSplitting:
    """The first version of this prompt used 'a supermarket meal deal is a
    different habit from a sit-down restaurant lunch' as its example of a GOOD
    split. It is not -- both are lunch, and splitting on venue or price is
    exactly the over-granularity that makes a taxonomy unmaintainable. These
    pin the corrected framing so that example cannot creep back in."""

    def _prompt(self):
        return build_prompt("Food & Drink", "Lunches Out",
                            [{"id": "tx_a", "amount": -3.95, "merchant": "Tesco",
                              "context": "Meal deal"}])

    def test_venue_is_named_as_an_invalid_reason_to_split(self):
        p = self._prompt().lower()
        assert "where it was bought" in p
        assert "both lunch" in p

    def test_price_and_format_are_named_as_invalid_reasons(self):
        p = self._prompt().lower()
        assert "format, brand, or price" in p

    def test_the_valid_example_is_a_different_kind_of_thing_not_a_venue(self):
        p = self._prompt()
        assert "a breakfast is not a lunch" in p

    def test_the_test_is_framed_as_the_name_being_wrong(self):
        """'Is this a distinct cluster' finds meal deals. 'Does the name
        misdescribe these' finds breakfasts. Only the second is what we want."""
        p = self._prompt().upper()
        assert "MISDESCRIBES" in p


class TestMoveIntoExistingCategory:
    """Without the taxonomy in the prompt, "create a new subcategory" was the
    reviewer's only possible answer -- so a group that already had a perfectly
    good home would get a near-duplicate category built for it instead."""

    def test_a_move_into_an_existing_subcategory_is_accepted(self):
        kept = _filter([_proposal(name="Takeaway", action="move")])
        assert len(kept) == 1
        assert kept[0]["action"] == "move"
        assert kept[0]["target_sub"] == "Takeaway"
        assert kept[0]["target_parent"] == "Food & Drink"

    def test_a_move_can_target_a_different_parent(self):
        kept = _filter([_proposal(name="Parking", action="move")])
        assert kept[0]["target_parent"] == "Transport", \
            "the parent must follow the target, or the row lands in a parent that has no such subcategory"

    def test_a_move_to_a_category_that_does_not_exist_is_rejected(self):
        """The mirror of the create rule. Moving into a non-existent
        subcategory would leave transactions pointing at nothing."""
        assert _filter([_proposal(name="Breakfast Out", action="move")]) == []

    def test_a_move_back_into_the_source_subcategory_is_rejected(self):
        assert _filter([_proposal(name="Lunches Out", action="move")]) == []

    def test_move_target_casing_is_normalised_to_the_real_name(self):
        """The card and the UPDATE must use the stored name, not the model's
        casing, or the move silently matches nothing."""
        kept = _filter([_proposal(name="takeaway", action="move")])
        assert kept[0]["target_sub"] == "Takeaway"

    def test_an_unknown_action_is_rejected(self):
        assert _filter([_proposal(action="rename")]) == []
        assert _filter([_proposal(action="delete")]) == []

    def test_a_missing_action_falls_back_to_create(self):
        """Create is the safer default of the two: it is checked against the
        existing taxonomy and cannot drop transactions into an unrelated
        category that already exists."""
        p = _proposal()
        del p["action"]
        kept = _filter([p])
        assert len(kept) == 1 and kept[0]["action"] == "create"

    def test_a_create_of_a_name_proposed_in_an_earlier_month_is_rejected(self):
        """Whatever they answered last time, asking again is nagging."""
        assert _filter([_proposal(name="Breakfast Out")],
                       blocked_names={"breakfast out"}) == []

    def test_blocked_names_do_not_stop_a_move(self):
        """Blocking applies to inventing a name, not to using a category that
        genuinely exists in the taxonomy today."""
        kept = _filter([_proposal(name="Takeaway", action="move")],
                       blocked_names={"takeaway"})
        assert len(kept) == 1


class TestPromptShowsTheTaxonomy:
    def _prompt(self):
        return build_prompt("Food & Drink", "Lunches Out",
                            [{"id": "tx_a", "amount": -3.95, "merchant": "Tesco",
                              "context": "Meal deal"}], TAXONOMY)

    def test_existing_subcategories_are_listed(self):
        p = self._prompt()
        assert "Takeaway" in p and "Coffee Shops" in p and "Public Transport" in p

    def test_moving_is_preferred_when_something_already_fits(self):
        p = self._prompt()
        assert "Prefer this whenever something in that list genuinely fits" in p

    def test_creating_is_not_discouraged_when_nothing_fits(self):
        """An earlier wording ("ALWAYS prefer move") read as a blanket warning
        against creating anything, and measurably silenced the reviewer: it went
        from proposing a valid new category to proposing nothing at all across
        every subcategory. Preferring a move is a tie-breaker, not a veto."""
        p = self._prompt()
        assert "perfectly good answer" in p
        assert "do not force a bad \"move\"" in p

    def test_both_actions_are_described(self):
        p = self._prompt()
        assert '"move"' in p and '"create"' in p

    def test_omitting_the_taxonomy_still_produces_a_usable_prompt(self):
        """reviewable subcategories are read from the DB, so an empty taxonomy
        is possible on a brand-new database."""
        p = build_prompt("P", "S", [{"id": "tx_a", "amount": -1,
                                     "merchant": None, "context": None}])
        assert "tx_a" in p
        assert "Your existing categories" not in p
