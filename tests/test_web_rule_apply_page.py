"""Tests of the preview of a rule in the editor and of the apply dialog of the rules page.

The children of the service run in the test process: the real bodies of the preview and the apply, with fakes for the
anonymous reads of yt-dlp and for the account client. No test reads or changes YouTube.
"""

import asyncio
import contextlib
import io
import json
import logging
import re
import subprocess
import threading
import time
from datetime import timedelta

import pytest
from nicegui import ui
from nicegui.elements.mixins.label_element import LabelElement
from nicegui.elements.mixins.text_element import TextElement
from nicegui.testing.user_notify import UserNotify
from sqlalchemy import select
from test_run_events import service_with_pages
from test_sync import LONG, A, B, entry, feed_video, upcoming
from ui_support import (
    WAIT_SECONDS,
    PatientUser,
    channel_remove_button,
    click_on,
    element_of,
    elements_of,
    eventually,
    running_service,
    store,
    text_of,
)

from youtube_automanager import __main__ as cli
from youtube_automanager import i18n, rule_actions
from youtube_automanager import runner as runner_module
from youtube_automanager import scheduler as scheduler_module
from youtube_automanager.db.models import (
    AddedVideo,
    Channel,
    Playlist,
    Rule,
    RuleChannel,
    Run,
    RunAction,
    RunActionKind,
    RunStatus,
    RunTrigger,
    SeenVideo,
    SeenVideoState,
    Setting,
    utc_now,
)
from youtube_automanager.engine import account_session
from youtube_automanager.engine.rule_preview import APPLY_LIMIT, DraftRule, Mark, Preview, PreviewVideo
from youtube_automanager.engine.rules import MATCH_ALL
from youtube_automanager.engine.sync import PROCESSING_MODE_KEY, STATE_MODE_KEY
from youtube_automanager.i18n.setting import NAMES
from youtube_automanager.rule_actions import apply_once, preview_once
from youtube_automanager.runner import INTERRUPTED_ERROR, sync_lock_held
from youtube_automanager.settings import Settings
from youtube_automanager.web import applies as applies_module
from youtube_automanager.web.ui import rule_editor
from youtube_automanager.web.ui.common import DAY_CELL_FORMAT
from youtube_automanager.youtube import public
from youtube_automanager.youtube.session import CookieCipher, generate_key, save_cookies

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("logging_setups", "no_logged_errors")]

ZONE = "Europe/Kyiv"
COOKIES = (
    "# Netscape HTTP Cookie File\n"
    ".youtube.com\tTRUE\t/\tTRUE\t4000000000\tSAPISID\tfake-sapisid\n"
    ".youtube.com\tTRUE\t/\tFALSE\t4000000000\tSIDCC\tfake-sidcc-0\n"
)
BUSY = (
    "Another task works with YouTube now: a sync run, a session check, a refresh of the lists, a channel action or a "
    "yt-dlp update. Try again when it ends."
)
# How long a test waits for a click that must change nothing. A click that does change something shows it within ms.
SETTLE_SECONDS = 0.3


def today():
    return rule_editor.today_in(ZONE)


def days_back(days):
    return today() - timedelta(days=days)


@pytest.fixture
def settings(tmp_path):
    """The settings of the service, with a cookie key, so an apply can open the stored cookies."""
    home = tmp_path / "data"
    return Settings(
        home=home,
        database_url=f"sqlite:///{home / 'service.sqlite'}",
        ytdlp_dir=home / "yt-dlp",
        tz=ZONE,
        cookies_key=generate_key(),
    )


class FakeYouTube:
    """Replaces the anonymous reads of yt-dlp and the account client. A test fills ``uploads``, ``shorts`` and
    ``feed``, and the client records each add in ``added``."""

    def __init__(self):
        self.uploads = {}
        self.shorts = {}
        self.feed = []
        self.added = []
        self.reads = []

    def iter_channel_uploads(self, channel_id):
        self.reads.append(channel_id)
        uploads = self.uploads.get(channel_id, [])
        if isinstance(uploads, Exception):
            raise uploads
        return iter(uploads)

    def shorts_video_ids(self, channel_id, limit=None):
        return set(self.shorts.get(channel_id, ()))


@pytest.fixture
def youtube(monkeypatch):
    fake = FakeYouTube()
    monkeypatch.setattr(public, "iter_channel_uploads", fake.iter_channel_uploads)
    monkeypatch.setattr(public, "shorts_video_ids", fake.shorts_video_ids)

    class FakeClient:
        def __init__(self, text):
            self.text = text

        def check_session(self):
            pass

        def use_cookies(self, text):
            self.text = text

        def cookies_text(self):
            return self.text

        def iter_subscription_feed(self):
            return iter(fake.feed)

        def add_video(self, playlist_id, video_id):
            fake.added.append((playlist_id, video_id))

    monkeypatch.setattr(account_session, "AccountClient", FakeClient)
    return fake


class EndedChild:
    """A child process that ended with ``exit_code`` and wrote ``stderr``."""

    def __init__(self, exit_code=0, stderr=""):
        self.stderr = io.StringIO(stderr)
        self.returncode = exit_code

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode

    def terminate(self):
        pass

    kill = terminate


class TypedNotify(UserNotify):
    """The notifications of a simulated browser with their type. ``user.notify = TypedNotify()`` puts it in place."""

    def __init__(self):
        super().__init__()
        self.typed = []

    def __call__(self, message, **kwargs):
        super().__call__(message, **kwargs)
        self.typed.append((message, kwargs.get("type")))


class Gate:
    """Holds the next apply child until ``release``, so a test sees the apply while it runs.

    With ``run_body``, the released child runs the real body of the apply, and without it, the child ends with exit
    code 0 and changes nothing. A stop of the service terminates the held child.
    """

    TERMINATED_CODE = -15

    def __init__(self, *, run_body=True):
        self.run_body = run_body
        self.started = threading.Event()
        self.released = threading.Event()
        self.terminated = False

    def release(self):
        self.released.set()


class GatedChild:
    def __init__(self, gate, body):
        self.gate = gate
        self.body = body
        # The runner reads stderr from the start, so a held child has an empty stderr.
        self.stderr = io.StringIO()
        self.returncode = None
        gate.started.set()

    def wait(self, timeout=None):
        if not self.gate.released.wait(timeout) and timeout is not None:
            raise subprocess.TimeoutExpired("gated child", timeout)
        if self.returncode is None:
            self.returncode = self.body()[0] if self.gate.run_body else 0
        return self.returncode

    def poll(self):
        return self.returncode

    def terminate(self):
        if self.returncode is None:
            self.gate.terminated = True
            self.returncode = Gate.TERMINATED_CODE
        self.gate.release()

    kill = terminate


class Children:
    """Starts the children of the service in the test process and records each command.

    A preview and an apply run their real body. A child of ``plan`` replaces the next start, for a result that the
    body cannot give, and an exception of ``plan`` fails the start. A set ``gate`` holds the next apply child.
    """

    def __init__(self, settings):
        self.settings = settings
        self.commands = []
        self.plan = []
        self.gate = None

    def __call__(self, command, env, input_text):
        self.commands.append(list(command))
        if self.plan:
            planned = self.plan.pop(0)
            if isinstance(planned, Exception):
                raise planned
            return planned
        args = cli._parser().parse_args(command[3:])
        if args.command == "apply-rule" and self.gate is not None:
            gate, self.gate = self.gate, None
            return GatedChild(gate, lambda: self._run(args))
        return EndedChild(*self._run(args))

    def _run(self, args):
        """Run the body of the child ``args``, and return its exit code and its stderr."""
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            if args.command == "preview-rule":
                code = preview_once(self.settings, args.since, args.rule_id, args.draft)
            elif args.command == "apply-rule":
                code = apply_once(args.run_id, args.rule_id, args.since, self.settings)
            else:
                code = 0
        return code, stderr.getvalue()

    def of(self, name):
        return [command for command in self.commands if command[3] == name]


@pytest.fixture
def children(settings):
    return Children(settings)


@pytest.fixture
async def user(nicegui_globals, settings, sessions, children, youtube):
    async with running_service(settings, children) as user:
        yield user


