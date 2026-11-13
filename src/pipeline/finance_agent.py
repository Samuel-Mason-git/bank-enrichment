"""An agent that answers questions about your money from the database.

You ask in plain English ("how much did I spend eating out in October vs
September?"); Claude answers by calling the tools below, which read the same
data the dashboard shows, and replies with the figures and where they came
from. Every step it took is kept on the answer, so the page can show its
workings.

Totals go through get_totals, which counts money exactly as the Overview page
does (spend is outgoing spend-role money, income incoming income-role money,
investments and savings net, transfers one direction at a time) -- the usual
way a finance agent goes wrong is adding transfers or pot moves into spend.
run_sql is the escape hatch for anything the fixed tools can't answer: one
SELECT, run in a read-only transaction with a short timeout and a row cap.

Every tool reads through the agent's own database login (AGENT_DB_USER), made
by data-vault's new_reader.sh: read-only, and able to see only what
sql/postgres/schema.sql grants it -- the transaction_roles view, subscriptions
and the category tables. Not the raw tables, llm_calls, rules or anything
else. Without that login the agent doesn't run; it never falls back to the
main, read-write login. (Recording its calls in llm_calls is the one write,
and that goes through the main login, outside the agent's reach.)

Each call to Claude goes through llm_calls.create (job "finance_agent"), so
it is recorded and costed like every other call, and a daily ceiling on that
job's spend stops new questions once it is reached.

Kept free of pandas: the worker image doesn't have it, and a Telegram /ask
would run there."""
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import psycopg

import llm_calls
from database_functions import get_con, get_setting

log = logging.getLogger(__name__)

LONDON = ZoneInfo("Europe/London")

# The model and its settings (llm_calls.request_kwargs). Haiku with thinking:
# a question is a few tool calls, and thinking is what lets a small model plan
# "compare, then explain the difference" -- for fractions of a penny.
MODEL_SETTINGS = {"model": "claude-haiku-5-5", "effort": "medium", "thinking": True}
MAX_TOKENS = 16_000
# Rounds of tool calls per question before it must answer with what it has.
MAX_TOOL_ROUNDS = 10
# USD a London day across every question, after which new ones are refused.
# Set on the dashboard's Settings page (app_settings); this is the default
# until it has been.
DAILY_LIMIT_SETTING = "finance_agent_daily_limit_usd"
DEFAULT_DAILY_LIMIT_USD = 0.30


def daily_limit() -> float:
    """Today's limit, as set on the Settings page -- read on every question, so
    a change applies at once, even to a chat already open."""
    value = get_setting(DAILY_LIMIT_SETTING, DEFAULT_DAILY_LIMIT_USD)
    try:
        return max(float(value), 0.0)
    except (TypeError, ValueError):
        return DEFAULT_DAILY_LIMIT_USD

SQL_TIMEOUT_MS = 5_000
SQL_MAX_ROWS = 200
# Longest tool result handed back to the model, in characters.
MAX_RESULT_CHARS = 15_000
MAX_GROUPS = 60
MAX_TRANSACTIONS = 100


class DailyLimitReached(Exception):
    pass


class AgentNotConfigured(Exception):
    pass


# ── The agent's own, read-only connection ─────────────────────────────────────

_con: psycopg.Connection | None = None


def is_configured() -> bool:
    return bool(os.getenv("AGENT_DB_USER") and os.getenv("AGENT_DB_PASSWORD"))


def _connect() -> psycopg.Connection:
    if not is_configured():
        raise AgentNotConfigured(
            "The agent's read-only database login isn't set up: add AGENT_DB_USER and AGENT_DB_PASSWORD "
            "to config/.env (data-vault's new_reader.sh makes it).")
    e = os.environ
    return psycopg.connect(
        host=e["DB_HOST"], port=e.get("DB_PORT", "5432"), dbname=e["DB_NAME"],
        user=e["AGENT_DB_USER"], password=e["AGENT_DB_PASSWORD"], connect_timeout=10, autocommit=True,
        # Read-only even if the login's own setting were ever lost.
        options="-c TimeZone=UTC -c default_transaction_read_only=on", application_name="finance_agent",
    )


def agent_con() -> psycopg.Connection:
    """The agent's connection, made again if it has dropped."""
    global _con
    if _con is None or _con.closed or _con.broken:
        _con = _connect()
    return _con


