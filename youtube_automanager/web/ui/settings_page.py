"""The settings page: the YouTube cookies, the cookie rotation, the processing mode, yt-dlp and the notifications.

The web UI has no password by default, so anybody who can open the page can read it. ``UI_PASSWORD`` in ``.env`` adds
a password. The password goes over plain HTTP, so the deployer must reach the service through a VPN or a reverse proxy
with HTTPS. The page never shows a cookie value. It shows a stored notification URL only in the edit form of its
target, because the user needs to see the URL to change it. No log line of the page holds a cookie value or a target
URL. The block of yt-dlp is in the module ``ytdlp_section``. The block of the notification targets is in the module
``notifications_section``, and the page shows it last.

A control of a setting changes only the draft of the page, and one pair of floating buttons saves or discards each
change of the draft, see the module ``settings_draft``. The actions keep their own buttons and act at once: the cookie
upload, the session check, "Update now", the add, the change and the delete of a notification target, and its test.

The session check runs in a child process of the runner. An uploaded cookie text gets the same check in a child
process, which stores the text only when YouTube accepts it. A test notification can take up to a minute, so the page
waits for each of them in a thread. The page reads and writes the database in a thread, never on the event loop.
Each time shows in the time zone of the schedule.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from nicegui import events, ui

from youtube_automanager.cookie_upload_check import check_upload_in_child
from youtube_automanager.db.models import SessionState, utc_now
from youtube_automanager.engine.modes import Processing, ProcessingMode
from youtube_automanager.i18n import N_, _
from youtube_automanager.rotation import INTERVAL_S
from youtube_automanager.runner import SessionCheckOutcome, SessionCheckResult
from youtube_automanager.short_child import ChildBusy, ChildError, ChildLoggedOut, ChildPaused, ChildUnavailable
from youtube_automanager.web.services import Services
from youtube_automanager.web.ui.account_lists import child_error_text, child_pause, pause_text, show_pause_end
from youtube_automanager.web.ui.common import (
    SESSION_STATES,
    ResultLabel,
    format_time,
    set_loading,
    show_time,
    time_label,
)
from youtube_automanager.web.ui.cookie_upload import (
    COMPOSE_KEY_COMMAND,
    KEY_COMMAND,
    MAX_FILE_BYTES,
    YOUTUBE_DOMAIN,
    login_expired_at,
    missing_login_cookies,
    normalized,
)
from youtube_automanager.web.ui.dashboard import StoredState, read_stored_state
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
from youtube_automanager.web.ui.settings_draft import DraftPart, FormError, SettingsDraft, Write
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
    if result.pause is not None:
        # The text holds {time}, and the page lets the browser put the end of the pause into it.
        return _("The session check failed. {reason}").format(reason=pause_text(result.pause)), True
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


def expired_template() -> str:
    """Return the text of a cookie text whose login cookies expired, with ``{time}`` for the expiry time."""
    # Translators: {time} is the expiry time of the login cookies, for example "2026-10-12 18:30 EEST".
    return _("The login cookies of this export expired on {time}. Export new cookies and upload them.")


def expired_text(expiry: datetime, zone: ZoneInfo) -> str:
    """Return the text of a cookie text whose login cookies expired at ``expiry``, in the form of the service."""
    return expired_template().format(time=format_time(expiry, zone))


def upload_error_text(error: ChildError, *, had_cookies: bool) -> str:
    """Return the text of an upload that the service did not store. ``had_cookies`` tells whether cookies are stored.

    The text of a pause of the requests with cookies holds ``{time}`` for its end.
    """
    if isinstance(error, ChildBusy):
        return child_error_text(error)
    if isinstance(error, ChildPaused):
        return _("The service stored nothing. {reason}").format(reason=pause_text(error.pause))
    if isinstance(error, ChildLoggedOut):
        if had_cookies:
            return _(
                "YouTube does not accept these cookies. The service kept the stored cookies. Export new cookies and "
                "upload them."
            )
        return _(
            "YouTube does not accept these cookies, so the service did not store them. Export new cookies and upload "
            "them."
        )
    if isinstance(error, ChildUnavailable):
        # Translators: {cause} is the English cause from the service, for example that COOKIES_KEY is not set.
        return _("The service cannot use these cookies, so it stored nothing: {cause}").format(cause=error)
    # Translators: {cause} is the English cause from the service, for example an HTTP error of YouTube.
    return _(
        "The check with YouTube gave no clear answer, so the service stored nothing. Upload or paste the cookies "
        "again. The cause: {cause}"
    ).format(cause=error)


# The YouTube cookies.


@dataclass
class CookieState:
    stored: StoredState
    checking: bool = False
    # True while the check of an upload runs.
    uploading: bool = False


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
        if problem:
            result_label.problem(text)
        else:
            result_label.ok(text)

    def show_work() -> None:
        """Turn the controls on and off for the work of this moment."""
        working = state.checking or state.uploading
        check_button.set_enabled(not working and state.stored.session is not None)
        set_loading(check_button, state.checking)
        can_store = cipher is not None and not working
        for element in (upload, paste_input, paste_button):
            element.set_enabled(can_store)

    async def reload() -> None:
        state.stored = await asyncio.to_thread(read_stored_state, services.sessions, services.settings.tz)
        # The await rebuilds the view now, and not at the next turn of the event loop.
        await session_view.refresh()
        show_work()

    async def check_session() -> None:
        if state.checking or state.uploading:
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
        show_pause_end(result_label, text, result.pause)

    async def store(text: str) -> None:
        """Check the cookie text, first without a request and then with YouTube, which stores it when it works.

        A text that YouTube refuses, or a check without a clear answer, stores nothing, so the stored cookies stay.
        """
        if cipher is None or state.checking or state.uploading:
            return
        text = normalized(text)
        if not text:
            show_result(_("The text is empty. Upload the cookie file, or paste its text."), problem=True)
            return
        try:
            missing = missing_login_cookies(text)
        except ValueError as error:
            # The cause names only a line number, never a cookie value.
            show_result(_("The text is not a Netscape cookie file: {cause}.").format(cause=error), problem=True)
            return
        if missing:
            show_result(missing_text(missing), problem=True)
            return
        expired = login_expired_at(text, utc_now())
        if expired is not None:
            show_result(expired_text(expired, ZoneInfo(state.stored.timezone)), problem=True)
            # The browser puts the expiry time into the sentence in its own form.
            show_time(result_label, expired, ZoneInfo(state.stored.timezone), expired_template())
            return
        had_cookies = state.stored.session is not None
        state.uploading = True
        show_work()
        show_result(_("The service checks the cookies with YouTube. This takes up to a minute."))
        failure = None
        try:
            await asyncio.to_thread(check_upload_in_child, services.runner, text)
        except ChildError as error:
            failure = error
        finally:
            state.uploading = False
            show_work()
        # The text and the new state show together.
        await reload()
        if failure is not None:
            # The cause comes from the child, and it never holds a cookie value.
            LOG.warning("The settings page did not store the uploaded cookies: %s", failure)
            text = upload_error_text(failure, had_cookies=had_cookies)
            show_result(text, problem=True)
            show_pause_end(result_label, text, child_pause(failure))
            return
        LOG.info("The settings page stored new YouTube cookies, because YouTube accepted them")
        ui.notify(_("The cookies work and are stored."), type="positive")
        show_result(_("The cookies work and are stored."))

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
        await store(text)

    def file_refused() -> None:
        show_result(_("The file is larger than 1 MB, so it is no cookie file of YouTube."), problem=True)

    async def store_pasted() -> None:
        # The field holds cookie values, so it empties at each try, and no cookie value stays on the page.
        text = paste_input.value or ""
        paste_input.value = ""
        await store(text)

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
            (time_label(checked, zone) if checked else ui.label(_("Never"))).mark("session-checked")
            ui.label(_("Earliest cookie expiry")).classes("text-grey-7")
            expiry = cookie_session.earliest_cookie_expiry
            (time_label(expiry, zone) if expiry else ui.label(_("No expiry date"))).mark("session-expiry")

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
        result_label = ResultLabel().classes("whitespace-pre-wrap").mark("cookie-result")
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


@dataclass
class RotationState:
    stored: bool


async def _rotation_section(services: Services, draft: SettingsDraft) -> None:
    state = RotationState(await asyncio.to_thread(read_rotation, services.sessions))

    def prepare() -> Write:
        on = bool(switch.value)

        async def run() -> None:
            await asyncio.to_thread(save_rotation, services.sessions, on)
            state.stored = on
            LOG.info("The cookie rotation is now %s", "on" if on else "off")

        return Write(run)

    def discard() -> None:
        switch.value = state.stored

    with ui.card().classes("w-full").mark("rotation-card"):
        ui.label(_("Cookie rotation")).classes("text-h6")
        switch = ui.switch(_("Rotate the cookie session"), value=state.stored, on_change=lambda: draft.update()).mark(
            "rotation"
        )
        ui.label(
            _(
                "The rotation renews the session of the stored cookies about each {minutes} minutes, the way an open "
                "YouTube tab does. It is on by default. A saved change acts at the next turn of the rotation, "
                "without a restart."
            ).format(minutes=round(INTERVAL_S / 60))
        ).classes("text-grey-7").mark("rotation-help")
    draft.add(DraftPart(changed=lambda: bool(switch.value) != state.stored, prepare=prepare, discard=discard))


# The processing mode.


@dataclass
class ModeState:
    # None when the stored value is not valid.
    stored: Processing | None


async def _processing_section(services: Services, draft: SettingsDraft) -> None:
    stored = await asyncio.to_thread(read_processing, services.sessions)
    state = ModeState(stored.processing)
    shown = stored.processing or Processing()

    def read_form() -> Processing:
        """Return the mode of the form. Raise FormError when the form cannot be saved."""
        mode = ProcessingMode(mode_radio.value)
        if mode is not ProcessingMode.NEWEST:
            return Processing(mode)
        count = count_input.value
        # ``ui.number`` gives a float, and its limits act only in the browser.
        if count is None or not float(count).is_integer() or count < 1:
            raise FormError(_("Enter a whole number of videos, 1 or more."))
        return Processing(mode, int(count))

    def changed() -> bool:
        # A stored mode that is not valid always differs from the form, so the user can save a valid mode at once.
        try:
            return read_form() != state.stored
        except FormError:
            return True

    def show_error(text: str | None) -> None:
        error_label.set_text(text or "")
        error_label.set_visibility(bool(text))

    async def confirm() -> bool:
        return bool(await confirm_dialog)

    def prepare() -> Write:
        processing = read_form()

        async def run() -> None:
            await asyncio.to_thread(save_processing, services.sessions, processing)
            state.stored = processing
            invalid_label.set_visibility(False)
            LOG.info("The processing mode is now %s", processing.describe())

        return Write(run, confirm=confirm)

    def discard() -> None:
        shown = state.stored or Processing()
        mode_radio.value = shown.mode
        count_input.value = shown.count or DEFAULT_NEWEST_COUNT

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
        mode_radio = ui.radio(
            {mode: _(label) for mode, label in MODE_LABELS.items()}, value=shown.mode, on_change=lambda: draft.update()
        ).mark("mode")
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
                on_change=lambda: draft.update(),
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
    draft.add(DraftPart(changed=changed, prepare=prepare, discard=discard, show_error=show_error))


@register_page("/settings", label=N_("Settings"), icon="settings")
async def settings_page(services: Services) -> None:
    # One write of the page at a time, so a later change never loses to an earlier one that ends later.
    draft = SettingsDraft(asyncio.Lock())
    ui.label(_("Settings")).classes("text-h5")
    await _cookies_section(services)
    await _rotation_section(services, draft)
    await _processing_section(services, draft)
    await ytdlp_section(services, draft)
    await notifications_section(services, draft)
    draft.build()
