"""Every call to Claude goes through create() here, which makes the call and
records it in llm_calls: the prompt's version and the payments it covered
(the prompt itself isn't kept -- its wording is stored once per version, in
prompt_versions), tokens, how long it took, retries, what it cost at that
day's prices, and whether the reply was usable -- its text kept only when it
wasn't. The dashboard's AI page reads it.

Recording never gets in the way of a call: if the row can't be written, the
call's result is still returned (and the failure logged).

Retries are done here, not by the SDK (whose own retries are switched off for
these calls), so each attempt and why it failed is on record."""
import contextvars
import hashlib
import inspect
import json
import logging
import time
from datetime import date, datetime, timezone

import anthropic

from database_functions import _rows, get_con

log = logging.getLogger(__name__)

# ── Prices ────────────────────────────────────────────────────────────────────
#
# USD per million tokens, per model, each with the day it applies from (newest
# last). A call is costed at the prices in force on its day and the prices used
# are stored with it, so a later change never alters what an old call cost.
# Source: platform.claude.com/docs/en/about-claude/pricing (checked 2026-11-09).
# A model missing from here is still recorded, with cost_usd left NULL.

def _p(input, write_5m, write_1h, read, output, **extra) -> dict:
    return {"input": input, "cache_write_5m": write_5m, "cache_write_1h": write_1h,
            "cache_read": read, "output": output, **extra}


PRICES: dict[str, list[tuple[date, dict]]] = {
    "claude-sonnet-4-6": [(date(2026, 1, 1), _p(3, 3.75, 6, 0.30, 15))],
    "claude-sonnet-5":   [(date(2026, 1, 1), _p(2, 2.50, 4, 0.20, 10))],
    "claude-sonnet-5-5": [(date(2026, 1, 1), _p(2, 2.50, 4, 0.10, 10))],
    "claude-opus-4-6":   [(date(2026, 1, 1), _p(5, 6.25, 10, 0.50, 25))],
    "claude-opus-5-5":   [(date(2026, 1, 1), _p(4, 5, 8, 0.20, 20))],
    "claude-haiku-4-5":  [(date(2026, 1, 1), _p(1, 1.25, 2, 0.10, 5))],
    # Prompts over 100,000 tokens are charged at the higher rates.
    "claude-haiku-5-5":  [(date(2026, 1, 1), _p(0.10, 0.125, 0.20, 0.01, 0.50, long_over=100_000,
                                               long=_p(0.50, 0.625, 1, 0.05, 2.50)))],
}


def prices_for(model: str, on: date) -> dict | None:
    """The price list for a model on a day, with the day it applies from."""
    for since, prices in reversed(PRICES.get(model, [])):
        if since <= on:
            return {**prices, "since": since.isoformat()}
    return None


def cost(prices: dict | None, tokens: dict) -> float | None:
    """USD for one call's tokens ({input, cache_write_5m, cache_write_1h,
    cache_read, output}) at a price list from prices_for()."""
    if prices is None:
        return None
    prompt = tokens["input"] + tokens["cache_write_5m"] + tokens["cache_write_1h"] + tokens["cache_read"]
    if prices.get("long_over") and prompt > prices["long_over"]:
        prices = prices["long"]
    return sum(tokens[k] * prices[k] for k in ("input", "cache_write_5m", "cache_write_1h",
                                                "cache_read", "output")) / 1_000_000


# ── Model settings ────────────────────────────────────────────────────────────
#
# A job's model is a small dict: {"model": ..., "effort": ..., "thinking": ...}.
# effort (low/medium/high) and thinking (True/False) are optional; left out,
# the model's own default applies. On the 5.5 models thinking is ON by default
# and billed as output, so a classification job usually wants it off.

# How each model spells "thinking off". Opus 5.5 can't switch it off at all.
_THINKING_OFF = {
    "claude-haiku-5-5": {"type": "disabled"},
    "claude-sonnet-5-5": {"type": "between_tools"},
    "claude-sonnet-5": {"type": "disabled"},
    "claude-opus-5": {"type": "disabled"},
}
# Room for thinking on top of the answer, when thinking is on. Output is billed
# per token generated, so the ceiling itself costs nothing.
THINKING_MAX_TOKENS = 16_000


def thinks(settings: dict) -> bool:
    """Whether a call with these settings may think: on when asked for, and on
    by default for every model that has a "thinking off" switch above."""
    if settings.get("thinking") is not None:
        return bool(settings["thinking"])
    return settings["model"] in _THINKING_OFF or settings["model"] == "claude-opus-5-5"


