"""Ask: questions about your money in plain English, answered by the finance
agent (src/pipeline/finance_agent.py) from your own data.

Every answer has its workings underneath -- each tool the agent used, what it
asked for and what came back -- so any figure can be checked. The agent reads
through its own read-only database login and ignores the sidebar's filters:
the dates come from the question. A chat lasts as long as the browser tab,
unless you save it: saved chats (saved_chats) keep what was on screen, can be
opened and deleted here, and aren't exported to bronze, so deleting one
deletes it."""
import dataclasses
import json
import os

import anthropic
import plotly.express as px
import streamlit as st

import finance_agent as fa
from database_functions import delete_saved_chat, get_saved_chat, list_saved_chats, save_chat

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
    st.session_state.pop("ask_saving", None)


def _to_saved(history: list[dict]) -> list[dict]:
    """The chat as plain JSON, as on screen: questions, answers, charts and
    workings. Failed turns (a limit, an API error) aren't worth keeping."""
    out = []
    for e in history:
        if e.get("error"):
            continue
        d = {k: e[k] for k in ("role", "text", "caption", "charts") if e.get(k)}
        if e.get("steps"):
            d["steps"] = [dataclasses.asdict(s) for s in e["steps"]]
        out.append(d)
    return out


def _from_saved(messages: list[dict]) -> list[dict]:
    return [{**m, "steps": [fa.Step(**s) for s in m.get("steps", [])]} for m in messages]


def _saved_chats_list() -> None:
    chats = list_saved_chats()
    if not chats:
        return
    with st.expander(f"📁 Saved chats ({len(chats)})"):
        for c in chats:
            title, when, open_ = st.columns([6, 2, 1])
            title.markdown(f"**{_md(c['title'])}**")
            when.caption(f"{c['saved_at'].astimezone(fa.LONDON):%d %b %Y, %H:%M} · {c['messages']} messages")
            if open_.button("Open", key=f"ask_open_{c['id']}", width="stretch"):
                st.session_state["ask_viewing"] = c["id"]
                st.rerun()


def _view_saved(chat_id: int) -> None:
    """A saved chat, to read back (it can't be carried on: start a new one)."""
    chat = get_saved_chat(chat_id)
    if chat is None:
        st.session_state.pop("ask_viewing", None)
        st.rerun()
    back, delete, _ = st.columns([1, 1, 4])
    if back.button("← Back", key="ask_back", width="stretch"):
        st.session_state.pop("ask_viewing", None)
        st.rerun()
    if st.session_state.get("ask_confirm_delete") == chat_id:
        st.warning("Delete this saved chat? It can't be undone.")
        yes, no, _ = st.columns([1, 1, 4])
        if yes.button("Delete it", key="ask_delete_yes", type="primary", width="stretch"):
            delete_saved_chat(chat_id)
            st.session_state.pop("ask_viewing", None)
            st.session_state.pop("ask_confirm_delete", None)
            st.toast(f"Deleted \"{chat['title']}\"")
            st.rerun()
        if no.button("Keep it", key="ask_delete_no", width="stretch"):
            st.session_state.pop("ask_confirm_delete", None)
            st.rerun()
    elif delete.button("Delete", key="ask_delete", width="stretch"):
        st.session_state["ask_confirm_delete"] = chat_id
        st.rerun()
    st.subheader(chat["title"])
    st.caption(f"Saved {chat['saved_at'].astimezone(fa.LONDON):%d %B %Y at %H:%M}")
    for n, entry in enumerate(_from_saved(chat["messages"])):
        _render(entry, f"saved_{chat_id}_{n}")


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

if st.session_state.get("ask_viewing"):
    _view_saved(st.session_state["ask_viewing"])
    st.stop()

if "ask_agent" not in st.session_state:
    _new_chat()
agent = st.session_state["ask_agent"]
history: list[dict] = st.session_state["ask_history"]

# The question first (typed, or a starter tapped on the last run), so the rest
# of the page is drawn knowing a chat is under way.
question = st.chat_input("Ask about your money…") or st.session_state.pop("ask_pending", None)

intro, save, reset = st.columns([4, 1, 1])
intro.caption("Ask anything about your spending, income, savings or subscriptions. Answers come from your own "
              "data, through a read-only login, and ignore the sidebar's filters — say the dates in the question. "
              "Nothing is kept unless you save the chat.")
if (history or question) and reset.button("New chat", width="stretch"):
    _new_chat()
    st.rerun()
# Shown whenever a chat is under way -- including on the run that answers its
# first question, so the button is there as soon as the answer is.
if (history or question) and save.button("Save chat", width="stretch", key="ask_save"):
    st.session_state["ask_saving"] = True
if st.session_state.get("ask_saving") and history:
    with st.form("ask_save_form"):
        first = next((e["text"] for e in history if e["role"] == "user"), "Chat")
        title = st.text_input("Save as", value=first[:80])
        keep, cancel = st.columns(2)
        if keep.form_submit_button("Save", type="primary", width="stretch"):
            messages = _to_saved(history)
            if not any(m["role"] == "assistant" for m in messages):
                st.warning("There's no answer in this chat yet to save.")
            else:
                save_chat(title, messages)
                st.session_state.pop("ask_saving", None)
                st.toast(f"Saved \"{title.strip()[:80] or 'Untitled chat'}\" — find it under Saved chats")
                st.rerun()
        if cancel.form_submit_button("Cancel", width="stretch"):
            st.session_state.pop("ask_saving", None)
            st.rerun()

_saved_chats_list()

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