def _q(sql: str, params: list | None = None) -> list[dict]:
    cur = agent_con().execute(sql, params or [])
    cols = [c.name for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _plain(value):
    if isinstance(value, Decimal):
        return round(float(value), 2)
    if isinstance(value, float):
        return round(value, 2)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _json(obj) -> str:
    text = json.dumps(obj, default=_plain, ensure_ascii=False)
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + ' ... [cut short: narrow the question or the date range]'
    return text


def _clean(row: dict) -> dict:
    """Plain values, and no zero/empty fields -- fewer tokens, same meaning."""
    out = {}
    for k, v in row.items():
        v = _plain(v)
        if v in (None, "", 0, 0.0):
            continue
        out[k] = v
    return out


def _date(value, name: str) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise ToolError(f"{name} must be a date like 2026-10-31, not {value!r}")


class ToolError(Exception):
    """A problem with how a tool was called: shown to the model so it can retry."""


# The money columns, exactly as the Overview page computes them.
_METRICS = """
    COALESCE(-SUM(amount) FILTER (WHERE role = 'spend' AND amount < 0), 0)                          AS spend,
    COALESCE(SUM(amount) FILTER (WHERE role = 'income' AND amount > 0), 0)                          AS income,
    COALESCE(-SUM(amount) FILTER (WHERE role = 'investment'), 0)                                    AS invested,
    COALESCE(-SUM(amount) FILTER (WHERE role = 'saving'), 0)                                        AS saved,
    COALESCE(-SUM(amount) FILTER (WHERE role IN ('transfer', 'excluded') AND amount < 0), 0)        AS transferred_out,
    COALESCE(SUM(amount) FILTER (WHERE role IN ('transfer', 'excluded') AND amount > 0), 0)         AS transferred_in,
    COUNT(*)                                                                                        AS transactions"""

_PAYEE = "COALESCE(NULLIF(merchant_name, ''), NULLIF(counterparty_name, ''), description)"

_GROUPS = {
    "none": [],
    "category": [("COALESCE(llm_category, 'Unclassified')", "category")],
    "subcategory": [("COALESCE(llm_category, 'Unclassified')", "category"),
                    ("COALESCE(llm_subcategory, '-')", "subcategory")],
    "month": [("to_char(created_at, 'YYYY-MM')", "month")],
    "merchant": [(_PAYEE, "merchant")],
}


def _filters(args: dict) -> tuple[list[str], list]:
    """WHERE clauses shared by get_totals and find_transactions. Skipped
    transactions are left out, as the dashboard does by default."""
    where, params = ["NOT skipped"], []
    d_from, d_to = _date(args.get("date_from"), "date_from"), _date(args.get("date_to"), "date_to")
    if d_from:
        where.append("created_at >= %s")
        params.append(d_from)
    if d_to:
        where.append("created_at < %s::date + 1")
        params.append(d_to)
    if args.get("category"):
        where.append("lower(llm_category) = lower(%s)")
        params.append(args["category"])
    if args.get("subcategory"):
        where.append("lower(llm_subcategory) = lower(%s)")
        params.append(args["subcategory"])
    if args.get("text"):
        where.append("(merchant_name ILIKE %s OR counterparty_name ILIKE %s OR description ILIKE %s OR user_context ILIKE %s)")
        params.extend([f"%{args['text']}%"] * 4)
    return where, params


# ── Tools ─────────────────────────────────────────────────────────────────────

def get_totals(args: dict) -> dict:
    group_by = args.get("group_by") or "none"
    if group_by not in _GROUPS:
        raise ToolError(f"group_by must be one of {', '.join(_GROUPS)}")
    where, params = _filters(args)
    cols = _GROUPS[group_by]
    select = ", ".join(f"{expr} AS {alias}" for expr, alias in cols)
    sql = f"SELECT {select + ',' if select else ''} {_METRICS} FROM transaction_roles WHERE {' AND '.join(where)}"
    if cols:
        sql += f" GROUP BY {', '.join(str(i + 1) for i in range(len(cols)))}"
        # Wrapped so ORDER BY can use the metric names inside an expression.
        sql = f"SELECT * FROM ({sql}) g " + (
            "ORDER BY 1" if group_by == "month" else
            "ORDER BY GREATEST(spend, income, ABS(invested), ABS(saved), transferred_out, transferred_in) DESC")
    rows = _q(sql, params)
    result = {"period": {"from": args.get("date_from") or "all time", "to": args.get("date_to") or "today"}}
    if not cols:
        result["totals"] = _clean(rows[0])
        return result
    result["groups"] = [_clean(r) for r in rows[:MAX_GROUPS]]
    if len(rows) > MAX_GROUPS:
        result["note"] = f"{len(rows) - MAX_GROUPS} smaller group(s) left out"
    return result


def find_transactions(args: dict) -> dict:
    where, params = _filters(args)
    direction = args.get("direction")
    if direction == "out":
        where.append("amount < 0")
    elif direction == "in":
        where.append("amount > 0")
    elif direction:
        raise ToolError("direction must be 'in' or 'out'")
    for key, op in (("min_amount", ">="), ("max_amount", "<=")):
        if args.get(key) is not None:
            where.append(f"ABS(amount) {op} %s")
            params.append(float(args[key]))
    sort = {"newest": "created_at DESC", "oldest": "created_at ASC",
            "largest": "ABS(amount) DESC"}.get(args.get("sort") or "newest")
    if sort is None:
        raise ToolError("sort must be newest, oldest or largest")
    limit = max(1, min(int(args.get("limit") or 25), MAX_TRANSACTIONS))
    clause = " AND ".join(where)

    summary = _q(
        f"""SELECT COUNT(*) AS matches,
                   COALESCE(-SUM(amount) FILTER (WHERE amount < 0), 0) AS money_out,
                   COALESCE(SUM(amount) FILTER (WHERE amount > 0), 0) AS money_in
            FROM transaction_roles WHERE {clause}""", params)[0]
    rows = _q(
        f"""SELECT id, created_at::date AS date, amount, {_PAYEE} AS payee, description,
                   user_context AS context, llm_category AS category, llm_subcategory AS subcategory, role
            FROM transaction_roles WHERE {clause} ORDER BY {sort} LIMIT %s""", params + [limit])
    out = {"matches": summary["matches"], "money_out": _plain(summary["money_out"]),
           "money_in": _plain(summary["money_in"]), "transactions": [_clean(r) for r in rows]}
    if summary["matches"] > len(rows):
        out["note"] = f"showing {len(rows)} of {summary['matches']}; money_out/money_in cover all of them"
    return out


def list_subscriptions(args: dict) -> dict:
    monthly_factor = {"weekly": 52 / 12, "fortnightly": 26 / 12, "monthly": 1, "annual": 1 / 12}
    subs = _q("SELECT name, merchant_name, amount, frequency, active FROM subscriptions ORDER BY active DESC, name")
    for s in subs:
        s["per_month"] = float(s["amount"]) * monthly_factor[s["frequency"]]
    active = [s for s in subs if s["active"]]
    per_month = sum(s["per_month"] for s in active)
    return {"active_per_month": _plain(per_month), "active_per_year": _plain(per_month * 12),
            "subscriptions": [{**_clean(s), "active": s["active"]} for s in subs]}


def list_categories(args: dict) -> dict:
    """Read from the granted tables only (the agent can't see label_events)."""
    counts: dict[tuple, int] = {}
    for r in _q("""SELECT parent_id, subcategory_id, COUNT(*) AS n FROM transaction_roles
                   WHERE parent_id IS NOT NULL AND NOT skipped GROUP BY 1, 2"""):
        counts[(r["parent_id"], r["subcategory_id"])] = r["n"]
    rows = _q("""SELECT p.id AS parent_id, p.name AS parent,
                        COALESCE(pr.role, default_role_for_parent(p.name), 'spend') AS parent_role,
                        s.id AS sub_id, s.name AS sub, sr.role AS sub_role
                 FROM parent_categories p
                 LEFT JOIN subcategories s ON s.parent_id = p.id AND s.archived_at IS NULL
                 LEFT JOIN category_roles pr ON pr.parent_id = p.id AND pr.subcategory_id IS NULL
                 LEFT JOIN category_roles sr ON sr.subcategory_id = s.id
                 WHERE p.archived_at IS NULL
                 ORDER BY p.name, s.name""")
    cats: dict[str, dict] = {}
    for r in rows:
        cat = cats.setdefault(r["parent"], {
            "name": r["parent"], "role": r["parent_role"],
            "transactions": sum(n for (pid, _), n in counts.items() if pid == r["parent_id"]),
            "subcategories": []})
        if r["sub"]:
            cat["subcategories"].append({"name": r["sub"], "transactions": counts.get((r["parent_id"], r["sub_id"]), 0),
                                         **({"role": r["sub_role"]} if r["sub_role"] else {})})
    for cat in cats.values():
        cat["subcategories"].sort(key=lambda sub: -sub["transactions"])
    return {"categories": sorted(cats.values(), key=lambda c: -c["transactions"])}


_SQL_START = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)