def request_kwargs(settings: dict, max_tokens: int) -> dict:
    """create() arguments for a job's model settings."""
    kwargs = {"model": settings["model"], "max_tokens": max_tokens}
    if settings.get("effort"):
        kwargs["output_config"] = {"effort": settings["effort"]}
    thinking = settings.get("thinking")
    if thinking is False:
        if settings["model"] not in _THINKING_OFF:
            raise ValueError(f"{settings['model']} can't have thinking switched off this way")
        kwargs["thinking"] = _THINKING_OFF[settings["model"]]
    elif thinking is True:
        kwargs["thinking"] = {"type": "adaptive"}
    if thinks(settings):
        kwargs["max_tokens"] = max(max_tokens, THINKING_MAX_TOKENS)
    return kwargs


def response_text(response) -> str:
    """The reply's text. On a model that thinks, the first block can be a
    thinking block rather than the answer, so content[0].text isn't safe."""
    blocks = getattr(response, "content", None) or []
    def is_text(b):
        kind = getattr(b, "type", None)
        return not isinstance(kind, str) or kind == "text"   # untyped: a test double
    return "".join(_str(getattr(b, "text", None)) or "" for b in blocks if is_text(b)).strip()


# ── Estimating ────────────────────────────────────────────────────────────────

# How many recent calls an estimate is drawn from, and how many on the job's
# own model it needs before their recorded cost is trusted over re-pricing.
ESTIMATE_FROM = 50
OWN_CALLS_NEEDED = 5


def cost_per_call(job: str, settings: dict) -> tuple[float, str] | None:
    """What one call of a job is likely to cost on the model it's set to now,
    from the calls already recorded, with a note of how that was worked out:
    the average recorded cost of that model's recent calls of the job when
    there are enough of them; otherwise the job's recent average tokens on any
    model, priced at this model's rates. None when the job has no history (or
    the model has no price)."""
    model = settings["model"]
    con = get_con()
    n, avg = con.execute(
        """SELECT COUNT(*), AVG(cost_usd) FROM (
               SELECT cost_usd FROM llm_calls
               WHERE job = %s AND COALESCE(response_model, model) = %s
                 AND error IS NULL AND cost_usd IS NOT NULL
               ORDER BY started_at DESC LIMIT %s) c""",
        [job, model, ESTIMATE_FROM],
    ).fetchone()
    if n >= OWN_CALLS_NEEDED:
        return float(avg), f"the average of the last {n} {job} call(s) on {model}"

    row = con.execute(
        """SELECT COUNT(*), AVG(input_tokens), AVG(cache_write_5m_tokens), AVG(cache_write_1h_tokens),
                  AVG(cache_read_tokens), AVG(output_tokens) FROM (
               SELECT * FROM llm_calls WHERE job = %s AND error IS NULL
               ORDER BY started_at DESC LIMIT %s) c""",
        [job, ESTIMATE_FROM],
    ).fetchone()
    prices = prices_for(model, date.today())
    if not row[0] or prices is None:
        return None
    tokens = dict(zip(("input", "cache_write_5m", "cache_write_1h", "cache_read", "output"),
                      (float(v or 0) for v in row[1:])))
    note = f"the token counts of the last {row[0]} {job} call(s), priced for {model}"
    if thinks(settings):
        note += " -- thinking may add output on top"
    return cost(prices, tokens), note


# ── What a call is about ──────────────────────────────────────────────────────

current_run_id: int | None = None   # set by process.main for the length of a run

_last_call: contextvars.ContextVar[int | None] = contextvars.ContextVar("last_llm_call", default=None)
_versions: dict = {}   # prompt_fn -> (version, source)
_stored: set = set()   # versions already in prompt_versions, this process


def _version_and_source(prompt_fn) -> tuple[str, str | None]:
    if prompt_fn not in _versions:
        try:
            source = inspect.getsource(prompt_fn)
            digest = hashlib.sha256(source.encode()).hexdigest()[:10]
        except (OSError, TypeError):
            source, digest = None, "unknown"
        _versions[prompt_fn] = (f"{prompt_fn.__module__}.{prompt_fn.__name__}@{digest}", source)
    return _versions[prompt_fn]


