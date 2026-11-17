"""Monthly budgets per parent category: what's been spent against each, how
fast, and what that means -- worked out in one place for everything that shows
or acts on a budget: the Budget page, the Telegram messages and alerts, and the
Ask agent (through its own read-only login, so every function takes the
connection to use).

Only spend categories can have a budget. What counts against one is the
category's outgoing spend-role money, exactly as the Overview counts spend:
transfers, pot moves and refunds never use up a budget. Budgets run per
calendar month, London time.

Pace: how far through the month it is, against how much of the budget is
gone. A category is "off pace" from day 7 when spending at its rate so far
would end the month more than 10% over.

Kept free of pandas: the worker runs it."""
import calendar
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import app_settings
from database_functions import get_con, get_setting, set_setting

LONDON = ZoneInfo("Europe/London")

NEAR = 0.80            # the default "nearly used up" point; the Budgets tab sets it
PACE_OVER = 1.10       # the default: off pace when heading for more than this much of the budget
PACE_FROM_DAY = 7      # the default: too early before this to call a pace
MIN_BUDGET, MAX_BUDGET = 1, 100_000
SUGGEST_MONTHS = 3

# Settings (app_settings), changed on the Budget page.
DIGEST_SETTING = "budget_digest"   # how the budget message comes: see digest_settings()
ALERTS_SETTING = "budget_alerts"                 # the kinds of instant alert wanted
ALERT_KINDS = ("early", "near", "over", "pace", "week")
DEFAULT_ALERT_KINDS = ("near", "over", "pace")   # the halfway and weekly alerts are turned on by you, not by default
DIGEST_LAST_SENT = "budget_digest_last_sent"     # London date of the last digest
WEEK_ALERTED = "budget_week_alerted"             # the ISO week the weekly alert was last sent for
MUTED_SETTING = "budget_alerts_muted"            # parent ids whose alerts are off

# The alert thresholds and quiet hours, as settings with ranges (app_settings.Setting).
EARLY_PCT = app_settings.Setting("budget_early_percent", 50, 10, 90)
NEAR_PCT = app_settings.Setting("budget_near_percent", 80, 50, 100)
PACE_OVER_PCT = app_settings.Setting("budget_pace_over_percent", 110, 100, 200)
PACE_FROM = app_settings.Setting("budget_pace_from_day", 7, 1, 28)
WEEK_LIMIT = app_settings.Setting("budget_week_limit", 0, 0, 100_000)
QUIET_FROM = app_settings.Setting("budget_quiet_from_hour", 22, 0, 23)
QUIET_TO = app_settings.Setting("budget_quiet_to_hour", 8, 0, 23)
THRESHOLD_SETTINGS = [EARLY_PCT, NEAR_PCT, PACE_OVER_PCT, PACE_FROM, WEEK_LIMIT, QUIET_FROM, QUIET_TO]


def thresholds() -> dict:
    """The alert thresholds and quiet hours in use: each saved value, or its default."""
    return {
        "early": app_settings.get(EARLY_PCT) / 100,
        "near": app_settings.get(NEAR_PCT) / 100,
        "pace_over": app_settings.get(PACE_OVER_PCT) / 100,
        "pace_from": app_settings.get(PACE_FROM),
        "week_limit": float(app_settings.get(WEEK_LIMIT)),
        "quiet_from": app_settings.get(QUIET_FROM),
        "quiet_to": app_settings.get(QUIET_TO),
    }


def in_quiet_hours(hour: int, th: dict) -> bool:
    """Whether alerts wait at this hour (London). From and to the same hour means no quiet hours.
    A window across midnight, like 22 to 8, is quiet from 22 until 8 the next morning."""
    start, end = th["quiet_from"], th["quiet_to"]
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def muted_parents() -> set[int]:
    value = get_setting(MUTED_SETTING, [])
    return {int(x) for x in value} if isinstance(value, list) else set()


def set_muted_parents(parent_ids) -> None:
    set_setting(MUTED_SETTING, sorted(int(x) for x in parent_ids))


class BudgetError(ValueError):
    """A budget that can't be set: unknown category, not a spend category, or
    an amount out of range. The message says which, plainly."""


