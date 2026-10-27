from pathlib import Path

import pytest
from babel.messages.frontend import CommandLineInterface
from babel.messages.pofile import read_po
from ui_support import store

from youtube_automanager import i18n
from youtube_automanager.db.models import Setting
from youtube_automanager.i18n import N_, _, language, set_language
from youtube_automanager.i18n.setting import LANGUAGE_KEY, NAMES, NEXT_LANGUAGE, read_language, save_language

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "youtube_automanager" / "i18n" / "messages.pot"
# The same options as the command in the [tool.babel] comment of pyproject.toml.
EXTRACT_OPTIONS = ["-F", "pyproject.toml", "--add-location=file", "--sort-output", "--add-comments=Translators:"]


def _messages(path: Path) -> dict:
    """Return each message of the catalog with its locations, flags and comments. The header stays out, because it
    holds the creation date, the year and the version of Babel."""
    with path.open("rb") as file:
        catalog = read_po(file)
    return {
        (message.context, message.id if isinstance(message.id, str) else tuple(message.id)): (
            sorted(message.locations),
            sorted(message.flags),
            message.auto_comments,
        )
        for message in catalog
        if message.id
    }


def test_template_holds_each_translated_text_of_the_code(tmp_path, monkeypatch):
    fresh = tmp_path / "messages.pot"
    # A relative input folder gives locations relative to the repository root, as in the committed template.
    monkeypatch.chdir(ROOT)
    CommandLineInterface().run(["pybabel", "-q", "extract", *EXTRACT_OPTIONS, "-o", str(fresh), "youtube_automanager"])

    fresh_messages = _messages(fresh)
    assert fresh_messages, "The extraction found no message"
    assert _messages(TEMPLATE) == fresh_messages, (
        "messages.pot is out of date. In the repository root, run: uv run pybabel extract "
        + " ".join(EXTRACT_OPTIONS)
        + " -o youtube_automanager/i18n/messages.pot youtube_automanager"
    )


def test_english_text_is_the_translation_without_a_catalog():
    assert _("Dashboard") == "Dashboard"
    assert N_("Dashboard") == "Dashboard"


def test_a_language_with_a_catalog_translates_each_later_text(uk_catalog):
    assert set_language("uk") == "uk"

    assert language() == "uk"
    assert _("Dashboard") == "Панель"
    # A text without a translation in the catalog stays English.
    assert _("Scheduler") == "Scheduler"
    # N_ only marks a text, and the translation comes at the show.
    assert _(N_("History")) == "Історія"


def test_english_needs_no_catalog_and_ends_a_translation(uk_catalog):
    set_language("uk")

    assert set_language("en") == "en"

    assert language() == "en"
    assert _("Dashboard") == "Dashboard"


def test_an_unknown_language_gives_english(uk_catalog, caplog):
    set_language("uk")

    assert set_language("xx") == "en"

    assert language() == "en"
    assert _("Dashboard") == "Dashboard"
    assert "'xx' is not one of en, uk" in caplog.text


def test_a_language_without_a_catalog_keeps_its_code_and_shows_english(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(i18n, "LOCALE_DIR", tmp_path / "empty")

    assert set_language("uk") == "uk"

    assert language() == "uk"
    assert _("Dashboard") == "Dashboard"
    assert "The language uk has no compiled catalog" in caplog.text


# The two tests run in this order, and the second one proves that the language of the first one did not stay.
def test_the_language_of_a_test_changes_the_holder(uk_catalog):
    set_language("uk")

    assert _("Dashboard") == "Панель"


def test_each_test_starts_in_english():
    assert language() == "en"
    assert _("Dashboard") == "Dashboard"


def test_without_a_stored_language_the_language_is_english(sessions):
    assert read_language(sessions) == "en"


def test_the_stored_language_comes_back(sessions):
    save_language(sessions, "uk")
    save_language(sessions, "uk")

    assert read_language(sessions) == "uk"
    with sessions() as session:
        assert session.get(Setting, LANGUAGE_KEY).value == "uk"


@pytest.mark.parametrize("value", ["xx", 5, None, ["uk"]])
def test_a_stored_value_that_is_not_a_language_gives_english(sessions, value):
    store(sessions, Setting(key=LANGUAGE_KEY, value=value))

    assert read_language(sessions) == "en"


def test_a_save_refuses_an_unknown_language(sessions):
    with pytest.raises(ValueError, match="'xx' is not one of en, uk"):
        save_language(sessions, "xx")

    assert read_language(sessions) == "en"


def test_the_button_goes_through_each_language_and_names_it_in_that_language():
    assert NEXT_LANGUAGE == {"en": "uk", "uk": "en"}
    assert NAMES == {"en": "English", "uk": "Українська"}