def prompt_version(prompt_fn) -> str | None:
    """'llm_labelling._pass1_prompt@3f9a1c2b0e': the function that builds the
    prompt and a fingerprint of its source, so any edit to its wording is a
    new version without anyone having to remember to bump one."""
    return _version_and_source(prompt_fn)[0] if prompt_fn is not None else None


def _store_version(prompt_fn) -> None:
    """The prompt's wording, once per version (prompt_versions)."""
    version, source = _version_and_source(prompt_fn)
    if version in _stored or source is None:
        return
    get_con().execute(
        "INSERT INTO prompt_versions (prompt_version, source) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        [version, source],
    )
    _stored.add(version)


def _fingerprint(kwargs) -> tuple[str, int]:
    """(sha256, characters) of the prompt as sent: system + messages."""
    text = json.dumps(_jsonable({k: kwargs[k] for k in ("system", "messages") if k in kwargs}), sort_keys=True)
    return hashlib.sha256(text.encode()).hexdigest()[:16], len(text)


# ── The call ──────────────────────────────────────────────────────────────────

MAX_ATTEMPTS = 3
_sleep = time.sleep  # a name of its own, so tests can skip the waits


def _retryable(e: Exception) -> bool:
    if isinstance(e, (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.APITimeoutError)):
        return True
    return isinstance(e, anthropic.APIStatusError) and e.status_code >= 500


def _int(value) -> int:
    return value if isinstance(value, int) else 0


def _str(value) -> str | None:
    return value if isinstance(value, str) else None


def _tokens(usage) -> dict:
    get = lambda k: _int(getattr(usage, k, None))  # noqa: E731
    written = get("cache_creation_input_tokens")
    one_hour = _int(getattr(getattr(usage, "cache_creation", None), "ephemeral_1h_input_tokens", None))
    return {"input": get("input_tokens"), "cache_write_5m": max(written - one_hour, 0), "cache_write_1h": one_hour,
            "cache_read": get("cache_read_input_tokens"), "output": get("output_tokens")}


def _to_plain(o):
    """An SDK object as a dict, anything else as its text -- never recursing
    into something whose to_dict() doesn't give a plain dict (a test double)."""
    to_dict = getattr(o, "to_dict", None)
    plain = to_dict() if callable(to_dict) else None
    return plain if isinstance(plain, (dict, list)) else str(o)


def _jsonable(value):
    """As JSON-safe plain data: SDK objects become dicts."""
    return json.loads(json.dumps(value, default=_to_plain))


def create(client, *, job: str, prompt_fn=None, txn_ids=(), **kwargs):
    """client.messages.create(**kwargs), recorded. job names the call
    (classify_parents, placement_judge, ...); prompt_fn is the function that
    built the prompt (for prompt_version); txn_ids the payments it's about.
    Raises what the API raised once the attempts are used up -- recorded too."""
    started = datetime.now(timezone.utc)
    t0 = time.monotonic()
    retry_errors: list[str] = []
    response, error = None, None
    # The SDK's own retries off, so each attempt is ours to count (a real
    # client only: anything else, like a test double, is called as it is).
    caller = client.with_options(max_retries=0) if isinstance(client, anthropic.Anthropic) else client
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = caller.messages.create(**kwargs)
            break
        except Exception as e:
            if attempt < MAX_ATTEMPTS and _retryable(e):
                retry_errors.append(f"{type(e).__name__}: {e}"[:500])
                _sleep(2 ** attempt)
                continue
            error = e
            break
    duration_ms = int((time.monotonic() - t0) * 1000)
    _record(job, prompt_fn, txn_ids, kwargs, started, duration_ms, retry_errors, response, error)
    if error is not None:
        raise error
    return response


