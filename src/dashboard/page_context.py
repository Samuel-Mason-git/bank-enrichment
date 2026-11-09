"""How the sidebar's data reaches the pages. dashboard.py loads and filters the
transactions on every run, before the open page runs, and leaves them here; a
page asks for the ones it uses: `filtered, = shared("filtered")`.

Kept in the session, not in a module global, so two browser tabs with
different filters never see each other's data."""
import streamlit as st

_KEY = "_page_context"


def share(**values) -> None:
    st.session_state[_KEY] = values


def shared(*names: str) -> tuple:
    values = st.session_state[_KEY]
    return tuple(values[name] for name in names)
