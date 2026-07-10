import logging
import os
import time
from datetime import date, datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from dotenv import load_dotenv
import requests

load_dotenv(Path(__file__).parent.parent.parent / "config" / ".env")

DB_PATH = os.getenv("DB_PATH")
LOCAL_API_KEY = os.getenv("LOCAL_API_KEY")
SERVER_URL = (os.getenv("SERVER_URL") or "").rstrip("/")

LOG_PATH = os.path.join(os.path.dirname(DB_PATH), "monthly_summary.log") if DB_PATH else "monthly_summary.log"

from database_functions import init_db, get_con, get_totals_by_role, get_category_totals_by_role

log = logging.getLogger(__name__)


def _configure_standalone_logging():
    """Only used when this script is run directly — when imported (e.g. by
    process.py), the importing entrypoint owns root logger configuration so
    its own log file actually receives these log lines."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            RotatingFileHandler(LOG_PATH, maxBytes=5_000_000, backupCount=5),
            logging.StreamHandler(),
        ]
    )

HEADERS = {"X-API-Key": LOCAL_API_KEY}


def get_missing_months() -> list[str]:
    con = get_con()
    earliest = con.execute("SELECT MIN(created_at) FROM transactions").fetchone()[0]
    if not earliest:
        return []
    sent = {r[0] for r in con.execute("SELECT send_date FROM monthly_summaries").fetchall()}
    today = date.today()
    last_completed = today.replace(day=1) - timedelta(days=1)
    missing = []
    cursor = date.fromisoformat(str(earliest)[:10]).replace(day=1)
    end = last_completed.replace(day=1)
    while cursor <= end:
        month_str = cursor.strftime("%Y-%m")
        if month_str not in sent:
            missing.append(month_str)
        cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
    return missing


def get_month_stats(month_str: str) -> dict:
    month_start = f"{month_str}-01"
    year, month = int(month_str[:4]), int(month_str[5:7])
    last_day = (date(year, month, 28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    month_end = f"{last_day} 23:59:59"

    totals = get_totals_by_role(month_start, month_end)
    total_spend = totals["spend"]
    total_income = totals["income"]
    total_invested = totals["investment"]
    total_transferred = totals["transfer"] + totals["excluded"]

    spend_by_cat = get_category_totals_by_role("spend", month_start, month_end)
    income_by_cat = get_category_totals_by_role("income", month_start, month_end)

    return {
        "month": month_str,
        "total_spend": float(total_spend),
        "total_income": float(total_income),
        "total_invested": float(total_invested),
        "total_transferred": float(total_transferred),
        "net": float(total_income - total_spend),
        "spend_by_category": [{"category": r["category"], "amount": abs(float(r["total"]))} for r in spend_by_cat],
        "income_by_category": [{"category": r["category"], "amount": float(r["total"])} for r in income_by_cat],
    }


def format_monthly_message(stats: dict) -> str:
    month_label = datetime.strptime(stats["month"], "%Y-%m").strftime("%B %Y")
    lines = [f"📊 *{month_label} Summary*\n"]
    lines.append(f"💸 Spend: £{stats['total_spend']:.2f}")
    lines.append(f"💰 Income: £{stats['total_income']:.2f}")
    lines.append(f"{'📈' if stats['net'] >= 0 else '📉'} Net: £{stats['net']:.2f}")
    if stats["total_invested"] > 0.005:
        lines.append(f"📊 Invested: £{stats['total_invested']:.2f}")
    if stats["total_transferred"] > 0.005:
        lines.append(f"↔ Transferred: £{stats['total_transferred']:.2f} (excluded from totals)")

    if stats["spend_by_category"]:
        lines.append("\n*Spend by category:*")
        for c in stats["spend_by_category"]:
            lines.append(f"  • {c['category']}: £{c['amount']:.2f}")

    if stats["income_by_category"]:
        lines.append("\n*Income by category:*")
        for c in stats["income_by_category"]:
            lines.append(f"  • {c['category']}: £{c['amount']:.2f}")

    return "\n".join(lines)


def send_monthly_report(month_str: str, message: str) -> None:
    con = get_con()
    response = requests.post(
        f"{SERVER_URL}/monthly-report",
        headers=HEADERS,
        json={"message": message},
        timeout=30,
    )
    response.raise_for_status()
    next_id = con.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM monthly_summaries").fetchone()[0]
    stats = get_month_stats(month_str)
    con.execute(
        """INSERT INTO monthly_summaries (id, send_date, total_spend, total_income, net, total_invested, sent_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        [next_id, month_str, stats["total_spend"], stats["total_income"], stats["net"],
         stats["total_invested"], time.strftime("%Y-%m-%d %H:%M:%S")]
    )
    log.info(f"Monthly report sent and recorded for {month_str}")


def run() -> None:
    run_start = time.time()
    log.info("--- Monthly summary run started ---")
    init_db()
    missing = get_missing_months()
    if not missing:
        log.info("No missing monthly reports — all up to date")
        return
    for month in missing:
        try:
            stats = get_month_stats(month)
            message = format_monthly_message(stats)
            send_monthly_report(month, message)
        except Exception as e:
            log.error(f"Failed to send monthly report for {month}: {e}", exc_info=True)
    log.info(f"--- Monthly summary complete in {time.time() - run_start:.2f}s ---")


if __name__ == "__main__":
    _configure_standalone_logging()
    run()
