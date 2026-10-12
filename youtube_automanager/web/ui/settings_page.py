"""The settings page: the YouTube cookies, the cookie rotation, the processing mode, yt-dlp and the notifications.

The web UI has no login, so anybody who can open the page can read it. The page therefore never shows a cookie value
or a stored notification URL. No log line of the page holds a cookie value or a target URL. The block of yt-dlp is in
the module ``ytdlp_section``. The block of the notification targets is in the module ``notifications_section``, and
the page shows it last.

The session check runs in a child process of the runner, and a test notification can take up to a minute, so the page
waits for both in a thread. The page reads and writes the database in a thread, never on the event loop. Each time
shows in the time zone of the schedule.
"""

import asyncio
import logging
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from nicegui import events, ui

from youtube_automanager.db.models import SessionState
from youtube_automanager.engine.modes import Processing, ProcessingMode
from youtube_automanager.i18n import N_, _
from youtube_automanager.rotation import INTERVAL_S
from youtube_automanager.runner import RunAlreadyActive, SessionCheckOutcome, SessionCheckResult
from youtube_automanager.short_child import ChildBusy
from youtube_automanager.web.services import Services
from youtube_automanager.web.ui.account_lists import child_error_text
from youtube_automanager.web.ui.cookie_upload import (
    COMPOSE_KEY_COMMAND,
    KEY_COMMAND,
    MAX_FILE_BYTES,
    YOUTUBE_DOMAIN,
    missing_login_cookies,
    normalized,
    store_upload,
)
from youtube_automanager.web.ui.dashboard import SESSION_STATES, StoredState, format_time, read_stored_state
from youtube_automanager.web.ui.layout import register_page
from youtube_automanager.web.ui.notifications_section import notifications_section
from youtube_automanager.web.ui.service_settings import (
    DEFAULT_NEWEST_COUNT,
    ENTRIES_PER_REQUEST,
    MODE_LABELS,
    mode_help,
    read_processing,
    read_rotation,
    save_processing,
    save_rotation,
)
from youtube_automanager.web.ui.ytdlp_section import ytdlp_section
from youtube_automanager.youtube.cookie_text import LOGIN_COOKIES
from youtube_automanager.youtube.session import CookieCipher, CookieKeyError

LOG = logging.getLogger(__name__)

# The steps of a cookie export that keeps the session alive for the service.
EXPORT_STEPS = (
    N_(
        "Use Firefox. Chrome and other Chromium browsers can give cookies that stop to work a few hours after the "
        "export."
    ),
    N_(
        "Close each private window of Firefox. All private windows share one session, so only a new private window "
        "after that gives a new session."
    ),
    N_("Open a new private window, and sign in to YouTube there with the account that the service uses."),
    N_(
        "In the same window, open https://www.youtube.com/robots.txt in a new tab, and close the YouTube tab, so that "
        "this page is the only tab of the window."
    ),
    N_(
        "Export the cookies of youtube.com as a cookies.txt file in the Netscape format, with a cookie export add-on "
        "that has the permission to run in private windows."
    ),
    N_(
        "Close the private window without a sign-out. A sign-out ends the session of the exported cookies, and a "
        "YouTube tab that stays open can end it within about 20 minutes."
    ),
    N_("Upload the file here, or paste its text."),
)


def _cipher(services: Services) -> tuple[CookieCipher | None, str | None]:
    """Return the cipher of COOKIES_KEY, or None and the cause when the key is not valid.

    Return None and no cause when the key is not set. The cause never repeats the key.
    """
    key = services.settings.cookies_key
    if key is None:
        return None, None
    try:
        return CookieCipher(key.get_secret_value()), None
    except CookieKeyError as error:
        return None, str(error)


def check_text(result: SessionCheckResult) -> tuple[str, bool]:
    """Return the text of a session check, and whether the text tells of a problem."""
    if result.outcome is SessionCheckOutcome.BUSY:
        return child_error_text(ChildBusy(result.detail or "")), True
    if result.outcome is SessionCheckOutcome.UNAVAILABLE:
        # Translators: {cause} is the English cause from the service, for example that no cookies are stored.
        return _("The session check cannot use the stored cookies: {cause}").format(cause=result.detail or ""), True
    if result.outcome is SessionCheckOutcome.FAILED:
        return _("The session check failed: {cause}").format(cause=result.detail or ""), True
    if result.session_state is SessionState.DEAD:
        return _("YouTube does not accept the cookies. Export new cookies and upload them."), True
    if result.session_state is SessionState.EXPIRING:
        return _("YouTube accepts the cookies, but they expire soon. Export new cookies and upload them."), False
    return _("YouTube accepts the cookies."), False


