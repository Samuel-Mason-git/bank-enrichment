# AI usage

[← Back to the README](../../README.md)

The **AI usage** page on the dashboard shows what the calls to Claude cost and did: spend by day,
job, model and prompt version; each call's tokens, cost, response and thinking; and how good the
answers were (how often you corrected the classifier, and what the placement judge caught).

Failures are reported on Telegram, see [Failure alerts](failure-alerts.md).

## Models

Haiku 5.5 handles matching existing categories, the placement judge and the Ask agent. Sonnet 4.6
picks and suggests categories. The models were chosen by the accuracy evals (`agent_eval.py`,
`model_eval.py`).