@pytest.fixture
def account_copy(sessions):
    store(
        sessions,
        Channel(channel_id=A, title="Alpha", handle="@alpha", is_subscribed=True),
        Channel(channel_id=B, title="Beta", is_subscribed=True),
        Playlist(playlist_id="WL", title="Watch later", is_own=True),
        Playlist(playlist_id="PLhome", title="Home", is_own=True),
    )


def saved_rule(sessions, condition=LONG, channels=(A,), target="WL", name="Long videos"):
    store(
        sessions,
        Rule(
            id=1, name=name, enabled=True, position=1, condition=condition, target_playlist_id=target, no_shorts=False
        ),
    )
    store(sessions, *[RuleChannel(rule_id=1, channel_id=channel_id) for channel_id in channels])
    return 1


def store_cookies(sessions, settings):
    with sessions.begin() as session:
        save_cookies(session, CookieCipher(settings.cookies_key.get_secret_value()), COOKIES)


def rows(user, marker):
    tables = elements_of(user, marker)
    return tables[0].rows if tables else []


def shown(user, marker):
    return [(row["title"], row["mark"]) for row in rows(user, marker)]


async def open_editor(user, rule_id=None):
    if rule_id is None:
        user.find(marker="new-rule").click()
    else:
        user.find(marker="rules-table").trigger("rowClick", args=rule_id)
    await eventually(lambda: elements_of(user, "editor"), "The editor")


def set_value(user, marker, value):
    """Give the newest element with ``marker`` the value, as the user does. A closed editor keeps old elements."""
    element = elements_of(user, marker)[-1]
    with user.client:
        element.value = value


async def open_apply(user):
    user.find(marker="editor-apply").click()
    await eventually(lambda: element_of(user, "apply-dialog").value, "The open apply dialog")


async def preview_in_editor(user):
    user.find(marker="preview-run").click()
    await eventually(
        lambda: (
            elements_of(user, "preview-table")
            or elements_of(user, "preview-empty")
            or elements_of(user, "preview-failed")
        ),
        "The result of the preview",
    )


async def preview_in_dialog(user):
    user.find(marker="apply-preview").click()
    await eventually(
        lambda: (
            elements_of(user, "apply-result-table")
            or elements_of(user, "apply-result-empty")
            or elements_of(user, "apply-result-failed")
        ),
        "The result of the preview",
    )


def runs(sessions):
    with sessions() as session:
        return session.scalars(select(Run).order_by(Run.id)).all()


def added(sessions):
    with sessions() as session:
        return {(row.playlist_id, row.video_id): row.rule_id for row in session.scalars(select(AddedVideo))}


# The preview in the editor.


def read_before(sessions, channel_id, *video_ids):
    """Store the state of a channel that a run read the day before: its videos as seen, and the time of the read."""
    with sessions.begin() as session:
        session.get(Channel, channel_id).last_checked_at = utc_now() - timedelta(days=1)
        session.add_all(
            SeenVideo(channel_id=channel_id, video_id=video_id, state=SeenVideoState.SEEN, upload_date=days_back(2))
            for video_id in video_ids
        )


def seen_count(sessions):
    with sessions() as session:
        return len(session.scalars(select(SeenVideo)).all())


MESH = {"regex": [{"var": "title"}, "meshtastic", "i"]}
# The words of the inside of the service that a user of the editor never sees.
INNER_WORDS = ("baseline", "source", "seen", "processing mode")


def inner_words(user):
    return [
        (word, text)
        for text in shown_texts(user)
        for word in INNER_WORDS
        if isinstance(text, str) and word in text.casefold()
    ]


async def test_the_preview_of_a_new_rule_lists_what_the_next_run_adds_and_stores_nothing(
    user, sessions, settings, account_copy, youtube, children
):
    store_cookies(sessions, settings)
    old = [entry(f"old{index:02}", A, title=f"Old video {index}", day=days_back(10)) for index in range(25)]
    read_before(sessions, A, "seen long", *(item.video_id for item in old))
    youtube.uploads[A] = [
        entry("long", A, title="A long video", day=today()),
        entry("short", A, title="A short video", duration_s=30, day=today()),
        entry("live soon", A, title="Live soon", duration_s=None, day=None, live_status="is_upcoming"),
        entry("seen long", A, title="A long video of last week", day=days_back(3)),
        *old,
    ]
    await user.open("/rules")
    await open_editor(user)
    assert not elements_of(user, "preview-since")
    assert text_of(user, "preview-hint").startswith("The preview shows what the next run does with the rule")
    assert element_of(user, "rule-catch-up").value == days_back(7).isoformat()

    set_value(user, "rule-channels", [A])
    set_value(user, "rule-playlist", "WL")
    user.find(marker="add-condition").click()
    await eventually(lambda: elements_of(user, "field-0"), "The condition")
    set_value(user, "field-0", "duration_s")
    await eventually(lambda: elements_of(user, "minutes-0"), "The input of the minutes")
    assert elements_of(user, "operator-0")[-1].value == ">="
    set_value(user, "minutes-0", 2)
    await preview_in_editor(user)

    # The video of last week is older than the last run, and it comes from the day of the field.
    assert shown(user, "preview-table") == [
        ("Live soon", "Waits: no value yet for a field of the rule"),
        ("A long video", "Will be added"),
        ("A long video of last week", "Will be added"),
    ]
    assert rows(user, "preview-table")[1] | {"id": "long"} == {
        "id": "long",
        "date": today().isoformat(),
        "channel": "Alpha",
        "title": "A long video",
        "url": "https://www.youtube.com/watch?v=long",
        "mark": "Will be added",
        "mark_color": "positive",
    }
    assert rows(user, "preview-table")[0]["date"] == "No date"
    # The browser shows the day of the upload in its own form, and "No date" as it is.
    (date_column,) = [column for column in element_of(user, "preview-table").columns if column["name"] == "date"]
    assert date_column[":format"] == DAY_CELL_FORMAT
    assert text_of(user, "preview-counts") == "Will be added: 2, Waits: no value yet for a field of the rule: 1"
    assert (
        text_of(user, "preview-footer") == "The next run also checks each new video that comes before the run starts."
    )
    assert not elements_of(user, "preview-not-added")
    (command,) = children.of("preview-rule")
    draft = DraftRule({"and": [{">=": [{"var": "duration_s"}, 120]}]}, (A,), False, "WL", days_back(7))
    assert command[4:] == ["--draft=" + draft.to_json()]
    with sessions() as session:
        assert session.scalars(select(Rule)).all() == []
    assert seen_count(sessions) == 26
    assert youtube.added == []
    assert inner_words(user) == []


async def test_a_new_rule_on_a_channel_that_no_rule_reads_lists_the_videos_from_its_day(
    user, sessions, settings, account_copy, youtube
):
    # The case of a user: the new rule reads a channel that no rule read before.
    store_cookies(sessions, settings)
    youtube.uploads[A] = [
        entry("mesh", A, title="Meshtastic node", day=days_back(1)),
        entry("solder", A, title="Soldering", day=days_back(2)),
        entry("mesh old", A, title="Meshtastic v1", day=days_back(10)),
    ]
    await user.open("/rules")
    await open_editor(user)
    set_value(user, "rule-channels", [A])
    set_value(user, "rule-playlist", "WL")
    user.find(marker="add-condition").click()
    await eventually(lambda: elements_of(user, "value-0"), "The condition")
    set_value(user, "value-0", "meshtastic")

    await preview_in_editor(user)

    assert shown(user, "preview-table") == [("Meshtastic node", "Will be added")]
    assert not elements_of(user, "preview-not-added")
    assert inner_words(user) == []


async def test_without_a_day_the_preview_says_that_the_videos_on_the_channel_are_not_added(
    user, sessions, settings, account_copy, youtube
):
    store_cookies(sessions, settings)
    youtube.uploads[A] = [entry("new", A, day=today())]
    await user.open("/rules")
    await open_editor(user)
    set_value(user, "rule-channels", [A])
    set_value(user, "rule-catch-up", None)

    await preview_in_editor(user)

    assert text_of(user, "preview-empty") == "The next run adds no video for this rule."
    assert text_of(user, "preview-not-added") == (
        "The next run does not add the videos that are already on these channels: Alpha. Set Also add videos from to "
        "add them from that day."
    )
    assert youtube.reads == []
    assert inner_words(user) == []


