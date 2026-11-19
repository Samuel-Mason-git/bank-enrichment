"""The Queue page's "Run the pipeline now": one background run of process.py, its output in a
log, and a status the page can show."""
import sys

import manual_run


class FakeProc:
    def __init__(self, code):
        self.code = code

    def poll(self):
        return self.code


def test_a_run_still_going_is_running():
    assert manual_run.status(FakeProc(None)) == "running"


def test_a_run_that_exits_cleanly_is_finished():
    assert manual_run.status(FakeProc(0)) == "finished"


def test_a_run_that_exits_with_an_error_is_failed():
    assert manual_run.status(FakeProc(1)) == "failed"


def test_start_runs_the_pipeline_and_logs_its_output(tmp_path, monkeypatch):
    script = tmp_path / "fake_process.py"
    script.write_text("print('pipeline ran')\n", encoding="utf-8")
    log = tmp_path / "manual_run.log"
    monkeypatch.setattr(manual_run, "PIPELINE", script)
    monkeypatch.setattr(manual_run, "LOG", log)

    proc = manual_run.start()
    assert proc.wait(timeout=30) == 0
    assert "pipeline ran" in manual_run.log_tail()
    assert "started from the dashboard" in log.read_text(encoding="utf-8")
    assert sys.executable  # the run uses this same Python, so it sees the same packages


def test_log_tail_shows_only_the_latest_run(tmp_path, monkeypatch):
    log = tmp_path / "manual_run.log"
    log.write_text("--- started from the dashboard Mon 01 Jun, 09:00:00 ---\nold run\n"
                   "--- started from the dashboard Tue 02 Jun, 10:00:00 ---\nnew run line\n", encoding="utf-8")
    monkeypatch.setattr(manual_run, "LOG", log)
    tail = manual_run.log_tail()
    assert "new run line" in tail and "old run" not in tail


def test_log_tail_is_empty_before_any_run(tmp_path, monkeypatch):
    monkeypatch.setattr(manual_run, "LOG", tmp_path / "missing.log")
    assert manual_run.log_tail() == ""
