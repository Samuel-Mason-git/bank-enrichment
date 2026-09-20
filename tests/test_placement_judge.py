import json
from types import SimpleNamespace

import anthropic
import httpx
import pytest

import category_proposals as cp
import database_functions as dbf
import llm_labelling as ll
import placement_judge as pj
from database_functions import upsert_parent, upsert_subcategory


# ── helpers ────────────────────────────────────────────────────────────────────

class FakeClient:
    """Stands in for anthropic.Anthropic. respond(prompt) returns a verdict dict
    (or JSON string), or an Exception to raise."""

    def __init__(self, respond):
        self.respond = respond
        self.calls = []
        self.messages = self

    def create(self, model, max_tokens, messages):
        prompt = messages[0]["content"]
        self.calls.append(prompt)
        out = self.respond(prompt)
        if isinstance(out, Exception):
            raise out
        text = out if isinstance(out, str) else json.dumps(out)
        return SimpleNamespace(content=[SimpleNamespace(text=text)])


def reject(parent, sub, confidence=0.9, reason="It is a one-off, not a monthly bill."):
    return {"verdict": "reject", "confidence": confidence, "issue": "distorts_totals", "reason": reason,
            "suggested_parent": parent, "suggested_subcategory": sub, "suggested_is_new": True}


APPROVE = {"verdict": "approve", "confidence": 0.9, "issue": "none", "reason": "Fits.",
           "suggested_parent": None, "suggested_subcategory": None, "suggested_is_new": False}


def add_txn(db, tid, amount=-10.0, description="x", context=None, merchant=None, counterparty=None,
            category=None, sub=None, created="2026-08-01 10:00:00", model=None):
    db.execute(
        """INSERT INTO transactions (id, amount, currency, description, user_context, merchant_name,
               counterparty_name, created_at, llm_category, llm_subcategory, llm_model, skipped)
           VALUES (?, ?, 'GBP', ?, ?, ?, ?, ?, ?, ?, ?, FALSE)""",
        [tid, amount, description, context, merchant, counterparty, created, category, sub, model],
    )


def txn(db, tid):
    return dbf._rows("SELECT * FROM transactions WHERE id = ?", [tid])[0]


def seed_taxonomy(db):
    bills = upsert_parent("Bills & Utilities")
    upsert_subcategory("Rent", bills)
    upsert_subcategory("Electricity", bills)
    upsert_parent("Food & Drink")


def make_reviewer(client, gate=True):
    return pj.PlacementReviewer(client, "test-model", lambda t: f"TXN {t['id']} {t.get('user_context')}",
                                gate_check=lambda: gate)


def outcome(db, tid):
    row = db.execute("SELECT outcome FROM judge_reviews WHERE txn_id = ?", [tid]).fetchone()
    return row[0] if row else None


# ── verdict handling ───────────────────────────────────────────────────────────

class TestIsObjection:
    def test_a_confident_reject_with_a_different_suggestion_counts(self):
        assert pj.is_objection(reject("Bills & Utilities", "Rent Deposit"), "Bills & Utilities", "Rent")

    def test_an_approval_is_not_an_objection(self):
        assert not pj.is_objection(APPROVE, "Bills & Utilities", "Rent")

    def test_below_the_confidence_floor_is_ignored(self):
        assert not pj.is_objection(reject("Bills & Utilities", "Rent Deposit", confidence=0.7), "Bills & Utilities", "Rent")

    def test_the_floor_itself_counts(self):
        assert pj.is_objection(reject("A", "B", confidence=pj.MIN_CONFIDENCE), "Bills & Utilities", "Rent")

    def test_suggesting_the_placement_it_is_objecting_to_is_not_actionable(self):
        """The judge does this when its real complaint is the amount -- the card
        would offer the same thing twice."""
        assert not pj.is_objection(reject("bills & utilities", " RENT "), "Bills & Utilities", "Rent")

    @pytest.mark.parametrize("parent,sub", [(None, "X"), ("X", None), ("", "X"), ("X", "  ")])
    def test_a_rejection_with_nothing_to_offer_is_not_actionable(self, parent, sub):
        assert not pj.is_objection(reject(parent, sub), "Bills & Utilities", "Rent")

    def test_a_garbage_confidence_is_not_an_objection(self):
        verdict = reject("A", "B")
        verdict["confidence"] = "high"
        assert not pj.is_objection(verdict, "X", "Y")


