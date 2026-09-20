import json

import pytest

import category_proposals as cp
import database_functions as dbf
import judge_backlog as jb
import placement_judge as pj
from tests.test_placement_judge import (
    APPROVE, FakeClient, add_txn, make_reviewer, outcome, reject, seed_taxonomy, txn,
)


def classified(db, tid, category="Bills & Utilities", sub="Rent", model="claude-x", **kw):
    add_txn(db, tid, category=category, sub=sub, model=model, **kw)


@pytest.fixture
def run(db, monkeypatch):
    """jb.main with the reviewer, backup and cost stubbed. Returns the fake client."""
    seed_taxonomy(db)
    holder = {}
    monkeypatch.setattr(jb, "backup_db", lambda reason: holder.setdefault("backups", []).append(reason))
    monkeypatch.setattr(cp, "sync_new_proposals", lambda ids: holder.setdefault("synced", []).extend(ids))

    def go(respond, args=("--yes",), gate=True):
        client = FakeClient(respond)
        holder["client"] = client
        monkeypatch.setattr(jb, "make_placement_reviewer", lambda: make_reviewer(client, gate=gate))
        holder["code"] = jb.main(list(args))
        return holder

    return go


class TestCandidates:
    def test_selects_classified_transactions_the_judge_has_not_seen(self, db):
        seed_taxonomy(db)
        classified(db, "llm")
        classified(db, "tap", model="quick-tap")
        found = {t["id"] for t in jb.candidates()}
        assert found == {"llm", "tap"}, "quick-taps are included: a mis-tap is exactly what this is for"

    @pytest.mark.parametrize("why", ["unclassified", "manual", "card choice", "already judged", "held", "skipped"])
    def test_leaves_out_what_is_not_the_judges_business(self, db, why):
        seed_taxonomy(db)
        if why == "unclassified":
            add_txn(db, "x")
        elif why == "manual":
            classified(db, "x", model="manual")
        elif why == "card choice":
            classified(db, "x", model="category-proposal")
        elif why == "already judged":
            classified(db, "x")
            db.execute("INSERT INTO judge_reviews VALUES ('x', NULL, 'Bills & Utilities', 'Rent', 'approved', NOW())")
        elif why == "held":
            classified(db, "x")
            db.execute("UPDATE transactions SET pending_category_proposal_id = 5 WHERE id = 'x'")
        else:
            classified(db, "x")
            db.execute("UPDATE transactions SET skipped = TRUE WHERE id = 'x'")
        assert jb.candidates() == []

    def test_works_before_the_judge_table_exists(self, db):
        """A database that predates the judge has no judge_reviews table yet."""
        classified(db, "a")
        db.execute("DROP TABLE judge_reviews")
        assert [t["id"] for t in jb.candidates()] == ["a"]

    def test_newest_first(self, db):
        seed_taxonomy(db)
        classified(db, "old", created="2026-05-01 10:00:00")
        classified(db, "new", created="2026-09-01 10:00:00")
        assert [t["id"] for t in jb.candidates()] == ["new", "old"]


class TestEstimateStep:
    def test_by_default_it_only_counts_and_prices(self, db, monkeypatch, capsys):
        seed_taxonomy(db)
        for i in range(3):
            classified(db, f"t{i}")
        monkeypatch.setattr(jb, "make_placement_reviewer", lambda: pytest.fail("must not build a reviewer without --yes"))
        monkeypatch.setattr(jb, "backup_db", lambda r: pytest.fail("must not back up or write without --yes"))

        assert jb.main([]) == 0

        out = capsys.readouterr().out
        assert "3 classified transaction(s)" in out and "$0.02" in out and "Nothing has been sent" in out
        assert db.execute("SELECT COUNT(*) FROM judge_reviews").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM category_proposals").fetchone()[0] == 0

    def test_limit_applies_to_the_estimate_too(self, db, capsys):
        seed_taxonomy(db)
        for i in range(5):
            classified(db, f"t{i}")
        jb.main(["--limit", "2"])
        assert "2 classified transaction(s)" in capsys.readouterr().out