async def test_the_preview_names_only_the_channels_without_a_read_and_reads_the_others(
    user, sessions, settings, account_copy, youtube
):
    store_cookies(sessions, settings)
    read_before(sessions, A, "old")
    youtube.uploads[A] = [entry("new", A, title="New in Alpha", day=today()), entry("old", A, day=days_back(10))]
    youtube.uploads[B] = [entry("beta", B, title="From Beta", day=today())]
    await user.open("/rules")
    await open_editor(user)
    set_value(user, "rule-channels", [A, B])
    set_value(user, "rule-catch-up", None)

    await preview_in_editor(user)

    assert shown(user, "preview-table") == [("New in Alpha", "Will be added")]
    assert "Beta" in text_of(user, "preview-not-added")
    assert youtube.reads == [A]


async def test_the_preview_says_in_plain_words_when_a_setting_of_the_service_changed(
    user, sessions, settings, account_copy, youtube
):
    store_cookies(sessions, settings)
    read_before(sessions, A, "old")
    store(
        sessions,
        Setting(key=PROCESSING_MODE_KEY, value={"mode": "newest", "count": 5}),
        Setting(key=STATE_MODE_KEY, value={"mode": "stop_at_seen"}),
    )
    youtube.uploads[A] = [entry("new", A, title="New", day=today()), entry("old", A, day=days_back(10))]
    await user.open("/rules")
    await open_editor(user)
    set_value(user, "rule-channels", [A])

    await preview_in_editor(user)

    assert text_of(user, "preview-mode-changed") == (
        "A setting of the service changed how far a run reads the channels. So the next run adds only the videos from "
        "the day in Also add videos from. After that run, the rule adds the videos that come later."
    )
    assert shown(user, "preview-table") == [("New", "Will be added")]
    assert not elements_of(user, "preview-not-added")
    assert inner_words(user) == []

    set_value(user, "rule-catch-up", None)
    await preview_in_editor(user)

    assert text_of(user, "preview-mode-changed").startswith(
        "A setting of the service changed how far a run reads the channels. So the next run adds no video"
    )
    assert elements_of(user, "preview-empty")


async def test_the_preview_of_a_saved_rule_shows_what_the_next_run_does_with_the_form(
    user, sessions, settings, account_copy, youtube, children
):
    store_cookies(sessions, settings)
    saved_rule(sessions, condition={"and": [LONG]})
    store(sessions, AddedVideo(playlist_id="WL", video_id="in playlist", rule_id=1))
    read_before(sessions, A, "old short")
    youtube.uploads[A] = [
        entry("long", A, title="Long", day=today()),
        entry("short", A, title="Short", duration_s=30, day=today()),
        entry("in playlist", A, title="In the playlist", day=today()),
        entry("old short", A, title="Old short", duration_s=30, day=days_back(2)),
    ]
    await user.open("/rules")
    await open_editor(user, 1)
    assert element_of(user, "rule-catch-up").value is None

    await preview_in_editor(user)
    assert shown(user, "preview-table") == [("Long", "Will be added"), ("In the playlist", "Already in the playlist")]

    set_value(user, "operator-0", "<")
    await eventually(lambda: elements_of(user, "minutes-0")[-1].value == 2, "The comparison")
    # The change of the condition gives the field its default day.
    assert element_of(user, "rule-catch-up").value == days_back(7).isoformat()
    await preview_in_editor(user)

    await eventually(lambda: ("Short", "Will be added") in shown(user, "preview-table"), "The videos of the change")
    assert shown(user, "preview-table") == [("Short", "Will be added"), ("Old short", "Will be added")]
    # The preview reads only the form, also for a saved rule.
    assert "--rule-id=1" not in children.of("preview-rule")[-1]
    with sessions() as session:
        assert session.get(Rule, 1).condition == {"and": [LONG]}


@pytest.mark.parametrize(
    ("marker", "value"),
    [
        ("rule-channels", [A, B]),
        ("rule-playlist", "PLhome"),
        ("rule-no-shorts", True),
        ("rule-catch-up", None),
        ("rule-enabled", False),
        ("value-0", "other"),
    ],
)
async def test_a_change_of_the_form_removes_the_old_preview(
    user, sessions, settings, account_copy, youtube, marker, value
):
    store_cookies(sessions, settings)
    saved_rule(sessions, condition={"and": [{"regex": [{"var": "title"}, "video", "i"]}]})
    youtube.uploads[A] = [entry("new", A, title="New video", day=today())]
    await user.open("/rules")
    await open_editor(user, 1)
    set_value(user, "rule-catch-up", days_back(7).isoformat())
    await preview_in_editor(user)
    assert shown(user, "preview-table") == [("New video", "Will be added")]

    set_value(user, marker, value)

    await eventually(lambda: not elements_of(user, "preview-table"), "The removed preview")


async def test_a_remove_button_of_a_channel_removes_the_old_preview(user, sessions, settings, account_copy, youtube):
    store_cookies(sessions, settings)
    saved_rule(sessions, condition={"and": [{"regex": [{"var": "title"}, "video", "i"]}]}, channels=(A, B))
    youtube.uploads[A] = [entry("new", A, title="New video", day=today())]
    await user.open("/rules")
    await open_editor(user, 1)
    set_value(user, "rule-catch-up", days_back(7).isoformat())
    await preview_in_editor(user)
    assert shown(user, "preview-table") == [("New video", "Will be added")]

    click_on(user, channel_remove_button(user, B))

    await eventually(lambda: not elements_of(user, "preview-table"), "The removed preview")
    assert elements_of(user, "rule-channels")[-1].value == [A]


async def test_a_change_of_the_name_keeps_the_preview(user, sessions, settings, account_copy, youtube):
    store_cookies(sessions, settings)
    saved_rule(sessions)
    youtube.uploads[A] = [entry("new", A, title="New video", day=today())]
    await user.open("/rules")
    await open_editor(user, 1)
    set_value(user, "rule-catch-up", days_back(7).isoformat())
    await preview_in_editor(user)

    set_value(user, "rule-name", "Another name")
    await asyncio.sleep(SETTLE_SECONDS)

    assert shown(user, "preview-table") == [("New video", "Will be added")]


async def test_a_change_while_the_preview_runs_shows_no_old_result(user, sessions, account_copy, children):
    finish = threading.Event()
    value = Preview((PreviewVideo("v", "Video", "Alpha", today(), Mark.WILL_ADD),), {Mark.WILL_ADD: 1}).to_value()

    class SlowChild(EndedChild):
        def wait(self, timeout=None):
            finish.wait(timeout=10)
            return self.returncode

        def poll(self):
            return self.returncode if finish.is_set() else None

    children.plan.append(SlowChild(0, json.dumps(value) + "\n"))
    await user.open("/rules")
    await open_editor(user)
    user.find(marker="preview-run").click()
    await eventually(lambda: elements_of(user, "preview-working"), "The running preview")

    set_value(user, "rule-no-shorts", True)
    finish.set()

    await eventually(lambda: elements_of(user, "preview-note"), "The note")
    assert text_of(user, "preview-note") == "The form changed while the preview ran. Press Preview again."
    assert not elements_of(user, "preview-table")


async def test_the_preview_button_of_the_apply_dialog_keeps_its_name_while_it_loads(
    user, sessions, settings, account_copy, children
):
    store_cookies(sessions, settings)
    saved_rule(sessions)
    finish = threading.Event()
    value = Preview((PreviewVideo("v", "Video", "Alpha", today(), Mark.WILL_ADD),), {Mark.WILL_ADD: 1}).to_value()

    class SlowChild(EndedChild):
        def wait(self, timeout=None):
            finish.wait(timeout=10)
            return self.returncode

        def poll(self):
            return self.returncode if finish.is_set() else None

    children.plan.append(SlowChild(0, json.dumps(value) + "\n"))
    await user.open("/rules")
    await open_editor(user, 1)
    await open_apply(user)
    button = element_of(user, "apply-preview")

    user.find(marker="apply-preview").click()
    await eventually(lambda: button.props.get("loading"), "The loading preview button")
    # Quasar hides the text of a loading button, so the button keeps its text as its name.
    assert button.props["aria-label"] == "Preview"

    finish.set()
    await eventually(lambda: elements_of(user, "apply-result-table"), "The result of the preview")
    assert "loading" not in button.props
    assert "aria-label" not in button.props