def missing_text(missing: tuple[str, ...]) -> str:
    """Return the text of a cookie text that lacks the login cookies ``missing``. A cookie name is no secret."""
    if set(missing) == LOGIN_COOKIES:
        return _(
            "The text holds no login cookie of {domain}. Export the cookies of youtube.com while you are signed in to "
            "YouTube."
        ).format(domain=YOUTUBE_DOMAIN)
    return _(
        "The text lacks these login cookies of {domain}: {names}. Export the cookies again while you are signed in to "
        "YouTube."
    ).format(domain=YOUTUBE_DOMAIN, names=", ".join(missing))


# The YouTube cookies.


@dataclass
class CookieState:
    stored: StoredState
    checking: bool = False
    storing: bool = False


def _key_help(cause: str | None) -> None:
    """Tell how to set COOKIES_KEY. ``cause`` is None when the key is not set, else why the key is not valid."""
    with ui.column().classes("w-full gap-1").mark("cookie-key-help"):
        if cause is None:
            ui.label(_("The service cannot store cookies, because COOKIES_KEY is not set.")).classes(
                "text-negative"
            ).mark("cookie-key-missing")
        else:
            # Translators: {cause} is the English cause from the service. It never holds the key.
            text = _("The service cannot store cookies, because COOKIES_KEY is not valid: {cause}").format(cause=cause)
            ui.label(text).classes("text-negative").mark("cookie-key-invalid")
        ui.label(
            _("Make a key with this command, and give it to the service as COOKIES_KEY, for example in the file .env:")
        )
        ui.label(KEY_COMMAND).classes("font-mono text-sm select-all break-all").mark("cookie-key-command")
        ui.label(_("With Docker Compose, run the command in the image of the service:"))
        ui.label(COMPOSE_KEY_COMMAND).classes("font-mono text-sm select-all break-all").mark("cookie-key-compose")
        ui.label(
            _(
                "Then restart the service. Keep the key: with another key, the service cannot read the stored cookies, "
                "and you upload them again."
            )
        )