class TestHoldOptions:
    def test_offers_the_suggestion_then_keep_it_and_marks_both_as_a_judge_card(self):
        options = pj.hold_options("Bills & Utilities", "Rent", reject("Bills & Utilities", "Rent Deposit"), {"bills & utilities"})
        assert options[0] == {
            "parent_name": "Bills & Utilities", "subcategory_name": "Rent Deposit", "parent_is_new": False,
            "rationale": "Second look: It is a one-off, not a monthly bill.", "judge": True,
        }
        assert options[1] == {
            "parent_name": "Bills & Utilities", "subcategory_name": "Rent", "parent_is_new": False,
            "rationale": "Keep it where it was.", "judge": True, "is_original": True,
        }

    def test_a_suggested_parent_that_does_not_exist_is_flagged_new(self):
        options = pj.hold_options("Food & Drink", "Groceries", reject("Housing", "Deposits"), {"food & drink"})
        assert options[0]["parent_is_new"] is True

    def test_a_long_reason_is_trimmed_for_the_phone_card(self):
        options = pj.hold_options("A", "B", reject("C", "D", reason="word " * 200), set())
        assert len(options[0]["rationale"]) <= len("Second look: ") + pj.MAX_RATIONALE


class TestMerchantKey:
    def test_prefers_merchant_then_counterparty_lowercased(self):
        assert pj.merchant_key({"merchant_name": " Lime ", "counterparty_name": "x"}) == "lime"
        assert pj.merchant_key({"merchant_name": None, "counterparty_name": "Rhea D"}) == "rhea d"

    def test_none_when_there_is_nothing_to_generalise_from(self):
        assert pj.merchant_key({"merchant_name": None, "counterparty_name": None}) is None
        assert pj.merchant_key({"merchant_name": "  "}) is None


# ── subcategory profile ────────────────────────────────────────────────────────

class TestSubcategoryStats:
    def test_regular_identical_payments_read_as_a_rhythm(self, db):
        for i, day in enumerate(["2026-05-01", "2026-06-01", "2026-07-01", "2026-08-01"]):
            add_txn(db, f"r{i}", amount=-1315.0, category="Bills & Utilities", sub="Rent", created=f"{day} 09:00:00")
        stat = dbf.get_subcategory_stats()[("Bills & Utilities", "Rent")]
        assert stat["count"] == 4 and stat["min"] == stat["max"] == 1315.0
        assert stat["gap_days"] == 31
        assert dbf.format_subcategory_stats(stat) == "amounts always £1315.00; typically one every 31 day(s)"

    def test_two_transactions_are_not_enough_to_claim_a_rhythm(self, db):
        add_txn(db, "a", amount=-5.0, category="Food & Drink", sub="Snacks", created="2026-08-01 09:00:00")
        add_txn(db, "b", amount=-9.0, category="Food & Drink", sub="Snacks", created="2026-08-09 09:00:00")
        stat = dbf.get_subcategory_stats()[("Food & Drink", "Snacks")]
        assert stat["gap_days"] is None
        assert dbf.format_subcategory_stats(stat) == "amounts £5.00-£9.00, median £7.00; only 2 so far, no clear rhythm"

    def test_amounts_are_absolute_so_refunds_and_spend_share_a_scale(self, db):
        add_txn(db, "a", amount=-20.0, category="X", sub="Y")
        add_txn(db, "b", amount=20.0, category="X", sub="Y")
        assert dbf.get_subcategory_stats()[("X", "Y")]["min"] == 20.0

    def test_unclassified_transactions_are_ignored(self, db):
        add_txn(db, "a", amount=-20.0)
        assert dbf.get_subcategory_stats() == {}


