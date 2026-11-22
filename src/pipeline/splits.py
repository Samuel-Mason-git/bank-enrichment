"""Splitting one payment across several categories: the rules, with no database and no Telegram.

A split is a list of lines. Each line is a category (a parent, and optionally a subcategory) and an
amount in pence, always positive; the payment's own sign is applied when the totals are worked out.
The lines of a split must add up to the payment's amount, to the penny. A split has at least two
lines: one line would just be a relabel, which the normal label already does.

Telegram and the dashboard both use these checks, so a split is judged the same way whichever
one it's entered in.
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

MIN_LINES = 2


@dataclass(frozen=True)
class Line:
    parent_id: int
    subcategory_id: int | None
    pence: int

    def as_json(self) -> dict:
        return {"parent_id": self.parent_id, "subcategory_id": self.subcategory_id, "pence": self.pence}


class SplitError(ValueError):
    """A split that doesn't add up or isn't complete. The message is for the user."""


def parse_amount(text: str) -> int:
    """A typed amount in pounds, as pence. Accepts "20", "20.5", "20.50", "£20", " 20.00 ".
    Rejects anything else, including zero, negatives, more than two decimal places and commas.
    Raises SplitError with the message to show."""
    cleaned = (text or "").strip()
    if cleaned.startswith("£"):
        cleaned = cleaned[1:].strip()
    if not cleaned:
        raise SplitError("Type an amount, e.g. 20 or 12.50.")
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        raise SplitError(f"'{text.strip()}' isn't an amount. Type a number, e.g. 20 or 12.50.")
    if value != value.quantize(Decimal("0.01")):
        raise SplitError("Use at most two decimal places, e.g. 12.50.")
    if value <= 0:
        raise SplitError("The amount has to be more than zero.")
    return int((value * 100).to_integral_value())


def left_over(total_pence: int, lines: list[Line]) -> int:
    """What's still to be given to a category. Zero when the lines add up."""
    return abs(total_pence) - sum(line.pence for line in lines)


def check(total_pence: int, lines: list[Line]) -> None:
    """Raise SplitError unless the lines are a valid split of this payment."""
    if len(lines) < MIN_LINES:
        raise SplitError(f"A split needs at least {MIN_LINES} parts. For one category, just set the category.")
    if any(line.pence <= 0 for line in lines):
        raise SplitError("Every part needs an amount above zero.")
    if total_pence == 0:
        raise SplitError("A payment of £0 can't be split.")
    remaining = left_over(total_pence, lines)
    if remaining > 0:
        raise SplitError(f"£{remaining / 100:,.2f} is still to be given to a category.")
    if remaining < 0:
        raise SplitError(f"The parts add up to £{-remaining / 100:,.2f} more than the payment.")
