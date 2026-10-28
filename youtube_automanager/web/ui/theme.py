"""The color theme of the web pages: as the system of the device, dark or light.

The theme is one setting of the service, so each browser shows the same choice. The default follows the system of the
device, so a phone in dark mode gets dark pages. This module imports no NiceGUI, so its functions have tests of their
own. Each function blocks.
"""

import enum

from sqlalchemy import delete
from sqlalchemy.orm import Session, sessionmaker

from youtube_automanager.db.models import Setting
from youtube_automanager.i18n import N_

THEME_KEY = "ui_theme"


class Theme(enum.StrEnum):
    AUTO = "auto"
    DARK = "dark"
    LIGHT = "light"


# The value of the dark mode of Quasar for each theme: None follows the system of the device.
DARK_MODE: dict[Theme, bool | None] = {Theme.AUTO: None, Theme.DARK: True, Theme.LIGHT: False}
# The button of the header goes through the themes in this order.
NEXT_THEME = {Theme.AUTO: Theme.DARK, Theme.DARK: Theme.LIGHT, Theme.LIGHT: Theme.AUTO}
ICONS = {Theme.AUTO: "brightness_auto", Theme.DARK: "dark_mode", Theme.LIGHT: "light_mode"}
LABELS = {
    Theme.AUTO: N_("Theme: as the system of the device. Click for the dark theme."),
    Theme.DARK: N_("Theme: dark. Click for the light theme."),
    Theme.LIGHT: N_("Theme: light. Click for the theme of the system."),
}
# The background of a page of Quasar in the dark mode.
DARK_PAGE_COLOR = "#121212"
# Quasar gives the body its classes when the page mounts, so this rule acts only until then.
_FIRST_PAINT_RULE = f"body:not([class]) {{ background-color: {DARK_PAGE_COLOR}; }}"


def first_paint_style(theme: Theme) -> str:
    """Return the style for the head of a page that paints the page dark before its scripts run, or an empty text.

    The first HTML of a page names no color scheme, so the browser paints it light, and the dark mode of the page
    comes only when its scripts mount. A load of each page thus showed a light screen first. The dark theme gets the
    dark background at once, the theme of the system gets it when the system is dark, and the light theme needs none.
    """
    if theme is Theme.DARK:
        return f"<style>{_FIRST_PAINT_RULE}</style>"
    if theme is Theme.AUTO:
        return f"<style>@media (prefers-color-scheme: dark) {{ {_FIRST_PAINT_RULE} }}</style>"
    return ""


def read_theme(sessions: sessionmaker[Session]) -> Theme:
    """Read the stored theme. Without a stored value, or with a value that is not a theme, the theme is AUTO."""
    with sessions() as session:
        row = session.get(Setting, THEME_KEY)
        value = None if row is None else row.value
    try:
        return Theme(value)
    except ValueError:
        return Theme.AUTO


def save_theme(sessions: sessionmaker[Session], theme: Theme) -> None:
    """Store the theme as a new row, so a value of another JSON type always goes into the database."""
    with sessions.begin() as session:
        session.execute(delete(Setting).where(Setting.key == THEME_KEY))
        session.add(Setting(key=THEME_KEY, value=Theme(theme).value))