class TestPromptsCarryStats:
    SUBS = [{"name": "Rent", "parent_name": "Bills & Utilities", "transaction_count": 4}]

    def test_pass0_and_pass2_are_unchanged_without_stats(self):
        assert "amounts" not in ll._pass0_prompt([], self.SUBS)
        assert "rhythm" not in ll._pass2_prompt([], "Bills & Utilities", self.SUBS, ["Bills & Utilities"])

    def test_pass0_prints_stats_and_the_instruction_to_use_them(self):
        subs = [{**self.SUBS[0], "stats": "amounts always £1315.00; typically one every 31 day(s)"}]
        prompt = ll._pass0_prompt([], subs)
        assert "Rent (under: Bills & Utilities) — amounts always £1315.00" in prompt
        assert "one-off payment next to a subcategory of regular recurring ones" in prompt

    def test_pass2_prints_stats_and_the_instruction_to_use_them(self):
        subs = [{**self.SUBS[0], "stats": "amounts always £1315.00; typically one every 31 day(s)"}]
        prompt = ll._pass2_prompt([], "Bills & Utilities", subs, ["Bills & Utilities"])
        assert "Rent (4 transactions) — amounts always £1315.00" in prompt
        assert "propose a new, accurately named subcategory rather than forcing the fit" in prompt

    def test_with_stats_only_annotates_subcategories_that_have_history(self, db):
        seed_taxonomy(db)
        add_txn(db, "a", amount=-1315.0, category="Bills & Utilities", sub="Rent")
        by_name = {s["name"]: s for s in ll._with_stats(dbf.get_subcategories())}
        assert "stats" in by_name["Rent"]
        assert "stats" not in by_name["Electricity"]


# ── reviewer ───────────────────────────────────────────────────────────────────