async def test_the_preview_says_that_the_matching_videos_wait_for_the_account(user, sessions, account_copy, youtube):
    # The settings have a cookie key, but no cookies are stored.
    read_before(sessions, A, "old")
    youtube.uploads[A] = [entry("new", A, title="New", day=today()), entry("old", A, day=days_back(10))]
    await user.open("/rules")
    await open_editor(user)
    set_value(user, "rule-channels", [A])

    await preview_in_editor(user)

    assert shown(user, "preview-table") == [("New", "Waits for the YouTube account")]
    assert text_of(user, "preview-account") == (
        "The YouTube account is not available now, so the matching videos wait for it. Upload new YouTube cookies on "
        "the Settings page."
    )


async def test_the_preview_says_when_the_next_run_adds_only_a_part_of_the_videos(user, account_copy, children):
    videos = [PreviewVideo(f"v{index}", f"Video {index}", "Alpha", today(), Mark.WILL_ADD) for index in range(3)]
    preview = Preview(tuple(videos), {Mark.WILL_ADD: 3}, catch_up_adds=APPLY_LIMIT + 30)
    children.plan.append(EndedChild(0, json.dumps(preview.to_value()) + "\n"))
    await user.open("/rules")
    await open_editor(user)
    set_value(user, "rule-channels", [A])

    await preview_in_editor(user)

    assert text_of(user, "preview-limit") == (
        "The next run adds the 100 oldest of the 130 videos from the day in Also add videos from. The runs after it add "
        "the rest."
    )


async def test_the_preview_of_a_rule_that_is_off_says_that_no_run_uses_it(
    user, sessions, settings, account_copy, youtube
):
    store_cookies(sessions, settings)
    read_before(sessions, A, "old")
    youtube.uploads[A] = [entry("new", A, title="New", day=today()), entry("old", A, day=days_back(10))]
    await user.open("/rules")
    await open_editor(user)
    set_value(user, "rule-channels", [A])
    set_value(user, "rule-enabled", False)

    await preview_in_editor(user)

    assert text_of(user, "preview-off") == (
        "The rule is off, so no run uses it. The preview shows what the next run does with the rule on."
    )
    assert shown(user, "preview-table") == [("New", "Will be added")]


async def test_the_preview_of_a_rule_that_is_on_has_no_note(user, sessions, settings, account_copy, youtube):
    store_cookies(sessions, settings)
    read_before(sessions, A, "old")
    youtube.uploads[A] = [entry("new", A, title="New", day=today()), entry("old", A, day=days_back(10))]
    await user.open("/rules")
    await open_editor(user)
    set_value(user, "rule-channels", [A])

    await preview_in_editor(user)

    assert shown(user, "preview-table") == [("New", "Will be added")]
    assert not elements_of(user, "preview-off")


async def test_a_condition_that_is_not_complete_stops_the_preview(user, account_copy, children):
    await user.open("/rules")
    await open_editor(user)
    user.find(marker="add-condition").click()
    await eventually(lambda: elements_of(user, "value-0"), "The condition")

    user.find(marker="preview-run").click()

    await eventually(lambda: text_of(user, "preview-failed") == "Title: Enter a pattern.", "The error")
    assert children.of("preview-rule") == []


async def test_a_preview_while_the_sync_lock_is_taken_says_so(user, account_copy, children):
    await user.open("/rules")
    await open_editor(user)

    with sync_lock_held(timeout=0):
        user.find(marker="preview-run").click()
        await eventually(lambda: elements_of(user, "preview-failed"), "The failure")

    assert text_of(user, "preview-failed") == f"The preview failed. {BUSY}"
    assert children.commands == []


async def test_a_preview_names_the_errors_and_the_sources_that_its_limit_cut(user, account_copy, children):
    videos = [PreviewVideo(f"v{index}", f"Video {index}", "Alpha", today(), Mark.MATCHES) for index in range(200)]
    preview = Preview(tuple(videos), {Mark.MATCHES: 230}, ("Channel Beta: no answer",), ("Alpha",))
    children.plan.append(EndedChild(0, json.dumps(preview.to_value()) + "\n"))
    await user.open("/rules")
    await open_editor(user)
    set_value(user, "rule-channels", [A, B])

    await preview_in_editor(user)

    assert text_of(user, "preview-error") == "Channel Beta: no answer"
    assert text_of(user, "preview-cut") == (
        "The read stops after 500 videos of a channel, so the older videos of these channels are not in the list: Alpha"
    )
    assert text_of(user, "preview-more") == "The table shows the newest 200 of the 230 videos."
    assert len(rows(user, "preview-table")) == 200
    assert element_of(user, "preview-table").pagination == {"rowsPerPage": 20}


# The apply dialog.


async def test_the_today_button_puts_today_into_the_day_of_the_apply(user, sessions, account_copy, children):
    saved_rule(sessions)
    await user.open("/rules")
    await open_editor(user, 1)
    await open_apply(user)

    user.find(marker="apply-since-today").click()

    await eventually(lambda: element_of(user, "apply-since").value == today().isoformat(), "Today as the day")
    await preview_in_dialog(user)
    assert children.of("preview-rule")[0][4] == f"--since={today().isoformat()}"


async def test_an_apply_previews_the_saved_rule_and_adds_the_videos_as_a_run_of_the_history(
    user, sessions, settings, account_copy, youtube, children
):
    store_cookies(sessions, settings)
    saved_rule(sessions, target="PLhome")
    store(sessions, AddedVideo(playlist_id="PLhome", video_id="added before", rule_id=None))
    youtube.uploads[A] = [
        entry("new", A, title="New", day=today()),
        entry("added before", A, title="Added before", day=days_back(1)),
        upcoming("soon", A),
        entry("unknown", A, title="No duration", duration_s=None, day=days_back(2)),
        entry("short", A, title="Short", duration_s=30, day=days_back(3)),
        entry("older", A, title="Older", day=days_back(7)),
        entry("too old", A, title="Too old", day=days_back(8)),
    ]
    await user.open("/rules")
    await open_editor(user, 1)

    await open_apply(user)
    assert text_of(user, "apply-title") == 'Apply the rule "Long videos" to videos from a date'
    assert "to the playlist Home, the oldest first, and at most 100 videos at a time" in text_of(user, "apply-hint")
    assert elements_of(user, "apply-unsaved") == []
    assert element_of(user, "apply-since").value == days_back(7).isoformat()
    await user.should_not_see(marker="apply-run")

    await preview_in_dialog(user)
    assert shown(user, "apply-result-table") == [
        ("Video soon", "Undecided"),
        ("New", "Will be added"),
        ("Added before", "Already in the playlist"),
        ("No duration", "Undecided"),
        ("Older", "Will be added"),
    ]
    assert text_of(user, "apply-result-counts") == "Will be added: 2, Already in the playlist: 1, Undecided: 2"
    assert text_of(user, "apply-run") == "Add 2 videos"
    assert children.of("preview-rule")[0][4:] == [f"--since={days_back(7).isoformat()}", "--rule-id=1"]

    user.find(marker="apply-run").click()
    await eventually(lambda: "The apply is done" in text_of(user, "apply-status"), "The end of the apply")

    (run,) = runs(sessions)
    assert text_of(user, "apply-status") == (
        f"The apply is done. Videos added: 2. The History page shows it as run {run.id}."
    )
    assert (run.trigger, run.status, run.added_count) == (RunTrigger.RULE_APPLY, RunStatus.SUCCEEDED, 2)
    assert youtube.added == [("PLhome", "older"), ("PLhome", "new")]
    assert added(sessions) == {("PLhome", "added before"): None, ("PLhome", "older"): 1, ("PLhome", "new"): 1}
    (command,) = children.of("apply-rule")
    assert command[4:] == [f"--run-id={run.id}", "--rule-id=1", f"--since={days_back(7).isoformat()}"]
    # The preview is old after the apply, so a second apply needs a new preview.
    await user.should_not_see(marker="apply-run")

    user.find(marker="apply-close").click()
    await eventually(lambda: not element_of(user, "apply-dialog").value, "The close of the dialog")
    await user.open("/history")
    assert [row["trigger"] for row in rows(user, "runs-table")] == ["Rule apply"]