def run_sql(args: dict) -> dict:
    query = (args.get("query") or "").strip().rstrip(";").strip()
    if not _SQL_START.match(query):
        raise ToolError("Only a single SELECT (or WITH ... SELECT) query is allowed.")
    if ";" in query:
        raise ToolError("One statement only: no semicolons inside the query.")
    con = agent_con()
    try:
        # Read-only whatever the query says, and given up on after the timeout.
        with con.transaction():
            con.execute("SET TRANSACTION READ ONLY")
            con.execute(f"SET LOCAL statement_timeout = {int(SQL_TIMEOUT_MS)}")
            cur = con.execute(query)
            cols = [c.name for c in cur.description] if cur.description else []
            rows = cur.fetchmany(SQL_MAX_ROWS + 1)
    except Exception as e:
        raise ToolError(f"{type(e).__name__}: {str(e).strip()[:500]}")
    out = {"columns": cols, "rows": [[_plain(v) for v in r] for r in rows[:SQL_MAX_ROWS]]}
    if len(rows) > SQL_MAX_ROWS:
        out["note"] = f"cut at {SQL_MAX_ROWS} rows: aggregate in SQL instead of listing"
    return out


# ── Charts (the dashboard only) ───────────────────────────────────────────────
#
# The model never types a chart's figures: it says what to plot, and the
# figures come straight from get_totals, so a chart can't show a number the
# data doesn't hold.

