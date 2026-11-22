"""The split rules on their own: amounts typed in, and whether a set of lines is a valid split."""
import pytest

import splits
from splits import Line, SplitError


@pytest.mark.parametrize("text,pence", [("20", 2000), ("20.5", 2050), ("20.50", 2050), ("£20", 2000),
                                        ("  12.00 ", 1200), ("£ 7.25", 725), ("0.01", 1), ("20.000", 2000)])
def test_amounts_people_type_are_read(text, pence):
    assert splits.parse_amount(text) == pence


@pytest.mark.parametrize("text", ["", "   ", "abc", "£", "0", "-5", "1,000", "12.345", "20 quid"])
def test_amounts_that_are_not_amounts_are_refused(text):
    with pytest.raises(SplitError):
        splits.parse_amount(text)


def test_lines_that_add_up_are_a_valid_split():
    lines = [Line(1, 10, 2000), Line(2, 20, 1000), Line(3, None, 2000)]
    splits.check(-5000, lines)                      # the sign of the payment doesn't matter
    splits.check(5000, lines)


def test_lines_short_of_the_total_say_how_much_is_left():
    with pytest.raises(SplitError, match=r"£10\.00 is still to be given"):
        splits.check(-5000, [Line(1, 10, 2000), Line(2, 20, 2000)])


def test_lines_over_the_total_say_how_much_too_much():
    with pytest.raises(SplitError, match=r"£10\.00 more"):
        splits.check(-5000, [Line(1, 10, 2000), Line(2, 20, 3000), Line(3, None, 1000)])


def test_a_single_line_is_not_a_split():
    with pytest.raises(SplitError, match="at least 2 parts"):
        splits.check(-5000, [Line(1, 10, 5000)])


def test_a_zero_or_negative_part_is_refused():
    with pytest.raises(SplitError, match="above zero"):
        splits.check(-5000, [Line(1, 10, 5000), Line(2, None, 0)])
    with pytest.raises(SplitError, match="above zero"):
        splits.check(-5000, [Line(1, 10, 6000), Line(2, None, -1000)])


def test_a_zero_payment_cant_be_split():
    with pytest.raises(SplitError, match="£0"):
        splits.check(0, [Line(1, None, 1), Line(2, None, 1)])


def test_left_over_is_the_difference_either_way():
    assert splits.left_over(-5000, [Line(1, None, 2000)]) == 3000
    assert splits.left_over(-5000, [Line(1, None, 6000)]) == -1000


LOOKUP = {"Food & Drink › Groceries": (1, 10), "Food & Drink › Coffee Shops": (1, 11),
          "Personal Care & Consumables › Toiletries": (2, 20)}


def test_a_form_with_one_blank_amount_gives_that_part_the_rest():
    lines = splits.lines_from_rows([("Personal Care & Consumables › Toiletries", "20"),
                                    ("Food & Drink › Coffee Shops", "10"),
                                    ("Food & Drink › Groceries", "")], -5000, LOOKUP)
    assert [(l.parent_id, l.subcategory_id, l.pence) for l in lines] == [(2, 20, 2000), (1, 11, 1000), (1, 10, 2000)]


def test_blank_rows_are_ignored():
    lines = splits.lines_from_rows([("Food & Drink › Groceries", "30"), ("", ""),
                                    ("Food & Drink › Coffee Shops", "20")], -5000, LOOKUP)
    assert len(lines) == 2


def test_two_blank_amounts_are_refused():
    with pytest.raises(SplitError, match="Only one part can take the rest"):
        splits.lines_from_rows([("Food & Drink › Groceries", ""), ("Food & Drink › Coffee Shops", "")], -5000, LOOKUP)


def test_a_rest_that_is_nothing_is_refused():
    with pytest.raises(SplitError, match="Nothing is left"):
        splits.lines_from_rows([("Food & Drink › Groceries", "50"), ("Food & Drink › Coffee Shops", "")], -5000, LOOKUP)


def test_a_category_that_isnt_known_is_refused():
    with pytest.raises(SplitError, match="isn't a category"):
        splits.lines_from_rows([("Made up › Thing", "10"), ("Food & Drink › Groceries", "")], -5000, LOOKUP)


def test_no_categories_is_refused():
    with pytest.raises(SplitError, match="Pick a category"):
        splits.lines_from_rows([("", "10")], -5000, LOOKUP)
