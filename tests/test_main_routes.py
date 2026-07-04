import asyncio

from starlette.requests import Request

import main
from main import logout


def _fake_request() -> Request:
    return Request({
        "type": "http", "method": "GET", "path": "/dashboard/logout",
        "headers": [], "query_string": b"", "app": main.app,
    })


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
    """Regression guard: logout must never trigger a 401/WWW-Authenticate
    challenge — that traps the browser in a prompt loop it can never satisfy
    (this endpoint would keep rejecting whatever credentials are re-entered)."""

    def test_returns_200_not_401(self):
        response = asyncio.run(logout(_fake_request()))
        assert response.status_code == 200

    def test_does_not_set_www_authenticate_header(self):
        response = asyncio.run(logout(_fake_request()))
        assert "www-authenticate" not in {k.lower() for k in response.headers.keys()}

    def test_body_explains_the_limitation(self):
        response = asyncio.run(logout(_fake_request()))
        assert b"close this browser tab" in response.body.lower()
