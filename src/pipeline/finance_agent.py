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

import llm_calls
from database_functions import _rows, get_con, get_parents, get_subcategories

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
DAILY_LIMIT_USD = float(os.getenv("FINANCE_AGENT_DAILY_LIMIT_USD", "0.30"))

SQL_TIMEOUT_MS = 5_000
SQL_MAX_ROWS = 200
# Longest tool result handed back to the model, in characters.
MAX_RESULT_CHARS = 15_000
MAX_GROUPS = 60
MAX_TRANSACTIONS = 100


class DailyLimitReached(Exception):
    pass


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
    rows = _rows(sql, params)
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

    summary = _rows(
        f"""SELECT COUNT(*) AS matches,
                   COALESCE(-SUM(amount) FILTER (WHERE amount < 0), 0) AS money_out,
                   COALESCE(SUM(amount) FILTER (WHERE amount > 0), 0) AS money_in
            FROM transaction_roles WHERE {clause}""", params)[0]
    rows = _rows(
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
    subs = _rows("SELECT name, merchant_name, amount, frequency, active FROM subscriptions ORDER BY active DESC, name")
    for s in subs:
        s["per_month"] = float(s["amount"]) * monthly_factor[s["frequency"]]
    active = [s for s in subs if s["active"]]
    per_month = sum(s["per_month"] for s in active)
    return {"active_per_month": _plain(per_month), "active_per_year": _plain(per_month * 12),
            "subscriptions": [{**_clean(s), "active": s["active"]} for s in subs]}


def list_categories(args: dict) -> dict:
    subs_by_parent: dict[str, list] = {}
    for s in get_subcategories():
        subs_by_parent.setdefault(s["parent_name"], []).append(
            {"name": s["name"], "transactions": s["transaction_count"],
             **({"role": s["role_override"]} if s["role_override"] else {})})
    return {"categories": [
        {"name": p["name"], "role": p["role"], "transactions": p["transaction_count"],
         "subcategories": subs_by_parent.get(p["name"], [])}
        for p in get_parents()
    ]}


_SQL_START = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)


def run_sql(args: dict) -> dict:
    query = (args.get("query") or "").strip().rstrip(";").strip()
    if not _SQL_START.match(query):
        raise ToolError("Only a single SELECT (or WITH ... SELECT) query is allowed.")
    if ";" in query:
        raise ToolError("One statement only: no semicolons inside the query.")
    con = get_con()
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
            "Other tables: subscriptions (name, merchant_name, amount, frequency, active)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "The SELECT statement."}},
            "required": ["query"],
        },
    },
]


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

def system_prompt(today: date) -> str:
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
- Treat transfers between the user's own accounts, savings pots and refunds as what the data says they are, not as spending or income."""


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

    def __init__(self, client, settings: dict | None = None, daily_limit_usd: float | None = None):
        self.client = client
        self.settings = settings or MODEL_SETTINGS
        self.daily_limit_usd = DAILY_LIMIT_USD if daily_limit_usd is None else daily_limit_usd
        self.messages: list[dict] = []
        self.cost_usd = 0.0

    def _call(self, today: date, **extra):
        response = llm_calls.create(
            self.client, job="finance_agent", prompt_fn=system_prompt,
            **llm_calls.request_kwargs(self.settings, MAX_TOKENS),
            # Cached: the tools and instructions are the same on every call
            # of the day, so each follow-up only pays full price for what's new.
            system=[{"type": "text", "text": system_prompt(today), "cache_control": {"type": "ephemeral"}}],
            tools=TOOLS, messages=self.messages, **extra,
        )
        tokens = llm_calls._tokens(getattr(response, "usage", None))
        model = llm_calls._str(getattr(response, "model", None)) or self.settings["model"]
        cost = llm_calls.cost(llm_calls.prices_for(model, today), tokens) or 0.0
        return response, cost

    def ask(self, question: str, on_step=None) -> Answer:
        """Answer one question. on_step(Step) is called as each tool finishes,
        for a live view of what it's doing. Raises DailyLimitReached before
        sending anything once today's spend has reached the limit."""
        if spent_today() >= self.daily_limit_usd:
            raise DailyLimitReached(
                f"Today's limit of ${self.daily_limit_usd:.2f} for questions has been reached -- it resets at midnight.")
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
                self.messages.append({"role": "assistant", "content": response.content})

                tool_uses = [b for b in response.content if getattr(b, "type", None) == "tool_use"]
                if response.stop_reason != "tool_use" or not tool_uses:
                    break
                results = []
                for block in tool_uses:
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
