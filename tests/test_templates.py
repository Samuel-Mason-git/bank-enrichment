from pathlib import Path

import jinja2
import pytest

_TEMPLATES_DIR = Path(__file__).parent.parent / "src" / "server_scripts" / "templates"


@pytest.fixture
def env():
    e = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=jinja2.select_autoescape(),
    )
    e.globals["url_for"] = lambda *a, **k: "#"
    return e


def _nav_links(html: str) -> list[str]:
    return [line.strip() for line in html.splitlines() if 'class="nav-link' in line]


def _dashboard_context(**overrides):
    base = dict(
        total_received=0, total_amount="£0.00", requests_sent=0, total_enriched=0,
        total_processed=0, queue_stats=[], total_queue=0, page=1, total_pages=1, queue=[],
    )
    base.update(overrides)
    return base


def _queue_row(**overrides):
    base = {
        "id": "tx_001", "amount": "-£1.00", "is_debit": True, "received_at": "now",
        "status": "pending", "request_count": 1, "skipped": False,
    }
    base.update(overrides)
    return base


def _rule(**overrides):
    base = {
        "id": 1, "name": "Test rule", "match_field": "merchant_name", "match_type": "contains",
        "match_value": "Tesco", "match_field_2": None, "match_type_2": None, "match_value_2": None,
        "auto_context": "Groceries", "auto_skip": False, "enabled": True,
    }
    base.update(overrides)
    return base


def _transaction_context(**overrides):
    base = dict(
        transaction_id="tx_001", received_at="now", status="pending", user_context=None,
        enriched_at=None, amount="-£1.00", is_debit=True, description="Weekly shop",
        category="eating_out", currency="GBP", created="now", settled=None, is_load=False,
        merchant=None, counterparty=None, raw="{}", skipped=False,
    )
    base.update(overrides)
    return base


class TestNavConsistency:
    """Regression guard for the bug where transaction.html was missing the Rules link."""

    def test_dashboard_has_three_nav_links_and_correct_active(self, env):
        html = env.get_template("dashboard.html").render(**_dashboard_context(active_nav="dashboard"))
        assert len(_nav_links(html)) == 3
        assert 'href="/dashboard" class="nav-link active"' in html

    def test_rules_has_three_nav_links_and_correct_active(self, env):
        html = env.get_template("rules.html").render(rules=[], active_nav="rules")
        assert len(_nav_links(html)) == 3
        assert 'href="/dashboard/rules" class="nav-link active"' in html

    def test_db_has_three_nav_links_and_correct_active(self, env):
        html = env.get_template("db.html").render(tables=[], active_nav="db")
        assert len(_nav_links(html)) == 3
        assert 'href="/dashboard/db" class="nav-link active"' in html

    def test_transaction_has_three_nav_links(self, env):
        html = env.get_template("transaction.html").render(**_transaction_context())
        assert len(_nav_links(html)) == 3

    def test_transaction_has_no_active_nav_item(self, env):
        html = env.get_template("transaction.html").render(**_transaction_context())
        assert "nav-link active" not in html

    def test_all_pages_have_logout_link(self, env):
        for name, kwargs in [
            ("dashboard.html", _dashboard_context()),
            ("rules.html", dict(rules=[])),
            ("db.html", dict(tables=[])),
            ("transaction.html", _transaction_context()),
        ]:
            html = env.get_template(name).render(**kwargs)
            assert 'href="/dashboard/logout"' in html, f"{name} missing logout link"


class TestRobotsMeta:
    def test_all_pages_block_indexing(self, env):
        for name, kwargs in [
            ("dashboard.html", _dashboard_context()),
            ("rules.html", dict(rules=[])),
            ("db.html", dict(tables=[])),
            ("transaction.html", _transaction_context()),
        ]:
            html = env.get_template(name).render(**kwargs)
            assert 'name="robots" content="noindex, nofollow"' in html


class TestTransactionTitle:
    def test_title_includes_description_and_site_suffix(self, env):
        html = env.get_template("transaction.html").render(**_transaction_context(description="Costa Coffee"))
        assert "<title>Costa Coffee — Bank Enrichment</title>" in html

    def test_title_falls_back_when_description_empty(self, env):
        html = env.get_template("transaction.html").render(**_transaction_context(description=""))
        assert "<title>Transaction — Bank Enrichment</title>" in html

    def test_category_underscore_replaced(self, env):
        html = env.get_template("transaction.html").render(**_transaction_context(category="eating_out"))
        assert "Eating out" in html
        assert "Eating_out" not in html

    def test_merchant_category_underscore_replaced(self, env):
        html = env.get_template("transaction.html").render(
            **_transaction_context(merchant={"name": "Tesco", "category": "supermarkets_groceries"})
        )
        assert "Supermarkets groceries" in html


class TestRulesXSSFix:
    def test_rule_name_not_embedded_in_inline_js(self, env):
        html = env.get_template("rules.html").render(rules=[_rule(name="x'); alert(1); //")])
        assert "onsubmit=" not in html
        assert 'data-rule-name="x&#39;); alert(1); //"' in html

    def test_no_inline_style_block(self, env):
        html = env.get_template("rules.html").render(rules=[])
        assert "<style>" not in html

    def test_form_labels_have_for_attribute(self, env):
        html = env.get_template("rules.html").render(rules=[])
        assert 'for="name"' in html
        assert 'for="match_field"' in html
        assert 'for="auto_context"' in html


class TestDashboardEnrichButton:
    def test_uses_data_attribute_not_inline_string(self, env):
        html = env.get_template("dashboard.html").render(
            **_dashboard_context(total_queue=1, queue=[_queue_row(id="tx_001", status="pending")])
        )
        assert 'data-tx-id="tx_001"' in html
        assert 'onclick="openEnrich(this.dataset.txId)"' in html
        assert "openEnrich('tx_001')" not in html


class TestLogoutPage:
    """Regression guard: the logout page must never invite a fake-login
    challenge — it should just explain the HTTP Basic limitation plainly."""

    def test_renders_without_error(self, env):
        html = env.get_template("logout.html").render()
        assert "Bank Enrichment" in html

    def test_explains_close_the_tab(self, env):
        html = env.get_template("logout.html").render()
        assert "close this browser tab" in html.lower()

    def test_has_link_back_to_dashboard(self, env):
        html = env.get_template("logout.html").render()
        assert 'href="/dashboard"' in html


class TestDbView:
    def test_renders_empty_tables(self, env):
        html = env.get_template("db.html").render(tables=[
            {"name": "stats", "columns": ["id"], "rows": []},
        ])
        assert "No rows" in html

    def test_renders_table_rows(self, env):
        html = env.get_template("db.html").render(tables=[
            {"name": "stats", "columns": ["id", "total_received"], "rows": [(1, 5)]},
        ])
        assert "stats" in html
        assert "total_received" in html
