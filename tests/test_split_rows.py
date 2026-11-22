"""The dashboard's parts table: a split payment becomes one row per line, and the rest stay as they are."""
import pandas as pd

from dashboard_helpers import expand_split_rows


def _payments():
    return pd.DataFrame([
        {"id": "tesco", "amount": -50.0, "llm_category": "Food & Drink", "llm_subcategory": "Groceries",
         "role": "spend", "merchant_name": "Tesco"},
        {"id": "rent", "amount": -950.0, "llm_category": "Bills & Utilities", "llm_subcategory": "Rent",
         "role": "spend", "merchant_name": "Landlord"},
    ])


def test_an_unsplit_payment_keeps_its_one_row():
    rows = expand_split_rows(_payments(), pd.DataFrame(columns=["id", "line_no", "amount", "llm_category",
                                                                "llm_subcategory", "role"]))
    assert len(rows) == 2
    assert set(rows["line_no"]) == {0}


def test_a_split_payment_becomes_one_row_per_line():
    parts = pd.DataFrame([
        {"id": "tesco", "line_no": 1, "amount": -20.0, "llm_category": "Personal Care & Consumables",
         "llm_subcategory": "Toiletries", "role": "spend"},
        {"id": "tesco", "line_no": 2, "amount": -10.0, "llm_category": "Food & Drink",
         "llm_subcategory": "Coffee Shops", "role": "spend"},
        {"id": "tesco", "line_no": 3, "amount": -20.0, "llm_category": "Food & Drink",
         "llm_subcategory": "Groceries", "role": "spend"},
    ])
    rows = expand_split_rows(_payments(), parts)
    tesco = rows[rows["id"] == "tesco"].sort_values("line_no")
    assert list(tesco["amount"]) == [-20.0, -10.0, -20.0]
    assert list(tesco["llm_subcategory"]) == ["Toiletries", "Coffee Shops", "Groceries"]
    assert tesco["amount"].sum() == -50.0
    assert len(rows[rows["id"] == "rent"]) == 1


def test_the_payment_details_come_along_to_every_line():
    parts = pd.DataFrame([
        {"id": "tesco", "line_no": 1, "amount": -25.0, "llm_category": "Food & Drink",
         "llm_subcategory": "Groceries", "role": "spend"},
        {"id": "tesco", "line_no": 2, "amount": -25.0, "llm_category": "Food & Drink",
         "llm_subcategory": "Coffee Shops", "role": "spend"},
    ])
    rows = expand_split_rows(_payments(), parts)
    assert set(rows[rows["id"] == "tesco"]["merchant_name"]) == {"Tesco"}
