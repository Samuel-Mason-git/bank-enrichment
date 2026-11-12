"""Compare models on your own labelled history before switching one.

Takes a random sample of transactions that already have their right category
(everything labelled and not since corrected), runs the classifier's real
prompts over them with each candidate model, and scores the answers:

  Pass 0 (match existing)  how many it matched, and how many matches were WRONG
                           -- a wrong match goes straight in, so that's the
                           number that matters most
  Pass 1 (parent)          right parent category
  Pass 2 (subcategory)     right subcategory, given the right parent
  Judge                    approves right placements, catches deliberately
                           wrong ones (moved to a different parent category)

Nothing is written to the database (it is opened read-only), and the calls
are not recorded in llm_calls, so they don't show on the dashboard's AI page.
Every call is paid, so by default this only estimates the cost:

    python src/pipeline/model_eval.py                 # sample size, estimated cost
    python src/pipeline/model_eval.py --yes           # run it (stops at --budget)

The detail -- every answer that disagreed with the label -- is written to
logs/model_eval-<time>.json.
"""
import argparse
import json
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

import anthropic

import llm_calls
import llm_labelling as ll
import placement_judge as pj
from database_functions import (
    _rows, init_db, get_parents, get_subcategories, get_subcategory_examples,
    get_subcategory_stats, get_merchant_history, log_path,
)

# The candidates. Sonnet 4.6 is what runs today; the 5.5 models think by
# default (billed as output), which classification doesn't need.
ARMS: dict[str, dict] = {
    "sonnet-4-6 (now)": {"model": "claude-sonnet-4-6"},
    "sonnet-5-5 low": {"model": "claude-sonnet-5-5", "effort": "low", "thinking": False},
    "haiku-5-5 low": {"model": "claude-haiku-5-5", "effort": "low", "thinking": False},
    "haiku-5-5 low +think": {"model": "claude-haiku-5-5", "effort": "low", "thinking": True},
}

# For the estimate only: characters per input token (conservative), and output
# tokens per transaction answered / per judge verdict / per call spent thinking.
CHARS_PER_TOKEN = 3.3
OUT_PER_TXN = 30
OUT_PER_VERDICT = 110
OUT_THINKING = 800


def _bucket(source: str) -> str:
    """Where a label came from: the classifier itself (Sonnet 4.6 -- a home
    advantage for it), migrated from the old system, or you / your rules."""
    return {"llm": "classifier", "migration": "migrated"}.get(source, "you/rules")


def _same(a, b) -> bool:
    return (a or "").strip().casefold() == (b or "").strip().casefold()


# ── Budget ────────────────────────────────────────────────────────────────────

class Budget:
    """A hard ceiling: a call is only sent if its estimate still fits beside
    what has been spent and what calls in flight may spend."""

    def __init__(self, cap: float):
        self.cap, self.spent, self.reserved = cap, 0.0, 0.0
        self.skipped = 0
        self._lock = threading.Lock()

    def reserve(self, est: float) -> bool:
        with self._lock:
            if self.spent + self.reserved + est > self.cap:
                self.skipped += 1
                return False
            self.reserved += est
            return True

    def settle(self, est: float, actual: float) -> None:
        with self._lock:
            self.reserved -= est
            self.spent += actual


# ── The sample and the prompts (built once, the same for every model) ─────────

def labelled_sample(n: int, rng: random.Random) -> list[dict]:
    rows = _rows(
        """SELECT t.*, l.source AS label_source
           FROM transactions t
           JOIN (SELECT DISTINCT ON (txn_id) txn_id, source FROM label_events
                 ORDER BY txn_id, event_id DESC) l ON l.txn_id = t.id
           WHERE t.llm_subcategory IS NOT NULL AND NOT t.skipped"""
    )
    live = {(s["parent_name"], s["name"]) for s in get_subcategories()}
    rows = [r for r in rows if (r["llm_category"], r["llm_subcategory"]) in live]
    rng.shuffle(rows)
    return rows[:n]


