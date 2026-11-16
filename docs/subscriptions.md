# Subscriptions

[← Back to the README](../README.md)

Recurring payments are spotted automatically and listed on the dashboard's
[Subscriptions](dashboard.md) page, with what each costs per month and per year.

- Matching is by merchant name *or* counterparty name, since many direct debits only populate the latter.
- A fast-cadence pattern (weekly or fortnightly) needs more supporting occurrences before it's suggested.
- A pattern whose last occurrence is stale relative to its cadence is ignored.
- A confirmed subscription auto-deactivates if no matching transaction has landed in the last 2 months.