def _record(job, prompt_fn, txn_ids, kwargs, started, duration_ms, retry_errors, response, error):
    _last_call.set(None)
    try:
        # The model has a column of its own; params holds only the other settings.
        params = {k: v for k, v in kwargs.items() if k not in ("messages", "system", "model")}
        # Tool definitions are kilobytes repeated on every call: their names say which.
        if isinstance(params.get("tools"), list):
            params["tools"] = [t.get("name") if isinstance(t, dict) else str(t) for t in params["tools"]]
        if prompt_fn is not None:
            _store_version(prompt_fn)
        sha, chars = _fingerprint(kwargs)
        row = {
            "started_at": started, "duration_ms": duration_ms, "run_id": current_run_id, "job": job,
            "prompt_version": prompt_version(prompt_fn), "provider": "anthropic", "model": kwargs.get("model", "?"),
            "params": json.dumps(_jsonable(params)), "prompt_sha256": sha, "prompt_chars": chars,
            "txn_ids": json.dumps([str(t) for t in txn_ids]),
            "retry_errors": json.dumps(retry_errors) if retry_errors else None,
        }
        if error is not None:
            row.update(error=f"{type(error).__name__}: {error}"[:4000])
        else:
            tokens = _tokens(getattr(response, "usage", None))
            answered_by = _str(getattr(response, "model", None)) or row["model"]
            prices = prices_for(answered_by, started.date())
            blocks = getattr(response, "content", None)
            blocks = blocks if isinstance(blocks, list) else []
            row.update(
                # Only when a different model answered (a fallback).
                response_model=answered_by if answered_by != row["model"] else None,
                response_text="".join(_str(getattr(b, "text", None)) or "" for b in blocks
                                      if getattr(b, "type", None) == "text"),
                thinking="".join(_str(getattr(b, "thinking", None)) or "" for b in blocks
                                 if getattr(b, "type", None) == "thinking") or None,
                stop_reason=_str(getattr(response, "stop_reason", None)),
                input_tokens=tokens["input"], cache_write_5m_tokens=tokens["cache_write_5m"],
                cache_write_1h_tokens=tokens["cache_write_1h"], cache_read_tokens=tokens["cache_read"],
                output_tokens=tokens["output"],
                cost_usd=cost(prices, tokens),
                request_id=_str(getattr(response, "_request_id", None)),
            )
            if prices is None:
                log.warning(f"No price for {answered_by}: its calls are recorded without a cost "
                            "(add it to PRICES in llm_calls.py)")
        columns = list(row)
        call_id = get_con().execute(
            f"INSERT INTO llm_calls ({', '.join(columns)}) VALUES ({', '.join('%s' for _ in columns)}) "
            "RETURNING call_id", [row[c] for c in columns],
        ).fetchone()[0]
        if error is None:
            _last_call.set(call_id)
    except Exception as e:
        log.warning(f"Couldn't record a call to Claude ({job}): {e}")


def parsed(ok: bool, answered: int | None = None, expected: int | None = None) -> None:
    """Whether the reply to the last call (on this thread) could be used, and
    how many of the items it was asked about it answered for."""
    call_id = _last_call.get()
    if call_id is None:
        return
    try:
        # A usable reply's text isn't kept: what it said is in the history it
        # wrote (label_events, judge_reviews, the proposals). Only a reply that
        # couldn't be used keeps its text, to see what went wrong.
        get_con().execute(
            """UPDATE llm_calls SET parsed = %(ok)s, answered = %(answered)s, expected = %(expected)s,
                   response_text = CASE WHEN %(ok)s THEN NULL ELSE response_text END,
                   thinking = CASE WHEN %(ok)s THEN NULL ELSE thinking END
               WHERE call_id = %(id)s""",
            {"ok": ok, "answered": answered, "expected": expected, "id": call_id})
    except Exception as e:
        log.warning(f"Couldn't record whether call {call_id}'s reply was usable: {e}")


# ── Reading (the dashboard's AI page) ─────────────────────────────────────────
#
# Times are London's. The call list leaves out the bulky text; get_call()
# fetches one call whole.


# What's not stored but follows from what is: the model that answered (the
# one asked unless a fallback answered), ok/error, and how many attempts.
_CALL_COLUMNS = """call_id, started_at AT TIME ZONE 'Europe/London' AS started_at, duration_ms, run_id, job,
                   provider, prompt_version, model, COALESCE(response_model, model) AS response_model,
                   stop_reason, input_tokens, cache_write_5m_tokens, cache_write_1h_tokens, cache_read_tokens,
                   output_tokens, cost_usd, CASE WHEN error IS NULL THEN 'ok' ELSE 'error' END AS status, error,
                   COALESCE(jsonb_array_length(retry_errors), 0) + 1 AS attempts,
                   parsed, answered, expected, txn_ids, request_id"""


def get_calls(since=None, until=None) -> list[dict]:
    """Calls between two London dates (inclusive; either may be None), newest first."""
    return _rows(
        f"""SELECT {_CALL_COLUMNS} FROM llm_calls
            WHERE (%(since)s::date IS NULL OR (started_at AT TIME ZONE 'Europe/London')::date >= %(since)s)
              AND (%(until)s::date IS NULL OR (started_at AT TIME ZONE 'Europe/London')::date <= %(until)s)
            ORDER BY started_at DESC""",
        {"since": since, "until": until},
    )


