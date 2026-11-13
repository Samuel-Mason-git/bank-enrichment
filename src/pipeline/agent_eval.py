"""How accurate the Ask page's agent is, measured on your own data.

Builds about 15 questions whose answers are worked out here, straight from the
database by the dashboard's own rules (spend is outgoing spend-role money,
income incoming income-role money, savings net, skipped transactions left
out), asks each model the same questions, and grades every answer
automatically: the expected figures (to within 51p, or 0.1%) and names must
appear in it. No model marks another's work.

The models take turns question by question, so if the budget runs out both
have answered the same questions. Each question is a fresh conversation.
Calls are recorded in llm_calls as job agent_eval (so they show on the AI
usage page, but don't count against the Ask page's daily limit), and the
agent reads through its own read-only login, as it does on the Ask page.

    python src/pipeline/agent_eval.py            # the questions and their answers, nothing sent
    python src/pipeline/agent_eval.py --yes      # run it (stops at --budget, USD)

Every answer, and why each one passed or failed, goes to
logs/agent_eval-<time>.json (local only: logs/ is gitignored).
"""
import argparse
import calendar
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import anthropic

import finance_agent as fa
from database_functions import init_db, log_path

ARMS: dict[str, dict] = {
    "haiku-5-5 (now)": fa.MODEL_SETTINGS,
    "sonnet-5-5 medium": {"model": "claude-sonnet-5-5", "effort": "medium"},
}
# Before a model has answered anything, what one question is assumed to cost
# when deciding whether the next one still fits the budget.
FIRST_GUESS_USD = {"claude-haiku-5-5": 0.006, "claude-sonnet-5-5": 0.08}
SAFETY = 1.5


# ── The answer key ────────────────────────────────────────────────────────────

_SPEND = "COALESCE(-SUM(amount) FILTER (WHERE role = 'spend' AND amount < 0), 0)"
_INCOME = "COALESCE(SUM(amount) FILTER (WHERE role = 'income' AND amount > 0), 0)"
_SAVED = "COALESCE(-SUM(amount) FILTER (WHERE role = 'saving'), 0)"
_PAYEE = "COALESCE(NULLIF(merchant_name, ''), NULLIF(counterparty_name, ''), description)"


def _one(sql: str, params: list | None = None):
    row = fa.agent_con().execute(sql, params or []).fetchone()
    return row if row is None or len(row) > 1 else row[0]


def _range(d1: date, d2: date) -> tuple[str, list]:
    return "NOT skipped AND created_at >= %s AND created_at < %s::date + 1", [d1, d2]


def _sum(expr: str, d1: date, d2: date, extra: str = "", params: list | None = None) -> float:
    where, p = _range(d1, d2)
    return float(_one(f"SELECT {expr} FROM transaction_roles WHERE {where} {extra}", p + (params or [])))


def _month(d: date) -> tuple[date, date]:
    return d.replace(day=1), d.replace(day=calendar.monthrange(d.year, d.month)[1])


def _name(d: date) -> str:
    return f"{calendar.month_name[d.month]} {d.year}"


@dataclass
class Question:
    text: str
    amounts: list[float] = field(default_factory=list)   # each must appear
    names: list[str] = field(default_factory=list)       # each must appear (case ignored)
    counts: list[int] = field(default_factory=list)      # each must appear as a whole number
    what: str = ""                                       # what it tests


