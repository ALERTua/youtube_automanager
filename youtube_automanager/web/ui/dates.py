"""The day of today in the time zone of the schedule, and the button that puts it into a date field.

A date field of the pages is the native date input of the browser, ``ui.input`` with the prop ``type=date``. Its value
is a text in the form 2026-10-05.
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from nicegui import ui

from youtube_automanager.i18n import _

# The first day of an apply, of its preview and of the older videos of a new rule is this many days back by default.
DEFAULT_DAYS_BACK = 7


def today_in(timezone: str) -> date:
    """Return the day of today in the time zone ``timezone``."""
    return datetime.now(ZoneInfo(timezone)).date()


def default_first_day(timezone: str) -> date:
    """Return the day DEFAULT_DAYS_BACK days before today in the time zone ``timezone``."""
    return today_in(timezone) - timedelta(days=DEFAULT_DAYS_BACK)


def today_button(field: ui.input, timezone: str) -> ui.button:
    """Add a button next to the date field ``field`` that sets the field to today in the time zone ``timezone``.

    The change of the value calls the change handlers of the field, as a choice in the field does.
    """
    return ui.button(_("Today"), on_click=lambda: field.set_value(today_in(timezone).isoformat())).props(
        "flat dense no-caps"
    )