# ── Months ────────────────────────────────────────────────────────────────────

def today_london() -> date:
    return datetime.now(LONDON).date()


def month_start(d: date) -> date:
    return d.replace(day=1)


def month_end(d: date) -> date:
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


def add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    return date(d.year + y, m + 1, 1)


def ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


# ── Reading ───────────────────────────────────────────────────────────────────

def _rows(con, sql: str, params: list | None = None) -> list[dict]:
    cur = con.execute(sql, params or [])
    cols = [c.name for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


_SPEND_PARENTS = """SELECT p.id, p.name FROM parent_categories p
                    LEFT JOIN category_roles pr ON pr.parent_id = p.id AND pr.subcategory_id IS NULL
                    WHERE p.archived_at IS NULL
                      AND COALESCE(pr.role, default_role_for_parent(p.name), 'spend') = 'spend'"""


def spend_categories(con=None) -> list[dict]:
    """Live parent categories whose role is spend: the ones a budget can be on."""
    return _rows(con or get_con(), _SPEND_PARENTS + " ORDER BY p.name")


def spent_by_category(first: date, last: date, con=None) -> dict[str, float]:
    """Outgoing spend-role money per parent category in [first, last], as the
    Overview counts it (skipped transactions left out)."""
    rows = _rows(con or get_con(), """
        SELECT llm_category AS category,
               COALESCE(-SUM(amount) FILTER (WHERE role = 'spend' AND amount < 0), 0) AS spent
        FROM transaction_roles
        WHERE NOT skipped AND llm_category IS NOT NULL
          AND created_at >= %s AND created_at < %s::date + 1
        GROUP BY 1""", [first, last])
    return {r["category"]: float(r["spent"]) for r in rows}


def suggestions(month: date, con=None) -> dict[int, float]:
    """A starting budget per spend category: the average of the full months
    before `month` (up to three), rounded up to the next £10. Categories with
    no spending then get no suggestion. Two queries in all: the spend totals,
    and how many of those months had any transactions."""
    con = con or get_con()
    first = add_months(month_start(month), -SUGGEST_MONTHS)
    last = month_start(month) - timedelta(days=1)
    spent = spent_by_category(first, last, con)
    months = max(int(_rows(con, """SELECT COUNT(DISTINCT date_trunc('month', created_at)) AS n
                                   FROM transaction_roles WHERE NOT skipped
                                    AND created_at >= %s AND created_at < %s::date + 1""",
                           [first, last])[0]["n"] or 0), 1)
    out = {}
    for c in spend_categories(con):
        avg = spent.get(c["name"], 0.0) / months
        if avg > 0:
            out[c["id"]] = float(max(10, math.ceil(avg / 10) * 10))
    return out


@dataclass
class BudgetStatus:
    parent_id: int
    category: str
    budget: float
    spent: float
    left: float          # negative when over
    used: float          # spent / budget
    projected: float     # where the month ends at this rate (the spend so far, for a past month)
    state: str           # "ok" | "pace" | "near" | "over"
    note: str            # one plain line: "on track", "£40 left, 9 days to go", ...


@dataclass
class MonthBudgets:
    month: date                      # the 1st
    day: int                         # days of the month gone, counting today (all of them for a past month)
    days: int                        # days in the month
    current: bool                    # this month (pace and alerts only make sense for it)
    statuses: list[BudgetStatus] = field(default_factory=list)
    unbudgeted: list[dict] = field(default_factory=list)   # spend categories with no budget: spent, suggested

    @property
    def budgeted(self) -> float:
        return round(sum(s.budget for s in self.statuses), 2)

    @property
    def spent(self) -> float:
        return round(sum(s.spent for s in self.statuses), 2)

    @property
    def projected(self) -> float:
        return round(sum(s.projected for s in self.statuses), 2)

    @property
    def pace(self) -> float:
        """How far through the month it is, 0-1: where spending 'on pace' would be."""
        return self.day / self.days if self.days else 1.0


def _state(spent: float, budget: float, projected: float, day: int, current: bool, th: dict | None = None) -> str:
    th = th or {"near": NEAR, "pace_over": PACE_OVER, "pace_from": PACE_FROM_DAY}
    if spent > budget:
        return "over"
    if spent >= budget * th["near"]:
        return "near"
    if current and day >= th["pace_from"] and projected > budget * th["pace_over"]:
        return "pace"
    return "ok"


def _note(state: str, spent: float, budget: float, projected: float, day: int, days: int, current: bool) -> str:
    left = budget - spent
    if state == "over":
        return f"£{-left:,.0f} over"
    if not current:
        return f"£{left:,.0f} under"
    to_go = days - day
    if state == "near":
        return f"£{left:,.0f} left, {to_go} day{'s' if to_go != 1 else ''} to go"
    if state == "pace":
        return f"at this rate £{projected - budget:,.0f} over by the {ordinal(days)}"
    return "on track"


def month_budgets(month: date | None = None, today: date | None = None, con=None) -> MonthBudgets:
    """Every budget's standing for a month (default: this one), and the spend
    categories without a budget, with a suggested amount for each."""
    con = con or get_con()
    today = today or today_london()
    first = month_start(month or today)
    last = month_end(first)
    days = last.day
    current = first == month_start(today)
    day = today.day if current else (days if first < month_start(today) else 0)

    spend = {c["id"]: c["name"] for c in spend_categories(con)}
    budgets = {r["parent_id"]: float(r["monthly_amount"])
               for r in _rows(con, "SELECT parent_id, monthly_amount FROM budgets")}
    spent = spent_by_category(first, last, con)
    th = thresholds()

    mb = MonthBudgets(month=first, day=day, days=days, current=current)
    for pid, name in sorted(spend.items(), key=lambda kv: kv[1]):
        s = round(spent.get(name, 0.0), 2)
        if pid in budgets:
            b = budgets[pid]
            projected = round(s / day * days, 2) if current and day else s
            state = _state(s, b, projected, day, current, th)
            mb.statuses.append(BudgetStatus(
                parent_id=pid, category=name, budget=b, spent=s, left=round(b - s, 2),
                used=round(s / b, 4), projected=projected, state=state,
                note=_note(state, s, b, projected, day, days, current)))
        else:
            mb.unbudgeted.append({"parent_id": pid, "category": name, "spent": s})
    if mb.unbudgeted:
        suggested = suggestions(first, con)
        for u in mb.unbudgeted:
            u["suggested"] = suggested.get(u["parent_id"])
        mb.unbudgeted.sort(key=lambda u: (-u["spent"], -(u["suggested"] or 0), u["category"]))
    mb.statuses.sort(key=lambda s: -s.used)
    return mb


def history(months: int = 6, today: date | None = None, con=None) -> list[dict]:
    """Spend in budgeted categories per month, the last `months` up to this one,
    against today's budgets (budgets aren't kept per month). One query for all
    the months together."""
    con = con or get_con()
    today = today or today_london()
    budgets = {r["name"]: float(r["monthly_amount"]) for r in _rows(con, """
        SELECT p.name, b.monthly_amount FROM budgets b JOIN parent_categories p ON p.id = b.parent_id
        WHERE p.archived_at IS NULL""")}
    first = add_months(month_start(today), -(months - 1))
    last = month_end(month_start(today))
    rows = _rows(con, """
        SELECT date_trunc('month', created_at)::date AS m, llm_category AS category,
               COALESCE(-SUM(amount) FILTER (WHERE role = 'spend' AND amount < 0), 0) AS spent
        FROM transaction_roles
        WHERE NOT skipped AND llm_category IS NOT NULL AND created_at >= %s AND created_at < %s::date + 1
        GROUP BY 1, 2""", [first, last])
    by_month: dict[date, float] = {}
    for r in rows:
        if r["category"] in budgets:
            by_month[r["m"]] = by_month.get(r["m"], 0.0) + float(r["spent"])
    out = []
    for i in range(months):
        m = add_months(first, i)
        out.append({"month": m, "spent": round(by_month.get(m, 0.0), 2), "budget": round(sum(budgets.values()), 2)})
    return out


# ── Changing ──────────────────────────────────────────────────────────────────

def find_spend_category(name: str, con=None) -> dict:
    """The spend category with this name (case ignored), or a BudgetError."""
    con = con or get_con()
    rows = [c for c in spend_categories(con) if c["name"].strip().lower() == (name or "").strip().lower()]
    if rows:
        return rows[0]
    exists = _rows(con, "SELECT 1 FROM parent_categories WHERE archived_at IS NULL AND lower(name) = lower(%s)", [name])
    if exists:
        raise BudgetError(f"{name} isn't a spending category, so it can't have a budget.")
    raise BudgetError(f"There's no category called {name!r}.")


def check_amount(amount) -> float:
    try:
        value = round(float(amount), 2)
    except (TypeError, ValueError):
        raise BudgetError(f"{amount!r} isn't an amount.")
    if not (MIN_BUDGET <= value <= MAX_BUDGET):
        raise BudgetError(f"A budget has to be between £{MIN_BUDGET} and £{MAX_BUDGET:,}.")
    return value


def set_budget(parent_id: int, amount, con=None) -> float:
    """Set a category's monthly budget. Only spend categories; £1 to £100,000."""
    con = con or get_con()
    value = check_amount(amount)
    if parent_id not in {c["id"] for c in spend_categories(con)}:
        raise BudgetError("Only a spending category can have a budget.")
    con.execute("""INSERT INTO budgets (parent_id, monthly_amount, updated_at) VALUES (%s, %s, now())
                   ON CONFLICT (parent_id) DO UPDATE SET monthly_amount = EXCLUDED.monthly_amount,
                                                         updated_at = EXCLUDED.updated_at""",
                [parent_id, value])
    return value


def clear_budget(parent_id: int, con=None) -> None:
    (con or get_con()).execute("DELETE FROM budgets WHERE parent_id = %s", [parent_id])


def current_budget(parent_id: int, con=None) -> float | None:
    row = (con or get_con()).execute("SELECT monthly_amount FROM budgets WHERE parent_id = %s", [parent_id]).fetchone()
    return float(row[0]) if row else None


# ── Settings: how often messages come, which alerts ───────────────────────────

# How often the budget message comes. Each mode is decided on London's date:
#   off       never
#   daily     every morning
#   weekdays  Monday to Friday
#   days      on the days of the week chosen (any mix: Mon and Thu for twice a week)
#   monthly   once a month, on the chosen day of the month (1 to 28)
# The worker runs hourly from 08:00, so the first run of the day sends it.
DIGEST_MODES = {
    "off": "Off",
    "daily": "Every morning",
    "weekdays": "Weekdays only (Mon to Fri)",
    "days": "On the days I choose",
    "monthly": "Once a month, on a day I choose",
}
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
DEFAULT_DIGEST = {"mode": "days", "days": [0], "monthday": 1}   # Mondays, as before


def digest_settings() -> dict:
    """How the budget message is set to come: {"mode", "days", "monthday"}.
    Reads the older values too ("off", "daily", "weekly")."""
    value = get_setting(DIGEST_SETTING, None)
    if value is None:
        return dict(DEFAULT_DIGEST)
    if isinstance(value, str):
        value = {"off": {"mode": "off"}, "daily": {"mode": "daily"},
                 "weekly": {"mode": "days", "days": [0]}}.get(value, {})
    mode = value.get("mode") if isinstance(value, dict) else None
    if mode not in DIGEST_MODES:
        return dict(DEFAULT_DIGEST)
    days = sorted({int(d) for d in value.get("days", []) if 0 <= int(d) <= 6}) if mode == "days" else []
    monthday = min(max(int(value.get("monthday", 1)), 1), 28) if mode == "monthly" else 1
    return {"mode": mode, "days": days, "monthday": monthday}


def set_digest(mode: str, days=(), monthday: int = 1) -> None:
    """Choose how the budget message comes. Errors say what's wrong, plainly."""
    if mode not in DIGEST_MODES:
        raise BudgetError(f"How often must be one of: {', '.join(DIGEST_MODES)}.")
    days = sorted({int(d) for d in days})
    if mode == "days":
        if not days or any(d < 0 or d > 6 for d in days):
            raise BudgetError("Choose at least one day of the week.")
    if mode == "monthly" and not (1 <= int(monthday) <= 28):
        raise BudgetError("Choose a day of the month from 1 to 28.")
    set_setting(DIGEST_SETTING, {"mode": mode, "days": days if mode == "days" else [],
                                 "monthday": int(monthday) if mode == "monthly" else 1})


def alerts_wanted() -> set[str]:
    value = get_setting(ALERTS_SETTING, list(DEFAULT_ALERT_KINDS))
    return {k for k in (value if isinstance(value, list) else DEFAULT_ALERT_KINDS) if k in ALERT_KINDS}


def set_alerts_wanted(kinds) -> None:
    set_setting(ALERTS_SETTING, [k for k in ALERT_KINDS if k in set(kinds)])


def due_today(settings: dict, day: date) -> bool:
    """Whether the budget message is due on this day, under these settings."""
    mode = settings["mode"]
    if mode == "off":
        return False
    if mode == "daily":
        return True
    if mode == "weekdays":
        return day.weekday() < 5
    if mode == "days":
        return day.weekday() in settings["days"]
    last = calendar.monthrange(day.year, day.month)[1]
    return day.day == min(settings["monthday"], last)


def digest_due(now: datetime | None = None) -> bool:
    """Whether today's budget message is still to go: due today, and not sent yet."""
    now = now or datetime.now(LONDON)
    if not due_today(digest_settings(), now.date()):
        return False
    return get_setting(DIGEST_LAST_SENT) != now.date().isoformat()


def digest_sent(now: datetime | None = None) -> None:
    set_setting(DIGEST_LAST_SENT, (now or datetime.now(LONDON)).date().isoformat())


# ── Alerts: once per category per month ───────────────────────────────────────

def alerts_due(mb: MonthBudgets, con=None) -> list[tuple[BudgetStatus, str]]:
    """The alerts this month's standing calls for that haven't been sent yet, that you
    want, and whose category isn't muted. At most one per category per run: the most serious."""
    if not mb.current:
        return []
    con = con or get_con()
    th = thresholds()
    wanted = alerts_wanted()
    muted = muted_parents()
    sent = {(r["parent_id"], r["kind"]) for r in _rows(
        con, "SELECT parent_id, kind FROM budget_alerts WHERE month = %s", [mb.month])}
    due = []
    for s in mb.statuses:
        if s.parent_id in muted:
            continue
        for kind in ("over", "near", "pace", "early"):
            hit = (kind == "over" and s.state == "over") or \
                  (kind == "near" and s.used >= th["near"]) or \
                  (kind == "pace" and s.state == "pace") or \
                  (kind == "early" and s.used >= th["early"])
            if hit and kind in wanted and (s.parent_id, kind) not in sent:
                due.append((s, kind))
                break
    return due


def week_alert_due(today: date, con=None) -> float | None:
    """This week's spend across the budgeted categories, if it's over the weekly limit and the
    weekly alert hasn't gone for this week yet. None otherwise. Monday to today, London time."""
    th = thresholds()
    if "week" not in alerts_wanted() or th["week_limit"] <= 0:
        return None
    con = con or get_con()
    monday = today - timedelta(days=today.weekday())
    if get_setting(WEEK_ALERTED) == _iso_week(today):
        return None
    names = {r["name"] for r in _rows(con, "SELECT p.name FROM parent_categories p JOIN budgets b ON b.parent_id = p.id")}
    spent = spent_by_category(monday, today, con)
    total = round(sum(v for k, v in spent.items() if k in names), 2)
    return total if total > th["week_limit"] else None


def mark_week_alerted(today: date) -> None:
    set_setting(WEEK_ALERTED, _iso_week(today))


def _iso_week(day: date) -> str:
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def alert_sent(status: BudgetStatus, kind: str, month: date, con=None) -> None:
    """Recorded once sent. A stronger alert also counts as the weaker ones, so a category that
    jumps straight past several isn't told about each."""
    con = con or get_con()
    covers = {"over": {"over", "near", "early"}, "near": {"near", "early"}}.get(kind, {kind})
    for k in covers:
        con.execute("""INSERT INTO budget_alerts (parent_id, month, kind) VALUES (%s, %s, %s)
                       ON CONFLICT DO NOTHING""", [status.parent_id, month, k])