@pytest.mark.parametrize(
    ("day", "error"),
    [
        (lambda: (today() + timedelta(days=1)).isoformat(), "Choose a day that is not in the future."),
        (lambda: days_back(366).isoformat(), "Choose a day at most 365 days back."),
        (lambda: None, "Choose the first day of the videos."),
    ],
)
async def test_the_apply_refuses_a_day_out_of_its_range_and_starts_no_child(
    user, sessions, account_copy, children, day, error
):
    saved_rule(sessions)
    await user.open("/rules")
    await open_editor(user, 1)
    await open_apply(user)

    set_value(user, "apply-since", day())
    user.find(marker="apply-preview").click()

    await eventually(lambda: text_of(user, "apply-result-failed") == error, "The error of the day")
    assert children.of("preview-rule") == []


async def test_an_apply_takes_the_day_a_year_back(user, sessions, account_copy, children, youtube):
    saved_rule(sessions)
    await user.open("/rules")
    await open_editor(user, 1)
    await open_apply(user)

    set_value(user, "apply-since", days_back(365).isoformat())
    await preview_in_dialog(user)

    assert elements_of(user, "apply-result-empty")
    assert children.of("preview-rule")[0][4] == f"--since={days_back(365).isoformat()}"


async def test_the_apply_dialog_says_when_the_editor_has_changes_that_are_not_saved(user, sessions, account_copy):
    saved_rule(sessions)
    await user.open("/rules")
    await open_editor(user, 1)
    await open_apply(user)
    assert elements_of(user, "apply-unsaved") == []
    user.find(marker="apply-close").click()
    await eventually(lambda: not element_of(user, "apply-dialog").value, "The close of the dialog")

    set_value(user, "rule-name", "Another name")
    await open_apply(user)

    assert text_of(user, "apply-unsaved") == (
        "The editor has changes that are not saved. The apply uses the saved version of the rule."
    )
    assert text_of(user, "apply-title") == 'Apply the rule "Long videos" to videos from a date'


async def test_the_dialog_says_when_the_limit_of_one_apply_cuts_the_list(user, sessions, account_copy, children):
    saved_rule(sessions)
    videos = [PreviewVideo(f"v{index}", f"Video {index}", "Alpha", today(), Mark.WILL_ADD) for index in range(200)]
    preview = Preview(tuple(videos), {Mark.WILL_ADD: 250, Mark.UNDECIDED: 3})
    children.plan.append(EndedChild(0, json.dumps(preview.to_value()) + "\n"))
    await user.open("/rules")
    await open_editor(user, 1)
    await open_apply(user)

    await preview_in_dialog(user)

    assert text_of(user, "apply-run") == f"Add {APPLY_LIMIT} videos"
    assert (
        text_of(user, "apply-limit") == "One apply adds the 100 oldest of the 250 videos. Apply again to add the rest."
    )


async def test_one_video_gets_a_button_of_one_video(user, sessions, account_copy, youtube):
    saved_rule(sessions)
    youtube.uploads[A] = [entry("new", A, day=today())]
    await user.open("/rules")
    await open_editor(user, 1)
    await open_apply(user)

    await preview_in_dialog(user)

    assert text_of(user, "apply-run") == "Add 1 video"
    await user.should_not_see(marker="apply-limit")


async def test_a_preview_without_a_video_to_add_offers_no_apply(user, sessions, account_copy, youtube):
    saved_rule(sessions)
    youtube.uploads[A] = [entry("short", A, duration_s=30, day=today())]
    await user.open("/rules")
    await open_editor(user, 1)
    await open_apply(user)

    await preview_in_dialog(user)

    assert text_of(user, "apply-result-empty") == "The rule matches no video of its sources since this day."
    await user.should_not_see(marker="apply-run")


async def test_another_day_needs_a_new_preview_before_an_apply(user, sessions, account_copy, youtube, children):
    saved_rule(sessions)
    youtube.uploads[A] = [entry("new", A, day=today())]
    await user.open("/rules")
    await open_editor(user, 1)
    await open_apply(user)
    await preview_in_dialog(user)
    await user.should_see(marker="apply-run")

    set_value(user, "apply-since", days_back(3).isoformat())

    await user.should_not_see(marker="apply-run")
    assert elements_of(user, "apply-result-table") == []
    await preview_in_dialog(user)
    assert children.of("preview-rule")[-1][4] == f"--since={days_back(3).isoformat()}"


async def test_an_apply_while_the_sync_lock_is_taken_writes_no_run_and_can_start_again(
    user, sessions, account_copy, youtube, children
):
    saved_rule(sessions)
    youtube.uploads[A] = [entry("new", A, day=today())]
    await user.open("/rules")
    await open_editor(user, 1)
    await open_apply(user)
    await preview_in_dialog(user)

    with sync_lock_held(timeout=0):
        user.find(marker="apply-run").click()
        await eventually(lambda: "did not start" in text_of(user, "apply-status"), "The refusal")

    assert text_of(user, "apply-status") == f"The apply did not start. {BUSY}"
    assert runs(sessions) == []
    assert children.of("apply-rule") == []
    await user.should_see(marker="apply-run")


async def test_a_failed_apply_shows_the_error_of_its_run(user, sessions, account_copy, youtube, children):
    saved_rule(sessions)
    youtube.uploads[A] = [entry("new", A, day=today())]
    await user.open("/rules")
    await open_editor(user, 1)
    await open_apply(user)
    await preview_in_dialog(user)
    children.plan.append(EndedChild(1, "The apply needs the YouTube account. No cookies.\n"))

    user.find(marker="apply-run").click()

    await eventually(lambda: "The apply failed" in text_of(user, "apply-status"), "The failure")
    (run,) = runs(sessions)
    assert text_of(user, "apply-status") == (
        f"The apply failed: The apply needs the YouTube account. No cookies. The History page shows it as run {run.id}."
    )
    assert (run.status, run.trigger) == (RunStatus.FAILED, RunTrigger.RULE_APPLY)


async def test_an_apply_with_errors_counts_them(user, sessions, settings, account_copy, youtube, children):
    notify = user.notify = TypedNotify()
    store_cookies(sessions, settings)
    saved_rule(sessions, channels=(A, B))
    youtube.uploads[A] = [entry("new", A, day=today())]
    await user.open("/rules")
    await open_editor(user, 1)
    await open_apply(user)
    await preview_in_dialog(user)
    # The read of the apply finds another video, and a channel that fails.
    youtube.uploads[B] = ValueError("not a YouTube channel id")

    user.find(marker="apply-run").click()

    await eventually(lambda: "with errors" in text_of(user, "apply-status"), "The end of the apply")
    (run,) = runs(sessions)
    text = f"The apply is done with errors. Videos added: 1. Errors: 1. The History page shows them in run {run.id}."
    assert text_of(user, "apply-status") == text
    assert notify.typed == [(text, "warning")]
    with sessions() as session:
        actions = session.scalars(select(RunAction).where(RunAction.run_id == run.id).order_by(RunAction.id)).all()
        assert [(action.action, action.channel_id) for action in actions] == [
            (RunActionKind.ERROR, B),
            (RunActionKind.ADDED, A),
        ]


async def test_a_second_click_on_add_starts_one_apply(user, sessions, settings, account_copy, youtube, children):
    store_cookies(sessions, settings)
    saved_rule(sessions)
    youtube.uploads[A] = [entry("new", A, day=today())]
    await user.open("/rules")
    await open_editor(user, 1)
    await open_apply(user)
    await preview_in_dialog(user)

    user.find(marker="apply-run").click()
    user.find(marker="apply-run").click()
    await eventually(lambda: "The apply is done" in text_of(user, "apply-status"), "The end of the apply")
    await asyncio.sleep(SETTLE_SECONDS)

    assert len(runs(sessions)) == 1
    assert youtube.added == [("WL", "new")]


