from unittest.mock import MagicMock

import anthropic
import httpx
import pytest
import requests

import alerts
import llm_labelling as ll


def _response(status):
    return httpx.Response(status, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))


def credit_error():
    return anthropic.BadRequestError("Your credit balance is too low to access the Anthropic API", response=_response(400), body=None)


def auth_error():
    return anthropic.AuthenticationError("invalid x-api-key", response=_response(401), body=None)


def server_error():
    return anthropic.InternalServerError("overloaded", response=_response(500), body=None)


def sent(alert_post):
    return [c.kwargs["json"] for c in alert_post.call_args_list]


class TestClassifyError:
    def test_running_out_of_credit_says_how_to_fix_it(self):
        kind, title, message = alerts.classify_error(credit_error())
        assert kind == "credit" and "credit balance" in title.lower()
        assert "Billing" in message and "retried" in message

    def test_a_rejected_key_points_at_the_config(self):
        kind, title, message = alerts.classify_error(auth_error())
        assert kind == "auth" and "CLAUDE_SECRET" in message

    def test_other_bad_requests_are_not_mistaken_for_credit(self):
        other = anthropic.BadRequestError("prompt is too long", response=_response(400), body=None)
        assert alerts.classify_error(other)[0] == "llm-error"

    def test_a_server_error_reports_the_class_and_status_only(self):
        kind, _, message = alerts.classify_error(server_error())
        assert kind == "llm-error" and "InternalServerError (HTTP 500)" in message
        assert "overloaded" not in message

    def test_a_parse_failure_never_repeats_what_the_model_said(self):
        """_extract_json's error message quotes the model's raw output, which can
        contain merchant names and amounts -- none of that may reach Telegram."""
        e = ValueError("Could not extract JSON array from response: x\nRaw: Tesco £42.10 for Rhea's dinner")
        _, title, message = alerts.classify_error(e)
        assert "Tesco" not in message and "Rhea" not in message and "£" not in message
        assert "ValueError" in message


class TestSendAlert:
    def test_posts_title_and_message_to_the_server_with_the_api_key(self, alert_post):
        assert alerts.send_alert("credit", "Low credit", "Top up.") is True
        args, kwargs = alert_post.call_args
        assert args[0] == "http://test-server/send-alert"
        assert kwargs["headers"] == {"X-API-Key": "test-api-key"}
        assert kwargs["json"] == {"title": "Low credit", "message": "Top up."}

    def test_each_kind_is_sent_once_per_run(self, alert_post):
        assert alerts.send_alert("credit", "a", "b") is True
        assert alerts.send_alert("credit", "a", "b") is False
        assert alerts.send_alert("auth", "c", "d") is True
        assert alert_post.call_count == 2

    def test_a_network_failure_is_swallowed(self, alert_post):
        alert_post.side_effect = requests.ConnectionError("no route")
        assert alerts.send_alert("credit", "a", "b") is False

    def test_an_older_server_without_the_endpoint_is_tolerated(self, alert_post):
        alert_post.return_value = MagicMock(status_code=404, ok=False)
        assert alerts.send_alert("credit", "a", "b") is False

    def test_a_rejected_alert_is_reported_not_raised(self, alert_post):
        alert_post.return_value = MagicMock(status_code=500, ok=False)
        assert alerts.send_alert("credit", "a", "b") is False

    def test_no_server_configured_means_no_request(self, alert_post, monkeypatch):
        monkeypatch.setattr(alerts, "SERVER_URL", "")
        assert alerts.send_alert("credit", "a", "b") is False
        alert_post.assert_not_called()


class TestClassifierReportsFailures:
    SUBS = [{"name": "Rent", "parent_name": "Bills & Utilities", "transaction_count": 1}]
    TXNS = [{"id": "t1", "amount": -5.0}]

    def _client(self, error):
        client = MagicMock()
        client.messages.create.side_effect = error
        return client

    def test_running_out_of_credit_during_a_pass_sends_one_alert_and_the_run_carries_on(self, alert_post):
        client = self._client(credit_error())
        assert ll.match_existing(client, self.TXNS, self.SUBS) == {}
        assert ll.classify_parents(client, self.TXNS, [{"name": "Bills & Utilities", "transaction_count": 1}]) == {}
        assert ll.classify_subcategories(client, self.TXNS, "Bills & Utilities", self.SUBS, ["Bills & Utilities"]) == {}

        messages = sent(alert_post)
        assert len(messages) == 1, "three failed passes, one alert"
        assert messages[0]["title"] == "Anthropic credit balance too low"
        assert "Pass 0" in messages[0]["message"], "it says where it was first seen"

    def test_a_bad_key_is_reported_as_such(self, alert_post):
        ll.match_existing(self._client(auth_error()), self.TXNS, self.SUBS)
        assert sent(alert_post)[0]["title"] == "Anthropic API key rejected"

    def test_unparseable_model_output_is_reported_without_quoting_it(self, alert_post):
        client = MagicMock()
        client.messages.create.return_value = MagicMock(
            content=[MagicMock(text="SECRETMERCHANT paid £99 — no JSON here")], stop_reason="end_turn")
        ll.match_existing(client, self.TXNS, self.SUBS)
        payload = sent(alert_post)[0]
        assert "SECRETMERCHANT" not in payload["message"] and "£99" not in payload["message"]

    def test_a_working_pass_sends_nothing(self, alert_post):
        client = MagicMock()
        client.messages.create.return_value = MagicMock(
            content=[MagicMock(text='[{"id": "t1", "category": null, "subcategory": null}]')], stop_reason="end_turn")
        ll.match_existing(client, self.TXNS, self.SUBS)
        alert_post.assert_not_called()