class TestRun:
    def test_approvals_are_recorded_and_nothing_else_changes(self, run, db):
        classified(db, "a")
        run(lambda p: APPROVE)
        assert outcome(db, "a") == "approved"
        assert txn(db, "a")["llm_subcategory"] == "Rent" and txn(db, "a")["pending_category_proposal_id"] is None

    def test_an_objection_is_held_but_the_transaction_stays_classified(self, run, db):
        """The point: totals must not move while a card is waiting."""
        classified(db, "dep", context="Holding deposit")
        held = run(lambda p: reject("Bills & Utilities", "Rent Deposit"))

        row = txn(db, "dep")
        assert (row["llm_category"], row["llm_subcategory"]) == ("Bills & Utilities", "Rent")
        assert row["pending_category_proposal_id"] is not None and outcome(db, "dep") == "held"
        assert held["synced"] == [row["pending_category_proposal_id"]]
        options = json.loads(db.execute("SELECT options FROM category_proposals").fetchone()[0])
        assert options[0]["subcategory_name"] == "Rent Deposit" and options[1]["is_original"] is True

    def test_a_backup_is_taken_before_anything_is_judged(self, run, db):
        classified(db, "a")
        assert run(lambda p: APPROVE)["backups"] == ["judge-backlog"]

    def test_list_only_judges_and_reports_but_holds_nothing(self, run, db, capsys):
        classified(db, "dep")
        held = run(lambda p: reject("Bills & Utilities", "Rent Deposit"), args=("--yes", "--list-only"))
        assert "1 placement(s) the judge questioned" in capsys.readouterr().out
        assert txn(db, "dep")["pending_category_proposal_id"] is None
        assert db.execute("SELECT COUNT(*) FROM category_proposals").fetchone()[0] == 0 and "synced" not in held

    def test_the_report_shows_what_is_being_questioned(self, run, db, capsys):
        classified(db, "dep", amount=-282.0, merchant="45 Sapphire House", context="Holding deposit")
        run(lambda p: reject("Bills & Utilities", "Rent Deposit", reason="One-off next to monthly rent."))
        out = capsys.readouterr().out
        assert "£  -282.00" in out and "45 Sapphire House" in out and "ctx: Holding deposit" in out
        assert "Bills & Utilities > Rent  ->  Bills & Utilities > Rent Deposit" in out and "One-off next" in out

    def test_cards_are_capped_most_confident_first_and_the_rest_wait(self, run, db, capsys):
        """Three different questions, room for two cards: the two most confident
        go out, and the third is neither held nor recorded so a re-run finds it."""
        for tid in ("low", "mid", "high"):
            classified(db, tid, context=tid)
        confidence = {"low": 0.81, "mid": 0.9, "high": 0.99}

        def respond(prompt):
            tid = next(t for t in confidence if f"TXN {t} {t}" in prompt)
            return reject("Bills & Utilities", f"Idea {tid}", confidence=confidence[tid])

        run(respond, args=("--yes", "--cards", "2"))

        assert txn(db, "high")["pending_category_proposal_id"] and txn(db, "mid")["pending_category_proposal_id"]
        assert txn(db, "low")["pending_category_proposal_id"] is None and outcome(db, "low") is None
        assert "1 more objection(s) are waiting" in capsys.readouterr().out

    def test_objections_that_share_a_suggestion_share_one_card(self, run, db):
        for tid in ("a", "b", "c"):
            classified(db, tid)
        run(lambda p: reject("Bills & Utilities", "Rent Deposit"), args=("--yes", "--cards", "1"))
        assert db.execute("SELECT COUNT(*) FROM category_proposals").fetchone()[0] == 1
        assert all(txn(db, t)["pending_category_proposal_id"] for t in ("a", "b", "c"))

    def test_a_rerun_only_reviews_what_is_left(self, run, db):
        classified(db, "a")
        classified(db, "b")
        first = run(lambda p: APPROVE)
        assert len(first["client"].calls) == 2
        second = run(lambda p: APPROVE)
        assert second["client"].calls == [], "already-judged transactions cost nothing the second time"

    def test_running_out_of_credit_stops_cleanly_and_can_be_resumed(self, run, db, capsys, monkeypatch):
        import anthropic
        import httpx
        err = anthropic.BadRequestError(
            "Your credit balance is too low",
            response=httpx.Response(400, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")), body=None)
        for i in range(3):
            classified(db, f"t{i}")

        run(lambda p: err)

        assert "credit balance is too low" in capsys.readouterr().err
        assert db.execute("SELECT COUNT(*) FROM judge_reviews").fetchone()[0] == 0, "nothing wrongly recorded as reviewed"
        assert len(jb.candidates()) == 3

    def test_a_switched_off_judge_is_reported_not_silently_skipped(self, run, db, capsys):
        classified(db, "a")
        held = run(lambda p: APPROVE, gate=False)
        assert held["code"] == 1 and "switched off" in capsys.readouterr().err
        assert held["client"].calls == []

    def test_an_empty_backlog_is_a_clean_no_op(self, run, capsys):
        held = run(lambda p: APPROVE)
        assert held["code"] == 0 and "Nothing left to review" in capsys.readouterr().out
        assert "backups" not in held


class TestChoosingTheCard:
    """The end of the road for a held, already-classified transaction."""

    def _held(self, run, db):
        classified(db, "dep", context="Holding deposit")
        run(lambda p: reject("Bills & Utilities", "Rent Deposit"))
        return txn(db, "dep")["pending_category_proposal_id"]

    def test_choosing_the_suggestion_reclassifies_it(self, run, db):
        pid = self._held(run, db)
        assert cp.apply_selected(pid, 0) == 1
        row = txn(db, "dep")
        assert (row["llm_subcategory"], row["pending_category_proposal_id"]) == ("Rent Deposit", None)
        assert outcome(db, "dep") == "changed"

    def test_choosing_keep_changes_nothing_and_is_remembered(self, run, db):
        pid = self._held(run, db)
        cp.apply_selected(pid, 1)
        assert txn(db, "dep")["llm_subcategory"] == "Rent" and outcome(db, "dep") == "kept"

    def test_declining_the_card_leaves_it_exactly_where_it_was(self, run, db):
        pid = self._held(run, db)
        cp.deny_all(pid)
        row = txn(db, "dep")
        assert (row["llm_subcategory"], row["pending_category_proposal_id"]) == ("Rent", None)

    def test_a_manual_edit_made_while_the_card_waited_is_not_overwritten(self, run, db):
        pid = self._held(run, db)
        dbf.update_classification("dep", "Food & Drink", "Groceries", 1.0, "manual")
        assert cp.apply_selected(pid, 0) == 0
        assert txn(db, "dep")["llm_subcategory"] == "Groceries"

    def test_a_classified_transaction_locked_by_anything_else_is_still_never_touched(self, db):
        """apply_selected's NULL check only relaxes for the judge's own held rows."""
        seed_taxonomy(db)
        classified(db, "t")
        options = [{"parent_name": "Tax", "subcategory_name": "X", "parent_is_new": True, "rationale": "r"}]
        pid, _ = cp.register_group(options, ["t"])
        assert cp.apply_selected(pid, 0) == 0
        assert txn(db, "t")["llm_subcategory"] == "Rent"
