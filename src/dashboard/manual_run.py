"""Run the pipeline now, from the dashboard. It's the same run the hourly worker does
(src/pipeline/process.py), started as its own process so the page doesn't wait on it.

The pipeline takes a database lock at the start. If the worker is already running, this
run says so in its log and stops, so a button press can't classify the same payments twice.
"""
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PIPELINE = ROOT / "src" / "pipeline" / "process.py"
LOG = ROOT / "logs" / "manual_run.log"


def start() -> subprocess.Popen:
    """Start one run in the background. Its output goes to LOG, after a line saying when it began."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as log:
        log.write(f"\n--- started from the dashboard {datetime.now():%a %d %b, %H:%M:%S} ---\n")
    out = open(LOG, "a", encoding="utf-8")
    try:
        return subprocess.Popen([sys.executable, str(PIPELINE)], cwd=ROOT, stdout=out,
                                stderr=subprocess.STDOUT)
    finally:
        out.close()   # the child keeps its own copy of the file handle


def status(proc) -> str:
    """'running', 'finished' or 'failed'."""
    code = proc.poll()
    if code is None:
        return "running"
    return "finished" if code == 0 else "failed"


def log_tail(lines: int = 15) -> str:
    """The end of this run's output: the last section of LOG."""
    if not LOG.exists():
        return ""
    text = LOG.read_text(encoding="utf-8", errors="replace")
    sections = text.split("--- started from the dashboard")
    return "\n".join(sections[-1].splitlines()[-lines:]).strip()
