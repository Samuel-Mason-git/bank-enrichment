import logging
import time
from datetime import date, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent.parent / "config" / ".env")

import html

from alerts import send_telegram
from database_functions import init_db, get_con, get_totals_by_role, get_category_totals_by_role, log_path

LOG_PATH = log_path("weekly_summary.log")

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



def _week_key(d: date) -> str:
    """Return ISO week key like '2026-W26' for a given date."""
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _week_bounds(week_key: str) -> tuple[date, date]:
    """Return (monday, sunday) for a '2026-W26' key."""
    year, week = int(week_key[:4]), int(week_key[6:])
    monday = date.fromisocalendar(year, week, 1)
    sunday = date.fromisocalendar(year, week, 7)
    return monday, sunday


def _prev_week(week_key: str) -> str:
    monday, _ = _week_bounds(week_key)
    prev_monday = monday - timedelta(days=7)
    return _week_key(prev_monday)


def get_missing_weeks() -> list[str]:
    con = get_con()
    earliest_row = con.execute("SELECT MIN(created_at) FROM transactions").fetchone()[0]
    if not earliest_row:
        return []
    sent = {r[0] for r in con.execute("SELECT period_key FROM summaries WHERE period = 'week'").fetchall()}
    today = date.today()
    # Only include completed weeks — the week that ended before this week started
    last_completed_monday = today - timedelta(days=today.weekday() + 7)
    last_completed_sunday = last_completed_monday + timedelta(days=6)

    start = date.fromisoformat(str(earliest_row)[:10])
    start_monday = start - timedelta(days=start.weekday())  # round back to Monday

    missing = []
    cursor = start_monday
    while cursor <= last_completed_sunday:
        key = _week_key(cursor)
        if key not in sent:
            missing.append(key)
        cursor += timedelta(days=7)
    return missing


def get_week_stats(week_key: str) -> dict:
    monday, sunday = _week_bounds(week_key)
    week_start = f"{monday} 00:00:00"
    week_end = f"{sunday} 23:59:59"

    totals = get_totals_by_role(week_start, week_end)
    total_spend = totals["spend"]
    total_income = totals["income"]
    total_invested = totals["investment"]
    total_saved = totals["saving"]
    total_transferred = totals["transfer"] + totals["excluded"]

    spend_by_cat = get_category_totals_by_role("spend", week_start, week_end)
    income_by_cat = get_category_totals_by_role("income", week_start, week_end)

    classified_spend = sum(abs(float(r["total"])) for r in spend_by_cat)
    classified_income = sum(float(r["total"]) for r in income_by_cat)

    return {
        "week": week_key,
        "monday": monday,
        "sunday": sunday,
        "total_spend": float(total_spend),
        "total_income": float(total_income),
        "total_invested": float(total_invested),
        "total_saved": float(total_saved),
        "total_transferred": float(total_transferred),
        "net": float(total_income - total_spend),
        "unclassified_spend": float(total_spend - classified_spend),
        "unclassified_income": float(total_income - classified_income),
        "spend_by_category": [{"category": r["category"], "amount": abs(float(r["total"]))} for r in spend_by_cat],
        "income_by_category": [{"category": r["category"], "amount": float(r["total"])} for r in income_by_cat],
    }


def format_weekly_message(stats: dict) -> str:
    monday: date = stats["monday"]
    sunday: date = stats["sunday"]
    date_range = f"{monday.day} {monday.strftime('%b')} – {sunday.day} {sunday.strftime('%b %Y')}"
    iso = monday.isocalendar()
    lines = [f"📅 *Week {iso[1]}, {iso[0]} ({date_range})*\n"]
    lines.append(f"💸 Spend: £{stats['total_spend']:.2f}")
    lines.append(f"💰 Income: £{stats['total_income']:.2f}")
    lines.append(f"{'📈' if stats['net'] >= 0 else '📉'} Net: £{stats['net']:.2f}")
    if stats["total_invested"] > 0.005:
        lines.append(f"📊 Invested: £{stats['total_invested']:.2f}")
    if stats["total_saved"] > 0.005:
        lines.append(f"🏦 Saved: £{stats['total_saved']:.2f}")
    if stats["total_transferred"] > 0.005:
        lines.append(f"↔ Transferred: £{stats['total_transferred']:.2f} (excluded from totals)")

    if stats["spend_by_category"] or stats["unclassified_spend"] > 0.005:
        lines.append("\n*Spend by category:*")
        for c in stats["spend_by_category"]:
            lines.append(f"  • {c['category']}: £{c['amount']:.2f}")
        if stats["unclassified_spend"] > 0.005:
            lines.append(f"  • Unclassified: £{stats['unclassified_spend']:.2f}")

    if stats["income_by_category"] or stats["unclassified_income"] > 0.005:
        lines.append("\n*Income by category:*")
        for c in stats["income_by_category"]:
            lines.append(f"  • {c['category']}: £{c['amount']:.2f}")
        if stats["unclassified_income"] > 0.005:
            lines.append(f"  • Unclassified: £{stats['unclassified_income']:.2f}")

    return "\n".join(lines)


def send_weekly_report(week_key: str, stats: dict, message: str) -> None:
    con = get_con()
    # Escaped: category names like "Food & Drink" would otherwise break
    # Telegram's HTML parsing and the message would be rejected.
    if not send_telegram(html.escape(message)):
        raise RuntimeError("Telegram didn't take the message -- not recorded, so it's retried next run")
    con.execute(
        """INSERT INTO summaries (period, period_key, total_spend, total_income, net, total_invested, total_saved, sent_at)
           VALUES ('week', ?, ?, ?, ?, ?, ?, ?)""",
        [week_key, stats["total_spend"], stats["total_income"], stats["net"],
         stats["total_invested"], stats["total_saved"], time.strftime("%Y-%m-%d %H:%M:%S")]
    )
    log.info(f"Weekly report sent and recorded for {week_key}")


def run() -> None:
    run_start = time.time()
    log.info("--- Weekly summary run started ---")
    init_db()
    missing = get_missing_weeks()
    if not missing:
        log.info("No missing weekly reports — all up to date")
        return
    for week in missing:
        try:
            stats = get_week_stats(week)
            message = format_weekly_message(stats)
            send_weekly_report(week, stats, message)
        except Exception as e:
            log.error(f"Failed to send weekly report for {week}: {e}", exc_info=True)
    log.info(f"--- Weekly summary complete in {time.time() - run_start:.2f}s ---")


if __name__ == "__main__":
    _configure_standalone_logging()
    run()
