# Rules

[← Back to the README](../../README.md)

Rules are matched against each incoming transaction before the Telegram notification fires. 
If a rule matches, the transaction is auto-enriched with the rule's context and the 
notification is skipped entirely.

Each rule specifies:

| Field | Description |
|---|---|
| Name | A label for the rule (e.g. "Wifi Bill") |
| Match field | What to check — merchant name, description, counterparty name, category, payment scheme, or amount |
| Match type | How to compare — `contains`, `exact`, `regex`, `amount_range`, `amount_exact`, or (amount field only) `amount_negative`/`amount_positive` for direction alone |
| Match value | The value to match against (e.g. `EE`, `490-510` for a £ range, `9.99` for exact) — not needed for `amount_negative`/`amount_positive`, which only look at the sign |
| Auto context | The context sentence to store (e.g. "Monthly wifi bill") — optional if Auto Skip is on or a Fixed Category is set |
| Fixed category | Optional — a category and subcategory picked from your own taxonomy. A match is filed straight there, and the classifier never runs on it |
| Auto Skip | If checked, the transaction is marked as skipped automatically — no Telegram notification, no manual enrichment needed |
| Second condition | Optional — a second match field/type/value that must also pass (AND logic) |

Rules live on the dashboard's **Rules** page, which previews what a rule would have caught
across every payment so far (and which an earlier rule would take first: the first match wins),
and can pause any rule without deleting it.
Amount ranges and exact amounts are specified in pounds and matched against the absolute
transaction value, regardless of direction — `amount_negative`/`amount_positive` exist
specifically for when direction itself is what you need to match on (see the Savings
example below). A rule with two conditions only fires if both match — useful for cases
like a specific merchant at a specific amount, or a payment scheme in one direction only.

**Payment scheme** is Monzo's own classification of how money moved (e.g. `uk_retail_pot`
for a transfer to or from a pot) — matching on it is exact by nature, unlike a text match
on the description, which for a pot transfer is just the pot's internal ID and can
coincidentally contain another merchant's name as a substring.

**Auto Skip** is useful for transactions you never want to see — internal transfers, 
refunds from known merchants, or any recurring charge you're confident needs no context. 
The transaction is still stored and will appear in your local database; it just won't 
interrupt you via Telegram. You can combine Auto Skip with an Auto Context if you want 
the transaction labelled but not notified.

**Auto Context vs Fixed Category.** An Auto Context rule only supplies the context
sentence — the transaction still goes through quick-tap or Claude to be placed, which
costs a classification call and can, in principle, land somewhere different from last
time. A **Fixed Category** rule skips all of that: the server files the match straight
into the chosen category as it arrives (logged as source `rule`) and the classifier never
sees it. Use it when the answer is never in doubt — a phone bill, a subscription, rent.
The dropdowns list your real categories, so a typo can't quietly create a near-duplicate
category. The rule points at the category itself, so renaming it carries the rule along;
if it's deleted, matches are left for the classifier instead. Editing or resetting a transaction's context by hand
clears any rule placement on it, so what you typed always wins.

**A card payment's amount isn't always final.** A provisional authorisation hold (the
classic UK fuel-pump pattern — a flat ~£99/£100 hold before the pump knows how much fuel
was actually taken) is detected automatically and auto-skipped, the same way a declined
payment is: no Telegram card, since the amount was never going to be the real charge. The
real, final amount still arrives normally, as its own separate transaction.

**Worked example — tracking a Monzo savings pot separately from real investing:** two
rules, both matching **Payment scheme** `exact` `uk_retail_pot`, with a second condition
of **Amount** `amount_negative` on one (Auto Context "Moved into savings pot") and
`amount_positive` on the other ("Moved out of savings pot"). Once a few transactions have
gone through each, set that subcategory's Role to **Saving** in the Settings page — see
[Category Roles & Standard vs Actual](../classification/categories-and-roles.md).

### Checking Rules Against Payments

The Rules page shows what a rule would catch while you're still typing it: every payment
ever received that it matches, and whether an earlier rule would take it first (the first
match wins). Saving a rule only affects new payments; the preview shows the history it
would have changed. The **Payment** selector at the top of the page shows which rules catch
any one payment, which is the quickest way to see why a payment was filed where it was.

### Rule Suggestions

Each pipeline run also scans your already-classified transactions for a merchant or
counterparty that has shown up 5+ times with the *exact same* category/subcategory every
single time — one disagreement anywhere in its history disqualifies it, since the whole
point is a pattern reliable enough to automate.

Statistical consistency in your own history isn't the same as a merchant being safe to
automate, though — a supermarket that's happened to be Groceries every time so far could
just as easily be something else next time, while a single-purpose merchant (a phone
bill, a subscription) realistically never will be. Every qualifying pattern is checked by
Claude for exactly this before anything is suggested — one small batched call per run, 
only when there's a pattern it hasn't asked about before — and anything judged a general-purpose
retailer/marketplace is dropped rather than suggested. (Fails closed: if that check
itself fails, nothing is suggested that run rather than risk suggesting something
unverified — it's simply reconsidered next time.)

A pattern that survives both checks, and isn't already covered by an existing enabled
rule, gets a Telegram card of its own: **💡 Rule suggestion**, with the suggested context
pre-filled from whichever context sentence you've typed most often for that merchant
(falling back to a generic "`<merchant>` purchase" if your wording has never settled on
one) and Claude's own reasoning for why it's safe to automate. Tapping **Create rule**
adds it immediately as a **Fixed Category** rule — future matches go straight into that
category without a classification call, which is safe precisely because the pattern has
never once varied and was judged single-purpose first. Tapping **No thanks** means that
exact pattern is never suggested again.
