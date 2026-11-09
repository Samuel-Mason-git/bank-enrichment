"""What every dashboard page draws with: money formatting, the coloured metric
cards, section headings, and a small query helper."""
import html
import pandas as pd
import streamlit as st
from database_functions import get_con


def _query(sql: str, params: list = []) -> list[dict]:
    """Run a query and return a list of dicts. get_con() each time, so a
    dropped connection is made again rather than failing every query."""
    result = get_con().execute(sql, params)
    cols = [c.name for c in result.description]
    return [dict(zip(cols, row)) for row in result.fetchall()]


def _fmt(v, is_pct=False):
    """£28,906.31 and −£4,710.64 (the minus before the £, as it's said),
    or 12.3% -- thousands separated so big figures read at a glance."""
    if v is None or pd.isna(v):
        return "—"
    if is_pct:
        return f"{v:.1f}%".replace("-", "−")
    return f"{'−' if v < 0 else ''}£{abs(v):,.2f}"


def _info_icon(tooltip: str) -> str:
    return (
        f'<span title="{html.escape(tooltip)}" '
        f'style="cursor:help;opacity:0.6;margin-left:5px;font-size:0.72rem;">ⓘ</span>'
    )


def _delta_badge(delta_value, suffix, direction: str) -> str:
    """A small colored pill for the vs-prev change -- a card within the
    card. direction picks which way is "good": "normal" = up is good
    (Income, Net, Savings Rate, Invested), "inverse" = up is bad (Spend --
    spending more is a bad thing, unlike everything else going up),
    "neutral" = no good/bad direction at all (Transferred/Excluded is just
    money moving, not a gain or a loss)."""
    if delta_value is None:
        return ""
    arrow = "▲" if delta_value > 0 else "▼" if delta_value < 0 else "■"
    text = f"{arrow} {abs(delta_value):.1f}{suffix} vs prev"
    if direction == "neutral":
        bg, fg = "rgba(149, 165, 166, 0.25)", "rgba(210, 214, 215, 0.95)"
    else:
        favorable = (delta_value >= 0) if direction == "normal" else (delta_value <= 0)
        bg, fg = (
            ("rgba(46, 204, 113, 0.25)", "rgba(150, 232, 181, 0.95)") if favorable
            else ("rgba(231, 76, 60, 0.25)", "rgba(243, 158, 148, 0.95)")
        )
    return (
        f'<span style="display:inline-block;background:{bg};'
        f'color:{fg};border-radius:999px;padding:2px 9px;font-size:0.7rem;font-weight:600;'
        f'white-space:nowrap;">{html.escape(text)}</span>'
    )


def _colour_range(values) -> list[float]:
    """A bar chart's colour scale, started a little below zero so its
    smallest bar is a pale tint of the colour, not white on a dark page."""
    top = float(values.max()) if len(values) else 1.0
    return [-0.25 * top, top]


def _accent(color: str) -> str:
    """A row's tint at full strength, for the card's edge."""
    return color.rsplit(",", 1)[0] + ", 0.85)"


def _card(label, value, color, tooltip=None, delta_html="", reserve_delta=False):
    """A colored div standing in for a metric card -- st.metric can't take
    a background color, and st.dataframe can't do a real per-cell hover,
    so this is plain HTML: a visible ⓘ icon with its own native `title`
    tooltip (hover, no JS) rather than a silent hover-anywhere-on-the-card,
    plus a faint tint and a solid edge in the row's color. reserve_delta
    keeps room for a vs-prev pill even when there isn't one, so a row's
    cards all come out the same height."""
    icon = _info_icon(tooltip) if tooltip else ""
    delta_row = (
        f'<div style="margin-top:8px;height:20px;">{delta_html}</div>'
        if delta_html or reserve_delta else ""
    )
    return (
        f'<div style="background:{color};border:1px solid rgba(255,255,255,0.06);'
        f'border-left:3px solid {_accent(color)};border-radius:10px;'
        f'padding:12px 16px;margin-bottom:12px;">'
        f'<div style="font-size:0.7rem;letter-spacing:0.05em;text-transform:uppercase;'
        f'color:rgba(255,255,255,0.6);font-weight:600;white-space:nowrap;overflow:hidden;'
        f'text-overflow:ellipsis;">{html.escape(label)}{icon}</div>'
        f'<div style="font-size:1.6rem;font-weight:700;margin-top:6px;line-height:1.2;'
        f'white-space:nowrap;overflow:hidden;text-overflow:ellipsis;'
        f'font-variant-numeric:tabular-nums;">{html.escape(value)}</div>'
        f'{delta_row}</div>'
    )


def _section(title: str, tooltip: str, subtitle: str) -> None:
    st.markdown(
        f'<div style="margin:6px 0 10px;"><span style="font-size:1rem;font-weight:700;">'
        f'{html.escape(title)}</span>{_info_icon(tooltip)}'
        f'<span style="margin-left:10px;font-size:0.8rem;color:rgba(255,255,255,0.5);">'
        f'{html.escape(subtitle)}</span></div>',
        unsafe_allow_html=True,
    )


def _card_rows(cards: list[tuple], per_row: int = 4, reserve_delta=False) -> None:
    """Cards in rows of per_row, so a long row never squeezes its numbers
    onto two lines, and the same position lines up from row to row."""
    for start in range(0, len(cards), per_row):
        cols = st.columns(per_row)
        for col, (label, value, color, tooltip, delta_html) in zip(cols, cards[start:start + per_row]):
            with col:
                st.markdown(_card(label, value, color, tooltip=tooltip, delta_html=delta_html,
                                  reserve_delta=reserve_delta), unsafe_allow_html=True)


row_colors = {
    "Income": "rgba(46, 204, 113, 0.14)",
    "Spend": "rgba(231, 76, 60, 0.14)",
    "Net": "rgba(52, 152, 219, 0.16)",
    "Savings Rate": "rgba(52, 152, 219, 0.16)",
    "Invested": "rgba(155, 89, 182, 0.14)",
    "Saved": "rgba(52, 219, 193, 0.14)",
    "Transferred / Excluded": "rgba(149, 165, 166, 0.14)",
}
