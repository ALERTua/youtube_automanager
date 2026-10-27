"""The language of the user interface as a stored setting of the service, and the language button of the header.

The language is one setting of the service, as the color theme: one person uses the pages, so each browser shows the
same choice. This module imports no NiceGUI, so the start of the service applies the stored language before the first
page. Each function blocks.
"""

import logging

from sqlalchemy import delete
from sqlalchemy.orm import Session, sessionmaker

from youtube_automanager.db.models import Setting
from youtube_automanager.i18n import ENGLISH, LANGUAGES

LANGUAGE_KEY = "ui_language"

LOG = logging.getLogger(__name__)

# The button of the header goes through the languages in the order of LANGUAGES.
NEXT_LANGUAGE = {code: LANGUAGES[(index + 1) % len(LANGUAGES)] for index, code in enumerate(LANGUAGES)}
# The name of each language in that language. The button names the language of its click with it, as it is, so the
# name gets no translation.
NAMES = {"en": "English", "uk": "Українська"}


def read_language(sessions: sessionmaker[Session]) -> str:
    """Read the stored language. Without a stored value, or with a value that is not a language, the language is
    English."""
    with sessions() as session:
        row = session.get(Setting, LANGUAGE_KEY)
        if row is None:
            return ENGLISH
        value = row.value
    if isinstance(value, str) and value in LANGUAGES:
        return value
    LOG.warning("The stored language %r is not one of %s, so the pages are in English", value, ", ".join(LANGUAGES))
    return ENGLISH


def save_language(sessions: sessionmaker[Session], code: str) -> None:
    """Store the language ``code`` as a new row, so a value of another JSON type always goes into the database."""
    if code not in LANGUAGES:
        raise ValueError(f"The language {code!r} is not one of {', '.join(LANGUAGES)}")
    with sessions.begin() as session:
        session.execute(delete(Setting).where(Setting.key == LANGUAGE_KEY))
        session.add(Setting(key=LANGUAGE_KEY, value=code))
