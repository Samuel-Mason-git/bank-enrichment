# Architecture

[← Back to the README](../../README.md)

## Project structure

```
├── src/
│   ├── server/                # FastAPI server (runs in Docker on the server)
│   │   ├── main.py            # Webhooks, Telegram callbacks and commands (/ask, /budget, /health), the server's dashboard
│   │   ├── ask_tg.py          # /ask on Telegram: the finance agent's conversations and budget proposals
│   │   ├── telegram.py        # Telegram bot logic (messages, photos, the typing indicator)
│   │   ├── check_rules.py     # Rule matching logic
│   │   ├── follow_up_tg.py    # Follow-ups, and sending the cards for new proposals
│   │   ├── monzo_oauth.py     # Monzo API access, refreshed automatically
│   │   └── server_db.py       # The server's database functions (psycopg)
│   ├── pipeline/              # The pipeline's runs (the server's worker container; can be run by hand)
│   │   ├── worker.py          # Runs process.py hourly, 08:00–22:00 London time
│   │   ├── process.py         # The run: classify, act on decisions, send summaries and budget messages
│   │   ├── llm_labelling.py   # LLM classification (Claude): the models per pass are set in JOBS
│   │   ├── llm_calls.py       # Every call to Claude: recorded, costed, and retried
│   │   ├── placement_judge.py # The second look at each placement
│   │   ├── category_proposals.py  # Real-time new-category approval (holds transactions behind a Telegram card)
│   │   ├── taxonomy_review.py # Monthly taxonomy review (cluster detection + Telegram cards)
│   │   ├── rule_suggestions.py    # Spots patterns worth a rule
│   │   ├── weekly_summary.py, monthly_summary.py  # Telegram summaries (with budget lines and charts)
│   │   ├── budgets.py         # Budgets: what's spent, the pace, alerts, and the settings for the budget message
│   │   ├── budget_messages.py # The budget message, its alerts, and /budget
│   │   ├── bot_health.py      # /health: the app's status, sent to Telegram
│   │   ├── alert_settings.py  # The alert settings (unusual payments, subscriptions), with ranges and defaults
│   │   ├── ask_settings.py    # The Ask settings (page, Telegram, model, effort, timeouts)
│   │   ├── unusual_payments.py    # Telegram alert for a payment that's an outlier for its merchant
│   │   ├── subscription_alerts.py # Telegram alerts for a changed subscription price or a missed payment
│   │   ├── telegram_charts.py # Charts drawn as images for Telegram (matplotlib)
│   │   ├── finance_agent.py   # The Ask agent: answers questions from the database, with its tools
│   │   ├── agent_eval.py      # How accurate the Ask agent is, on your own data (manual, costs API credit)
│   │   ├── model_eval.py      # Compares classifier models on labelled history (manual, costs API credit)
│   │   ├── alerts.py          # Telegram messages and alerts, including photos
│   │   ├── activity.py        # Warnings and errors into the database, for the Activity page
│   │   ├── judge_backlog.py   # Manual: a second look at older history
│   │   └── database_functions.py  # The database library, shared with the dashboard — includes a CLI (run the file directly) to print stats/transactions
│   └── dashboard/             # The Streamlit dashboard, on your own machine
│       ├── dashboard.py       # Entry point: loads the data, the sidebar's filters, the page navigation (Ask and Budget in the top bar)
│       ├── views/             # One script per page: ask, budget, overview, over_time, transactions, drill_down, merchants, subscriptions, queue, payment, rules, settings, activity, ai
│       ├── ui.py              # Shared drawing helpers: money formatting, metric cards
│       ├── page_context.py    # Hands the sidebar's filtered data to the open page
│       ├── payment_actions.py # Labels for the activity log and the AI page
│       └── dashboard_helpers.py   # Pure logic used by the dashboard (role/subscription/rule/chart helpers) — unit tested standalone
├── scripts/
│   ├── run_dashboard_wsl.sh   # Runs the dashboard in WSL (launch_dashboard.bat calls it)
│   ├── find_poetry.sh         # Finds Poetry and installs the packages, for the two WSL scripts
│   ├── run_process_wsl.sh     # Runs the pipeline once, by hand, in WSL
│   └── setup_swap.sh          # Adds swap to the server (rerunnable)
├── sql/
│   └── postgres/schema.sql    # The database: tables, views and grants (applied by the server on every start)
├── config/
│   └── .env.example           # Environment variable template
├── Dockerfile                 # The server and worker image (src/server + src/pipeline, not the dashboard)
├── docker-compose.yml         # Server, worker and Caddy
└── Caddyfile                  # Reverse proxy + automatic HTTPS
```

## Tech stack

| Layer | Technology |
|-------|-----------|
| Server | Python, FastAPI, psycopg |
| Reverse proxy | Caddy (automatic HTTPS) |
| Notifications | Telegram Bot API |
| Database | Postgres 18, on any host the server can reach (and your machine, for the dashboard) |
| LLM | Anthropic Claude: Haiku 5.5 for matching existing categories, the placement judge and the Ask agent; Sonnet 4.6 for picking and suggesting categories. Chosen by the accuracy evals (`agent_eval.py`, `model_eval.py`) |
| Local dashboard | Streamlit, Plotly |
| Deployment | Docker Compose: the server, the hourly worker, and Caddy |
| CI/CD | GitHub Actions — tests (with coverage, which must stay at 80% or more) and server-image build/smoke tests run on every PR; Dependabot proposes dependency updates; merging to `main` auto-deploys over SSH, but only when something the server or the worker is built from changed (`src/server/`, `src/pipeline/`, `sql/postgres/schema.sql`, the Dockerfile/compose/Caddy files, `pyproject.toml`, `poetry.lock`) — dashboard changes don't restart it. Deploys never overlap. |