async def test_the_apply_of_a_feed_rule_reads_the_feed_with_the_account(
    user, sessions, settings, account_copy, youtube, children
):
    store_cookies(sessions, settings)
    saved_rule(sessions, condition=MATCH_ALL, channels=())
    youtube.feed = [feed_video("f1", B, day=today())]
    await user.open("/rules")
    await open_editor(user, 1)
    await open_apply(user)

    await preview_in_dialog(user)
    assert shown(user, "apply-result-table") == [("Video f1", "Will be added")]
    user.find(marker="apply-run").click()
    await eventually(lambda: "The apply is done" in text_of(user, "apply-status"), "The end of the apply")

    assert youtube.added == [("WL", "f1")]


# The apply in the background of the service.

BANNER_TEXT = 'An apply of the rule "Long videos" runs. Videos added: {count}.'


async def preview_a_saved_rule(user, sessions, settings, youtube):
    """Open the apply dialog of a saved rule with one video to add, and preview it."""
    store_cookies(sessions, settings)
    saved_rule(sessions)
    youtube.uploads[A] = [entry("new", A, day=today())]
    await user.open("/rules")
    await open_editor(user, 1)
    await open_apply(user)
    await preview_in_dialog(user)


async def start_held_apply(user, children, *, run_body=True):
    """Click Add with a gate on the apply child, and wait until the child runs. Return the gate."""
    gate = children.gate = Gate(run_body=run_body)
    user.find(marker="apply-run").click()
    assert await asyncio.to_thread(gate.started.wait, WAIT_SECONDS)
    return gate


def set_added_count(sessions, run_id, count):
    """Write the added count of a run, as the child of an apply does after each add."""
    with sessions.begin() as session:
        session.get(Run, run_id).added_count = count


def banner_shown(user):
    return element_of(user, "apply-banner").visible


async def test_an_apply_goes_on_after_the_dialog_and_the_tab_closed_and_the_history_shows_it(
    nicegui_globals, settings, sessions, account_copy, youtube, children
):
    async with service_with_pages(settings, children) as (user, services):
        await preview_a_saved_rule(user, sessions, settings, youtube)
        gate = await start_held_apply(user, children)
        user.find(marker="apply-close").click()
        await eventually(lambda: not element_of(user, "apply-dialog").value, "The close of the dialog")
        # The tab closes: NiceGUI deletes the client of the page, and with it each subscription of the page.
        user.client.delete()
        events = (services.run_events.progress, services.run_events.finished, services.applies.changed)
        assert [event.callbacks for event in events] == [[], [], []]

        gate.release()

        await eventually(lambda: runs(sessions)[0].status is RunStatus.SUCCEEDED, "The end of the apply")
        (run,) = runs(sessions)
        assert (run.trigger, run.added_count, run.finished_at is not None) == (RunTrigger.RULE_APPLY, 1, True)
        assert youtube.added == [("WL", "new")]
        await user.open("/history")
        assert [(row["id"], row["trigger"], row["status"]) for row in rows(user, "runs-table")] == [
            (run.id, "Rule apply", "Succeeded")
        ]


async def test_the_dialog_and_the_banner_show_the_count_while_the_apply_runs_and_the_end_after_it(
    nicegui_globals, settings, sessions, account_copy, youtube, children
):
    async with service_with_pages(settings, children) as (user, services):
        notify = user.notify = TypedNotify()
        await preview_a_saved_rule(user, sessions, settings, youtube)
        assert not banner_shown(user)

        gate = await start_held_apply(user, children, run_body=False)

        (run,) = runs(sessions)
        running = f"The apply runs. Videos added: 1 of 1. The History page shows the apply as run {run.id}."
        await eventually(lambda: text_of(user, "apply-banner-text") == BANNER_TEXT.format(count=0), "The banner")
        set_added_count(sessions, run.id, 1)
        await eventually(lambda: text_of(user, "apply-status") == running, "The count in the dialog")
        await eventually(lambda: text_of(user, "apply-banner-text") == BANNER_TEXT.format(count=1), "The count")
        assert element_of(user, "apply-preview").enabled is False
        # The end of another run, for example of a run of the schedule, is not the end of the apply.
        services.run_events.finished.emit(run.id + 1, RunStatus.SUCCEEDED)
        await asyncio.sleep(SETTLE_SECONDS)
        assert text_of(user, "apply-status") == running
        assert notify.typed == []

        gate.release()

        done = f"The apply is done. Videos added: 1. The History page shows it as run {run.id}."
        await eventually(lambda: text_of(user, "apply-status") == done, "The end in the dialog")
        await eventually(lambda: not banner_shown(user), "The end of the banner")
        assert element_of(user, "apply-preview").enabled is True
        # A progress of the run that comes after its end changes nothing.
        services.run_events.progress.emit(run.id, 7, 0, ())
        await asyncio.sleep(SETTLE_SECONDS)
        assert text_of(user, "apply-status") == done
        assert not banner_shown(user)
        # The end of the run and the end of its task both reach the dialog, and the dialog tells the end once.
        assert notify.typed == [(done, "positive")]


async def test_the_banner_shows_a_running_apply_after_a_new_load_of_the_page(
    user, sessions, settings, account_copy, youtube, children
):
    await preview_a_saved_rule(user, sessions, settings, youtube)
    gate = await start_held_apply(user, children, run_body=False)
    (run,) = runs(sessions)
    set_added_count(sessions, run.id, 3)
    await eventually(lambda: text_of(user, "apply-banner-text") == BANNER_TEXT.format(count=3), "The count")

    # The count does not change after the new load, so no progress comes, and the page reads the count itself.
    await user.open("/rules")

    await eventually(lambda: text_of(user, "apply-banner-text") == BANNER_TEXT.format(count=3), "The banner")
    gate.release()
    await eventually(lambda: not banner_shown(user), "The end of the banner")
    # A load after the end shows no banner.
    await user.open("/rules")
    await eventually(lambda: not banner_shown(user), "No banner after the end")


async def test_a_second_apply_from_another_tab_while_an_apply_runs_gets_the_busy_text(
    user, sessions, settings, account_copy, youtube, children
):
    await preview_a_saved_rule(user, sessions, settings, youtube)
    other = PatientUser(user.http_client)
    await other.open("/rules")
    await open_editor(other, 1)
    await open_apply(other)
    await preview_in_dialog(other)

    gate = await start_held_apply(user, children, run_body=False)
    other.find(marker="apply-run").click()

    await eventually(lambda: "did not start" in text_of(other, "apply-status"), "The refusal")
    assert text_of(other, "apply-status") == f"The apply did not start. {BUSY}"
    # The dialog lets the user try again.
    assert (element_of(other, "apply-run").enabled, element_of(other, "apply-preview").enabled) == (True, True)
    assert len(runs(sessions)) == 1
    assert text_of(other, "apply-banner-text") == BANNER_TEXT.format(count=0)
    gate.release()
    await eventually(lambda: "The apply is done" in text_of(user, "apply-status"), "The end of the first apply")
    await other.should_see(marker="apply-run")


async def test_an_apply_whose_child_cannot_start_shows_the_error_of_its_run(
    user, sessions, settings, account_copy, youtube, children, caplog
):
    await preview_a_saved_rule(user, sessions, settings, youtube)
    children.plan.append(OSError("no such file"))

    user.find(marker="apply-run").click()

    await eventually(lambda: "The apply failed" in text_of(user, "apply-status"), "The failure")
    (run,) = runs(sessions)
    assert text_of(user, "apply-status") == (
        "The apply failed: The service cannot start the sync process: no such file The History page shows it as run "
        f"{run.id}."
    )
    assert element_of(user, "apply-preview").enabled is True
    # The runner logs the failed start as an error, and the failed start is the subject of this test.
    caplog.clear()


