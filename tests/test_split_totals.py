"""A split payment counts by its lines: in the totals, in the category breakdown and against budgets.
An unsplit payment reads exactly as it did."""
import database_functions as dbf
from tests.local_helpers import ensure_category, insert_transaction, label
from splits import Line


def _spend(db, tid, pounds, category, sub=None, created="2026-08-10 12:00:00"):
    parent_id, sub_id = ensure_category(db, category, sub)
    insert_transaction(db, tid, amount=-pounds, description="Tesco", merchant_name="Tesco",
                       created_at=created)
    label(db, tid, category, sub)
    return parent_id, sub_id


def _split(db, tid, lines):
    dbf.set_split(tid, [{"parent_id": p, "subcategory_id": s, "pence": pence} for p, s, pence in lines],
                  source="dashboard")


def test_a_split_payment_counts_by_its_lines_in_the_category_totals(db):
    groceries, groc_sub = _spend(db, "tesco", 50.0, "Food & Drink", "Groceries")
    toiletries_parent, toiletries = ensure_category(db, "Personal Care & Consumables", "Toiletries")
    coffee_parent, coffee = ensure_category(db, "Food & Drink", "Coffee Shops")
    _split(db, "tesco", [(toiletries_parent, toiletries, 2000), (coffee_parent, coffee, 1000),
                         (groceries, groc_sub, 2000)])

    by_cat = {r["category"]: float(r["total"]) for r in
              dbf.get_category_totals_by_role("spend", "2026-08-01", "2026-08-31")}
    # Category totals keep their sign: spend is negative, as the dashboard has always had it.
    assert by_cat == {"Personal Care & Consumables": -20.0, "Food & Drink": -30.0}
    assert sum(by_cat.values()) == -50.0


def test_unsplit_payments_total_as_before(db):
    _spend(db, "a", 12.5, "Food & Drink", "Groceries")
    totals = dbf.get_totals_by_role("2026-08-01", "2026-08-31")
    assert totals["spend"] == 12.5


def test_a_split_refund_keeps_its_lines_as_money_back(db):
    parent, sub = ensure_category(db, "Shopping", "Clothing")
    other_parent, other = ensure_category(db, "Shopping", "Gifts")
    insert_transaction(db, "ref", amount=30.0, description="Refund", created_at="2026-08-12 12:00:00")
    label(db, "ref", "Shopping", "Clothing")
    dbf.set_split("ref", [{"parent_id": parent, "subcategory_id": sub, "pence": 1000},
                          {"parent_id": other_parent, "subcategory_id": other, "pence": 2000}], source="dashboard")
    lines = db.execute("SELECT amount FROM transaction_lines WHERE txn_id = 'ref' ORDER BY line_no").fetchall()
    assert [float(r[0]) for r in lines] == [10.0, 20.0]


def test_clearing_a_split_puts_the_payment_back_as_one(db):
    groceries, groc_sub = _spend(db, "tesco", 50.0, "Food & Drink", "Groceries")
    coffee_parent, coffee = ensure_category(db, "Food & Drink", "Coffee Shops")
    _split(db, "tesco", [(groceries, groc_sub, 4000), (coffee_parent, coffee, 1000)])
    assert len(dbf.get_split("tesco")) == 2

    dbf.set_split("tesco", [], source="dashboard")
    assert dbf.get_split("tesco") == []
    by_cat = {r["category"]: float(r["total"]) for r in
              dbf.get_category_totals_by_role("spend", "2026-08-01", "2026-08-31")}
    assert by_cat == {"Food & Drink": -50.0}


def test_a_split_that_does_not_add_up_is_refused_and_not_stored(db):
    groceries, groc_sub = _spend(db, "tesco", 50.0, "Food & Drink", "Groceries")
    coffee_parent, coffee = ensure_category(db, "Food & Drink", "Coffee Shops")
    try:
        _split(db, "tesco", [(groceries, groc_sub, 4000), (coffee_parent, coffee, 500)])
    except ValueError:
        pass
    else:
        raise AssertionError("a split that doesn't add up was accepted")
    assert dbf.get_split("tesco") == []


def test_a_split_keeps_its_history(db):
    groceries, groc_sub = _spend(db, "tesco", 50.0, "Food & Drink", "Groceries")
    coffee_parent, coffee = ensure_category(db, "Food & Drink", "Coffee Shops")
    _split(db, "tesco", [(groceries, groc_sub, 4000), (coffee_parent, coffee, 1000)])
    _split(db, "tesco", [(groceries, groc_sub, 3000), (coffee_parent, coffee, 2000)])
    assert [l["pence"] for l in dbf.get_split("tesco")] == [3000, 2000]
    count = db.execute("SELECT count(*) FROM split_events WHERE txn_id = 'tesco'").fetchone()[0]
    assert count == 2


def test_budgets_see_the_split_lines(db):
    from datetime import date
    import budgets
    groceries, groc_sub = _spend(db, "tesco", 50.0, "Food & Drink", "Groceries", created="2026-08-10 12:00:00")
    coffee_parent, coffee = ensure_category(db, "Food & Drink", "Coffee Shops")
    _split(db, "tesco", [(groceries, groc_sub, 4000), (coffee_parent, coffee, 1000)])
    assert budgets.spent_by_category(date(2026, 8, 1), date(2026, 8, 31)) == {"Food & Drink": 50.0}
