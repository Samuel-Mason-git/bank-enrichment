"""The hourly runs on the server: when they happen, what Telegram hears when
they can't, and that two runs never overlap."""
from datetime import date, datetime, timezone
from unittest.mock import patch

import alerts
import process
import worker
from worker import LONDON, next_run


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def london(*args):
    return datetime(*args, tzinfo=LONDON)


def sent(alert_post):
    return [c.kwargs["json"]["text"] for c in alert_post.call_args_list]


class TestNextRun:
    def test_before_eight_the_first_run_is_at_eight(self):
        assert next_run(london(2026, 7, 1, 6, 0)) == london(2026, 7, 1, 8, 0)

    def test_during_the_day_it_is_the_next_hour(self):
        assert next_run(london(2026, 7, 1, 9, 20)) == london(2026, 7, 1, 10, 0)

    def test_on_the_hour_exactly_waits_for_the_next(self):
        """A run that finishes within the minute it started must not
        schedule itself again."""
        assert next_run(london(2026, 7, 1, 9, 0)) == london(2026, 7, 1, 10, 0)

    def test_the_last_run_is_at_ten_at_night(self):
        assert next_run(london(2026, 7, 1, 21, 59)) == london(2026, 7, 1, 22, 0)

    def test_nothing_overnight(self):
        assert next_run(london(2026, 7, 1, 22, 0)) == london(2026, 7, 2, 8, 0)
        assert next_run(london(2026, 7, 1, 23, 30)) == london(2026, 7, 2, 8, 0)
        assert next_run(london(2026, 7, 2, 3, 0)) == london(2026, 7, 2, 8, 0)

    def test_stays_on_londons_hours_across_the_clocks_going_back(self):
        """25 Oct 2026: BST ends. 08:00 London is 07:00 UTC before, 08:00 after."""
        assert next_run(utc(2026, 10, 24, 22, 0)).astimezone(timezone.utc) == utc(2026, 10, 25, 8, 0)
        assert next_run(utc(2026, 10, 23, 22, 0)).astimezone(timezone.utc) == utc(2026, 10, 24, 7, 0)

    def test_stays_on_londons_hours_across_the_clocks_going_forward(self):
        """29 Mar 2026: BST starts, so 08:00 London becomes 07:00 UTC."""
        assert next_run(utc(2026, 3, 28, 22, 0)).astimezone(timezone.utc) == utc(2026, 3, 29, 7, 0)
        assert next_run(utc(2026, 3, 29, 12, 30)) == london(2026, 3, 29, 14, 0)

    def test_a_utc_time_late_in_the_evening_is_read_on_londons_clock(self):
        # 21:30 UTC on 1 July is 22:30 in London: today's runs are over.
        assert next_run(utc(2026, 7, 1, 21, 30)) == london(2026, 7, 2, 8, 0)
        # 23:30 UTC on 30 June is 00:30 on 1 July in London: 08:00 is still ahead.
        assert next_run(utc(2026, 6, 30, 23, 30)) == london(2026, 7, 1, 8, 0)


class TestRunOnce:
    def test_a_good_run_sends_nothing(self, alert_post):
        with patch.object(process, "main", return_value=process.RAN):
            assert worker.run_once(london(2026, 7, 1, 9, 0)) == process.RAN
        alert_post.assert_not_called()

    def test_a_run_that_cannot_reach_the_database_says_so(self, alert_post):
        with patch.object(process, "main", return_value=process.NO_DATABASE):
            assert worker.run_once(london(2026, 7, 1, 9, 0)) == process.NO_DATABASE
        [text] = sent(alert_post)
        assert "stopped working" in text and "09:00" in text and "database" in text

    def test_a_crash_is_reported_and_the_worker_carries_on(self, alert_post):
        with patch.object(process, "main", side_effect=RuntimeError("boom")):
            assert worker.run_once(london(2026, 7, 1, 9, 0)) == 1
        [text] = sent(alert_post)
        assert "crashed" in text and "RuntimeError" in text and "boom" not in text

    def test_failing_hour_after_hour_alerts_once(self, alert_post):
        with patch.object(process, "main", return_value=process.NO_DATABASE):
            for hour in range(9, 14):
                worker.run_once(london(2026, 7, 1, hour, 0))
        assert len(sent(alert_post)) == 1

    def test_working_again_is_said_once(self, alert_post):
        with patch.object(process, "main", side_effect=[process.NO_DATABASE, RuntimeError("x"),
                                                        process.RAN, process.RAN]):
            for hour in range(9, 13):
                worker.run_once(london(2026, 7, 1, hour, 0))
        first, back = sent(alert_post)
        assert "stopped working" in first
        assert "running again" in back and "since Wed 09:00" in back and "11:00" in back

    def test_a_new_failure_after_recovering_alerts_again(self, alert_post):
        with patch.object(process, "main", side_effect=[process.NO_DATABASE, process.RAN, process.NO_DATABASE]):
            for hour in range(9, 12):
                worker.run_once(london(2026, 7, 1, hour, 0))
        texts = sent(alert_post)
        assert len(texts) == 3
        assert "stopped working" in texts[0] and "running again" in texts[1] and "stopped working" in texts[2]

    def test_another_run_going_is_only_logged(self, alert_post):
        """A run started by hand overlapping the hourly one is harmless: the
        next hour picks up whatever it didn't."""
        with patch.object(process, "main", return_value=process.ALREADY_RUNNING):
            assert worker.run_once(london(2026, 7, 1, 9, 0)) == process.ALREADY_RUNNING
        alert_post.assert_not_called()

    def test_another_run_going_neither_starts_nor_ends_a_failing_streak(self, alert_post):
        with patch.object(process, "main", side_effect=[process.NO_DATABASE, process.ALREADY_RUNNING,
                                                        process.NO_DATABASE]):
            for hour in range(9, 12):
                worker.run_once(london(2026, 7, 1, hour, 0))
        assert len(sent(alert_post)) == 1

    def test_now_runs_once_and_exits(self):
        with patch.object(worker, "run_once", return_value=0) as run:
            assert worker.main(["--now"]) == 0
        run.assert_called_once()