CHART_METRICS = ("spend", "income", "invested", "saved", "transferred_out", "transferred_in")
CHART_GROUPS = ("category", "subcategory", "month", "merchant")
CHART_MAX_BARS = 25


def build_chart(args: dict) -> dict:
    metric, group_by = args.get("metric"), args.get("group_by")
    if metric not in CHART_METRICS:
        raise ToolError(f"metric must be one of {', '.join(CHART_METRICS)}")
    if group_by not in CHART_GROUPS:
        raise ToolError(f"group_by must be one of {', '.join(CHART_GROUPS)}")
    kind = args.get("kind") or ("line" if group_by == "month" else "bar")
    if kind not in ("bar", "line"):
        raise ToolError("kind must be bar or line")
    top_n = max(1, min(int(args.get("top_n") or 12), CHART_MAX_BARS))
    filters = {k: args[k] for k in ("date_from", "date_to", "category", "subcategory", "text") if args.get(k)}
    rows = get_totals({**filters, "group_by": group_by})["groups"]

    def label(r):
        if group_by == "subcategory":
            return r["subcategory"] if args.get("category") else f"{r['category']} › {r['subcategory']}"
        return str(r[group_by])

    points = [{"label": label(r), "value": float(r.get(metric, 0))} for r in rows]
    if group_by != "month":
        points = sorted((p for p in points if p["value"]), key=lambda p: -p["value"])
        if len(points) > top_n:
            rest = points[top_n:]
            points = points[:top_n] + [{"label": f"Everything else ({len(rest)})",
                                        "value": round(sum(p["value"] for p in rest), 2)}]
    if not any(p["value"] for p in points):
        raise ToolError(f"Nothing to chart: no {metric} for that period and filter.")
    return {"kind": kind, "title": str(args.get("title") or "").strip()[:80] or f"{metric.title()} by {group_by}",
            "metric": metric, "group_by": group_by,
            "period": {"from": args.get("date_from") or "all time", "to": args.get("date_to") or "today"},
            "points": points}


def chart_summary(chart: dict) -> dict:
    """What the model is told: the chart is on screen, and its shape."""
    biggest = max(chart["points"], key=lambda p: p["value"])
    return {"chart_shown": True, "points": len(chart["points"]),
            "total": round(sum(p["value"] for p in chart["points"]), 2),
            "largest": [biggest["label"], biggest["value"]]}


