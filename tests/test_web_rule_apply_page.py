"""Tests of the preview of a rule in the editor and of the apply dialog of the rules page.

The children of the service run in the test process: the real bodies of the preview and the apply, with fakes for the
anonymous reads of yt-dlp and for the account client. No test reads or changes YouTube.
"""

import asyncio
import contextlib
import io
import json
import re
from datetime import timedelta

import pytest
from nicegui import ui
from nicegui.elements.mixins.label_element import LabelElement
from nicegui.elements.mixins.text_element import TextElement
from sqlalchemy import select
from test_sync import LONG, A, B, entry, feed_video, upcoming
from ui_support import element_of, elements_of, eventually, running_service, store, text_of

from youtube_automanager import __main__ as cli
from youtube_automanager import i18n
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
from youtube_automanager.rule_actions import apply_once, preview_once
from youtube_automanager.runner import sync_lock_held
from youtube_automanager.settings import Settings
from youtube_automanager.web.ui import rules_page
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
    return rules_page.today_in(ZONE)


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


class Children:
    """Starts the children of the service in the test process and records each command.

    A preview and an apply run their real body. A child of ``plan`` replaces the next start, for a result that the
    body cannot give.
    """

    def __init__(self, settings):
        self.settings = settings
        self.commands = []
        self.plan = []

    def __call__(self, command, env, input_text):
        self.commands.append(list(command))
        if self.plan:
            return self.plan.pop(0)
        args = cli._parser().parse_args(command[3:])
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            if args.command == "preview-rule":
                code = preview_once(self.settings, args.since, args.rule_id, args.draft)
            elif args.command == "apply-rule":
                code = apply_once(args.run_id, args.rule_id, args.since, self.settings)
            else:
                code = 0
        return EndedChild(code, stderr.getvalue())

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


async def test_the_preview_of_a_new_rule_lists_what_the_next_run_checks_and_stores_nothing(
    user, sessions, account_copy, youtube, children
):
    old = [entry(f"old{index:02}", A, title=f"Old video {index}", day=days_back(2)) for index in range(25)]
    read_before(sessions, A, *(item.video_id for item in old))
    youtube.uploads[A] = [
        entry("long", A, title="A long video", day=today()),
        entry("short", A, title="A short video", duration_s=30, day=today()),
        entry("live soon", A, title="Live soon", duration_s=None, day=None, live_status="is_upcoming"),
        *old,
    ]
    await user.open("/rules")
    await open_editor(user)
    assert not elements_of(user, "preview-since")
    assert text_of(user, "preview-hint").startswith("The preview lists the videos of the next run that this rule")

    set_value(user, "rule-channels", [A])
    user.find(marker="add-condition").click()
    await eventually(lambda: elements_of(user, "field-0"), "The condition")
    set_value(user, "field-0", "duration_s")
    await eventually(lambda: elements_of(user, "minutes-0"), "The input of the minutes")
    assert elements_of(user, "operator-0")[-1].value == ">="
    set_value(user, "minutes-0", 2)
    await preview_in_editor(user)

    # The old videos match too, but the next run saw them before, so the preview leaves them out.
    assert shown(user, "preview-table") == [("Live soon", "Undecided"), ("A long video", "Matches")]
    assert rows(user, "preview-table")[1] | {"id": "long"} == {
        "id": "long",
        "date": today().isoformat(),
        "channel": "Alpha",
        "title": "A long video",
        "url": "https://www.youtube.com/watch?v=long",
        "mark": "Matches",
        "mark_color": "positive",
    }
    assert rows(user, "preview-table")[0]["date"] == "No date"
    assert text_of(user, "preview-counts") == "Matches: 1, Undecided: 1"
    assert not elements_of(user, "preview-baselines")
    (command,) = children.of("preview-rule")
    assert command[4:] == [
        "--draft=" + DraftRule({"and": [{">=": [{"var": "duration_s"}, 120]}]}, (A,), False).to_json()
    ]
    with sessions() as session:
        assert session.scalars(select(Rule)).all() == []
    assert seen_count(sessions) == 25
    assert youtube.added == []


async def test_the_preview_names_the_channels_that_the_next_run_starts_with_a_baseline(
    user, sessions, account_copy, youtube, children
):
    read_before(sessions, A, "old")
    youtube.uploads[A] = [entry("new", A, title="New in Alpha", day=today()), entry("old", A, day=days_back(2))]
    youtube.uploads[B] = [entry("beta", B, title="From Beta", day=today())]
    await user.open("/rules")
    await open_editor(user)
    set_value(user, "rule-channels", [A, B])

    await preview_in_editor(user)

    assert shown(user, "preview-table") == [("New in Alpha", "Matches")]
    assert text_of(user, "preview-baselines") == (
        "The service has not read these sources yet, so the next run stores their newest videos as seen and adds none "
        "of them: Beta"
    )
    assert youtube.reads == [A]


