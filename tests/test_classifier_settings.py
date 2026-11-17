"""The classifier's model: the Settings page's Classification tab sets the model for
passes 1 to 3. The match against existing categories and the placement judge stay on Haiku."""
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import classifier_settings as CLS
import llm_labelling as ll
from database_functions import get_setting

_DASHBOARD = str(Path(__file__).parent.parent / "src" / "dashboard" / "dashboard.py")


def test_the_default_is_the_model_the_code_always_used(db):
    assert CLS.get(CLS.MODEL) == "claude-sonnet-4-6"
    assert ll.job("classify_parents")["model"] == "claude-sonnet-4-6"


def test_the_classifying_passes_follow_the_setting(db):
    CLS.save(CLS.MODEL, "claude-sonnet-5-5")
    for name in ll.CLASSIFY_PASSES:
        assert ll.job(name)["model"] == "claude-sonnet-5-5", name


def test_match_and_the_judge_stay_on_haiku(db):
    CLS.save(CLS.MODEL, "claude-sonnet-5-5")
    assert ll.job("match_existing")["model"] == "claude-haiku-5-5"
    assert ll.JOBS["placement_judge"]["model"] == "claude-haiku-5-5"


def test_an_unknown_model_is_refused(db):
    with pytest.raises(ValueError):
        CLS.save(CLS.MODEL, "gpt-whatever")


def test_the_classification_tab_saves_the_model(db):
    at = AppTest.from_file(_DASHBOARD, default_timeout=30)
    at.switch_page("views/settings.py").run()
    assert not at.exception
    at.selectbox(key="classifier_model").set_value("claude-haiku-5-5").run()
    at.button(key="classifier_save").click().run()
    assert not at.exception
    assert get_setting("classifier_model") == "claude-haiku-5-5"