async def _cookies_section(services: Services) -> None:
    cipher, key_error = _cipher(services)
    state = CookieState(await asyncio.to_thread(read_stored_state, services.sessions, services.settings.tz))

    def show_result(text: str, *, problem: bool = False) -> None:
        result_label.classes(remove="text-negative")
        if problem:
            result_label.classes(add="text-negative")
        result_label.set_text(text)

    def show_work() -> None:
        """Turn the controls on and off for the work of this moment."""
        working = state.checking or state.storing
        check_button.set_enabled(not working and state.stored.session is not None)
        if state.checking:
            check_button.props("loading")
        else:
            check_button.props(remove="loading")
        can_store = cipher is not None and not working
        for element in (upload, paste_input, paste_button):
            element.set_enabled(can_store)

    async def reload() -> None:
        state.stored = await asyncio.to_thread(read_stored_state, services.sessions, services.settings.tz)
        # The await rebuilds the view now, and not at the next turn of the event loop.
        await session_view.refresh()
        show_work()

    async def check_session() -> None:
        if state.checking or state.storing:
            return
        state.checking = True
        show_work()
        show_result(_("The service checks the session with YouTube. This takes up to a minute."))
        try:
            result = await asyncio.to_thread(services.runner.check_session)
        finally:
            state.checking = False
            show_work()
        if result.outcome is not SessionCheckOutcome.CHECKED:
            LOG.warning("The session check of the settings page gave %s", result.outcome.value)
        text, problem = check_text(result)
        # The text and the new state show together.
        await reload()
        show_result(text, problem=problem)

    async def store(text: str) -> bool:
        """Check the cookie text, and store it when it is good. Return True when the text is stored."""
        if cipher is None or state.checking or state.storing:
            return False
        text = normalized(text)
        if not text:
            show_result(_("The text is empty. Upload the cookie file, or paste its text."), problem=True)
            return False
        try:
            missing = missing_login_cookies(text)
        except ValueError as error:
            # The cause names only a line number, never a cookie value.
            show_result(_("The text is not a Netscape cookie file: {cause}.").format(cause=error), problem=True)
            return False
        if missing:
            show_result(missing_text(missing), problem=True)
            return False
        state.storing = True
        show_work()
        try:
            await asyncio.to_thread(store_upload, services.sessions, services.runner, cipher, text)
        except RunAlreadyActive:
            show_result(child_error_text(ChildBusy("")), problem=True)
            return False
        finally:
            state.storing = False
            show_work()
        LOG.info("Stored new YouTube cookies from the settings page")
        ui.notify(_("The cookies are stored."), type="positive")
        await reload()
        return True

    async def receive_file(event: events.UploadEventArguments) -> None:
        upload.reset()
        file = event.file
        if file.size() > MAX_FILE_BYTES:
            file_refused()
            return
        try:
            text = (await file.read()).decode("utf-8-sig")
        except UnicodeDecodeError:
            show_result(_("The file is no text in UTF-8, so it is no cookie file."), problem=True)
            return
        if await store(text):
            await check_session()

    def file_refused() -> None:
        show_result(_("The file is larger than 1 MB, so it is no cookie file of YouTube."), problem=True)

    async def store_pasted() -> None:
        # The field holds cookie values, so it empties at each try, and no cookie value stays on the page.
        text = paste_input.value or ""
        paste_input.value = ""
        if await store(text):
            await check_session()

    @ui.refreshable
    def session_view() -> None:
        zone = ZoneInfo(state.stored.timezone)
        cookie_session = state.stored.session
        if cookie_session is None:
            ui.label(_("No cookies are stored yet.")).mark("no-cookies")
            return
        label, color = SESSION_STATES[cookie_session.state]
        with ui.grid(columns="auto 1fr").classes("gap-x-4 gap-y-1 items-center"):
            ui.label(_("State")).classes("text-grey-7")
            with ui.row():
                ui.badge(_(label), color=color).mark("session-state")
            ui.label(_("Last check")).classes("text-grey-7")
            checked = cookie_session.last_checked_at
            ui.label(format_time(checked, zone) if checked else _("Never")).mark("session-checked")
            ui.label(_("Earliest cookie expiry")).classes("text-grey-7")
            expiry = cookie_session.earliest_cookie_expiry
            ui.label(format_time(expiry, zone) if expiry else _("No expiry date")).mark("session-expiry")

    with ui.card().classes("w-full").mark("cookies-card"):
        ui.label(_("YouTube cookies")).classes("text-h6")
        ui.label(
            _(
                "The service reads the subscriptions and changes the playlists of your account with the cookies of a "
                "YouTube session. The page never shows a cookie value."
            )
        ).classes("text-grey-7")
        session_view()
        check_button = ui.button(_("Check the session"), icon="verified_user", on_click=check_session).mark(
            "check-session"
        )
        result_label = ui.label().classes("whitespace-pre-wrap").mark("cookie-result")
        if cipher is None:
            _key_help(key_error)
        with ui.expansion(_("How to export the cookies"), icon="help_outline").classes("w-full").mark("export-help"):
            for number, step in enumerate(EXPORT_STEPS, start=1):
                # Translators: one step of a numbered list, for example "1. Use Firefox."
                ui.label(_("{number}. {step}").format(number=number, step=_(step))).mark("export-step")
        upload = (
            ui.upload(
                label=_("Cookie file (cookies.txt)"),
                auto_upload=True,
                max_files=1,
                max_file_size=MAX_FILE_BYTES,
                on_upload=receive_file,
                on_rejected=file_refused,
            )
            .classes("w-full max-w-md")
            .mark("cookie-upload")
        )
        paste_input = (
            ui.textarea(_("Or paste the text of the cookie file"))
            .props("outlined")
            .classes("w-full font-mono")
            .mark("cookie-text")
        )
        paste_button = ui.button(_("Store the pasted cookies"), icon="save", on_click=store_pasted).mark("cookie-store")
    show_work()


# The cookie rotation.


async def _rotation_section(services: Services, writes: asyncio.Lock) -> None:
    enabled = await asyncio.to_thread(read_rotation, services.sessions)

    async def change(event: events.ValueChangeEventArguments) -> None:
        on = bool(event.value)
        async with writes:
            await asyncio.to_thread(save_rotation, services.sessions, on)
        LOG.info("The cookie rotation is now %s", "on" if on else "off")
        ui.notify(_("The cookie rotation is on.") if on else _("The cookie rotation is off."), type="positive")

    with ui.card().classes("w-full").mark("rotation-card"):
        ui.label(_("Cookie rotation")).classes("text-h6")
        ui.switch(_("Rotate the cookie session"), value=enabled, on_change=change).mark("rotation")
        ui.label(
            _(
                "The rotation renews the session of the stored cookies about each {minutes} minutes, the way an open "
                "YouTube tab does. It is off by default, and this default can change after a measurement that still "
                "runs. A change acts at the next turn of the rotation, without a restart."
            ).format(minutes=round(INTERVAL_S / 60))
        ).classes("text-grey-7").mark("rotation-help")