async def test_a_new_rule_on_a_new_channel_previews_only_the_baseline(user, account_copy, youtube, children):
    youtube.uploads[A] = [entry("new", A, day=today())]
    await user.open("/rules")
    await open_editor(user)
    set_value(user, "rule-channels", [A])

    await preview_in_editor(user)

    assert text_of(user, "preview-empty") == "The next run finds no new video that this rule matches."
    assert "Alpha" in text_of(user, "preview-baselines")
    assert youtube.reads == []


async def test_the_preview_says_when_a_new_mode_starts_each_source_again(user, sessions, account_copy, youtube):
    read_before(sessions, A, "old")
    store(
        sessions,
        Setting(key=PROCESSING_MODE_KEY, value={"mode": "newest", "count": 5}),
        Setting(key=STATE_MODE_KEY, value={"mode": "stop_at_seen"}),
    )
    youtube.uploads[A] = [entry("new", A, day=today()), entry("old", A, day=days_back(2))]
    await user.open("/rules")
    await open_editor(user)
    set_value(user, "rule-channels", [A])

    await preview_in_editor(user)

    assert text_of(user, "preview-mode-changed") == (
        "The processing mode changed, so the next run starts each source again: it stores the newest videos of each "
        "source as seen and adds none of them."
    )
    assert not elements_of(user, "preview-baselines")
    assert elements_of(user, "preview-empty")
    assert youtube.reads == []


async def test_the_preview_of_a_change_lists_the_new_videos_whose_result_changes(
    user, sessions, account_copy, youtube, children
):
    saved_rule(sessions, condition={"and": [LONG]})
    read_before(sessions, A, "old short")
    read_before(sessions, B, "old beta")
    youtube.uploads[A] = [
        entry("long", A, title="Long", day=today()),
        entry("short", A, title="Short", duration_s=30, day=today()),
        entry("old short", A, title="Old short", duration_s=30, day=days_back(2)),
    ]
    youtube.uploads[B] = [
        entry("beta", B, title="From Beta", duration_s=30, day=today()),
        entry("old beta", B, title="Old from Beta", duration_s=30, day=days_back(2)),
    ]
    await user.open("/rules")
    await open_editor(user, 1)
    assert "your changes change the result" in text_of(user, "preview-hint")

    await preview_in_editor(user)
    assert text_of(user, "preview-empty") == "Your changes change the result for no new video of the next run."

    set_value(user, "operator-0", "<")
    await eventually(lambda: elements_of(user, "minutes-0")[-1].value == 2, "The comparison")
    set_value(user, "rule-channels", [A, B])
    await preview_in_editor(user)

    await eventually(lambda: shown(user, "preview-table"), "The videos of the change")
    assert sorted(shown(user, "preview-table")) == [
        ("From Beta", "Matches now"),
        ("Long", "No longer matches"),
        ("Short", "Matches now"),
    ]
    assert children.of("preview-rule")[-1][4] == "--rule-id=1"
    with sessions() as session:
        assert session.get(Rule, 1).condition == {"and": [LONG]}


async def test_the_preview_of_a_rule_that_is_off_says_that_no_run_uses_it(user, sessions, account_copy, youtube):
    read_before(sessions, A, "old")
    youtube.uploads[A] = [entry("new", A, title="New", day=today()), entry("old", A, day=days_back(2))]
    await user.open("/rules")
    await open_editor(user)
    set_value(user, "rule-channels", [A])
    set_value(user, "rule-enabled", False)

    await preview_in_editor(user)

    assert text_of(user, "preview-note") == (
        "The rule is off, so no run uses it. The preview shows what the next run does with the rule on."
    )
    assert shown(user, "preview-table") == [("New", "Matches")]


async def test_the_preview_of_a_rule_that_is_on_has_no_note(user, sessions, account_copy, youtube):
    read_before(sessions, A, "old")
    youtube.uploads[A] = [entry("new", A, title="New", day=today()), entry("old", A, day=days_back(2))]
    await user.open("/rules")
    await open_editor(user)
    set_value(user, "rule-channels", [A])

    await preview_in_editor(user)

    assert shown(user, "preview-table") == [("New", "Matches")]
    assert not elements_of(user, "preview-note")


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
        "The read stops after 500 videos of a source, so the older videos of these sources are not in the list: Alpha"
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
        ("Added before", "Already added"),
        ("No duration", "Undecided"),
        ("Older", "Will be added"),
    ]
    assert text_of(user, "apply-result-counts") == "Will be added: 2, Already added: 1, Undecided: 2"
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
    assert text_of(user, "apply-status") == (
        f"The apply is done with errors. Videos added: 1. Errors: 1. The History page shows them in run {run.id}."
    )
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
        await eventually(lambda: elements_of(user, "preview-baselines"), "The baselines")
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
        if not (text.startswith("<<") and text.endswith(">>")) and text not in data and not time_text.fullmatch(text)
    ]
    assert untranslated == []