def build_tasks(sample: list[dict], judge_n: int, rng: random.Random) -> list[dict]:
    parents = get_parents()
    subs = get_subcategories()
    subs_stats = ll._with_stats(subs)
    all_parent_names = [p["name"] for p in parents]
    batches = [sample[i:i + ll.BATCH_SIZE] for i in range(0, len(sample), ll.BATCH_SIZE)]
    tasks = []

    for b in batches:
        tasks.append({"kind": "pass0", "txns": b, "prompt": ll._pass0_prompt(b, subs_stats)})
        tasks.append({"kind": "pass1", "txns": b, "prompt": ll._pass1_prompt(b, parents)})

    by_parent: dict[str, list[dict]] = {}
    for t in sample:
        by_parent.setdefault(t["llm_category"], []).append(t)
    for parent, txns in by_parent.items():
        for i in range(0, len(txns), ll.BATCH_SIZE):
            chunk = txns[i:i + ll.BATCH_SIZE]
            tasks.append({"kind": "pass2", "txns": chunk, "parent": parent,
                          "prompt": ll._pass2_prompt(chunk, parent, subs_stats, all_parent_names)})

    # Judge: half placed where they belong, half moved to a subcategory under
    # a different parent -- clearly wrong, which the judge must catch.
    stats = get_subcategory_stats()
    taxonomy = pj._taxonomy_block()
    for i, t in enumerate(sample[:judge_n]):
        right = i % 2 == 0
        if right:
            cat, sub = t["llm_category"], t["llm_subcategory"]
        else:
            others = [s for s in subs if s["parent_name"] != t["llm_category"]]
            pick = rng.choice(others)
            cat, sub = pick["parent_name"], pick["name"]
        key = pj.merchant_key(t)
        prompt = pj.build_prompt(
            ll._format_transaction(t), cat, sub,
            pj._profile_block(stats.get((cat, sub)), get_subcategory_examples(cat, sub)),
            pj._merchant_block(get_merchant_history(key, t["id"]) if key else []),
            taxonomy,
        )
        tasks.append({"kind": "judge", "txns": [t], "placed": (cat, sub), "right": right, "prompt": prompt})
    return tasks


def estimate(task: dict, settings: dict) -> float:
    out = OUT_PER_VERDICT if task["kind"] == "judge" else OUT_PER_TXN * len(task["txns"])
    if llm_calls.thinks(settings):
        out += OUT_THINKING
    tokens = {"input": int(len(task["prompt"]) / CHARS_PER_TOKEN), "cache_write_5m": 0,
              "cache_write_1h": 0, "cache_read": 0, "output": out}
    return llm_calls.cost(llm_calls.prices_for(settings["model"], date.today()), tokens) or 0.0


# ── Running ───────────────────────────────────────────────────────────────────

def run_task(client, settings: dict, task: dict, budget: Budget) -> dict:
    est = estimate(task, settings)
    if not budget.reserve(est):
        return {"skipped": True}
    max_tokens = pj.MAX_TOKENS if task["kind"] == "judge" else ll.MAX_TOKENS
    t0 = time.monotonic()
    try:
        r = client.messages.create(**llm_calls.request_kwargs(settings, max_tokens),
                                   messages=[{"role": "user", "content": task["prompt"]}])
    except anthropic.APIError as e:
        budget.settle(est, 0.0)
        return {"error": f"{type(e).__name__}: {e}"[:300]}
    tokens = llm_calls._tokens(r.usage)
    cost = llm_calls.cost(llm_calls.prices_for(r.model, date.today()), tokens) or 0.0
    budget.settle(est, cost)
    return {"text": llm_calls.response_text(r), "stop": r.stop_reason, "cost": cost,
            "secs": time.monotonic() - t0, "output_tokens": tokens["output"]}