def build_questions(today: date) -> list[Question]:
    m1, m2 = _month(today.replace(day=1) - timedelta(days=1))      # last full month
    p1, p2 = _month(m1 - timedelta(days=1))                        # the one before
    M, P = _name(m1), _name(p1)
    qs: list[Question] = []

    spend_m, spend_p = _sum(_SPEND, m1, m2), _sum(_SPEND, p1, p2)
    income_m = _sum(_INCOME, m1, m2)
    qs.append(Question(f"How much did I spend in {M}?", [spend_m], what="a month's spend (pot moves and transfers must not count)"))
    qs.append(Question(f"What was my total income in {M}?", [income_m], what="income only, not refunds or transfers in"))
    qs.append(Question(f"How did my spending in {M} compare with {P}?", [spend_m, spend_p], what="two periods"))
    qs.append(Question(f"What was my net for {M}: income minus spending?", [round(income_m - spend_m, 2)], what="arithmetic over two totals"))
    qs.append(Question(f"What was my average monthly spending across {P} and {M}?",
                       [round((spend_m + spend_p) / 2, 2)], what="average over two months"))

    where, p = _range(m1, m2)
    cats = fa.agent_con().execute(
        f"""SELECT COALESCE(llm_category, 'Unclassified'), {_SPEND} AS s FROM transaction_roles WHERE {where}
            GROUP BY 1 HAVING {_SPEND} > 0 ORDER BY s DESC LIMIT 2""", p).fetchall()
    if cats:
        qs.append(Question(f"Which category did I spend the most on in {M}, and how much?",
                           [float(cats[0][1])], names=[cats[0][0]], what="top category"))
    if len(cats) > 1:
        qs.append(Question(f"How much did I spend on {cats[1][0]} in {M}?", [float(cats[1][1])],
                           what="one named category"))

    sub = fa.agent_con().execute(
        f"""SELECT llm_subcategory, {_SPEND} AS s FROM transaction_roles WHERE {_range(p1, p2)[0]}
            AND llm_subcategory IS NOT NULL GROUP BY 1 HAVING {_SPEND} > 0 ORDER BY s DESC LIMIT 1""", [p1, p2]).fetchone()
    if sub:
        qs.append(Question(f"How much did I spend on {sub[0]} in {P}?", [float(sub[1])], what="one subcategory, earlier month"))

    big = fa.agent_con().execute(
        f"""SELECT {_PAYEE}, -amount FROM transaction_roles WHERE {where} AND role = 'spend' AND amount < 0
            ORDER BY amount ASC LIMIT 1""", p).fetchone()
    if big:
        qs.append(Question(f"What was my biggest single purchase in {M}?", [float(big[1])], what="largest spend, not a pot move"))

    shop = fa.agent_con().execute(
        f"""SELECT {_PAYEE}, {_SPEND} AS s FROM transaction_roles WHERE {where} AND {_PAYEE} IS NOT NULL
            GROUP BY 1 HAVING {_SPEND} > 0 ORDER BY s DESC LIMIT 1""", p).fetchone()
    if shop:
        qs.append(Question(f"How much did I spend at {shop[0]} in {M}?", [float(shop[1])], what="one merchant"))

    d1, d2 = today - timedelta(days=90), today
    freq = fa.agent_con().execute(
        f"""SELECT {_PAYEE}, COUNT(*), -SUM(amount) FROM transaction_roles WHERE {_range(d1, d2)[0]}
            AND amount < 0 AND role = 'spend' AND {_PAYEE} IS NOT NULL GROUP BY 1 ORDER BY 2 DESC, 3 DESC LIMIT 1""",
        [d1, d2]).fetchone()
    if freq:
        qs.append(Question(f"How many payments did I make to {freq[0]} between {d1:%d %B %Y} and {d2:%d %B %Y}, "
                           f"and how much did they come to?", [float(freq[2])], counts=[int(freq[1])],
                           what="count and total for one payee, exact dates"))

    saved = _sum(_SAVED, m1, m2)
    if abs(saved) >= 1:
        qs.append(Question(f"How much did I put into savings in {M}, after any withdrawals?", [saved],
                           what="savings, net"))

    y1 = date(today.year, 1, 1)
    qs.append(Question(f"How much have I spent so far this year, from 1 January to today?",
                       [_sum(_SPEND, y1, today)], what="year to date"))
    months = fa.agent_con().execute(
        f"""SELECT to_char(created_at, 'YYYY-MM') AS m, {_SPEND} AS s FROM transaction_roles WHERE {_range(y1, m2)[0]}
            GROUP BY 1 ORDER BY s DESC LIMIT 1""", [y1, m2]).fetchone()
    if months:
        best = date.fromisoformat(months[0] + "-01")
        qs.append(Question(f"Which full month this year did I spend the most in, and how much?",
                           [float(months[1])], names=[calendar.month_name[best.month]], what="which month, of several"))

    per_month = {"weekly": 52 / 12, "fortnightly": 26 / 12, "monthly": 1, "annual": 1 / 12}
    subs = fa.agent_con().execute("SELECT amount, frequency FROM subscriptions WHERE active").fetchall()
    if subs:
        yearly = sum(float(a) * per_month[f] for a, f in subs) * 12
        qs.append(Question("What do my active subscriptions cost me a year?", [round(yearly, 2)], what="subscriptions"))
    return qs


# ── Grading ───────────────────────────────────────────────────────────────────

_MONEY = re.compile(r"£\s?(\d[\d,]*(?:\.\d+)?)\s*(k)?", re.IGNORECASE)


def money_in(text: str) -> list[float]:
    found = []
    for num, k in _MONEY.findall(text):
        value = float(num.replace(",", ""))
        found.append(value * 1000 if k else value)
    return found


