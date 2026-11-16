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

from database_functions import get_con, get_setting, set_setting

LONDON = ZoneInfo("Europe/London")

NEAR = 0.80            # "nearly used up" from here
PACE_OVER = 1.10       # off pace when heading for more than this much of the budget
PACE_FROM_DAY = 7      # too early before this to call a pace
MIN_BUDGET, MAX_BUDGET = 1, 100_000
SUGGEST_MONTHS = 3

# Settings (app_settings), changed on the Budget page.
DIGEST_SETTING = "budget_digest"                 # "off" | "daily" | "weekly"
DIGEST_CHOICES = ("off", "daily", "weekly")
DEFAULT_DIGEST = "weekly"
ALERTS_SETTING = "budget_alerts"                 # the kinds of instant alert wanted
ALERT_KINDS = ("near", "over", "pace")
DIGEST_LAST_SENT = "budget_digest_last_sent"     # London date of the last digest


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


def _state(spent: float, budget: float, projected: float, day: int, current: bool) -> str:
    if spent > budget:
        return "over"
    if spent >= budget * NEAR:
        return "near"
    if current and day >= PACE_FROM_DAY and projected > budget * PACE_OVER:
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

    mb = MonthBudgets(month=first, day=day, days=days, current=current)
    for pid, name in sorted(spend.items(), key=lambda kv: kv[1]):
        s = round(spent.get(name, 0.0), 2)
        if pid in budgets:
            b = budgets[pid]
            projected = round(s / day * days, 2) if current and day else s
            state = _state(s, b, projected, day, current)
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

def digest_frequency() -> str:
    value = get_setting(DIGEST_SETTING, DEFAULT_DIGEST)
    return value if value in DIGEST_CHOICES else DEFAULT_DIGEST


def alerts_wanted() -> set[str]:
    value = get_setting(ALERTS_SETTING, list(ALERT_KINDS))
    return {k for k in (value if isinstance(value, list) else ALERT_KINDS) if k in ALERT_KINDS}


def set_digest_frequency(value: str) -> None:
    if value not in DIGEST_CHOICES:
        raise BudgetError(f"How often must be one of {', '.join(DIGEST_CHOICES)}.")
    set_setting(DIGEST_SETTING, value)


def set_alerts_wanted(kinds) -> None:
    set_setting(ALERTS_SETTING, [k for k in ALERT_KINDS if k in set(kinds)])


def digest_due(now: datetime | None = None) -> bool:
    """Whether this morning's (daily) or this Monday's (weekly) budget message
    is still to go. The worker runs hourly from 08:00, so the first run of the
    day sends it: you wake up to it."""
    now = now or datetime.now(LONDON)
    frequency = digest_frequency()
    if frequency == "off":
        return False
    if frequency == "weekly" and now.weekday() != 0:
        return False
    return get_setting(DIGEST_LAST_SENT) != now.date().isoformat()


def digest_sent(now: datetime | None = None) -> None:
    set_setting(DIGEST_LAST_SENT, (now or datetime.now(LONDON)).date().isoformat())


# ── Alerts: once per category per month ───────────────────────────────────────

def alerts_due(mb: MonthBudgets, con=None) -> list[tuple[BudgetStatus, str]]:
    """The alerts this month's standing calls for that haven't been sent yet,
    and that you want. At most one per category per run: the most serious."""
    if not mb.current:
        return []
    con = con or get_con()
    wanted = alerts_wanted()
    sent = {(r["parent_id"], r["kind"]) for r in _rows(
        con, "SELECT parent_id, kind FROM budget_alerts WHERE month = %s", [mb.month])}
    due = []
    for s in mb.statuses:
        for kind in ("over", "near", "pace"):
            hit = (kind == "over" and s.state == "over") or \
                  (kind == "near" and s.state in ("near", "over")) or \
                  (kind == "pace" and s.state == "pace")
            if hit and kind in wanted and (s.parent_id, kind) not in sent:
                due.append((s, kind))
                break
    return due


def alert_sent(status: BudgetStatus, kind: str, month: date, con=None) -> None:
    """Recorded once sent. An 'over' also counts as the 80% one, so a category
    that jumps straight past both isn't told twice."""
    con = con or get_con()
    for k in ({kind, "near"} if kind == "over" else {kind}):
        con.execute("""INSERT INTO budget_alerts (parent_id, month, kind) VALUES (%s, %s, %s)
                       ON CONFLICT DO NOTHING""", [status.parent_id, month, k])