class TestReview:
    def test_an_objection_is_returned_and_an_approval_is_recorded(self, db):
        seed_taxonomy(db)
        add_txn(db, "dep", amount=-282.0, context="Holding deposit")
        add_txn(db, "rent", amount=-1315.0, context="Rent")
        client = FakeClient(lambda p: reject("Bills & Utilities", "Rent Deposit") if "Holding deposit" in p else APPROVE)
        reviewer = make_reviewer(client)

        result = reviewer.review([
            (txn(db, "dep"), "Bills & Utilities", "Rent"), (txn(db, "rent"), "Bills & Utilities", "Rent"),
        ])

        assert list(result.objections) == ["dep"] and result.unavailable == set()
        assert outcome(db, "rent") == "approved"
        assert outcome(db, "dep") is None, "an objection is only recorded once it has actually been held"

    def test_the_prompt_carries_the_placement_its_history_and_the_merchant(self, db):
        seed_taxonomy(db)
        for i, day in enumerate(["2026-05-01", "2026-06-01", "2026-07-01"]):
            add_txn(db, f"r{i}", amount=-1315.0, description="Flat 411", category="Bills & Utilities", sub="Rent",
                    created=f"{day} 09:00:00")
        add_txn(db, "new", amount=-1315.0, merchant="Cauldwell", context="Rent for flat")
        add_txn(db, "old", amount=-500.0, merchant="Cauldwell", category="Bills & Utilities", sub="Electricity")
        client = FakeClient(lambda p: APPROVE)

        make_reviewer(client).review([(txn(db, "new"), "Bills & Utilities", "Rent")])

        prompt = client.calls[0]
        assert "Proposed placement: Bills & Utilities > Rent" in prompt
        assert "3 past transactions; amounts £1315.00-£1315.00" in prompt
        assert "description=Flat 411" in prompt
        assert "1 other transaction(s)" in prompt and "Bills & Utilities > Electricity ×1" in prompt
        assert "Bills & Utilities: Electricity, Rent" in prompt

    def test_the_judge_is_told_what_the_categories_are_for(self, db):
        seed_taxonomy(db)
        add_txn(db, "a")
        client = FakeClient(lambda p: APPROVE)
        make_reviewer(client).review([(txn(db, "a"), "Bills & Utilities", "Rent")])
        assert "aggregates spending by parent category and subcategory" in client.calls[0]

    def test_an_api_failure_fails_closed_and_records_nothing(self, db):
        """A judge that steps aside whenever the API hiccups is a check that only
        works when nothing is wrong -- the placement must NOT be saved unreviewed."""
        seed_taxonomy(db)
        add_txn(db, "a")
        reviewer = make_reviewer(FakeClient(lambda p: RuntimeError("boom")))
        result = reviewer.review([(txn(db, "a"), "Bills & Utilities", "Rent")])
        assert result.unavailable == {"a"} and result.objections == {}
        assert reviewer.unavailable_count == 1
        assert outcome(db, "a") is None, "no verdict was reached, so it must stay reviewable"

    def test_unparseable_output_fails_closed(self, db):
        seed_taxonomy(db)
        add_txn(db, "a")
        reviewer = make_reviewer(FakeClient(lambda p: "I think this is fine."))
        assert reviewer.review([(txn(db, "a"), "Bills & Utilities", "Rent")]).unavailable == {"a"}

    def test_a_credit_error_stops_the_calls_but_still_holds_everything_back(self, db):
        seed_taxonomy(db)
        add_txn(db, "a")
        add_txn(db, "b")
        err = anthropic.BadRequestError(
            "Your credit balance is too low to access the Anthropic API",
            response=httpx.Response(400, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")), body=None,
        )
        client = FakeClient(lambda p: err)
        reviewer = make_reviewer(client)

        assert reviewer.review([(txn(db, "a"), "Bills & Utilities", "Rent")]).unavailable == {"a"}
        second = reviewer.review([(txn(db, "b"), "Bills & Utilities", "Rent")])
        assert second.unavailable == {"b"}, "out of credit is not a reason to start saving things unreviewed"
        assert len(client.calls) == 1, "the second review must not even try the API"

    def test_only_the_transactions_that_failed_are_held_back(self, db):
        seed_taxonomy(db)
        add_txn(db, "ok", context="fine")
        add_txn(db, "bad", context="explode")
        client = FakeClient(lambda p: RuntimeError("boom") if "explode" in p else APPROVE)
        result = make_reviewer(client).review([
            (txn(db, "ok"), "Bills & Utilities", "Rent"), (txn(db, "bad"), "Bills & Utilities", "Rent")])
        assert result.unavailable == {"bad"} and outcome(db, "ok") == "approved"

    def test_a_transaction_already_reviewed_is_not_reviewed_again(self, db):
        seed_taxonomy(db)
        add_txn(db, "a")
        db.execute("INSERT INTO judge_reviews VALUES ('a', NULL, 'Bills & Utilities', 'Rent', 'held', NOW())")
        client = FakeClient(lambda p: reject("X", "Y"))
        result = make_reviewer(client).review([(txn(db, "a"), "Bills & Utilities", "Rent")])
        assert result.objections == {} and result.unavailable == set()
        assert client.calls == []

    def test_a_kept_placement_is_not_questioned_again_for_the_same_merchant(self, db):
        seed_taxonomy(db)
        add_txn(db, "old", merchant="Lime")
        db.execute("INSERT INTO judge_reviews VALUES ('old', 'lime', 'Transport', 'Commuting', 'kept', NOW())")
        add_txn(db, "new", merchant="LIME ")
        client = FakeClient(lambda p: reject("X", "Y"))

        result = make_reviewer(client).review([(txn(db, "new"), "Transport", "Commuting")])
        assert result.objections == {} and result.unavailable == set()
        assert client.calls == []

    def test_keeping_one_placement_does_not_silence_the_merchant_elsewhere(self, db):
        seed_taxonomy(db)
        add_txn(db, "old", merchant="Lime")
        db.execute("INSERT INTO judge_reviews VALUES ('old', 'lime', 'Transport', 'Commuting', 'kept', NOW())")
        add_txn(db, "new", merchant="Lime")
        client = FakeClient(lambda p: reject("X", "Y"))

        assert "new" in make_reviewer(client).review([(txn(db, "new"), "Health", "Optical")]).objections

    def test_a_declined_or_changed_outcome_is_not_a_standing_answer(self, db):
        seed_taxonomy(db)
        add_txn(db, "old", merchant="Lime")
        db.execute("INSERT INTO judge_reviews VALUES ('old', 'lime', 'Transport', 'Commuting', 'changed', NOW())")
        add_txn(db, "new", merchant="Lime")
        client = FakeClient(lambda p: reject("X", "Y"))
        assert "new" in make_reviewer(client).review([(txn(db, "new"), "Transport", "Commuting")]).objections

    @pytest.mark.parametrize("why", ["no client", "gate off", "switched off"])
    def test_a_deliberately_disabled_judge_passes_everything_through(self, db, monkeypatch, why):
        """Off on purpose is not the same as failing: these are the release
        valve, so nothing may be held back."""
        seed_taxonomy(db)
        add_txn(db, "a")
        client = FakeClient(lambda p: reject("X", "Y"))
        if why == "no client":
            reviewer = make_reviewer(None)
        elif why == "gate off":
            reviewer = make_reviewer(client, gate=False)
        else:
            monkeypatch.setattr(pj, "JUDGE_ENABLED", False)
            reviewer = make_reviewer(client)
        result = reviewer.review([(txn(db, "a"), "Bills & Utilities", "Rent")])
        assert result.objections == {} and result.unavailable == set()
        assert client.calls == []


class TestHold:
    def _hold(self, db, reviewer, ids, suggestion=("Bills & Utilities", "Rent Deposit")):
        items = [(txn(db, i), "Bills & Utilities", "Rent", reject(*suggestion)) for i in ids]
        return reviewer.hold(items)

    def test_locks_the_transactions_behind_a_two_option_card(self, db):
        seed_taxonomy(db)
        add_txn(db, "dep", context="Holding deposit")
        reviewer = make_reviewer(FakeClient(lambda p: APPROVE))

        assert self._hold(db, reviewer, ["dep"]) == 1

        row = txn(db, "dep")
        assert row["llm_category"] is None and row["pending_category_proposal_id"] is not None
        options = json.loads(db.execute("SELECT options FROM category_proposals").fetchone()[0])
        assert [(o["parent_name"], o["subcategory_name"]) for o in options] == [
            ("Bills & Utilities", "Rent Deposit"), ("Bills & Utilities", "Rent")]
        assert options[1]["is_original"] is True and all(o["judge"] for o in options)
        assert reviewer.new_proposal_ids == [row["pending_category_proposal_id"]]
        assert outcome(db, "dep") == "held"

    def test_identical_objections_share_one_card(self, db):
        seed_taxonomy(db)
        add_txn(db, "a")
        add_txn(db, "b")
        reviewer = make_reviewer(FakeClient(lambda p: APPROVE))
        self._hold(db, reviewer, ["a", "b"])
        assert db.execute("SELECT COUNT(*) FROM category_proposals").fetchone()[0] == 1
        assert txn(db, "a")["pending_category_proposal_id"] == txn(db, "b")["pending_category_proposal_id"]

    def test_different_suggestions_get_separate_cards(self, db):
        seed_taxonomy(db)
        add_txn(db, "a")
        add_txn(db, "b")
        reviewer = make_reviewer(FakeClient(lambda p: APPROVE))
        reviewer.hold([(txn(db, "a"), "Bills & Utilities", "Rent", reject("Bills & Utilities", "Rent Deposit"))])
        reviewer.hold([(txn(db, "b"), "Bills & Utilities", "Rent", reject("Transfers", "Outbound Transfer"))])
        assert db.execute("SELECT COUNT(*) FROM category_proposals").fetchone()[0] == 2

    def test_sync_sends_new_proposals_once(self, db, monkeypatch):
        seed_taxonomy(db)
        add_txn(db, "a")
        reviewer = make_reviewer(FakeClient(lambda p: APPROVE))
        self._hold(db, reviewer, ["a"])
        sent = []
        monkeypatch.setattr(cp, "sync_new_proposals", lambda ids: sent.append(list(ids)))
        reviewer.sync()
        reviewer.sync()
        assert len(sent) == 1 and len(sent[0]) == 1


class TestQuickTapReview:
    def _setup(self, db):
        seed_taxonomy(db)
        food = upsert_parent("Food & Drink")
        upsert_subcategory("Alcohol", food)
        health = upsert_parent("Health")
        upsert_subcategory("Dental", health)
        add_txn(db, "dentist", amount=-25.0, context="Food & Drink - Alcohol", merchant="The Hub Dental")

    def test_a_tap_the_judge_objects_to_is_held_not_classified(self, db):
        self._setup(db)
        client = FakeClient(lambda p: reject("Health", "Dental", confidence=0.99))
        reviewer = make_reviewer(client)

        count = dbf.apply_quick_tap_classifications(review=reviewer.review_tap)

        row = txn(db, "dentist")
        assert count == 0
        assert row["llm_category"] is None and row["pending_category_proposal_id"] is not None
        options = json.loads(db.execute("SELECT options FROM category_proposals").fetchone()[0])
        assert options[1]["is_original"] and (options[1]["parent_name"], options[1]["subcategory_name"]) == ("Food & Drink", "Alcohol")

    def test_a_held_tap_is_not_matched_and_reviewed_again_on_the_next_run(self, db):
        self._setup(db)
        client = FakeClient(lambda p: reject("Health", "Dental", confidence=0.99))
        reviewer = make_reviewer(client)
        dbf.apply_quick_tap_classifications(review=reviewer.review_tap)
        assert dbf.apply_quick_tap_classifications(review=reviewer.review_tap) == 0
        assert len(client.calls) == 1

    def test_a_declined_card_leaves_the_tap_as_tapped_rather_than_asking_again(self, db):
        self._setup(db)
        client = FakeClient(lambda p: reject("Health", "Dental", confidence=0.99))
        reviewer = make_reviewer(client)
        dbf.apply_quick_tap_classifications(review=reviewer.review_tap)
        cp.deny_all(txn(db, "dentist")["pending_category_proposal_id"])

        assert dbf.apply_quick_tap_classifications(review=reviewer.review_tap) == 1
        assert txn(db, "dentist")["llm_subcategory"] == "Alcohol"
        assert len(client.calls) == 1

    def test_a_tap_is_not_applied_while_the_judge_is_unavailable_and_is_retried_later(self, db):
        """Fail closed for taps too: a mis-tap must not slip through just because
        the API was down when it arrived. The tap isn't lost -- it is picked up
        again on the next run."""
        self._setup(db)
        down = make_reviewer(FakeClient(lambda p: RuntimeError("API down")))

        assert dbf.apply_quick_tap_classifications(review=down.review_tap) == 0
        row = txn(db, "dentist")
        assert row["llm_category"] is None and row["pending_category_proposal_id"] is None, \
            "unclassified but NOT locked, so the next run finds it again"

        back = make_reviewer(FakeClient(lambda p: APPROVE))
        assert dbf.apply_quick_tap_classifications(review=back.review_tap) == 1
        assert txn(db, "dentist")["llm_subcategory"] == "Alcohol"

    def test_an_approved_tap_is_classified_as_tapped(self, db):
        self._setup(db)
        reviewer = make_reviewer(FakeClient(lambda p: APPROVE))
        assert dbf.apply_quick_tap_classifications(review=reviewer.review_tap) == 1
        row = txn(db, "dentist")
        assert (row["llm_category"], row["llm_subcategory"], row["llm_model"]) == ("Food & Drink", "Alcohol", "quick-tap")

    def test_taps_are_untouched_without_a_review_hook(self, db):
        self._setup(db)
        assert dbf.apply_quick_tap_classifications() == 1


# ── decisions ──────────────────────────────────────────────────────────────────

class TestDecisions:
    def _held(self, db, merchant="Cauldwell"):
        seed_taxonomy(db)
        add_txn(db, "dep", merchant=merchant)
        reviewer = make_reviewer(FakeClient(lambda p: APPROVE))
        reviewer.hold([(txn(db, "dep"), "Bills & Utilities", "Rent", reject("Bills & Utilities", "Rent Deposit"))])
        return txn(db, "dep")["pending_category_proposal_id"]

    def test_choosing_keep_classifies_at_the_original_and_remembers_it(self, db):
        pid = self._held(db)
        assert cp.apply_selected(pid, 1) == 1
        row = txn(db, "dep")
        assert (row["llm_category"], row["llm_subcategory"]) == ("Bills & Utilities", "Rent")
        assert outcome(db, "dep") == "kept"

    def test_choosing_the_suggestion_creates_it_and_records_a_change(self, db):
        pid = self._held(db)
        assert cp.apply_selected(pid, 0) == 1
        row = txn(db, "dep")
        assert (row["llm_category"], row["llm_subcategory"]) == ("Bills & Utilities", "Rent Deposit")
        assert outcome(db, "dep") == "changed"

    def test_a_kept_answer_silences_the_same_merchant_and_placement_later(self, db):
        pid = self._held(db)
        cp.apply_selected(pid, 1)
        add_txn(db, "later", merchant="Cauldwell")
        client = FakeClient(lambda p: reject("X", "Y"))
        result = make_reviewer(client).review([(txn(db, "later"), "Bills & Utilities", "Rent")])
        assert result.objections == {} and result.unavailable == set()
        assert client.calls == []

    def test_a_regular_proposal_is_unaffected(self, db):
        """apply_selected is shared with the novelty gate -- no judge row, no
        error, same result as before."""
        seed_taxonomy(db)
        add_txn(db, "t")
        options = [{"parent_name": "Tax", "subcategory_name": "Self Assessment", "parent_is_new": True, "rationale": "x"}]
        pid, _ = cp.register_group(options, ["t"])
        assert cp.apply_selected(pid, 0) == 1
        assert db.execute("SELECT COUNT(*) FROM judge_reviews").fetchone()[0] == 0

    def test_declining_a_second_look_card_does_not_forbid_the_existing_names_on_it(self, db):
        """A judge card's options include the EXISTING placement it questioned.
        Feeding those into the declined-names list would tell every later
        classifier prompt never to use 'Groceries' again."""
        options = pj.hold_options("Food & Drink", "Groceries", reject("Food & Drink", "Takeaway"), {"food & drink"})
        db.execute("INSERT INTO category_proposals (id, options, status, proposed_at) VALUES (1, ?, 'denied', NOW())",
                   [json.dumps(options)])
        assert cp.denied_sub_names() == set()
        assert cp.denied_parent_names() == set()

    def test_a_declined_ordinary_card_still_forbids_its_names(self, db):
        options = [{"parent_name": "Tax", "subcategory_name": "Self Assessment", "parent_is_new": True, "rationale": "x"}]
        db.execute("INSERT INTO category_proposals (id, options, status, proposed_at) VALUES (1, ?, 'denied', NOW())",
                   [json.dumps(options)])
        assert cp.denied_sub_names() == {"self assessment"}


class TestCarryOverOnTryAgain:
    def test_keeps_the_keep_option_and_marks_the_fresh_ones(self):
        previous = pj.hold_options("Food & Drink", "Groceries", reject("Food & Drink", "Takeaway"), {"food & drink"})
        fresh = [{"parent_name": "A", "subcategory_name": "B", "parent_is_new": True, "rationale": "r"},
                 {"parent_name": "C", "subcategory_name": "D", "parent_is_new": True, "rationale": "r"},
                 {"parent_name": "E", "subcategory_name": "F", "parent_is_new": True, "rationale": "r"}]
        merged = ll._carry_over_judge_card(previous, fresh)
        assert len(merged) == ll.MAX_OPTIONS
        assert merged[-1]["is_original"] is True
        assert all(o["judge"] for o in merged)

    def test_an_ordinary_card_is_left_alone(self):
        previous = [{"parent_name": "Tax", "subcategory_name": "X", "parent_is_new": True, "rationale": "r"}]
        fresh = [{"parent_name": "A", "subcategory_name": "B", "parent_is_new": True, "rationale": "r"}]
        assert ll._carry_over_judge_card(previous, fresh) == fresh


# ── run() ──────────────────────────────────────────────────────────────────────

class TestRunUsesTheJudge:
    """The holding-deposit case end to end: two transactions both placed in the
    existing Rent subcategory, only one of which belongs there."""

    def _stub_passes(self, monkeypatch, client):
        monkeypatch.setattr(ll, "match_existing", lambda client, txns, subs: {})
        monkeypatch.setattr(ll, "classify_parents",
                            lambda client, txns, parents, denied_parent_names=None: {t["id"]: "Bills & Utilities" for t in txns})
        monkeypatch.setattr(ll, "classify_subcategories",
                            lambda client, txns, parent_name, subs, all_parent_names, denied_sub_names=None: {t["id"]: "Rent" for t in txns})
        monkeypatch.setattr(ll, "propose_alternatives", lambda client, groups, parents, subcategories: {})
        monkeypatch.setattr(ll, "anthropic", type("M", (), {"Anthropic": lambda **kw: client}))
        monkeypatch.setattr(cp, "server_supports_proposals", lambda: True)

    def _seed(self, db):
        seed_taxonomy(db)
        add_txn(db, "rent", amount=-1315.0, context="Rent for flat")
        add_txn(db, "deposit", amount=-282.0, context="Holding deposit for new flat")

    def _judge(self, prompt):
        if "Holding deposit" in prompt:
            return reject("Bills & Utilities", "Rent Deposit")
        return APPROVE

    def test_the_questioned_placement_is_held_and_the_rest_are_written(self, db, monkeypatch):
        self._seed(db)
        client = FakeClient(self._judge)
        self._stub_passes(monkeypatch, client)
        synced = []
        monkeypatch.setattr(cp, "sync_new_proposals", lambda ids: synced.extend(ids))

        ll.run()

        assert txn(db, "rent")["llm_subcategory"] == "Rent"
        deposit = txn(db, "deposit")
        assert deposit["llm_category"] is None and deposit["pending_category_proposal_id"] is not None
        assert synced == [deposit["pending_category_proposal_id"]]
        options = json.loads(db.execute("SELECT options FROM category_proposals").fetchone()[0])
        assert options[0]["subcategory_name"] == "Rent Deposit" and options[1]["is_original"]
        assert db.execute("SELECT COUNT(*) FROM subcategories WHERE name = 'Rent Deposit'").fetchone()[0] == 0, \
            "the suggested subcategory must not exist until it is chosen"
        assert outcome(db, "rent") == "approved" and outcome(db, "deposit") == "held"

    def test_an_approving_judge_changes_nothing(self, db, monkeypatch):
        self._seed(db)
        self._stub_passes(monkeypatch, FakeClient(lambda p: APPROVE))
        monkeypatch.setattr(cp, "sync_new_proposals", lambda ids: pytest.fail("nothing should be synced"))
        ll.run()
        assert txn(db, "rent")["llm_subcategory"] == "Rent" and txn(db, "deposit")["llm_subcategory"] == "Rent"
        assert db.execute("SELECT COUNT(*) FROM category_proposals").fetchone()[0] == 0

    def test_an_unavailable_judge_leaves_transactions_unclassified_for_the_next_run(self, db, monkeypatch):
        """Fail closed: nothing is saved unreviewed, nothing is locked behind a
        card, and the next run (with the judge back) picks it all up."""
        self._seed(db)
        self._stub_passes(monkeypatch, FakeClient(lambda p: RuntimeError("API down")))
        monkeypatch.setattr(cp, "sync_new_proposals", lambda ids: pytest.fail("no card without a verdict"))
        ll.run()
        for tid in ("rent", "deposit"):
            row = txn(db, tid)
            assert row["llm_category"] is None and row["pending_category_proposal_id"] is None
        assert db.execute("SELECT COUNT(*) FROM judge_reviews").fetchone()[0] == 0

        self._stub_passes(monkeypatch, FakeClient(self._judge))
        monkeypatch.setattr(cp, "sync_new_proposals", lambda ids: None)
        ll.run()
        assert txn(db, "rent")["llm_subcategory"] == "Rent"
        assert txn(db, "deposit")["pending_category_proposal_id"] is not None, "the retry reaches the judge and it objects"

    def test_switching_the_judge_off_releases_everything_it_was_holding_back(self, db, monkeypatch):
        self._seed(db)
        self._stub_passes(monkeypatch, FakeClient(lambda p: RuntimeError("API down")))
        monkeypatch.setattr(cp, "sync_new_proposals", lambda ids: None)
        ll.run()

        monkeypatch.setattr(pj, "JUDGE_ENABLED", False)
        ll.run()
        assert txn(db, "rent")["llm_subcategory"] == "Rent" and txn(db, "deposit")["llm_subcategory"] == "Rent"

    def test_a_failing_judge_sends_a_telegram_alert(self, db, monkeypatch, alert_post):
        self._seed(db)
        self._stub_passes(monkeypatch, FakeClient(lambda p: RuntimeError("API down")))
        monkeypatch.setattr(cp, "sync_new_proposals", lambda ids: None)
        ll.run()
        titles = [c.kwargs["json"]["title"] for c in alert_post.call_args_list]
        assert "Placement judge is holding transactions back" in titles

    def test_the_judge_stays_off_when_the_server_cannot_show_a_card(self, db, monkeypatch):
        self._seed(db)
        client = FakeClient(self._judge)
        self._stub_passes(monkeypatch, client)
        monkeypatch.setattr(cp, "server_supports_proposals", lambda: False)
        ll.run()
        assert client.calls == []
        assert txn(db, "deposit")["llm_subcategory"] == "Rent"

    def test_a_new_category_goes_through_the_gate_not_the_judge(self, db, monkeypatch):
        self._seed(db)
        client = FakeClient(lambda p: reject("X", "Y"))
        self._stub_passes(monkeypatch, client)
        monkeypatch.setattr(ll, "classify_subcategories",
                            lambda client, txns, parent_name, subs, all_parent_names, denied_sub_names=None: {t["id"]: "Brand New" for t in txns})
        monkeypatch.setattr(cp, "sync_new_proposals", lambda ids: None)
        ll.run()
        assert client.calls == [], "novel placements are already held by the gate; judging them too would double-ask"

    def test_the_classifier_prompts_get_the_subcategory_stats(self, db, monkeypatch):
        self._seed(db)
        add_txn(db, "past", amount=-1315.0, category="Bills & Utilities", sub="Rent")
        seen = []
        self._stub_passes(monkeypatch, FakeClient(lambda p: APPROVE))
        monkeypatch.setattr(ll, "match_existing", lambda client, txns, subs: seen.append(subs) or {})
        monkeypatch.setattr(cp, "sync_new_proposals", lambda ids: None)
        ll.run()
        rent = next(s for s in seen[0] if s["name"] == "Rent")
        assert rent["stats"] == "amounts always £1315.00; only 1 so far, no clear rhythm"