async def test_an_apply_whose_claim_fails_tells_the_user_and_can_start_again(
    user, sessions, settings, account_copy, youtube, children, monkeypatch, caplog
):
    await preview_a_saved_rule(user, sessions, settings, youtube)

    def failing_claim(self, trigger, wait_s=0):
        raise RuntimeError("the database is gone")

    monkeypatch.setattr(runner_module.SyncRunner, "claim", failing_claim)

    user.find(marker="apply-run").click()

    await user.should_see("Something went wrong on this page. The log of the service holds the details.")
    await user.should_see(marker="apply-run")
    assert element_of(user, "apply-run").enabled is True
    assert runs(sessions) == []
    assert "the database is gone" in caplog.text
    # The error of the click is the subject of this test.
    caplog.clear()


async def test_an_apply_whose_run_is_gone_at_its_end_says_so(
    user, sessions, settings, account_copy, youtube, children, monkeypatch, caplog
):
    await preview_a_saved_rule(user, sessions, settings, youtube)
    monkeypatch.setattr(rule_actions, "read_applied", lambda sessions, run_id: None)
    monkeypatch.setattr(rule_editor, "read_applied", lambda sessions, run_id: None)

    user.find(marker="apply-run").click()

    await eventually(lambda: "not in the database" in text_of(user, "apply-status"), "The end")
    (run,) = runs(sessions)
    assert text_of(user, "apply-status") == f"The apply ended, but its run {run.id} is not in the database any more."
    await eventually(lambda: "is not in the database any more" in caplog.text, "The log of the service task")
    assert f"The apply of rule 1 in run {run.id} failed" in caplog.text
    # The error of the service task is the subject of this test.
    caplog.clear()


async def test_the_stop_of_the_service_ends_a_running_apply_cleanly(
    nicegui_globals, settings, sessions, account_copy, youtube, children, monkeypatch, caplog
):
    caplog.set_level(logging.INFO, logger=applies_module.__name__)
    real_read = rule_actions.read_applied

    def slow_read(sessions, run_id):
        # The task of the apply still reads after the stop of its child, so the stop of the service must wait for it.
        time.sleep(0.5)
        return real_read(sessions, run_id)

    monkeypatch.setattr(rule_actions, "read_applied", slow_read)

    async with running_service(settings, children) as user:
        await preview_a_saved_rule(user, sessions, settings, youtube)
        gate = await start_held_apply(user, children)

    (run,) = runs(sessions)
    assert gate.terminated
    assert (run.status, run.error_text) == (RunStatus.FAILED, INTERRUPTED_ERROR)
    assert f"The apply of rule 1 ended as run {run.id} with failed" in caplog.text
    assert "Could not cancel" not in caplog.text
    assert "did not end" not in caplog.text


async def test_the_stop_of_the_service_cancels_the_task_of_an_apply_that_does_not_end(
    nicegui_globals, settings, sessions, account_copy, youtube, children, monkeypatch, caplog
):
    monkeypatch.setattr(applies_module, "CLOSE_WAIT_SECONDS", 0.1)
    real_read = rule_actions.read_applied
    stuck = threading.Event()

    def stuck_read(sessions, run_id):
        stuck.wait(WAIT_SECONDS)
        return real_read(sessions, run_id)

    monkeypatch.setattr(rule_actions, "read_applied", stuck_read)

    async with running_service(settings, children) as user:
        await preview_a_saved_rule(user, sessions, settings, youtube)
        await start_held_apply(user, children)
        stop_started = time.monotonic()

    stopped_in = time.monotonic() - stop_started
    stuck.set()
    # The stop does not wait for the read that does not end.
    assert stopped_in < WAIT_SECONDS / 2
    assert "An apply did not end within 0.1 s after the stop of the service" in caplog.text
    assert "Could not cancel" not in caplog.text


def hold_the_first_read_of_an_end(monkeypatch):
    """Hold the read of the end in the task of the first apply until the returned event ``release`` is set.

    The run of the apply has ended then, but its task still runs. Return the events ``started`` and ``release``.
    """
    started, release = threading.Event(), threading.Event()
    real_read = rule_actions.read_applied
    calls = []

    def held_read(sessions, run_id):
        calls.append(run_id)
        if len(calls) == 1:
            started.set()
            # Longer than each wait of a test, so a check of a test fails before the hold ends by itself.
            release.wait(3 * WAIT_SECONDS)
        return real_read(sessions, run_id)

    monkeypatch.setattr(rule_actions, "read_applied", held_read)
    return started, release


async def test_the_banner_goes_at_the_end_of_the_run_and_a_later_load_at_the_end_of_its_task(
    user, sessions, settings, account_copy, youtube, children, monkeypatch
):
    read_started, read_release = hold_the_first_read_of_an_end(monkeypatch)
    await preview_a_saved_rule(user, sessions, settings, youtube)
    gate = await start_held_apply(user, children, run_body=False)
    await eventually(lambda: banner_shown(user), "The banner")

    gate.release()
    assert await asyncio.to_thread(read_started.wait, WAIT_SECONDS)

    # The run ended, and its task still reads the end. The page learns the end of the run at once.
    await eventually(lambda: not banner_shown(user), "The end of the banner at the end of the run")
    # A page that loads now still sees the apply, and only the end of the task tells it the end.
    await user.open("/rules")
    await eventually(lambda: banner_shown(user), "The banner of the new load")
    read_release.set()
    await eventually(lambda: not banner_shown(user), "The end of the banner at the end of the task")


async def test_the_banner_shows_a_new_apply_while_the_task_of_the_old_one_still_reads_its_end(
    user, sessions, settings, account_copy, youtube, children, monkeypatch
):
    read_started, read_release = hold_the_first_read_of_an_end(monkeypatch)
    await preview_a_saved_rule(user, sessions, settings, youtube)
    gate = await start_held_apply(user, children, run_body=False)
    gate.release()
    assert await asyncio.to_thread(read_started.wait, WAIT_SECONDS)
    await eventually(lambda: "The apply is done" in text_of(user, "apply-status"), "The end of the first apply")
    user.find(marker="apply-preview").click()
    await user.should_see(marker="apply-run")

    second = await start_held_apply(user, children, run_body=False)

    # Both applies are active for a moment, and the newer one is the one that runs.
    await eventually(lambda: text_of(user, "apply-banner-text") == BANNER_TEXT.format(count=0), "The new banner")
    read_release.set()
    second.release()
    await eventually(lambda: not banner_shown(user), "The end of the banner")


async def test_the_banner_keeps_a_newer_count_that_came_during_its_first_read(
    user, sessions, settings, account_copy, youtube, children, monkeypatch
):
    await preview_a_saved_rule(user, sessions, settings, youtube)
    gate = await start_held_apply(user, children, run_body=False)
    (run,) = runs(sessions)
    old_label = element_of(user, "apply-banner-text")
    read_done, read_release = threading.Event(), threading.Event()
    real_read = rule_editor.read_applied

    def slow_read(sessions, run_id):
        applied = real_read(sessions, run_id)
        read_done.set()
        read_release.wait(WAIT_SECONDS)
        return applied

    monkeypatch.setattr(rule_editor, "read_applied", slow_read)
    opening = asyncio.create_task(user.open("/rules"))
    assert await asyncio.to_thread(read_done.wait, WAIT_SECONDS)
    # The page that loads read the count 0, and a progress with the count 2 comes before the read returns.
    set_added_count(sessions, run.id, 2)
    await eventually(lambda: old_label.text == BANNER_TEXT.format(count=2), "The progress")
    read_release.set()
    await opening

    await eventually(lambda: text_of(user, "apply-banner-text") == BANNER_TEXT.format(count=2), "The newer count")
    gate.release()
    await eventually(lambda: not banner_shown(user), "The end of the banner")


@contextlib.contextmanager
def held_claim(monkeypatch, outcome):
    """Hold each claim of a run in its thread until the returned event ``release`` is set. ``outcome`` takes the
    runner, the trigger and the real claim, and gives the result of the claim."""
    entered, release = threading.Event(), threading.Event()
    real_claim = runner_module.SyncRunner.claim

    def claim(self, trigger, wait_s=0):
        entered.set()
        release.wait(WAIT_SECONDS)
        return outcome(self, trigger, lambda: real_claim(self, trigger, wait_s))

    monkeypatch.setattr(runner_module.SyncRunner, "claim", claim)
    try:
        yield entered, release
    finally:
        release.set()