def grade(q: Question, answer: str) -> list[str]:
    """What's missing from the answer; empty means it passed."""
    problems = []
    found = money_in(answer)
    for want in q.amounts:
        tolerance = max(0.51, abs(want) * 0.001)
        if not any(abs(abs(f) - abs(want)) <= tolerance for f in found):
            problems.append(f"no £{abs(want):,.2f} (found {', '.join(f'£{f:,.2f}' for f in found[:8]) or 'no amounts'})")
    for name in q.names:
        if name.lower() not in answer.lower():
            problems.append(f"doesn't name {name!r}")
    for n in q.counts:
        if not re.search(rf"(?<![\d.,£]){n}(?![\d.,])", answer):
            problems.append(f"no count of {n}")
    return problems


# ── Running ───────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--budget", type=float, default=0.60, help="hard ceiling in USD across both models (default 0.60, about 47p)")
    p.add_argument("--arms", nargs="*", choices=list(ARMS), help="only these models")
    p.add_argument("--yes", action="store_true", help="actually ask them (costs API credit)")
    args = p.parse_args()

    init_db(read_only=False)   # llm_calls is written through the main login; the agent reads through its own
    today = datetime.now(fa.LONDON).date()
    questions = build_questions(today)
    arms = {k: ARMS[k] for k in (args.arms or ARMS)}
    print(f"{len(questions)} questions, answers worked out from your data ({today}):")
    for i, q in enumerate(questions, 1):
        want = ", ".join([f"£{a:,.2f}" for a in q.amounts] + q.names + [str(c) for c in q.counts])
        print(f"  {i:2}. {q.text}\n      expect: {want}   ({q.what})")
    if not args.yes:
        print(f"\nNothing sent. Run again with --yes to ask {', '.join(arms)} (hard stop at ${args.budget:.2f}).")
        return

    client = anthropic.Anthropic(api_key=os.environ["CLAUDE_SECRET"])
    spent = 0.0
    results = {name: [] for name in arms}
    stopped = False
    for i, q in enumerate(questions, 1):
        if stopped:
            break
        for name, settings in arms.items():
            done = [r for r in results[name] if "cost" in r]
            guess = (sum(r["cost"] for r in done) / len(done)) if done else FIRST_GUESS_USD.get(settings["model"], 0.1)
            if spent + guess * SAFETY > args.budget:
                print(f"Budget: stopping before question {i} ({name} would need ~${guess * SAFETY:.3f}, "
                      f"${args.budget - spent:.3f} left).")
                stopped = True
                break
            agent = fa.FinanceAgent(client, settings=settings, job="agent_eval")
            t0 = time.monotonic()
            try:
                answer = agent.ask(q.text)
                problems = grade(q, answer.text)
                r = {"q": i, "passed": not problems, "problems": problems, "cost": answer.cost_usd,
                     "secs": round(time.monotonic() - t0, 1), "steps": len(answer.steps),
                     "tools": [s.tool for s in answer.steps], "answer": answer.text}
            except Exception as e:
                r = {"q": i, "passed": False, "problems": [f"failed: {type(e).__name__}: {e}"[:300]],
                     "cost": agent.cost_usd, "secs": round(time.monotonic() - t0, 1), "steps": 0, "answer": ""}
            spent += r["cost"]
            results[name].append(r)
            mark = "PASS" if r["passed"] else "FAIL"
            print(f"  {i:2}. {name:18} {mark}  ${r['cost']:.4f}  {r['steps']} step(s)  {r['secs']}s"
                  + ("" if r["passed"] else f"   <- {'; '.join(r['problems'])}"), flush=True)

    print(f"\n{'':20} {'right':>9} {'cost':>9} {'per q':>8} {'steps':>6} {'secs':>6}")
    for name, rs in results.items():
        if not rs:
            continue
        right = sum(r["passed"] for r in rs)
        cost = sum(r["cost"] for r in rs)
        print(f"{name:20} {right:>3}/{len(rs):<5} ${cost:8.4f} ${cost / len(rs):7.4f} "
              f"{sum(r['steps'] for r in rs) / len(rs):6.1f} {sum(r['secs'] for r in rs) / len(rs):6.1f}")
    print(f"\nSpent ${spent:.4f} of ${args.budget:.2f}.")
    out = log_path(f"agent_eval-{datetime.now():%Y%m%d-%H%M}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"today": str(today), "arms": arms,
                   "questions": [q.__dict__ for q in questions], "results": results}, f, indent=1, default=str)
    print(f"Every answer and why it passed or failed: {out}")


if __name__ == "__main__":
    main()
