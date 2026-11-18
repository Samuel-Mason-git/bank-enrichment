"""The worker heartbeat: which run should have started by now, whether one was missed,
and the server telling you once when the worker stops and once when it's back."""
from datetime import datetime
from types import SimpleNamespace

import bot_health
import run_schedule as rs

L = rs.LONDON


def at(day, hour, minute=0):
    return datetime(2026, 11, day, hour, minute, tzinfo=L)


# ── which run is due ─────────────────────────────────────────────────────────

def test_mid_morning_the_last_hour_is_due():
    assert rs.latest_expected_run(at(18, 10, 45)) == at(18, 10)


def test_a_run_gets_half_an_hour_to_start():
    assert rs.latest_expected_run(at(18, 10, 20)) == at(18, 9)


def test_overnight_the_evening_run_is_the_last_one_due():
    assert rs.latest_expected_run(at(18, 3)) == at(17, 22)
    assert rs.latest_expected_run(at(18, 23, 10)) == at(18, 22)


def test_first_thing_in_the_morning_the_eight_oclock_run_is_due_after_its_grace():
    assert rs.latest_expected_run(at(18, 8, 20)) == at(17, 22)
    assert rs.latest_expected_run(at(18, 8, 31)) == at(18, 8)


# ── whether a run was missed ─────────────────────────────────────────────────

def test_a_run_that_started_on_time_is_not_missed():
    assert rs.missed_run(at(18, 10, 2), at(18, 10, 45)) is None


def test_a_run_that_didnt_start_is_missed():
    assert rs.missed_run(at(18, 9, 1), at(18, 10, 45)) == at(18, 10)


def test_no_run_ever_is_missed():
    assert rs.missed_run(None, at(18, 10, 45)) == at(18, 10)


def test_the_quiet_hours_are_never_a_missed_run():
    assert rs.missed_run(at(17, 22, 1), at(18, 6)) is None


# ── the heartbeat ────────────────────────────────────────────────────────────

class Con:
    def __init__(self, last):
        self.last = last

    def execute(self, sql, *args):
        return SimpleNamespace(fetchone=lambda: (self.last,))


def test_it_alerts_once_when_the_worker_stops_and_once_when_it_is_back():
    beat, con = bot_health.Heartbeat(), Con(at(18, 9, 1))
    first = beat.check(con, at(18, 10, 45))
    assert first is not None and "stopped running" in first and "10:00" in first
    assert beat.check(con, at(18, 11, 45)) is None           # still stopped: not told again
    con.last = at(18, 12, 0)
    back = beat.check(con, at(18, 12, 10))
    assert back is not None and "running again" in back
    assert beat.check(con, at(18, 12, 20)) is None           # and quiet after that


def test_a_healthy_worker_sends_nothing():
    assert bot_health.Heartbeat().check(Con(at(18, 10, 1)), at(18, 10, 45)) is None
