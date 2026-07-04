import asyncio

import main
from main import logout


class TestStaticVersion:
    """Regression guard: static assets must be cache-busted so a deploy can't
    silently leave browsers serving a stale style.css against new markup."""

    def test_static_version_is_set(self):
        assert main.templates.env.globals.get("static_version")

    def test_stylesheet_link_includes_version(self):
        html = main.templates.env.get_template("dashboard.html").render(
            total_received=0, total_amount="£0.00", requests_sent=0, total_enriched=0,
            total_processed=0, queue_stats=[], total_queue=0, page=1, total_pages=1, queue=[],
        )
        version = main.templates.env.globals["static_version"]
        assert f"/static/style.css?v={version}" in html


class TestLogout:
    def test_returns_401(self):
        response = asyncio.run(logout())
        assert response.status_code == 401

    def test_sets_www_authenticate_header(self):
        response = asyncio.run(logout())
        assert response.headers["www-authenticate"] == "Basic"

    def test_body_explains_logout(self):
        response = asyncio.run(logout())
        assert b"Logged out" in response.body