def score(tasks: list[dict], results: list[dict]) -> tuple[dict, list[dict]]:
    s = {"cost": 0.0, "secs": [], "calls": 0, "failed": 0, "skipped": 0, "output_tokens": 0,
         "p0_n": 0, "p0_matched": 0, "p0_wrong": 0,
         "p1": {}, "p2": {},  # bucket -> [right, total]
         "judge_right_n": 0, "judge_right_ok": 0, "judge_wrong_n": 0, "judge_wrong_caught": 0}
    misses = []

    def tally(table, bucket, ok):
        for key in (bucket, "all"):
            row = table.setdefault(key, [0, 0])
            row[0] += ok
            row[1] += 1

    for task, res in zip(tasks, results):
        if res.get("skipped"):
            s["skipped"] += 1
            continue
        s["calls"] += 1
        if "error" in res or res.get("stop") in ("refusal", "max_tokens"):
            s["failed"] += 1
            misses.append({"kind": task["kind"], "failed": res.get("error") or res.get("stop")})
            continue
        s["cost"] += res["cost"]
        s["secs"].append(res["secs"])
        s["output_tokens"] += res["output_tokens"]
        try:
            answer = pj.parse_verdict(res["text"]) if task["kind"] == "judge" else ll._extract_json(res["text"])
        except (ValueError, json.JSONDecodeError):
            s["failed"] += 1
            misses.append({"kind": task["kind"], "failed": "unparseable", "text": res["text"][:300]})
            continue

        if task["kind"] == "judge":
            t = task["txns"][0]
            objected = pj.is_objection(answer, *task["placed"])
            if task["right"]:
                s["judge_right_n"] += 1
                s["judge_right_ok"] += not objected
            else:
                s["judge_wrong_n"] += 1
                s["judge_wrong_caught"] += objected
            if objected == task["right"]:
                misses.append({"kind": "judge", "txn": ll._format_transaction(t),
                               "placed": " > ".join(task["placed"]), "placement_was_right": task["right"],
                               "verdict": answer})
            continue

        by_id = {str(a.get("id")): a for a in answer if isinstance(a, dict)}
        for t in task["txns"]:
            a = by_id.get(str(t["id"]), {})
            bucket = _bucket(t["label_source"])
            truth = f'{t["llm_category"]} > {t["llm_subcategory"]}'
            if task["kind"] == "pass0":
                s["p0_n"] += 1
                if a.get("category") and a.get("subcategory"):
                    s["p0_matched"] += 1
                    if not (_same(a["category"], t["llm_category"]) and _same(a["subcategory"], t["llm_subcategory"])):
                        s["p0_wrong"] += 1
                        misses.append({"kind": "pass0 WRONG MATCH", "txn": ll._format_transaction(t), "truth": truth,
                                       "said": f'{a["category"]} > {a["subcategory"]}'})
            elif task["kind"] == "pass1":
                ok = _same(a.get("category"), t["llm_category"])
                tally(s["p1"], bucket, ok)
                if not ok:
                    misses.append({"kind": "pass1", "txn": ll._format_transaction(t), "truth": truth,
                                   "said": a.get("category")})
            else:
                ok = _same(a.get("subcategory"), t["llm_subcategory"])
                tally(s["p2"], bucket, ok)
                if not ok:
                    misses.append({"kind": "pass2", "txn": ll._format_transaction(t), "truth": truth,
                                   "said": a.get("subcategory")})
    return s, misses


def _pct(a, b) -> str:
    return f"{100 * a / b:5.1f}%" if b else "   —  "


