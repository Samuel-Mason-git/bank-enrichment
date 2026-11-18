# Settings

[← Back to the README](../../README.md)

Everything you can change without touching the code is on the dashboard's **Settings** page, in tabs. Pages that
have settings link to the right tab, and the page says "Set this up in Settings → …".

## Setup check

Whether each part of the app is working, checked when you press **Run the checks** (two of the checks
ask Telegram and Anthropic, so they don't run on every page load):

| Check | OK when | Otherwise |
|---|---|---|
| Database | The dashboard can query it | A problem: check the `DB_` settings in `config/.env`. The checks that need the database are skipped |
| Worker | The last scheduled run (hourly, 08:00 to 22:00 London time, with half an hour's grace) started | A problem if a run didn't start, with the commands to look at it on the server. A warning if the last run had a failed step |
| Monzo payments | One arrived in the last 4 days | A warning: if you've paid for things since, the Monzo webhook isn't arriving |
| Telegram webhook | Telegram has this app's webhook address, and its last delivery worked | A problem if no webhook is registered or the token is wrong. A warning if it points somewhere else, or Telegram's last delivery failed (with Telegram's error) |
| Claude key | Anthropic accepts `ANTHROPIC_API_KEY` | A problem if it's missing or rejected |

The server also watches the worker on its own: see [Telegram](../capture/telegram.md#messages-the-bot-sends-you).

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

