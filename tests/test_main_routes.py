import asyncio

from main import logout


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