TOOL_FUNCTIONS = {
    "get_totals": get_totals,
    "find_transactions": find_transactions,
    "list_subscriptions": list_subscriptions,
    "list_categories": list_categories,
    "run_sql": run_sql,
}

# What the page shows while a tool runs.
TOOL_LABELS = {
    "get_totals": "Adding up totals",
    "find_transactions": "Looking through transactions",
    "list_subscriptions": "Checking subscriptions",
    "list_categories": "Reading your categories",
    "run_sql": "Running a query",
    "show_chart": "Drawing a chart",
}

_PERIOD = {
    "date_from": {"type": "string", "description": "First day included, YYYY-MM-DD. Omit for all time."},
    "date_to": {"type": "string", "description": "Last day included, YYYY-MM-DD. Omit for up to today."},
    "category": {"type": "string", "description": "Only this parent category (exact name from list_categories)."},
    "subcategory": {"type": "string", "description": "Only this subcategory (exact name from list_categories)."},
    "text": {"type": "string", "description": "Only transactions whose merchant, payee, description or the user's note contains this."},
}

TOOLS = [
    {
        "name": "get_totals",
        "description": (
            "Money totals for a period, optionally grouped -- the way to answer 'how much'. Counts exactly as the "
            "dashboard does: spend = outgoing spend-role money; income = incoming income-role money; invested / "
            "saved = net money into investments / savings; transferred_out / transferred_in = transfers and "
            "excluded items (e.g. refunds, moves between own accounts), never counted as spend or income. "
            "Zero figures are left out of each row. Call it twice to compare two periods."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                **_PERIOD,
                "group_by": {"type": "string", "enum": list(_GROUPS),
                             "description": "Split the totals by category, subcategory, month or merchant (default none)."},
            },
        },
    },
    {
        "name": "find_transactions",
        "description": (
            "Individual transactions matching filters, with how many matched and their money in/out in total "
            "(covering every match, not only the ones listed). Amounts are negative for money out. Use it for "
            "'when did I last...', 'show me...', or to see what makes up a total."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                **_PERIOD,
                "direction": {"type": "string", "enum": ["in", "out"]},
                "min_amount": {"type": "number", "description": "Smallest size in £, ignoring direction."},
                "max_amount": {"type": "number", "description": "Largest size in £, ignoring direction."},
                "sort": {"type": "string", "enum": ["newest", "oldest", "largest"]},
                "limit": {"type": "integer", "description": f"How many to list (default 25, at most {MAX_TRANSACTIONS})."},
            },
        },
    },
    {
        "name": "list_subscriptions",
        "description": "The user's confirmed subscriptions: amount, how often, whether active, and the active ones' cost per month and year.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_categories",
        "description": (
            "Every category and subcategory with its role (spend, income, investment, saving, transfer, excluded) "
            "and how many transactions it holds. Use it to turn the user's words ('eating out', 'bills') into "
            "the exact category names the other tools filter on."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "run_sql",
        "description": (
            "Last resort, for questions the other tools can't answer: one read-only PostgreSQL SELECT, "
            f"{SQL_TIMEOUT_MS // 1000}s timeout, at most {SQL_MAX_ROWS} rows -- aggregate in SQL rather than listing rows. "
            "Query the view transaction_roles: id, amount (NUMERIC £, negative = money out), created_at (London "
            "time, no zone), description, merchant_name, counterparty_name, user_context (the user's own note), "
            "monzo_category, llm_category (parent category), llm_subcategory, role (spend | income | investment | "
            "saving | transfer | excluded), skipped (BOOLEAN: leave skipped rows out, as the dashboard does). "
            "Totals must follow the role rules in get_totals' description -- never sum transfers into spend. "
            "The only other tables it can read: subscriptions (name, merchant_name, amount, frequency, active), "
            "parent_categories, subcategories and category_roles."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "The SELECT statement."}},
            "required": ["query"],
        },
    },
]


