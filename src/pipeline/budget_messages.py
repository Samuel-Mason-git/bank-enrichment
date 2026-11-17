"""Budget messages on Telegram, sent by the worker after each run (process.py):

- The digest: how full each category's budget is, as text and as a chart of
  bars, daily (each morning) or weekly (Mondays) -- or off -- as chosen on the
  Budget page. The worker's first run of the day is at 08:00, so a daily
  digest is there when you wake up.
- Alerts, each at most once per category per month: 80% used, over budget,
  and off pace (from day 7, heading for more than 10% over). Which of them you
  get is also chosen on the Budget page.

Nothing is sent while there are no budgets."""
import html
import logging
from datetime import datetime

import budgets as bg
import telegram_charts as charts
from alerts import send_telegram, send_telegram_photo

log = logging.getLogger(__name__)

ICONS = {"over": "🔴", "near": "🟠", "pace": "🟠", "ok": "🟢"}


def _e(text: str) -> str:
    return html.escape(str(text), quote=False)


def digest_text(mb: bg.MonthBudgets) -> str:
    used = mb.spent / mb.budgeted if mb.budgeted else 0
    lines = [f"📊 <b>Budgets, {mb.month:%B}</b> · day {mb.day} of {mb.days}",
             f"£{mb.spent:,.0f} of £{mb.budgeted:,.0f} spent ({used:.0%}), "
             f"with {mb.pace:.0%} of the month gone.", ""]
    for s in mb.statuses:
        lines.append(f"{ICONS[s.state]} <b>{_e(s.category)}</b>: £{s.spent:,.0f} of £{s.budget:,.0f} · {_e(s.note)}")
    others = [u for u in mb.unbudgeted if u["spent"] > 0][:3]
    if others:
        lines.append("")
        lines.append("Not budgeted: " + ", ".join(f"{_e(u['category'])} £{u['spent']:,.0f}" for u in others))
    return "\n".join(lines)


def week_alert_text(total: float, limit: float, today) -> str:
    return (f"📈 <b>This week's spending is £{total:,.2f}</b>, over your £{limit:,.0f} weekly limit. "
            f"That's the budgeted categories since Monday, up to {today:%a %d %b}.")


def alert_text(s: bg.BudgetStatus, kind: str, mb: bg.MonthBudgets) -> str:
    to_go = mb.days - mb.day
    left = f"{to_go} day{'s' if to_go != 1 else ''} left in {mb.month:%B}"
    if kind == "early":
        return (f"🟡 <b>{_e(s.category)} is halfway through its budget</b>\n"
                f"£{s.spent:,.2f} of £{s.budget:,.0f}: £{s.left:,.2f} left, with {left}.")
    if kind == "over":
        return (f"🔴 <b>{_e(s.category)} is over budget</b>\n"
                f"£{s.spent:,.2f} of £{s.budget:,.0f}: £{-s.left:,.2f} over, with {left}.")
    if kind == "near":
        return (f"🟠 <b>{_e(s.category)} is at {s.used:.0%} of its budget</b>\n"
                f"£{s.spent:,.2f} of £{s.budget:,.0f}: £{s.left:,.2f} left, with {left}.")
    return (f"🟠 <b>{_e(s.category)} is spending ahead of pace</b>\n"
            f"£{s.spent:,.2f} of £{s.budget:,.0f} with {mb.pace:.0%} of {mb.month:%B} gone: at this rate it ends the "
            f"month around £{s.projected:,.0f}, £{s.projected - s.budget:,.0f} over.")


def budget_lines(mb: bg.MonthBudgets) -> list[str]:
    """Budget against spend per category, for a summary's text (plain, as the
    summaries escape their whole message). Empty with no budgets."""
    if not mb.statuses:
        return []
    head = f"Budgets, {mb.month:%B}" + ("" if not mb.current else f" so far (day {mb.day} of {mb.days})")
    lines = ["", f"{head}: £{mb.spent:,.0f} of £{mb.budgeted:,.0f}"]
    for st in mb.statuses:
        lines.append(f"  {ICONS[st.state]} {st.category}: £{st.spent:,.0f} of £{st.budget:,.0f} · {st.note}")
    return lines


def send_charts(*pngs) -> int:
    """Charts after a message, best-effort: how many went."""
    sent = 0
    for png in pngs:
        try:
            sent += bool(png) and send_telegram_photo(png)
        except Exception as e:
            log.warning(f"Chart not sent: {type(e).__name__}: {e}")
    return sent


CAPTION_LIMIT = 1024   # Telegram's limit on a photo's caption


def send_digest(mb: bg.MonthBudgets) -> bool:
    """The digest, as the scheduled message and /budget send it. False with no
    budgets; otherwise True once the text has gone."""
    if not mb.statuses:
        return False
    return send_digest_message(mb)


def send_digest_message(mb: bg.MonthBudgets) -> bool:
    """The digest's text and bars. When the text fits a photo's caption, they go
    as one message. Otherwise the bars come first, then the text. The text is
    what counts: the result is whether it went, and a missing chart is logged."""
    text = digest_text(mb)
    try:
        png = charts.budget_bars(mb)
    except Exception as e:
        log.warning(f"Budget chart not drawn: {type(e).__name__}: {e}")
        png = None
    if png and len(text) <= CAPTION_LIMIT:
        if send_telegram_photo(png, caption=text):
            return True
        log.warning("Budget chart not sent with its text: sending the text on its own")
        return send_telegram(text)
    if png and not send_telegram_photo(png):
        log.warning("Budget chart not sent: Telegram refused the image")
    return send_telegram(text)


def report() -> bool:
    """/budget: this month's standing on demand -- the text and the bars."""
    mb = bg.month_budgets()
    if not mb.statuses:
        return send_telegram("No budgets are set yet. Set them on the Budget page of your dashboard.")
    return send_digest_message(mb)


def run(now: datetime | None = None) -> dict:
    """The digest if one is due, then any alerts due. Alerts wait during the quiet hours and go
    out at the first run after them. Returns what was sent."""
    when = (now or datetime.now(bg.LONDON)).astimezone(bg.LONDON)
    mb = bg.month_budgets(today=when.date())
    sent = {"digest": False, "alerts": 0}
    if not mb.statuses:
        return sent
    if bg.digest_due(now) and send_digest(mb):
        bg.digest_sent(now)
        sent["digest"] = True
        log.info("Sent the budget digest")
    if bg.in_quiet_hours(when.hour, bg.thresholds()):
        return sent
    week = bg.week_alert_due(when.date())
    if week is not None and send_telegram(week_alert_text(week, bg.thresholds()["week_limit"], when.date())):
        bg.mark_week_alerted(when.date())
        sent["alerts"] += 1
        log.info("Sent the weekly budget alert")
    for status, kind in bg.alerts_due(mb):
        if send_telegram(alert_text(status, kind, mb)):
            bg.alert_sent(status, kind, mb.month)
            sent["alerts"] += 1
            log.info(f"Sent a budget alert: {status.category} ({kind})")
    return sent
