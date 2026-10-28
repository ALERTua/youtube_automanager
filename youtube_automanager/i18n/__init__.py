"""The translation of the texts of the user interface, with ``gettext`` catalogs.

The English text is the message id, so English needs no catalog: without a compiled catalog of the language, each
function returns the English text. ``messages.pot`` next to this module holds each message of the code, and a test
fails when it is out of date. The ``[tool.babel]`` table of ``pyproject.toml`` tells how to extract it again.
The Ukrainian catalog is ``locale/uk/LC_MESSAGES/messages.po`` in git, and a test fails on a message that it lacks,
leaves untranslated or marks fuzzy. ``just mo`` and the image build compile it into the ``messages.mo`` that
``set_language`` loads, and git ignores that file.

The language is one setting of the service, for the whole process. ``set_language`` puts the catalog of a language
into this module, and ``_`` reads it at each call, so each text that the code makes after the change is in the new
language. A text that the code keeps in a constant, for example the label of a page in the navigation, therefore gets
the mark ``N_`` and goes through ``_`` when the page shows it. Then the text shows in the language of that moment, not
of the import.
"""

import gettext
import logging
from pathlib import Path

DOMAIN = "messages"
# A language gets its compiled catalog at locale/<language>/LC_MESSAGES/messages.mo.
LOCALE_DIR = Path(__file__).resolve().parent / "locale"
ENGLISH = "en"
# The languages of the user interface, in the order of the language button of the header.
LANGUAGES = (ENGLISH, "uk")

LOG = logging.getLogger(__name__)

# The language of the texts and its catalog. English is the message id, so it needs no catalog.
_language = ENGLISH
_translations: gettext.NullTranslations = gettext.NullTranslations()


def set_language(code: str) -> str:
    """Show each text in the language ``code`` from now on, and return the code of the language that applies.

    An unknown code gives English. A known language without a compiled catalog keeps its code, so the setting and the
    language of the Quasar components stay as the user chose them, but its texts are English, the message ids.
    """
    # One language serves the whole process.
    global _language, _translations
    if code not in LANGUAGES:
        LOG.warning("The language %r is not one of %s, so the texts are in English", code, ", ".join(LANGUAGES))
        code = ENGLISH
    translations = gettext.NullTranslations()
    if code != ENGLISH:
        translations = gettext.translation(DOMAIN, LOCALE_DIR, languages=[code], fallback=True)
        if type(translations) is gettext.NullTranslations:
            LOG.warning("The language %s has no compiled catalog in %s, so its texts are in English", code, LOCALE_DIR)
    _language, _translations = code, translations
    return code


def language() -> str:
    """Return the code of the language of the texts, for example "en"."""
    return _language


def _(message: str) -> str:
    """Return the translation of ``message`` in the language of the moment."""
    return _translations.gettext(message)


def N_(message: str) -> str:
    """Mark ``message`` for the extraction, and return it without a translation."""
    return message