CHART_TOOL = {
    "name": "show_chart",
    "description": (
        "Show the user a chart under your answer -- only when one genuinely helps: several categories, "
        "merchants or months side by side. Don't chart a single figure. You choose what to plot; the "
        "figures are fetched from the data by the same rules as get_totals, so you never type them. "
        "Months come out in order as a line; anything else as bars, biggest first, the smallest "
        "folded into 'Everything else'. Still give the key figures in words."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "A short title, e.g. 'Spending by category, October 2026'."},
            "metric": {"type": "string", "enum": list(CHART_METRICS)},
            "group_by": {"type": "string", "enum": list(CHART_GROUPS)},
            "kind": {"type": "string", "enum": ["bar", "line"], "description": "Default: line for months, bar otherwise."},
            "top_n": {"type": "integer", "description": f"Bars to show before 'Everything else' (default 12, at most {CHART_MAX_BARS})."},
            **_PERIOD,
        },
        "required": ["title", "metric", "group_by"],
    },
}


def run_tool(name: str, args: dict) -> tuple[str, bool]:
    """(result text, is_error). A bad call comes back as an error the model
    can read and correct, never as an exception out of the loop."""
    fn = TOOL_FUNCTIONS.get(name)
    if fn is None:
        return f"No tool called {name!r}.", True
    try:
        return _json(fn(args or {})), False
    except ToolError as e:
        return str(e), True
    except Exception as e:
        log.exception(f"finance agent tool {name} failed")
        return f"The tool failed: {type(e).__name__}: {e}"[:500], True


# ── Prompt ────────────────────────────────────────────────────────────────────

def system_prompt(today: date, channel: str = "dashboard") -> str:
    return f"""You answer questions about the user's personal finances from their own bank data (a UK Monzo account, amounts in £), using the tools provided. Today is {today:%A %d %B %Y}.

How to work:
- Get every figure from a tool. Never estimate, guess, or carry figures over from general knowledge. If the data can't answer the question, say so plainly.
- Use get_totals for any "how much" question: it counts spend, income, savings and transfers the same way the user's dashboard does, so its figures match what they see there. Call it once per period to compare periods.
- When the user names a kind of spending in their own words, check list_categories first and use the exact category names.
- Use find_transactions for specific payments, and to explain what is behind a total.
- Use run_sql only when no other tool can answer, and follow the same role rules for totals.
- Turn relative dates ("last month", "this year", "since the summer") into exact dates, and say which dates you used.

How to answer:
- Lead with the answer in one or two sentences, with the figures (£1,234.56). Then add only the detail that helps: the biggest contributors, the change versus the comparison period, anything surprising.
- Keep it short and plain. Use a small table only when comparing several items.
- Treat transfers between the user's own accounts, savings pots and refunds as what the data says they are, not as spending or income.""" + (
        "\n- You're replying in a Telegram chat on a phone: keep it to a few short lines (about 120 words at most), "
        "bold the key figures with **, and don't use tables or headings."
        if channel == "telegram" else
        "\n- Whenever your answer breaks money down across three or more categories, merchants or months "
        "(\"where did my money go\", \"month by month\", \"compared with\"), also call show_chart once, with the "
        "same period and filters, so the breakdown appears as a chart under your answer. Not for one or two figures.")


# ── The agent ─────────────────────────────────────────────────────────────────

@dataclass
class Step:
    tool: str
    input: dict
    output: str
    is_error: bool = False


@dataclass
class Answer:
    text: str
    steps: list[Step] = field(default_factory=list)
    cost_usd: float = 0.0
    stop_reason: str | None = None
    charts: list[dict] = field(default_factory=list)   # from show_chart, for the page to draw


def spent_today() -> float:
    """USD spent on finance_agent calls so far today, London time."""
    return float(get_con().execute(
        """SELECT COALESCE(SUM(cost_usd), 0) FROM llm_calls
           WHERE job = 'finance_agent'
             AND (started_at AT TIME ZONE 'Europe/London')::date = (now() AT TIME ZONE 'Europe/London')::date"""
    ).fetchone()[0])