async def test_a_cancel_of_the_task_of_an_apply_before_its_claim_leaves_no_active_apply(
    nicegui_globals, settings, sessions, account_copy, children, monkeypatch, caplog
):
    saved_rule(sessions)
    children.plan.append(EndedChild(0))
    with held_claim(monkeypatch, lambda runner, trigger, claim: claim()) as (entered, release):
        async with service_with_pages(settings, children) as (_, services):
            caller = asyncio.create_task(services.applies.start(1, today()))
            assert await asyncio.to_thread(entered.wait, WAIT_SECONDS)
            (task,) = services.applies._tasks

            task.cancel()

            # The caller learns the cancel, and does not wait for ever.
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(caller, WAIT_SECONDS)
            release.set()
            await eventually(lambda: "started after the end of its task" in caplog.text, "The late claim")
            assert len(runs(sessions)) == 1
            assert services.applies.current() is None
            # The thread runs the claimed run to its end, so the sync lock is free again.
            await eventually(lambda: runs(sessions)[0].status is RunStatus.SUCCEEDED, "The end of the run")
            assert not services.runner.is_active()


def refuse_claim(runner, trigger, claim):
    raise RuntimeError("the database is gone")


@pytest.mark.parametrize(
    ("outcome", "logged"),
    [
        (lambda runner, trigger, claim: None, f"The apply of rule 1 did not start: {runner_module.BUSY_TEXT}"),
        (refuse_claim, "the database is gone"),
    ],
    ids=["busy", "error"],
)
async def test_a_start_that_fails_after_its_caller_is_gone_goes_to_the_log(
    nicegui_globals, settings, sessions, account_copy, children, monkeypatch, caplog, outcome, logged
):
    saved_rule(sessions)
    with held_claim(monkeypatch, outcome) as (entered, release):
        async with service_with_pages(settings, children) as (_, services):
            caller = asyncio.create_task(services.applies.start(1, today()))
            assert await asyncio.to_thread(entered.wait, WAIT_SECONDS)
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller

            release.set()

            await eventually(lambda: not services.applies._tasks, "The end of the task")
            assert "The apply of rule 1 did not start" in caplog.text
            assert logged in caplog.text
            assert runs(sessions) == []
    if outcome is refuse_claim:
        # The error of the claim is the subject of this test, so only its one record leaves the check of the fixture
        # `no_logged_errors`. Each other error record of the test stays in that check.
        (record,) = [
            record
            for record in caplog.records
            if record.levelno >= logging.ERROR and record.getMessage() == "The apply of rule 1 did not start"
        ]
        caplog.records.remove(record)


async def test_close_cancels_the_task_of_an_apply_that_does_not_end_and_waits_for_the_cancel(
    nicegui_globals, settings, sessions, account_copy, youtube, children, monkeypatch
):
    monkeypatch.setattr(applies_module, "CLOSE_WAIT_SECONDS", 0.1)
    async with service_with_pages(settings, children) as (user, services):
        await preview_a_saved_rule(user, sessions, settings, youtube)
        gate = await start_held_apply(user, children, run_body=False)
        tasks = list(services.applies._tasks)

        await services.applies.close()

        assert [task.cancelled() for task in tasks] == [True]
        gate.release()
        await eventually(lambda: runs(sessions)[0].status is RunStatus.SUCCEEDED, "The end of the run")


async def test_the_stop_of_the_service_waits_for_the_applies_before_it_stops_the_scheduler(
    nicegui_globals, settings, sessions, children, monkeypatch
):
    """The order of the comments of the lifespan: the stop of the runner, the end of the applies, then the scheduler."""
    order = []
    real_close = applies_module.Applies.close
    real_stop = runner_module.SyncRunner.stop
    real_shutdown = scheduler_module.Scheduler.shutdown

    async def close(self):
        order.append("applies")
        await real_close(self)

    def stop(self, timeout):
        order.append("runner")
        return real_stop(self, timeout)

    async def shutdown(self):
        order.append("scheduler")
        await real_shutdown(self)

    monkeypatch.setattr(applies_module.Applies, "close", close)
    monkeypatch.setattr(runner_module.SyncRunner, "stop", stop)
    monkeypatch.setattr(scheduler_module.Scheduler, "shutdown", shutdown)

    async with running_service(settings, children):
        pass

    assert order == ["runner", "applies", "scheduler"]


# The texts.


def shown_texts(user) -> list[str]:
    """Each text that the page shows: the text of an element, the label of an input, each option of a select, each
    column label of a table, and each cell of a preview that the page writes itself."""
    texts = []
    for element in user.client.elements.values():
        if isinstance(element, TextElement) and element.text:
            texts.append(element.text)
        if isinstance(element, LabelElement) and element.props.get("label"):
            texts.append(element.props["label"])
        if isinstance(element, ui.select) and isinstance(element.options, dict):
            texts.extend(element.options.values())
        if isinstance(element, ui.table):
            texts.extend(column["label"] for column in element.columns)
            if element.columns[0]["name"] == "date":
                texts.extend(text for row in element.rows for text in (row["date"], row["mark"]))
    return texts


async def test_each_text_of_the_preview_and_the_apply_goes_through_the_translation(
    nicegui_globals, settings, sessions, youtube, monkeypatch
):
    class Marking:
        def gettext(self, message):
            return f"<<{message}>>"

    monkeypatch.setattr(i18n, "_translations", Marking())
    store(
        sessions,
        Channel(channel_id=A, title="Alpha", is_subscribed=True),
        Playlist(playlist_id="WL", title="Watch later", is_own=True),
    )
    saved_rule(sessions)
    videos = tuple(
        PreviewVideo(f"v{index}", f"Video {index}", "Alpha", None if mark is Mark.UNDECIDED else today(), mark)
        for index, mark in enumerate(Mark)
    )
    preview = Preview(videos, {mark: 101 for mark in Mark}, ("Channel Beta: no answer",), ("Alpha",))
    line = json.dumps(preview.to_value()) + "\n"
    baselines = json.dumps(Preview((), {}, baseline_sources=("Alpha",)).to_value()) + "\n"
    mode_changed = json.dumps(Preview((), {}, mode_changed=True).to_value()) + "\n"
    children = Children(settings)
    children.plan = [
        EndedChild(0, baselines),
        EndedChild(0, mode_changed),
        EndedChild(0, line),
        EndedChild(0, line),
        EndedChild(1, "The cause.\n"),
    ]
    data = {"Long videos", "Alpha", "Channel Beta: no answer", today().isoformat(), "Watch later"}
    data |= {video.title for video in videos}
    time_text = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} \S+")

    async with running_service(settings, children) as user:
        await user.open("/rules")
        await open_editor(user, 1)
        set_value(user, "rule-enabled", False)
        await preview_in_editor(user)
        await eventually(lambda: elements_of(user, "preview-not-added"), "The note")
        texts = shown_texts(user)
        await preview_in_editor(user)
        await eventually(lambda: elements_of(user, "preview-mode-changed"), "The change of the mode")
        texts.extend(shown_texts(user))
        await preview_in_editor(user)
        await eventually(lambda: elements_of(user, "preview-table"), "The videos")
        set_value(user, "rule-name", "Changed")
        await open_apply(user)
        await preview_in_dialog(user)
        texts.extend(shown_texts(user))
        user.find(marker="apply-run").click()
        await eventually(lambda: "<<The apply failed" in text_of(user, "apply-status"), "The failure")
        texts.extend(shown_texts(user))

    for expected in (
        "<<Preview>>",
        "<<Preview of the next run>>",
        "<<Videos from>>",
        "<<Will be added>>",
        "<<No longer matches>>",
        "<<No date>>",
        "<<Result>>",
        "<<Add 100 videos>>",
        "<<The editor has changes that are not saved. The apply uses the saved version of the rule.>>",
    ):
        assert expected in texts
    untranslated = [
        text
        for text in texts
        if not (text.startswith("<<") and text.endswith(">>"))
        and text not in data
        and text not in NAMES.values()
        and not time_text.fullmatch(text)
    ]
    assert untranslated == []
