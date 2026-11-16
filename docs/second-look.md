# Second look

[← Back to the README](../README.md)

New Category Approval only fires when a *new* name is needed, so a confidently wrong 
placement into an existing category (a holding deposit filed under Rent, a dentist 
tapped into Alcohol) would otherwise go straight through. So every placement into an 
existing category — including Telegram quick-taps — gets a second opinion from a 
separate Claude call (`placement_judge.py`) before it is saved. It sees the amounts and 
rhythm of what that subcategory already holds and where the same merchant has been 
filed before, and is told the categories exist to aggregate spending over time, so 
neighbouring-subcategory nitpicks aren't worth raising.

If it objects, nothing changes by itself: the transaction is held behind the same kind 
of Telegram card, offering its suggestion alongside **"Keep it where it was"**. Choosing 
keep is remembered — the same merchant with the same placement isn't questioned again — 
and a transaction is only ever judged once, so declining a card doesn't bring it back. 
"Try again" keeps the keep option. A declined second-look card never counts against 
any category name.

It fails **closed**: if the judge is switched on but can't reach a verdict (API down, 
balance out, unparseable reply), the transaction is left unclassified and retried on the 
next run rather than saved unreviewed — a check that quietly steps aside whenever 
something is wrong only works when nothing is. This applies to quick-taps too. Set
`PLACEMENT_JUDGE=off` in `config/.env` to turn it off deliberately, which releases
anything it was holding back; it is also off when there is no API key.

The card is worded for what it is ("🔍 Second look", the keep option marked ↩️, buttons
"Neither — try again" / "Skip — no change").

### Reviewing history

Everything classified before the judge existed can get the same second look with
`src/pipeline/judge_backlog.py`. It costs one API call per transaction, so by default it
only counts them and estimates the cost — nothing is sent, written or held until you add
`--yes`. Objections are held behind the same cards (10 at a time by default, `--cards N` to
change it, `--list-only` to see them without sending any); a held transaction **stays
classified where it is**, so your totals don't move while a card waits, and declining or
ignoring one leaves it exactly as it was. Each transaction is judged once, so re-running only
reviews what's left, and your own decisions (dashboard edits, card choices) are never
second-guessed.