class FinanceAgent:
    """One conversation. ask() can be called again with a follow-up: the
    history (thinking blocks included, passed back unchanged as the API
    requires) stays on the object."""

    def __init__(self, client, settings: dict | None = None, daily_limit_usd: float | None = None,
                 job: str = "finance_agent", channel: str = "dashboard"):
        """job names its calls in llm_calls: agent_eval.py uses its own, so an
        eval neither counts against nor is stopped by the Ask page's daily limit."""
        self.client = client
        self.settings = settings or MODEL_SETTINGS
        self._daily_limit_usd = daily_limit_usd   # None: the Settings page's, read when asked
        self.messages: list[dict] = []
        self.cost_usd = 0.0
        self.job = job
        self.channel = channel   # "telegram" asks for short, phone-sized replies

    @property
    def daily_limit_usd(self) -> float:
        return daily_limit() if self._daily_limit_usd is None else self._daily_limit_usd

    def _call(self, today: date, **extra):
        response = llm_calls.create(
            self.client, job=self.job, prompt_fn=system_prompt,
            **llm_calls.request_kwargs(self.settings, MAX_TOKENS),
            # Cached: the tools and instructions are the same on every call
            # of the day, so each follow-up only pays full price for what's new.
            system=[{"type": "text", "text": system_prompt(today, self.channel), "cache_control": {"type": "ephemeral"}}],
            tools=TOOLS + [CHART_TOOL] if self.channel == "dashboard" else TOOLS,
            messages=self.messages, **extra,
        )
        tokens = llm_calls._tokens(getattr(response, "usage", None))
        model = llm_calls._str(getattr(response, "model", None)) or self.settings["model"]
        cost = llm_calls.cost(llm_calls.prices_for(model, today), tokens) or 0.0
        return response, cost

    def ask(self, question: str, on_step=None) -> Answer:
        """Answer one question. on_step(Step) is called as each tool finishes,
        for a live view of what it's doing. Raises DailyLimitReached before
        sending anything once today's spend has reached the limit, and
        AgentNotConfigured without its read-only login."""
        agent_con()
        limit = self.daily_limit_usd
        if self.job == "finance_agent" and spent_today() >= limit:
            raise DailyLimitReached(
                f"Today's limit of ${limit:.2f} for questions has been reached -- it resets at midnight, "
                "or raise it on the Settings page.")
        today = datetime.now(LONDON).date()
        start = len(self.messages)
        self.messages.append({"role": "user", "content": question})
        answer = Answer(text="")
        try:
            for round_no in range(MAX_TOOL_ROUNDS + 1):
                last_round = round_no == MAX_TOOL_ROUNDS
                response, cost = self._call(today, **({"tool_choice": {"type": "none"}} if last_round else {}))
                answer.cost_usd += cost
                answer.stop_reason = response.stop_reason
                # Answers summarise your finances, and llm_calls goes to bronze
                # for good: like every other job, a reply's text is kept only
                # when it couldn't be used (cut off, refused), to see why.
                llm_calls.parsed(response.stop_reason in ("end_turn", "tool_use"))
                self.messages.append({"role": "assistant", "content": response.content})

                tool_uses = [b for b in response.content if getattr(b, "type", None) == "tool_use"]
                if response.stop_reason != "tool_use" or not tool_uses:
                    break
                results = []
                for block in tool_uses:
                    if block.name == CHART_TOOL["name"] and self.channel == "dashboard":
                        try:
                            chart = build_chart(dict(block.input or {}))
                            answer.charts.append(chart)
                            output, is_error = _json(chart_summary(chart)), False
                        except ToolError as e:
                            output, is_error = str(e), True
                        except Exception as e:
                            log.exception("finance agent chart failed")
                            output, is_error = f"The chart failed: {type(e).__name__}: {e}"[:500], True
                    else:
                        output, is_error = run_tool(block.name, block.input)
                    step = Step(block.name, dict(block.input or {}), output, is_error)
                    answer.steps.append(step)
                    if on_step:
                        on_step(step)
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": output, "is_error": is_error})
                if round_no == MAX_TOOL_ROUNDS - 1:
                    results.append({"type": "text", "text": "That is the last tool call allowed for this question: "
                                                            "answer now with what you have, and say if anything is missing."})
                self.messages.append({"role": "user", "content": results})
        except Exception:
            # Leave the conversation as it was, so a retry or follow-up isn't
            # sent a half-finished exchange the API would reject.
            del self.messages[start:]
            raise

        answer.text = llm_calls.response_text(response)
        if answer.stop_reason == "max_tokens":
            answer.text += "\n\n(The answer was cut off -- try a narrower question.)"
        elif answer.stop_reason == "refusal" or not answer.text:
            answer.text = answer.text or "I couldn't answer that one. Try rephrasing it."
        self.cost_usd += answer.cost_usd
        return answer
