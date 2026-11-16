# Categories and roles

[← Back to the README](../README.md)

Every category has a role, which decides what counts as income or spend in the figures.

Every category has a **Role** — Income, Spend, Investment, Saving, Transfer, or Excluded —
set from the Settings page, at the parent level (subcategories inherit it, or can override it
individually, e.g. a "Refunds" subcategory under "Income" is Excluded rather than Income).
Roles drive what actually counts as income or spend throughout the dashboard, so a refund or
an internal transfer between your own accounts doesn't inflate your real income or spend.

This is why most of the dashboard shows two versions of the same figures:

- **Standard** — every £ counted by amount sign alone (money in = income, money out = spend),
  regardless of category.
- **Actual** — the same figures computed from each category's Role instead, so refunds,
  transfers, and investment/saving movements are correctly excluded or reclassified.

**Investment vs Saving** are mechanically identical (both net their two directions together —
a contribution/deposit and a later withdrawal are the same account or pot, so £500 in then £200
back out is genuinely £300 net, not two unrelated amounts — and both are excluded from Actual
Income/Spend/Net so a deposit can't be miscounted as real spend) but are kept as two separate
roles rather than one, so a genuine investment contribution (stocks, ISA, pension) is never
silently combined with money moved into a savings pot under one figure. Use whichever fits —
e.g. a Monzo savings pot as Saving, a Trading 212/stocks & shares contribution as Investment.

Transfer/Excluded categories are *not* netted the same way — that bucket can mix genuinely
unrelated inbound and outbound movements (a refund in, an unrelated bill out), so it keeps
showing its outgoing side plus a separate incoming card whenever there's both, rather than
risking two unrelated amounts cancelling into a misleading number.
