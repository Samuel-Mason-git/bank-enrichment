"""The daily run on the server: when it runs, what happens when it can't, and
that two runs never overlap."""
from datetime import datetime, timezone
from unittest.mock import patch

import alerts
import process
import worker
from worker import LONDON, next_run


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


class TestNextRun:
    def test_before_eight_runs_today(self):
        assert next_run(datetime(2026, 7, 1, 6, 0, tzinfo=LONDON)) == datetime(2026, 7, 1, 8, 0, tzinfo=LONDON)

    def test_after_eight_runs_tomorrow(self):
        assert next_run(datetime(2026, 7, 1, 9, 0, tzinfo=LONDON)) == datetime(2026, 7, 2, 8, 0, tzinfo=LONDON)

    def test_at_eight_exactly_waits_for_tomorrow(self):
        """Never twice in one morning: a run that finishes within the same
        minute must not schedule itself again."""
        assert next_run(datetime(2026, 7, 1, 8, 0, tzinfo=LONDON)) == datetime(2026, 7, 2, 8, 0, tzinfo=LONDON)

    def test_stays_at_eight_london_across_the_clocks_going_back(self):
        """25 Oct 2026: BST ends. 08:00 London is 07:00 UTC before, 08:00 after."""
        due = next_run(utc(2026, 10, 24, 12, 0))
        assert due == datetime(2026, 10, 25, 8, 0, tzinfo=LONDON)
        assert due.astimezone(timezone.utc) == utc(2026, 10, 25, 8, 0)
        assert next_run(utc(2026, 10, 23, 12, 0)).astimezone(timezone.utc) == utc(2026, 10, 24, 7, 0)

    def test_stays_at_eight_london_across_the_clocks_going_forward(self):
        """29 Mar 2026: BST starts, so 08:00 London becomes 07:00 UTC."""
        due = next_run(utc(2026, 3, 28, 12, 0))
        assert due.astimezone(timezone.utc) == utc(2026, 3, 29, 7, 0)

    def test_a_utc_time_late_in_the_evening_is_read_on_londons_date(self):
        # 23:30 UTC on 30 June is 00:30 on 1 July in London: today's 08:00 is still ahead.
        assert next_run(utc(2026, 6, 30, 23, 30)) == datetime(2026, 7, 1, 8, 0, tzinfo=LONDON)


class TestRunOnce:
    def test_a_good_run_sends_nothing(self, alert_post):
        with patch.object(process, "main", return_value=0):
            assert worker.run_once() == 0
        alert_post.assert_not_called()

    def test_a_run_that_cannot_start_says_so(self, alert_post):
        with patch.object(process, "main", return_value=process.NO_DATABASE):
            assert worker.run_once() == process.NO_DATABASE
        assert "The daily run didn&#x27;t start" in alert_post.call_args.kwargs["json"]["text"]

    def test_no_database_is_retried_every_half_hour_before_alerting(self, alert_post, retry_waits):
        with patch.object(process, "main", return_value=process.NO_DATABASE) as main:
            worker.run_once()
        assert main.call_count == 1 + worker.RETRIES
        assert retry_waits == [worker.RETRY_EVERY] * worker.RETRIES
        assert alert_post.call_count == 1, "one alert, after the last try"

    def test_a_database_back_on_a_retry_runs_with_no_alert(self, alert_post, retry_waits):
        with patch.object(process, "main", side_effect=[process.NO_DATABASE, process.RAN]) as main:
            assert worker.run_once() == process.RAN
        assert main.call_count == 2 and retry_waits == [worker.RETRY_EVERY]
        alert_post.assert_not_called()

    def test_another_run_going_is_not_retried(self, alert_post, retry_waits):
        with patch.object(process, "main", return_value=process.ALREADY_RUNNING) as main:
            assert worker.run_once() == process.ALREADY_RUNNING
        assert main.call_count == 1 and retry_waits == []
        assert "Another run was already going" in alert_post.call_args.kwargs["json"]["text"]

    def test_a_crash_is_reported_and_the_worker_carries_on(self, alert_post):
        with patch.object(process, "main", side_effect=RuntimeError("boom")):
            assert worker.run_once() == 1
        text = alert_post.call_args.kwargs["json"]["text"]
        assert "crashed" in text and "RuntimeError" in text and "boom" not in text

    def test_every_run_can_alert_again(self, alert_post):
        """The worker lives for weeks; an alert sent yesterday must not
        silence the same alert today."""
        with patch.object(process, "main", return_value=process.ALREADY_RUNNING):
            worker.run_once()
            worker.run_once()
        assert alert_post.call_count == 2

    def test_now_runs_once_and_exits(self):
        with patch.object(worker, "run_once", return_value=0) as run:
            assert worker.main(["--now"]) == 0
        run.assert_called_once()


class TestAlertsPerRun:
    def test_the_same_kind_is_sent_once_per_run(self, alert_post):
        alerts.send_alert("credit", "a", "b")
        alerts.send_alert("credit", "a", "b")
        alerts.start_run()
        alerts.send_alert("credit", "a", "b")
        assert alert_post.call_count == 2


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
