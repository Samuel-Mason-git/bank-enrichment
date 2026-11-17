# Settings

[← Back to the README](../../README.md)

Everything you can change without touching the code is on the dashboard's **Settings** page, in tabs. Pages that
have settings link to the right tab, and the page says "Set this up in Settings → …".

## Categories

The taxonomy: parent categories and their subcategories, each one's **Role** (Income, Spend, Investment, Saving,
Transfer or Excluded), and adding, renaming, moving or deleting categories. You can also see the transactions in a
subcategory, reassign them, or wipe a category's labels so they're classified again. See
[Categories and roles](../classification/categories-and-roles.md) and [Classification](../classification/classification.md).

## Budgets

When the budget message comes to Telegram (off, every morning, weekdays only, chosen days of the week, or monthly on a day
you pick). Which instant alerts you get: halfway, nearly used up, over budget, off pace, and a weekly total. The points they
fire at (the halfway and nearly-used-up percentages, the pace rule, the weekly limit), the quiet hours, and which categories
get no alerts are all set here too. The budgets themselves are set on the Budget page. See [Budgets](budgets.md).

## Alerts

- **Unusual payments:** on or off, how sensitive it is (less, default or more), how many earlier payments a merchant
  needs before it's compared (3 to 24, default 5), and the least a payment must be above the usual (default £5).
  See [Point of purchase](../capture/point-of-purchase.md#unusual-payments).
- **Subscriptions:** a changed price (on or off, and the change that counts in pounds and percent, default 50p and 5%),
  and a missed payment (on or off, and how many days late before the alert, default 7). See
  [Subscriptions](../capture/subscriptions.md#alerts).

Each value has a range. A value out of range is refused when you save it, and falls back to its default if it's ever stored badly.

## Ask

- **The Ask page** and **Telegram `/ask`**: each can be switched off.
- **Model:** Haiku 5.5 (the default, cheapest) or Sonnet 5.5 (more capable, several times the cost per question).
- **How hard it thinks:** low, medium or high.
- **Telegram chat timeout:** minutes after a question before a chat starts afresh (default 30).
- **Budget suggestion timeout:** minutes a suggestion waits for a tap (default 30).
- **Daily limit:** the most the Ask page and Telegram may spend on Claude in a day. See [Ask](ask.md).

A chat that's already open keeps its model until it starts afresh.

## Backups

Where backups are saved (a folder on the machine running the dashboard; leave it blank for the default), taking a copy
now, the list of backups, downloading one as a zip, and deleting one. See [Backups](../data/backups.md).

## Danger zone

Wiping the whole taxonomy, behind a confirmation. See [Categories and roles](../classification/categories-and-roles.md).

## Storage

Where the data is kept. The page shows each layer and tests the optional bronze copy. It doesn't change
anything: those settings live in `config/.env`, and the setup command writes them.

- **Live database:** the one the app reads. Shown with its host, port and name. The password isn't shown.
- **Bronze copy:** optional, off by default. Shows its kind (folder or bucket), where it goes, how many copies
  are kept, and when the last one went. A test button runs the same checks the setup command does. See
  [Backups](../data/backups.md#a-daily-copy-somewhere-else).
- **Silver:** not in use yet. When it is, it lives in a schema in the live database.

