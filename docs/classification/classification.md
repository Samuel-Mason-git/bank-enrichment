# Classification

[← Back to the README](../../README.md)

A multi-pass classification system runs every hour through the day on the server, against the shared database:

**Pass 0 — Match existing taxonomy:** Claude first checks whether each transaction 
confidently matches an already-existing subcategory. If it does, the transaction is 
classified immediately and the remaining passes are skipped. This keeps the taxonomy 
consistent over time — a transaction classified as "Tobacco & Nicotine" once will 
always land there on future runs.

**Pass 1 — Parent category:** For transactions that didn't match in Pass 0, Claude 
assigns a broad parent category. It thinks carefully about overlapping categories 
and picks the most accurate fit — for example, "Holidays" is only used when the 
context explicitly mentions a holiday trip, not just any travel spend. A transaction 
with a genuinely distinct, one-off purpose (a payment to a tax authority, a one-time 
legal or medical event) is treated as deserving its own new parent, not squeezed into 
a loosely-related existing one just because it seems adjacent.

**Pass 2 — Subcategory:** Within each parent, Claude assigns a specific subcategory, 
creating new ones only when none of the existing ones are an accurate fit.

**Pass 3 — Alternatives:** Only for transactions that didn't cleanly match anything 
existing, Claude is asked for up to 2 more genuinely distinct placements alongside 
its first choice — a stretch-fit into an existing category it wasn't confident enough 
to auto-match, or a different new-category idea — each with a one-line rationale. See 
[New Category Approval](new-categories.md#new-category-approval) for what happens with these.

Passes 0 and 2 also see how big and how regular the transactions already in each 
subcategory are (amount range and typical gap between them), so a one-off £282 
deposit isn't matched into a subcategory of £1,315 monthly rent just because the 
topics sound related. See [Second-Look Review](second-look.md) for the check 
that runs after classification.

A default taxonomy is seeded on first run — 13 parent categories and ~70 subcategories 
covering most common personal spending:

| Parent | Example subcategories |
|--------|----------------------|
| Bills & Utilities | Rent, Electricity, Internet, Mobile Phone |
| Food & Drink | Groceries, Restaurants, Takeaway, Coffee Shops |
| Transport | Public Transport, Fuel, Taxi & Rideshare, Parking |
| Shopping | Clothing, Electronics, Gifts, Home Goods |
| Holidays | Accommodation, Holiday Food, Local Transport |
| Entertainment | Streaming, Cinema, Events, Video Games |
| Health | Pharmacy & Medication, Dental, GP / Medical |
| Income | Salary, Refunds, Interest, Dividends |
| Investments | Savings Deposits, Stocks & Shares ISA, Pension |
| Subscriptions & Software | AI Tools, Cloud Storage, Streaming |
| Personal Care & Consumables | Haircuts, Toiletries, Tobacco & Nicotine |
| Career & Learning | Online Learning, Books, Certifications |
| Transfers | Inbound Transfer, Outbound Transfer |

This is a starting point, not a constraint. Everything can be changed from the **Settings page** 
in the local dashboard — rename parents, add or delete subcategories, move subcategories 
between parents, view which transactions sit under each label, and wipe labels per category 
so the LLM re-classifies against your updated structure. Because context is stored separately 
from labels, restructuring the taxonomy and re-running classification never loses any data.

The default taxonomy is only seeded into a genuinely empty database — the very first time
you run the pipeline or dashboard. Once any parent category exists, seeding is skipped
entirely, so wiping the taxonomy from the Settings page and building your own from scratch
is safe: the defaults won't quietly get added back in alongside it on a later restart.
