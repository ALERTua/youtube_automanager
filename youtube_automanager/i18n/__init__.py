"""The translation of the texts of the user interface, with ``gettext`` catalogs.

The English text is the message id, so English needs no catalog: without a compiled catalog of the language, each
function returns the English text. ``messages.pot`` next to this module holds each message of the code, and a test
fails when it is out of date. The ``[tool.babel]`` table of ``pyproject.toml`` tells how to extract it again.

A text that the code keeps in a constant, for example the label of a page in the navigation, gets the mark ``N_`` and
goes through ``_`` when the page shows it. Then the text shows in the language of that moment, not of the import.
"""

import gettext
from pathlib import Path

DOMAIN = "messages"
# A language gets its compiled catalog at locale/<language>/LC_MESSAGES/messages.mo.
LOCALE_DIR = Path(__file__).resolve().parent / "locale"
LANGUAGES = ["en"]

_translations = gettext.translation(DOMAIN, LOCALE_DIR, languages=LANGUAGES, fallback=True)


def _(message: str) -> str:
    """Return the translation of ``message``."""
    return _translations.gettext(message)


def N_(message: str) -> str:
    """Mark ``message`` for the extraction, and return it without a translation."""
    return message
