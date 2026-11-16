"""Charts drawn as PNG images for Telegram: the budget bars in the budget
messages and summaries, spending by category, and the last few months.

matplotlib, without a display (Agg), because the worker has no screen and
doesn't carry the dashboard's plotly. Each function returns the image's bytes,
for alerts.send_telegram_photo."""
import io

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import FuncFormatter, PercentFormatter  # noqa: E402

GREEN, AMBER, RED = "#2e9d5b", "#e0a526", "#d64545"
TRACK, INK, MUTED, PACE = "#e9ecef", "#222222", "#6c757d", "#1f3a5f"
BAR_CAP = 2.0    # the budget bars' scale tops out at 200% of budget
WITHIN = "#5b7bb0"   # an over-budget bar fills up to the budget in this; only the overspend is red
WIDTH = 7.2      # inches, at DPI: wide enough for a phone screen held upright
DPI = 160


def _png(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=DPI, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return buf.getvalue()


def _money(v: float) -> str:
    return f"£{v:,.0f}" if abs(v) >= 100 else f"£{v:,.2f}"


def _colour(state: str) -> str:
    return {"over": RED, "near": AMBER, "pace": AMBER}.get(state, GREEN)




def budget_bars(mb, title: str | None = None) -> bytes | None:
    """One row per budgeted category, scaled to the budget: 100% is the budget
    line. Spending fills each bar to that line in its status colour (green,
    amber from 80% or when off pace); anything over the budget continues in
    red, so an overspend is visible in proportion. The scale grows to fit the
    biggest overspend, up to BAR_CAP (200% of budget); a bar past the cap is
    cut there and labelled with its true percentage. The dark marker is where
    on-pace spending would be now. None with no budgets."""
    statuses = mb.statuses
    if not statuses:
        return None
    rows = list(reversed(statuses))          # most used at the top
    height = 1.0 + 0.62 * len(rows)
    fig, ax = plt.subplots(figsize=(WIDTH, height))
    top = min(max(1.0, max(s.used for s in rows)) * 1.08, BAR_CAP)
    for i, s in enumerate(rows):
        ax.barh(i, 1.0, color=TRACK, height=0.62, zorder=1)
        ax.barh(i, min(s.used, 1.0), color=WITHIN if s.state == "over" else _colour(s.state), height=0.62, zorder=2)
        if s.used > 1.0:
            over = min(s.used, BAR_CAP) - 1.0
            ax.barh(i, over, left=1.0, color=RED, height=0.62, zorder=2)
            end = min(s.used, BAR_CAP)
            label = f"+{s.used - 1.0:.0%} over" + (" (cut off)" if s.used > BAR_CAP else "")
            ax.text(end + 0.02 * top, i, label, va="center", ha="left", fontsize=8.5, color=RED, fontweight="bold")
        if mb.current:
            ax.plot([mb.pace, mb.pace], [i - 0.38, i + 0.38], color=PACE, linewidth=1.6, zorder=3)
    ax.axvline(1.0, color=MUTED, linewidth=1.0, linestyle="--", zorder=0)
    ax.text(1.0, len(rows) - 0.35, "budget", ha="center", va="bottom", fontsize=8, color=MUTED)
    labels = [f"{s.category}\n{_money(s.spent)} of {_money(s.budget)} · {s.used:.0%}" for s in rows]
    ax.set_yticks(range(len(rows)), labels, fontsize=9)
    ticks = [t for t in (0, 0.5, 1.0, 1.5, 2.0) if t <= top + 1e-9]
    ax.set_xlim(0, top + 0.12 * top)
    ax.set_xticks(ticks, [f"{t:.0%}" for t in ticks])
    ax.tick_params(axis="x", labelsize=8, colors=MUTED)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    month = mb.month.strftime("%B %Y")
    sub = (f"Day {mb.day} of {mb.days} · the dark line is where on-pace spending would be ({mb.pace:.0%} of the month)"
           if mb.current else "The whole month")
    ax.set_title(f"{title or 'Budgets'}, {month}\n", loc="left", fontsize=12, fontweight="bold", color=INK)
    ax.text(0, 1.02, sub, transform=ax.transAxes, fontsize=8.5, color=MUTED)
    return _png(fig)


def spend_by_category(rows: list[dict], title: str, limit: int = 10) -> bytes | None:
    """Horizontal bars, biggest first; the smallest folded into 'Everything
    else'. rows: [{"category", "amount"}]."""
    rows = sorted((r for r in rows if r["amount"] > 0.005), key=lambda r: -r["amount"])
    if not rows:
        return None
    if len(rows) > limit:
        rest = rows[limit - 1:]
        rows = rows[:limit - 1] + [{"category": f"Everything else ({len(rest)})",
                                    "amount": sum(r["amount"] for r in rest)}]
    rows = list(reversed(rows))
    fig, ax = plt.subplots(figsize=(WIDTH, 1.0 + 0.42 * len(rows)))
    top = max(r["amount"] for r in rows)
    ax.barh(range(len(rows)), [r["amount"] for r in rows], color=PACE, height=0.62)
    for i, r in enumerate(rows):
        ax.text(r["amount"] + top * 0.01, i, _money(r["amount"]), va="center", fontsize=8.5, color=INK)
    ax.set_yticks(range(len(rows)), [r["category"] for r in rows], fontsize=9)
    ax.set_xlim(0, top * 1.18)
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"£{v:,.0f}"))
    ax.tick_params(axis="x", labelsize=8, colors=MUTED)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.set_title(title, loc="left", fontsize=12, fontweight="bold", color=INK)
    return _png(fig)


def months_trend(points: list[dict], title: str) -> bytes | None:
    """Spend per month as bars, with the budget as a line when there is one.
    points: [{"month": date, "spent", "budget"}]."""
    if not points or not any(p["spent"] for p in points):
        return None
    labels = [p["month"].strftime("%b") for p in points]
    fig, ax = plt.subplots(figsize=(WIDTH, 3.2))
    bars = ax.bar(labels, [p["spent"] for p in points], color=PACE, width=0.6)
    budget = [p.get("budget") or 0 for p in points]
    if any(budget):
        ax.plot(labels, budget, color=RED, linewidth=1.6, linestyle="--", label="Budget (today's)")
        ax.legend(frameon=False, fontsize=8, loc="upper left")
    for b, p in zip(bars, points):
        ax.text(b.get_x() + b.get_width() / 2, b.get_height(), _money(p["spent"]),
                ha="center", va="bottom", fontsize=8, color=INK)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"£{v:,.0f}"))
    ax.tick_params(labelsize=8, colors=MUTED)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.set_title(title, loc="left", fontsize=12, fontweight="bold", color=INK)
    return _png(fig)