def report(scores: dict[str, dict]) -> None:
    base = scores.get("sonnet-4-6 (now)", {}).get("cost")
    print()
    print(f"{'':22} {'cost':>8} {'vs now':>7} {'p50 s':>6} {'fail':>5} | {'P0 match':>8} {'P0 wrong':>8} "
          f"| {'P1 parent':>9} {'P2 sub':>7} | {'judge ok':>8} {'catches':>8}")
    for name, s in scores.items():
        secs = sorted(s["secs"])
        p50 = f"{secs[len(secs) // 2]:.1f}" if secs else "—"
        ratio = f"{s['cost'] / base:.2f}x" if base else "—"
        p1, p2 = s["p1"].get("all", [0, 0]), s["p2"].get("all", [0, 0])
        print(f"{name:22} ${s['cost']:7.4f} {ratio:>7} {p50:>6} {s['failed']:>5} | "
              f"{_pct(s['p0_matched'], s['p0_n']):>8} {_pct(s['p0_wrong'], s['p0_matched']):>8} | "
              f"{_pct(*p1):>9} {_pct(*p2):>7} | "
              f"{_pct(s['judge_right_ok'], s['judge_right_n']):>8} {_pct(s['judge_wrong_caught'], s['judge_wrong_n']):>8}"
              + (f"   ({s['skipped']} call(s) skipped: budget)" if s["skipped"] else ""))
    print("\nP0 wrong = share of Pass 0's confident matches that were wrong (lower is better).")
    print("judge ok = right placements approved; catches = wrong placements objected to.")
    print("\nPass 1 / Pass 2 accuracy by where the label came from "
          "(classifier = Sonnet 4.6's own labels, so it has a home advantage there):")
    for name, s in scores.items():
        parts = [f"{b}: P1 {_pct(*s['p1'][b]).strip()} P2 {_pct(*s['p2'].get(b, [0, 0])).strip()} (n={s['p1'][b][1]})"
                 for b in ("you/rules", "migrated", "classifier") if b in s["p1"]]
        print(f"  {name:22} " + "   ".join(parts))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--n", type=int, default=90, help="transactions in the sample (default 90)")
    p.add_argument("--judge", type=int, default=24, help="judge calls per model, half right / half wrong (default 24)")
    p.add_argument("--budget", type=float, default=1.00, help="hard ceiling in USD across all models (default 1.00)")
    p.add_argument("--arms", nargs="*", choices=list(ARMS), help="only these models")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--yes", action="store_true", help="actually run it (costs API credit)")
    args = p.parse_args()

    if not ll.CLAUDE_SECRET:
        raise SystemExit("CLAUDE_SECRET not set in config/.env")
    init_db(read_only=True)
    rng = random.Random(args.seed)
    sample = labelled_sample(args.n, rng)
    tasks = build_tasks(sample, args.judge, rng)
    arms = {k: ARMS[k] for k in (args.arms or ARMS)}

    sources: dict[str, int] = {}
    for t in sample:
        sources[_bucket(t["label_source"])] = sources.get(_bucket(t["label_source"]), 0) + 1
    print(f"Sample: {len(sample)} labelled transactions ({', '.join(f'{k} {v}' for k, v in sources.items())}); "
          f"{len(tasks)} calls per model")
    total = 0.0
    for name, settings in arms.items():
        est = sum(estimate(t, settings) for t in tasks)
        total += est
        print(f"  {name:22} ~${est:.3f}")
    print(f"  {'total':22} ~${total:.3f}   (hard stop at ${args.budget:.2f})")
    if not args.yes:
        print("\nNothing sent. Run again with --yes to do it.")
        return

    client = anthropic.Anthropic(api_key=ll.CLAUDE_SECRET)
    budget = Budget(args.budget)
    scores, detail = {}, {}
    for name, settings in arms.items():
        print(f"Running {name} ...", flush=True)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda t, st=settings: run_task(client, st, t, budget), tasks))
        scores[name], detail[name] = score(tasks, results)
        print(f"  ${scores[name]['cost']:.4f}, {scores[name]['failed']} failed", flush=True)

    report(scores)
    print(f"\nSpent ${budget.spent:.4f} of ${args.budget:.2f}.")
    out = log_path(f"model_eval-{datetime.now():%Y%m%d-%H%M}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"arms": arms, "scores": {k: {**v, "secs": None} for k, v in scores.items()},
                   "misses": detail}, f, indent=1, default=str)
    print(f"Every disagreement: {out}")


if __name__ == "__main__":
    main()