def get_call(call_id: int) -> dict | None:
    rows = _rows(
        f"""SELECT {_CALL_COLUMNS}, params, prompt_sha256, prompt_chars, response_text, thinking,
                   retry_errors,
                   (SELECT source FROM prompt_versions v WHERE v.prompt_version = llm_calls.prompt_version)
                       AS prompt_source
            FROM llm_calls WHERE call_id = %s""",
        [call_id],
    )
    return rows[0] if rows else None


def label_sources() -> list[dict]:
    """Who gave each payment its current category: the classifier, a rule, a
    quick tap, you, the judge, a proposal."""
    return _rows(
        """SELECT source, COUNT(*) AS payments FROM (
               SELECT DISTINCT ON (txn_id) txn_id, source, parent_id FROM label_events
               ORDER BY txn_id, event_id DESC) l
           WHERE parent_id IS NOT NULL GROUP BY source ORDER BY payments DESC"""
    )


def classifier_corrections() -> list[dict]:
    """Per model: payments the classifier labelled, and how many of them you
    (or a quick tap, or a new-category pick) later put somewhere else -- the
    classifier's real error rate, as you've judged it."""
    return _rows(
        """WITH first_llm AS (
               SELECT DISTINCT ON (txn_id) txn_id, model, subcategory_id, event_id
               FROM label_events WHERE source = 'llm' AND subcategory_id IS NOT NULL
               ORDER BY txn_id, event_id),
           latest AS (
               SELECT DISTINCT ON (txn_id) txn_id, source, subcategory_id, event_id
               FROM label_events ORDER BY txn_id, event_id DESC)
           SELECT f.model, COUNT(*) AS labelled,
                  COUNT(*) FILTER (WHERE l.event_id > f.event_id
                                     AND l.source IN ('manual', 'quick_tap', 'proposal')
                                     AND l.subcategory_id IS DISTINCT FROM f.subcategory_id) AS corrected
           FROM first_llm f JOIN latest l USING (txn_id)
           GROUP BY f.model ORDER BY labelled DESC"""
    )


def judge_outcomes() -> list[dict]:
    """The placement judge's reviews by outcome: approved (it agreed), held
    (it objected; a card is waiting), changed (you took its suggestion -- a
    real catch), kept (you kept the original -- a false alarm)."""
    return _rows("SELECT outcome, COUNT(*) AS reviews FROM judge_reviews GROUP BY outcome ORDER BY reviews DESC")


def judge_catches(limit: int = 30) -> list[dict]:
    """The judge's objections, newest first: what the payment was going to be,
    what happened, and where it is now."""
    return _rows(
        """SELECT j.reviewed_at AT TIME ZONE 'Europe/London' AS reviewed_at, j.txn_id, j.outcome,
                  j.category || ' › ' || j.subcategory AS was_going_to_be,
                  t.llm_category || COALESCE(' › ' || t.llm_subcategory, '') AS now_is,
                  COALESCE(NULLIF(t.merchant_name, ''), NULLIF(t.counterparty_name, ''), t.description) AS payee,
                  t.amount
           FROM judge_reviews j JOIN transactions t ON t.id = j.txn_id
           WHERE j.outcome <> 'approved'
           ORDER BY j.reviewed_at DESC LIMIT %s""",
        [limit],
    )


def card_outcomes() -> dict[str, list[dict]]:
    """How the cards Claude's suggestions became were answered, by kind, with
    the average hours from sending to your answer."""
    out = {}
    for kind, table in (("New-category cards", "category_proposals"), ("Rule suggestions", "rule_suggestions"),
                        ("Taxonomy review", "taxonomy_proposals")):
        out[kind] = _rows(
            f"""SELECT status, COUNT(*) AS cards,
                       ROUND(AVG(EXTRACT(EPOCH FROM decided_at - sent_at) / 3600)::NUMERIC, 1) AS avg_hours_to_answer
                FROM {table} GROUP BY status ORDER BY cards DESC"""
        )
    return out


def calls_for_payment(txn_id: str) -> list[dict]:
    """Every call that was about one payment, oldest first."""
    return _rows(
        f"""SELECT {_CALL_COLUMNS} FROM llm_calls WHERE txn_ids ? %s ORDER BY started_at""",
        [txn_id],
    )