class TestAlertsOncePerDay:
    def test_the_same_kind_is_sent_once_a_day_not_once_an_hour(self, alert_post):
        """The worker lives for weeks; out of credit at 09:00 is said once
        that day, and again the next day if it's still true."""
        def run(*when):
            worker.run_once(london(*when))
            alerts.send_alert("credit", "a", "b")
        with patch.object(process, "main", return_value=process.RAN):
            run(2026, 7, 1, 9, 0)
            run(2026, 7, 1, 10, 0)
            run(2026, 7, 1, 22, 0)
            run(2026, 7, 2, 8, 0)
        assert len(sent(alert_post)) == 2

    def test_the_day_is_londons(self):
        with patch.object(process, "main", return_value=process.RAN), \
                patch.object(alerts, "start_run") as start:
            worker.run_once(utc(2026, 6, 30, 22, 0))   # 23:00 on 30 June in London
            worker.run_once(utc(2026, 6, 30, 23, 30))  # 00:30 on 1 July in London
        assert start.call_count == 2 and worker._alerts_day == date(2026, 7, 1)


class TestOneRunAtATime:
    def test_a_second_run_does_not_start_while_one_is_going(self, db, pg):
        """The lock is held by a connection, so another connection (another
        run, the PC's or the worker's) is refused until it's released."""
        pg.execute("SELECT pg_advisory_lock(hashtext(%s))", [process.RUN_LOCK])
        with patch.object(process, "_steps") as steps:
            assert process.main() == process.ALREADY_RUNNING
        steps.assert_not_called()
        pg.execute("SELECT pg_advisory_unlock(hashtext(%s))", [process.RUN_LOCK])

    def test_the_lock_is_released_after_a_run(self, db, pg):
        with patch.object(process, "_steps"):
            assert process.main() == 0
            assert process.main() == 0
        assert pg.execute("SELECT pg_try_advisory_lock(hashtext(%s))", [process.RUN_LOCK]).fetchone()[0]
        pg.execute("SELECT pg_advisory_unlock(hashtext(%s))", [process.RUN_LOCK])

    def test_the_lock_is_released_even_if_a_step_blows_up(self, db, pg):
        with patch.object(process, "_steps", side_effect=RuntimeError("boom")):
            try:
                process.main()
            except RuntimeError:
                pass
        assert pg.execute("SELECT pg_try_advisory_lock(hashtext(%s))", [process.RUN_LOCK]).fetchone()[0]
        pg.execute("SELECT pg_advisory_unlock(hashtext(%s))", [process.RUN_LOCK])

    def test_no_database_means_no_run(self, monkeypatch):
        import database_functions
        monkeypatch.setattr(database_functions, "init_db", lambda read_only=False: (_ for _ in ()).throw(OSError("no route")))
        monkeypatch.setattr(process, "init_db", database_functions.init_db)
        with patch.object(process, "_steps") as steps:
            assert process.main() == process.NO_DATABASE
        steps.assert_not_called()


class TestTheClassifierWithoutAKey:
    def test_returns_rather_than_ending_the_worker(self, db, monkeypatch):
        import llm_labelling
        monkeypatch.setattr(llm_labelling, "CLAUDE_SECRET", None)
        assert llm_labelling.run() is None