# The processing mode.


@dataclass
class ModeState:
    # None when the stored value is not valid.
    stored: Processing | None


async def _processing_section(services: Services, writes: asyncio.Lock) -> None:
    stored = await asyncio.to_thread(read_processing, services.sessions)
    state = ModeState(stored.processing)
    shown = stored.processing or Processing()

    def read_form() -> Processing:
        """Return the mode of the form. Raise ValueError with a text for the user when the form cannot be saved."""
        mode = ProcessingMode(mode_radio.value)
        if mode is not ProcessingMode.NEWEST:
            return Processing(mode)
        count = count_input.value
        # ``ui.number`` gives a float, and its limits act only in the browser.
        if count is None or not float(count).is_integer() or count < 1:
            raise ValueError(_("Enter a whole number of videos, 1 or more."))
        return Processing(mode, int(count))

    def show_error(text: str | None) -> None:
        error_label.set_text(text or "")
        error_label.set_visibility(bool(text))

    async def save() -> None:
        try:
            processing = read_form()
        except ValueError as error:
            show_error(str(error))
            return
        show_error(None)
        if processing == state.stored:
            ui.notify(_("The processing mode did not change."))
            return
        if not await confirm_dialog:
            return
        async with writes:
            await asyncio.to_thread(save_processing, services.sessions, processing)
        state.stored = processing
        invalid_label.set_visibility(False)
        LOG.info("The processing mode is now %s", processing.describe())
        ui.notify(_("The processing mode is saved. The next run uses it."), type="positive")

    with ui.card().classes("w-full").mark("mode-card"):
        ui.label(_("Processing mode")).classes("text-h6")
        ui.label(_("How far each run reads the videos of each channel and of the subscriptions feed.")).classes(
            "text-grey-7"
        )
        invalid_label = (
            ui.label(
                # Translators: {cause} is the English cause from the service.
                _(
                    "The stored mode is not valid, so each run stops with an error: {cause}. Choose a mode and save it."
                ).format(cause=stored.error or "")
            )
            .classes("text-negative")
            .mark("mode-invalid")
        )
        invalid_label.set_visibility(stored.processing is None)
        mode_radio = ui.radio({mode: _(label) for mode, label in MODE_LABELS.items()}, value=shown.mode).mark("mode")
        ui.label().classes("text-grey-7").mark("mode-help").bind_text_from(
            mode_radio, "value", lambda mode: mode_help(ProcessingMode(mode))
        )
        count_input = (
            ui.number(
                _("Number of the newest videos"),
                value=shown.count or DEFAULT_NEWEST_COUNT,
                min=1,
                step=1,
                precision=0,
            )
            .props("outlined dense")
            .classes("w-64")
            .mark("mode-count")
        )
        count_input.bind_visibility_from(mode_radio, "value", lambda mode: mode == ProcessingMode.NEWEST)
        ui.label(
            _("Each {entries} videos cost one more request to YouTube for each source.").format(
                entries=ENTRIES_PER_REQUEST
            )
        ).classes("text-grey-7").mark("mode-count-hint").bind_visibility_from(
            mode_radio, "value", lambda mode: mode == ProcessingMode.NEWEST
        )
        error_label = ui.label().classes("text-negative").mark("mode-error")
        error_label.set_visibility(False)
        ui.button(_("Save"), icon="save", on_click=save).mark("mode-save")

    with ui.dialog().mark("mode-confirm-dialog") as confirm_dialog, ui.card().classes("max-w-lg"):
        ui.label(_("Change the processing mode?")).classes("text-h6")
        ui.label(
            _(
                "A new mode starts each source again at the next run. That run stores the current videos of each "
                "source as seen and adds none of them, so the videos that came since the last run are not checked."
            )
        ).mark("mode-confirm-text")
        with ui.row().classes("gap-2"):
            ui.button(_("Change the mode"), on_click=lambda: confirm_dialog.submit(True)).mark("mode-confirm")
            ui.button(_("Cancel"), on_click=lambda: confirm_dialog.submit(False)).props("flat").mark("mode-cancel")


@register_page("/settings", label=N_("Settings"), icon="settings")
async def settings_page(services: Services) -> None:
    # One write of the page at a time, so a later change never loses to an earlier one that ends later.
    writes = asyncio.Lock()
    ui.label(_("Settings")).classes("text-h5")
    await _cookies_section(services)
    await _rotation_section(services, writes)
    await _processing_section(services, writes)
    await ytdlp_section(services, writes)
    await notifications_section(services, writes)
