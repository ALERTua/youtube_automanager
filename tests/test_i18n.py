from pathlib import Path

from babel.messages.frontend import CommandLineInterface
from babel.messages.pofile import read_po

from youtube_automanager.i18n import N_, _

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
