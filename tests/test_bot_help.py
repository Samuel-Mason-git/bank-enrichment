"""/help: the bot lists its commands, to the owner only, and a word that only
starts like /help is still a plain message."""
from unittest.mock import patch

import pytest

import bot_help
from tests.test_ask_telegram import _message


@pytest.mark.parametrize("text, is_cmd", [
    ("/help", True), ("/help@MyMoneyBot", True), ("  /HELP  ", True),
    ("/helpful tips", False), ("please /help", False), ("weekly shop", False), ("", False), (None, False),
])
def test_help_is_recognised(text, is_cmd):
    assert bot_help.is_help(text) is is_cmd


def test_help_sends_the_command_list_and_asks_nothing():
    bot, chats, record_context = _message("/help")
    bot.send_message.assert_called_once_with(12345, bot_help.HELP)
    chats.start.assert_not_called()
    record_context.assert_not_called()


def test_the_list_names_every_command():
    for command in ("/ask", "/budget", "/health", "/help"):
        assert command in bot_help.HELP


def test_a_word_that_starts_like_help_is_a_plain_message():
    with patch("main.get_con"):
        bot, chats, record_context = _message("/helpful tips")
    sent = [c.args[1] for c in bot.send_message.call_args_list]
    assert bot_help.HELP not in sent                 # the help list wasn't sent
    record_context.assert_called_once()              # it was taken as the payment's context
