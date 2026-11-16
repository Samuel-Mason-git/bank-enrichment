# Asking at the point of purchase

[← Back to the README](../README.md)

Each payment arrives in Telegram with a one-line question, the moment it happens: one tap files it, a sentence explains it, or you skip it. Follow-ups and the rest of the flow are in [How it works](how-it-works.md).

## Why context at the point of purchase

Traditional budgeting tools ask you to categorise transactions at the end of 
the month. By then, you've forgotten why you spent £12 at a service station or 
what that random Amazon charge was.

This system sends you a Telegram notification the moment a transaction happens 
and asks for one sentence of context. That's it. Thirty seconds while the memory 
is still there.

## Quick categories

Typing a sentence for every transaction adds up. The server works out your most-used
subcategories overall and per-merchant from your labelled transactions, so the next
Telegram notification for a matching merchant shows up to 3 one-tap buttons for 
that merchant's usual categories, or 5 general ones if the merchant hasn't built up enough 
history yet, alongside the usual Enrich and Skip buttons. Tapping one instantly saves that
category as your context — no typing required, and the next pipeline run files it there
(after the [second look](second-look.md)). Each button names its category by id, so a card tapped
days later still files exactly what it said, even if the buttons have changed since.

The per-merchant personalization depends on Monzo actually sending a `merchant` name on new
transactions, which in turn depends on Monzo's own access confirmation staying current — see
"Set Up the Monzo OAuth Confidential Client" in SETUP.md. If that lapses, categorization itself
keeps working fine from the raw description text; only the personalized quick-pick buttons fall
back to a generic top-5 list instead of that merchant's own history.
