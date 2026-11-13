"""Ask: questions about your money in plain English, answered by the finance
agent (src/pipeline/finance_agent.py) from your own data.

Every answer has its workings underneath -- each tool the agent used, what it
asked for and what came back -- so any figure can be checked. The agent reads
through its own read-only database login and ignores the sidebar's filters:
the dates come from the question. A chat lasts as long as the browser tab."""
import json
import os

import anthropic
import plotly.express as px
import streamlit as st

import finance_agent as fa

STARTERS = [
    "How much did I spend last month, and on what?",
    "How does this month's spending compare with last month's so far?",
    "What are my subscriptions costing me a year?",
    "What were my five biggest purchases in the last 3 months?",
]


def _usd(v: float) -> str:
    """$0.30, or $0.0021 for the fractions of a cent one answer costs."""
    return f"${v:,.2f}" if v >= 0.1 else f"${v:,.4f}"


def _md(text: str) -> str:
    """Markdown with dollar signs shown as written, not read as maths."""
    return text.replace("$", "\\$")


def _pretty(output: str) -> str:
    try:
        return json.dumps(json.loads(output), indent=1, ensure_ascii=False)
    except ValueError:
        return output


def _workings(steps: list) -> None:
    if not steps:
        return
    with st.expander(f"Show workings ({len(steps)} step{'s' if len(steps) != 1 else ''})"):
        for i, s in enumerate(steps, 1):
            st.markdown(f"**{i}. {fa.TOOL_LABELS.get(s.tool, s.tool)}**"
                        + (" — the tool said no, so the agent tried again" if s.is_error else ""))
            if s.tool == "run_sql" and s.input.get("query"):
                st.code(s.input["query"], language="sql")
            elif s.input:
                st.code(json.dumps(s.input, indent=1, ensure_ascii=False), language="json")
            st.code(_pretty(s.output), language=None if s.is_error else "json")


METRIC_LABELS = {"spend": "Spend", "income": "Income", "invested": "Invested", "saved": "Saved",
                 "transferred_out": "Transferred out", "transferred_in": "Transferred in"}


def _charts(charts: list, key: str) -> None:
    """Each chart the agent asked for. The figures came from the data, not the
    model (finance_agent.build_chart)."""
    for i, c in enumerate(charts or []):
        labels = [p["label"] for p in c["points"]]
        values = [p["value"] for p in c["points"]]
        axis = {"x": labels, "y": values, "labels": {"x": "", "y": METRIC_LABELS.get(c["metric"], c["metric"])}}
        fig = px.line(**axis, markers=True) if c["kind"] == "line" else px.bar(**axis)
        fig.update_traces(hovertemplate="%{x}<br>£%{y:,.2f}<extra></extra>")
        fig.update_layout(title=c["title"], yaxis_tickprefix="£", height=360, margin={"t": 50, "b": 10, "l": 10, "r": 10})
        st.plotly_chart(fig, width="stretch", key=f"ask_chart_{key}_{i}")


def _new_chat() -> None:
    st.session_state["ask_agent"] = fa.FinanceAgent(anthropic.Anthropic(api_key=os.getenv("CLAUDE_SECRET")))
    st.session_state["ask_history"] = []


def _render(entry: dict, key: str) -> None:
    with st.chat_message(entry["role"]):
        if entry.get("error"):
            st.warning(entry["text"])
            return
        st.markdown(_md(entry["text"]) if entry["role"] == "assistant" else entry["text"])
        _charts(entry.get("charts"), key)
        _workings(entry.get("steps") or [])
        if entry.get("caption"):
            st.caption(_md(entry["caption"]))


st.header("Ask")

if not fa.is_configured():
    st.info("The Ask page needs the agent's own read-only database login: add AGENT_DB_USER and "
            "AGENT_DB_PASSWORD to config/.env (made with data-vault's new_reader.sh), then restart the dashboard.")
    st.stop()
if not os.getenv("CLAUDE_SECRET"):
    st.info("The Ask page needs CLAUDE_SECRET in config/.env.")
    st.stop()

if "ask_agent" not in st.session_state:
    _new_chat()
agent = st.session_state["ask_agent"]
history: list[dict] = st.session_state["ask_history"]

# The question first (typed, or a starter tapped on the last run), so the rest
# of the page is drawn knowing a chat is under way.
question = st.chat_input("Ask about your money…") or st.session_state.pop("ask_pending", None)

intro, reset = st.columns([5, 1])
intro.caption("Ask anything about your spending, income, savings or subscriptions. Answers come from your own "
              "data, through a read-only login, and ignore the sidebar's filters — say the dates in the question.")
if (history or question) and reset.button("New chat", width="stretch"):
    _new_chat()
    st.rerun()

for n, entry in enumerate(history):
    _render(entry, str(n))

if not history and not question:
    st.caption("Try one of these:")
    cols = st.columns(2)
    for i, starter in enumerate(STARTERS):
        if cols[i % 2].button(starter, key=f"ask_starter_{i}", width="stretch"):
            st.session_state["ask_pending"] = starter
            st.rerun()

if question:
    history.append({"role": "user", "text": question})
    _render(history[-1], str(len(history) - 1))
    with st.chat_message("assistant"):
        status = st.status("Working it out…", expanded=True)

        def on_step(step):
            status.write(fa.TOOL_LABELS.get(step.tool, step.tool) + ("… (retrying)" if step.is_error else "…"))

        try:
            answer = agent.ask(question, on_step=on_step)
        except fa.DailyLimitReached as e:
            status.update(label="Not sent", state="error", expanded=False)
            history.append({"role": "assistant", "text": str(e), "error": True})
            st.warning(str(e))
        except fa.AgentNotConfigured as e:
            status.update(label="Not sent", state="error", expanded=False)
            history.append({"role": "assistant", "text": str(e), "error": True})
            st.warning(str(e))
        except anthropic.APIError as e:
            status.update(label="Couldn't reach Claude", state="error", expanded=False)
            text = f"Claude couldn't be reached ({type(e).__name__}). Nothing was saved; try again in a moment."
            history.append({"role": "assistant", "text": text, "error": True})
            st.warning(text)
        except Exception as e:
            status.update(label="Something went wrong", state="error", expanded=False)
            text = f"Something went wrong answering that ({type(e).__name__}: {e})."
            history.append({"role": "assistant", "text": text, "error": True})
            st.warning(text)
        else:
            n = len(answer.steps)
            status.update(label=f"Worked it out in {n} step{'s' if n != 1 else ''}" if n else "Answered",
                          state="complete", expanded=False)
            caption = (f"This answer {_usd(answer.cost_usd)} · this chat {_usd(agent.cost_usd)} · "
                       f"today {_usd(fa.spent_today())} of the {_usd(agent.daily_limit_usd)} daily limit")
            entry = {"role": "assistant", "text": answer.text, "steps": answer.steps, "caption": caption,
                     "charts": answer.charts}
            history.append(entry)
            st.markdown(_md(answer.text))
            _charts(answer.charts, str(len(history) - 1))
            _workings(answer.steps)
            st.caption(_md(caption))
