import asyncio
import re
import time
from datetime import UTC, datetime, timedelta

import pytest
from nicegui import ui
from nicegui.elements.mixins.label_element import LabelElement
from nicegui.elements.mixins.text_element import TextElement
from sqlalchemy import delete, select
from ui_support import (
    element_of,
    elements_of,
    eventually,
    running_service,
    set_value,
    store,
    text_of,
)

from youtube_automanager import i18n
from youtube_automanager.db.models import (
    Run,
    RunAction,
    RunActionKind,
    RunStatus,
    RunTrigger,
    Schedule,
    ScheduleKind,
    Setting,
)
from youtube_automanager.web.ui import history_page
from youtube_automanager.web.ui.dates import today_in

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("logging_setups", "no_logged_errors")]

TIME = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} \S+")
# A moment of 12 October 2026 in UTC. Kyiv is at UTC+3 on this day.
BASE = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
# How long a test waits for a click that must change nothing. A click that does change something shows it within ms.
SETTLE_SECONDS = 0.3


def run(number: int, started_at: datetime | None = None, **values) -> Run:
    """A succeeded run by the schedule. Run ``number`` starts ``number`` minutes after ``BASE``."""
    values = {"status": RunStatus.SUCCEEDED, "trigger": RunTrigger.SCHEDULE} | values
    return Run(id=number, started_at=started_at or BASE + timedelta(minutes=number), **values)


def action(run_id: int, number: int, kind: RunActionKind = RunActionKind.ADDED, **values) -> RunAction:
    return RunAction(run_id=run_id, id=number, at=BASE + timedelta(seconds=number), action=kind, **values)


def rows_of(user, marker: str) -> list[dict]:
    return element_of(user, marker).rows


def run_ids(user) -> list[int]:
    tables = elements_of(user, "runs-table")
    return [row["id"] for row in tables[0].rows] if tables else []


async def runs_are(user, expected: list[int]) -> None:
    await eventually(lambda: run_ids(user) == expected, f"The table of the runs with {expected}")


def action_ids(user) -> list[int]:
    return [row["id"] for row in rows_of(user, "actions-table")]


async def open_run(user, run_id: int) -> None:
    user.find(marker="runs-table").trigger("rowClick", args=run_id)
    await eventually(
        lambda: text_of(user, "details-title") == f"Actions of run {run_id}", f"The actions of run {run_id}"
    )


async def test_history_page_is_in_the_drawer(user):
    await user.open("/")

    user.find(marker="nav:/history").click()

    await user.should_see(marker="refresh")


async def test_a_new_service_has_no_runs_and_the_default_retention(user):
    await user.open("/history")

    assert text_of(user, "no-runs") == "No run yet."
    await user.should_not_see(marker="runs-table")
    await user.should_not_see(marker="details")
    assert element_of(user, "retention-days").value == 90
    assert "03:30 (Europe/Kyiv)" in text_of(user, "retention-hint")


async def test_runs_come_newest_first_in_the_time_zone_of_the_schedule(user, sessions):
    store(
        sessions,
        run(1, datetime(2026, 10, 1, 15, 0, tzinfo=UTC)),
        run(
            3,
            datetime(2026, 10, 3, 21, 30, tzinfo=UTC),
            finished_at=datetime(2026, 10, 3, 21, 45, tzinfo=UTC),
            status=RunStatus.FAILED,
            trigger=RunTrigger.MANUAL,
            added_count=2,
            seen_count=40,
            error_text="The sync process exited with code 1.",
        ),
        run(2, datetime(2026, 10, 2, 9, 0, tzinfo=UTC), status=RunStatus.RUNNING),
    )

    await user.open("/history")

    table = element_of(user, "runs-table")
    assert [column["label"] for column in table.columns] == [
        "Run",
        "Started",
        "Finished",
        "Status",
        "Trigger",
        "Videos added",
        "Videos seen",
        "Error",
    ]
    assert [row["id"] for row in table.rows] == [3, 2, 1]
    assert table.rows[0] == {
        "id": 3,
        # Kyiv is at UTC+3 until 25 October 2026.
        "started": "2026-10-04 00:30 EEST",
        "finished": "2026-10-04 00:45 EEST",
        "status": "Failed",
        "status_color": "negative",
        "trigger": "Manual",
        "added": 2,
        "seen": 40,
        "error": "The sync process exited with code 1.",
    }
    assert (table.rows[1]["status"], table.rows[1]["finished"], table.rows[1]["error"]) == ("In progress", "", "")
    assert (table.rows[2]["started"], table.rows[2]["trigger"]) == ("2026-10-01 18:00 EEST", "Schedule")
    assert text_of(user, "runs-summary") == "Runs 1-3 of 3"
    await user.should_not_see(marker="runs-pagination")


async def test_runs_that_start_at_the_same_time_come_by_their_id_newest_first(user, sessions):
    store(sessions, *[run(number, BASE) for number in (4, 9, 2)])

    await user.open("/history")

    assert run_ids(user) == [9, 4, 2]


async def test_the_times_of_the_runs_follow_the_time_zone_of_the_schedule(
    nicegui_globals, settings, sessions, fake_starter
):
    store(
        sessions,
        Schedule(enabled=True, kind=ScheduleKind.CRON, timezone="America/New_York"),
        run(1, datetime(2026, 1, 5, 1, 0, tzinfo=UTC)),
    )

    async with running_service(settings, fake_starter) as user:
        await user.open("/history")

        assert rows_of(user, "runs-table")[0]["started"] == "2026-01-04 20:00 EST"
        assert "(America/New_York)" in text_of(user, "retention-hint")


async def test_the_table_has_pages_of_50_runs(user, sessions):
    store(sessions, *[run(number) for number in range(1, 121)])

    await user.open("/history")

    assert run_ids(user) == list(range(120, 70, -1))
    assert text_of(user, "runs-summary") == "Runs 1-50 of 120"
    pagination = element_of(user, "runs-pagination")
    assert (pagination.props["min"], pagination.props["max"], pagination.value) == (1, 3, 1)

    set_value(user, "runs-pagination", 2)
    await runs_are(user, list(range(70, 20, -1)))
    assert text_of(user, "runs-summary") == "Runs 51-100 of 120"
    assert element_of(user, "runs-pagination").value == 2

    set_value(user, "runs-pagination", 3)
    await runs_are(user, list(range(20, 0, -1)))
    assert text_of(user, "runs-summary") == "Runs 101-120 of 120"


@pytest.mark.parametrize(("count", "pages"), [(50, None), (51, 2)])
async def test_the_pagination_shows_only_when_the_runs_need_more_than_one_page(user, sessions, count, pages):
    store(sessions, *[run(number) for number in range(1, count + 1)])

    await user.open("/history")

    assert len(run_ids(user)) == 50
    if pages is None:
        await user.should_not_see(marker="runs-pagination")
    else:
        assert element_of(user, "runs-pagination").props["max"] == pages


async def test_the_refresh_shows_the_last_page_when_the_runs_became_fewer(user, sessions):
    store(sessions, *[run(number) for number in range(1, 121)])
    await user.open("/history")
    set_value(user, "runs-pagination", 3)
    await runs_are(user, list(range(20, 0, -1)))

    with sessions.begin() as session:
        session.execute(delete(Run).where(Run.id <= 60))
    user.find(marker="refresh").click()

    await runs_are(user, list(range(70, 60, -1)))
    assert text_of(user, "runs-summary") == "Runs 51-60 of 60"
    assert element_of(user, "runs-pagination").value == 2


async def test_a_slow_read_that_ends_late_does_not_replace_a_newer_table(user, sessions, monkeypatch):
    store(sessions, run(1, status=RunStatus.FAILED), run(2, status=RunStatus.SUCCEEDED))
    real_read_runs = history_page.read_runs

    slow_reads = []

    def slow_for_failed(*arguments):
        if arguments[1].status is RunStatus.FAILED:
            slow_reads.append(arguments)
            time.sleep(0.5)
        return real_read_runs(*arguments)

    monkeypatch.setattr(history_page, "read_runs", slow_for_failed)
    await user.open("/history")

    set_value(user, "filter-status", "failed")
    # The page reads the filter when its read starts, so the slow read of the failed runs must start first.
    await eventually(lambda: slow_reads, "The start of the slow read")
    set_value(user, "filter-status", "succeeded")

    await runs_are(user, [2])
    await asyncio.sleep(1)
    assert run_ids(user) == [2]


async def test_a_slow_read_of_the_actions_that_ends_late_does_not_replace_a_newer_table(user, sessions, monkeypatch):
    store(sessions, run(1), action(1, 1, RunActionKind.ERROR), action(1, 2, RunActionKind.ADDED))
    real_read_run_detail = history_page.read_run_detail

    def slow_for_errors(*arguments):
        if arguments[2] is RunActionKind.ERROR:
            time.sleep(0.5)
        return real_read_run_detail(*arguments)

    monkeypatch.setattr(history_page, "read_run_detail", slow_for_errors)
    await user.open("/history")
    await open_run(user, 1)

    set_value(user, "filter-action", "error")
    set_value(user, "filter-action", "added")

    await eventually(lambda: action_ids(user) == [2], "The table of the added actions")
    await asyncio.sleep(1)
    assert action_ids(user) == [2]


async def test_a_filter_goes_back_to_the_first_page(user, sessions):
    store(
        sessions,
        *[run(number, status=RunStatus.FAILED if number % 2 else RunStatus.SUCCEEDED) for number in range(1, 121)],
    )
    await user.open("/history")
    set_value(user, "runs-pagination", 3)
    await runs_are(user, list(range(20, 0, -1)))

    set_value(user, "filter-status", "failed")

    # 60 failed runs are the odd ids. The page is the first one of the two, not the third one.
    await runs_are(user, list(range(119, 19, -2)))
    assert text_of(user, "runs-summary") == "Runs 1-50 of 60"
    assert element_of(user, "runs-pagination").value == 1


async def test_the_status_filter(user, sessions):
    store(
        sessions,
        run(1, status=RunStatus.SUCCEEDED),
        run(2, status=RunStatus.FAILED),
        run(3, status=RunStatus.RUNNING),
        run(4, status=RunStatus.FAILED),
    )
    await user.open("/history")

    # The simulated user opens the list of the select and chooses an option, as a person does.
    user.find(marker="filter-status").click()
    user.find("Failed").click()

    await runs_are(user, [4, 2])
    assert text_of(user, "runs-summary") == "Runs 1-2 of 2"
    set_value(user, "filter-status", "running")
    await runs_are(user, [3])
    set_value(user, "filter-status", "succeeded")
    await runs_are(user, [1])
    set_value(user, "filter-status", "any")
    await runs_are(user, [4, 3, 2, 1])


async def test_the_trigger_filter(user, sessions):
    store(
        sessions,
        run(1, trigger=RunTrigger.SCHEDULE),
        run(2, trigger=RunTrigger.MANUAL),
        run(3, trigger=RunTrigger.SCHEDULE),
    )
    await user.open("/history")

    set_value(user, "filter-trigger", "manual")
    await runs_are(user, [2])
    set_value(user, "filter-trigger", "schedule")
    await runs_are(user, [3, 1])
    set_value(user, "filter-trigger", "any")
    await runs_are(user, [3, 2, 1])


async def test_an_apply_of_a_rule_shows_with_its_own_trigger_and_the_filter_selects_it(user, sessions):
    store(sessions, run(1, trigger=RunTrigger.SCHEDULE), run(2, trigger=RunTrigger.RULE_APPLY, added_count=4))
    await user.open("/history")

    assert [row["trigger"] for row in rows_of(user, "runs-table")] == ["Rule apply", "Schedule"]
    assert element_of(user, "filter-trigger").options == {
        "any": "All",
        "schedule": "Schedule",
        "manual": "Manual",
        "rule_apply": "Rule apply",
    }
    set_value(user, "filter-trigger", "rule_apply")
    await runs_are(user, [2])


async def test_the_filters_work_together(user, sessions):
    store(
        sessions,
        run(1, status=RunStatus.FAILED, trigger=RunTrigger.MANUAL),
        run(2, status=RunStatus.FAILED, trigger=RunTrigger.SCHEDULE),
        run(3, status=RunStatus.SUCCEEDED, trigger=RunTrigger.MANUAL),
    )
    await user.open("/history")

    set_value(user, "filter-status", "failed")
    set_value(user, "filter-trigger", "manual")

    await runs_are(user, [1])


async def test_a_filter_without_matches_says_that_no_run_matches(user, sessions):
    store(sessions, run(1))
    await user.open("/history")

    set_value(user, "filter-status", "failed")

    await eventually(
        lambda: text_of(user, "no-runs") == "No run matches the filters.", "The message that no run matches"
    )
    await user.should_not_see(marker="runs-table")
    set_value(user, "filter-status", "any")
    await runs_are(user, [1])


@pytest.fixture
def runs_of_four_days(sessions):
    """Runs around the edges of two days of Kyiv, which is at UTC+3: 2 October starts at 21:00 UTC on 1 October."""
    store(
        sessions,
        run(1, datetime(2026, 10, 1, 20, 59, tzinfo=UTC)),  # 1 October, 23:59
        run(2, datetime(2026, 10, 1, 21, 0, tzinfo=UTC)),  # 2 October, 00:00
        run(3, datetime(2026, 10, 2, 20, 59, tzinfo=UTC)),  # 2 October, 23:59
        run(4, datetime(2026, 10, 2, 21, 0, tzinfo=UTC)),  # 3 October, 00:00
        run(5, datetime(2026, 10, 4, 12, 0, tzinfo=UTC)),  # 4 October, 15:00
    )


@pytest.mark.parametrize(
    ("first", "last", "expected"),
    [
        ("2026-10-02", "2026-10-02", [3, 2]),
        ("2026-10-02", None, [5, 4, 3, 2]),
        (None, "2026-10-02", [3, 2, 1]),
        ("2026-10-03", "2026-10-04", [5, 4]),
        ("2026-10-04", "2026-10-02", []),
        ("1900-01-01", "9999-12-31", [5, 4, 3, 2, 1]),
        ("0001-01-01", "0001-01-02", []),
    ],
)
async def test_the_date_range_counts_whole_days_of_the_time_zone_of_the_schedule(
    user, runs_of_four_days, first, last, expected
):
    await user.open("/history")

    user.find(marker="filter-from").clear()
    if first:
        user.find(marker="filter-from").type(first)
    if last:
        user.find(marker="filter-to").type(last)

    if expected:
        await runs_are(user, expected)
    else:
        await eventually(lambda: elements_of(user, "no-runs"), "The message that no run matches")
        await user.should_not_see(marker="runs-table")


WRONG_DATE = "This is not a date, so the filter does not use it."


async def test_the_today_buttons_put_today_of_the_time_zone_of_the_schedule_into_the_date_fields(user, sessions):
    now = datetime.now(UTC)
    store(sessions, run(1, now - timedelta(days=2)), run(2, now))
    today = today_in("Europe/Kyiv").isoformat()
    await user.open("/history")
    await runs_are(user, [2, 1])

    user.find(marker="filter-from-today").click()

    await runs_are(user, [2])
    assert element_of(user, "filter-from").value == today
    user.find(marker="filter-to-today").click()
    await eventually(lambda: element_of(user, "filter-to").value == today, "Today as the last day")
    await runs_are(user, [2])


def date_error(user, marker: str) -> tuple[object, object]:
    element = element_of(user, marker)
    return element.props.get("error"), element.props.get("error-message")


async def test_a_date_that_is_not_a_date_is_marked_and_filters_nothing_and_a_cleared_date_lifts_the_filter(
    user, runs_of_four_days
):
    await user.open("/history")
    assert date_error(user, "filter-from") == (None, None)
    set_value(user, "filter-from", "2026-10-04")
    await runs_are(user, [5])

    set_value(user, "filter-from", "2026-13-45")
    await runs_are(user, [5, 4, 3, 2, 1])
    assert date_error(user, "filter-from") == (True, WRONG_DATE)
    assert date_error(user, "filter-to") == (None, None)
    set_value(user, "filter-from", "2026-10-04")
    await runs_are(user, [5])
    assert date_error(user, "filter-from") == (None, None)
    user.find(marker="filter-from").clear()
    await runs_are(user, [5, 4, 3, 2, 1])
    assert date_error(user, "filter-from") == (None, None)


async def test_a_wrong_date_in_one_field_keeps_its_mark_while_the_other_field_changes(user, runs_of_four_days):
    await user.open("/history")

    set_value(user, "filter-to", "yesterday")
    await eventually(lambda: date_error(user, "filter-to") == (True, WRONG_DATE), "The mark of the wrong date")
    set_value(user, "filter-from", "2026-10-03")

    await runs_are(user, [5, 4])
    assert date_error(user, "filter-to") == (True, WRONG_DATE)
    assert date_error(user, "filter-from") == (None, None)
    set_value(user, "filter-to", " ")
    await eventually(lambda: date_error(user, "filter-to") == (None, None), "The end of the mark of a blank field")


async def test_the_refresh_button_shows_new_runs(user, sessions):
    store(sessions, run(1))
    await user.open("/history")
    assert run_ids(user) == [1]

    store(sessions, run(2))
    user.find(marker="refresh").click()

    await runs_are(user, [2, 1])


async def test_a_failed_run_shows_its_error_text_in_full(user, sessions):
    error = "Traceback (most recent call last):\n" + "\n".join(
        f'  File "step{number}.py", line {number}' for number in range(30)
    )
    store(sessions, run(1), run(2, status=RunStatus.FAILED, error_text=error))
    await user.open("/history")

    assert [row["error"] for row in rows_of(user, "runs-table")] == [error, ""]
    await open_run(user, 2)
    assert text_of(user, "run-error") == error
    assert text_of(user, "details-status") == "Failed"

    await open_run(user, 1)
    await user.should_not_see(marker="run-error")
    assert text_of(user, "details-status") == "Succeeded"


async def test_a_click_on_a_run_opens_its_actions_in_the_order_of_their_time(user, sessions):
    store(
        sessions,
        run(1, started_at=datetime(2026, 10, 2, 15, 5, tzinfo=UTC)),
        run(2),
        action(1, 12, RunActionKind.SKIPPED, video_id="vid-skip", channel_id="UC2", rule_id=None, detail="too short"),
        action(
            1,
            11,
            RunActionKind.ADDED,
            video_id="dQw4w9WgXcQ",
            channel_id="UC1",
            rule_id=7,
            playlist_id="PL1",
            detail="matched the rule",
        ),
        action(1, 13, RunActionKind.ERROR, detail="HTTP Error 500"),
        action(2, 21, RunActionKind.ADDED, video_id="other-run"),
    )
    await user.open("/history")
    await user.should_not_see(marker="details")

    await open_run(user, 1)

    assert text_of(user, "details-started") == "2026-10-02 18:05 EEST"
    assert text_of(user, "details-status") == "Succeeded"
    table = element_of(user, "actions-table")
    assert [column["label"] for column in table.columns] == [
        "Time",
        "Action",
        "Video",
        "Channel",
        "Rule",
        "Playlist",
        "Detail",
    ]
    assert table.rows == [
        {
            "id": 11,
            "at": "2026-10-01 15:00 EEST",
            "action": "Added",
            "video": "dQw4w9WgXcQ",
            "video_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "channel": "UC1",
            "rule": 7,
            "playlist": "PL1",
            "detail": "matched the rule",
        },
        {
            "id": 12,
            "at": "2026-10-01 15:00 EEST",
            "action": "Skipped",
            "video": "vid-skip",
            "video_url": "https://www.youtube.com/watch?v=vid-skip",
            "channel": "UC2",
            "rule": "",
            "playlist": "",
            "detail": "too short",
        },
        {
            "id": 13,
            "at": "2026-10-01 15:00 EEST",
            "action": "Error",
            "video": "",
            "video_url": "",
            "channel": "",
            "rule": "",
            "playlist": "",
            "detail": "HTTP Error 500",
        },
    ]

    await open_run(user, 2)
    assert action_ids(user) == [21]


async def test_the_link_of_a_video_is_escaped(user, sessions):
    store(sessions, run(1), action(1, 1, video_id="a b&c=d/e"))
    await user.open("/history")

    await open_run(user, 1)

    assert rows_of(user, "actions-table")[0]["video_url"] == "https://www.youtube.com/watch?v=a%20b%26c%3Dd%2Fe"


async def test_the_actions_filter_by_kind(user, sessions):
    store(
        sessions,
        run(1),
        action(1, 1, RunActionKind.ADDED),
        action(1, 2, RunActionKind.SKIPPED),
        action(1, 3, RunActionKind.DEFERRED),
        action(1, 4, RunActionKind.ERROR),
        action(1, 5, RunActionKind.ADDED),
    )
    await user.open("/history")
    await open_run(user, 1)
    assert action_ids(user) == [1, 2, 3, 4, 5]

    user.find(marker="filter-action").click()
    user.find("Added").click()
    await eventually(lambda: action_ids(user) == [1, 5], "The filter of the added actions")
    set_value(user, "filter-action", "error")
    await eventually(lambda: action_ids(user) == [4], "The filter of the error actions")
    assert element_of(user, "filter-action").value == "error"
    set_value(user, "filter-action", "deferred")
    await eventually(lambda: action_ids(user) == [3], "The filter of the deferred actions")
    set_value(user, "filter-action", "any")
    await eventually(lambda: action_ids(user) == [1, 2, 3, 4, 5], "The end of the filter")


async def test_a_run_without_actions_and_a_filter_without_matches_say_so(user, sessions):
    store(sessions, run(1), run(2), action(2, 1, RunActionKind.ADDED))
    await user.open("/history")

    await open_run(user, 1)
    assert text_of(user, "no-actions") == "This run has no actions."

    await open_run(user, 2)
    set_value(user, "filter-action", "error")
    await eventually(lambda: elements_of(user, "no-actions"), "The message of the filter without matches")
    # The filter stays on, so the person can change it back.
    assert element_of(user, "filter-action").value == "error"
    await user.should_not_see(marker="actions-table")


async def test_a_long_run_has_pages_of_50_actions(user, sessions):
    store(sessions, run(1), *[action(1, number) for number in range(1, 61)])
    await user.open("/history")

    await open_run(user, 1)

    table = element_of(user, "actions-table")
    assert len(table.rows) == 60
    assert table.pagination == {"rowsPerPage": 50}


async def test_the_actions_panel_closes_and_forgets_a_run_that_is_gone(user, sessions):
    store(sessions, run(1), run(2), action(1, 1), action(2, 2))
    await user.open("/history")
    await open_run(user, 1)

    user.find(marker="details-close").click()
    await eventually(lambda: not elements_of(user, "details"), "The close of the panel")

    await open_run(user, 2)
    with sessions.begin() as session:
        session.delete(session.get(Run, 2))
    user.find(marker="refresh").click()

    await eventually(lambda: not elements_of(user, "details"), "The close of the panel of the deleted run")
    await runs_are(user, [1])


async def test_the_refresh_button_updates_the_open_run(user, sessions):
    store(sessions, run(1, status=RunStatus.RUNNING), action(1, 1))
    await user.open("/history")
    await open_run(user, 1)
    assert text_of(user, "details-status") == "In progress"

    with sessions.begin() as session:
        session.get(Run, 1).status = RunStatus.FAILED
        session.get(Run, 1).error_text = "It failed."
        session.add(action(1, 2, RunActionKind.ERROR))
    user.find(marker="refresh").click()

    await eventually(lambda: text_of(user, "details-status") == "Failed", "The new status of the open run")
    assert text_of(user, "run-error") == "It failed."
    assert action_ids(user) == [1, 2]


def stored_retention(sessions) -> object:
    with sessions() as session:
        row = session.get(Setting, "history_retention_days")
        return None if row is None else row.value


async def test_the_stored_retention_shows_on_the_page(nicegui_globals, settings, sessions, fake_starter):
    store(sessions, Setting(key="history_retention_days", value=45))

    async with running_service(settings, fake_starter) as user:
        await user.open("/history")

        assert element_of(user, "retention-days").value == 45


async def test_the_retention_is_stored_in_the_setting_table(user, sessions):
    await user.open("/history")
    await user.should_not_see(marker="retention-error")

    set_value(user, "retention-days", 30)
    user.find(marker="retention-save").click()

    await user.should_see("The retention is saved.")
    assert stored_retention(sessions) == 30
    set_value(user, "retention-days", 7.0)
    user.find(marker="retention-save").click()
    await eventually(lambda: stored_retention(sessions) == 7, "The second save")
    assert isinstance(stored_retention(sessions), int)


@pytest.mark.parametrize("wrong", [None, 0, -3, 0.5, 2.5])
async def test_a_wrong_retention_shows_the_error_and_stores_nothing(user, sessions, wrong):
    await user.open("/history")
    set_value(user, "retention-days", wrong)

    user.find(marker="retention-save").click()

    await eventually(
        lambda: text_of(user, "retention-error") == "Enter a whole number of days, 1 or more.", "The error"
    )
    assert stored_retention(sessions) is None
    assert not user.notify.contains("The retention is saved.")

    set_value(user, "retention-days", 1)
    user.find(marker="retention-save").click()
    await user.should_see("The retention is saved.")
    await user.should_not_see(marker="retention-error")
    assert stored_retention(sessions) == 1


def old_runs(*numbers: int) -> list[Run]:
    """Succeeded runs from 2020, older than each retention that a test sets."""
    return [run(number, started_at=datetime(2020, 1, 1, tzinfo=UTC)) for number in numbers]


def stored_run_ids(sessions) -> list[int]:
    with sessions() as session:
        return sorted(session.scalars(select(Run.id)).all())


def question_texts(user) -> list[str]:
    return [
        element.text for element in element_of(user, "retention-dialog").descendants() if isinstance(element, ui.label)
    ]


async def test_a_smaller_retention_asks_with_the_number_of_runs_that_go(user, sessions):
    store(sessions, *old_runs(1, 2, 3), run(4, started_at=datetime.now(UTC)))
    await user.open("/history")

    set_value(user, "retention-days", 30)
    user.find(marker="retention-save").click()

    await user.should_see(marker="retention-confirm")
    assert question_texts(user) == [
        "Keep the history for 30 days?",
        (
            "The next cleanup deletes 3 runs that are older. "
            "It runs at the start of the service and each day at 03:30 (Europe/Kyiv)."
        ),
    ]
    assert element_of(user, "retention-confirm").text == "Save"
    assert element_of(user, "retention-confirm").props["color"] == "negative"
    # The question waits: nothing is stored yet.
    assert stored_retention(sessions) is None
    assert not user.notify.contains("The retention is saved.")


async def test_cancel_of_the_question_writes_nothing(user, sessions):
    store(sessions, *old_runs(1))
    await user.open("/history")
    set_value(user, "retention-days", 30)
    user.find(marker="retention-save").click()
    await user.should_see(marker="retention-cancel")

    user.find(marker="retention-cancel").click()

    await eventually(lambda: not elements_of(user, "retention-dialog"), "The close of the question")
    await asyncio.sleep(SETTLE_SECONDS)
    assert stored_retention(sessions) is None
    assert stored_run_ids(sessions) == [1]
    assert not user.notify.contains("The retention is saved.")
    # The button works again, and the number in the field stays for another try.
    assert element_of(user, "retention-save").enabled
    assert element_of(user, "retention-days").value == 30


async def test_save_of_the_question_stores_the_retention_and_deletes_no_run_by_itself(user, sessions):
    store(sessions, *old_runs(1))
    await user.open("/history")
    set_value(user, "retention-days", 30)
    user.find(marker="retention-save").click()
    await user.should_see(marker="retention-confirm")

    user.find(marker="retention-confirm").click()

    await user.should_see("The retention is saved.")
    assert stored_retention(sessions) == 30
    await eventually(lambda: not elements_of(user, "retention-dialog"), "The close of the question")
    # The service deletes the old runs at its start and each day, so the question tells the truth.
    assert stored_run_ids(sessions) == [1]


async def test_a_smaller_retention_without_old_runs_saves_at_once(user, sessions):
    store(sessions, run(1, started_at=datetime.now(UTC)))
    await user.open("/history")

    set_value(user, "retention-days", 30)
    user.find(marker="retention-save").click()

    await user.should_see("The retention is saved.")
    assert stored_retention(sessions) == 30
    assert not elements_of(user, "retention-dialog")


async def test_a_larger_or_equal_retention_saves_at_once_also_with_old_runs(user, sessions):
    store(sessions, *old_runs(1), Setting(key="history_retention_days", value=30))
    await user.open("/history")

    set_value(user, "retention-days", 365)
    user.find(marker="retention-save").click()
    await eventually(lambda: stored_retention(sessions) == 365, "The save of the larger value")
    assert not elements_of(user, "retention-dialog")

    set_value(user, "retention-days", 365.0)
    user.find(marker="retention-save").click()
    await eventually(lambda: stored_retention(sessions) == 365 and len(user.notify.messages) == 2, "The equal save")
    assert not elements_of(user, "retention-dialog")
    assert stored_run_ids(sessions) == [1]


async def test_each_text_of_the_history_page_goes_through_the_translation(
    nicegui_globals, settings, sessions, fake_starter, monkeypatch
):
    class Marking:
        def gettext(self, message):
            return f"<<{message}>>"

    monkeypatch.setattr(i18n, "_translations", Marking())
    store(
        sessions,
        run(1, status=RunStatus.FAILED, error_text="boom", added_count=3),
        action(1, 1, RunActionKind.ADDED, video_id="vid", detail="detail text"),
    )
    data = {"Europe/Kyiv", "boom", "vid", "detail text"}
    texts = []
    column_labels = []

    async with running_service(settings, fake_starter) as user:
        await user.open("/history")
        user.find(marker="runs-table").trigger("rowClick", args=1)
        await eventually(lambda: elements_of(user, "details"), "The actions panel")
        for element in user.client.elements.values():
            if isinstance(element, TextElement) and element.text:
                texts.append(element.text)
            if isinstance(element, LabelElement) and element.props.get("label"):
                texts.append(element.props["label"])
            if isinstance(element, ui.select):
                texts.extend(element.options.values() if isinstance(element.options, dict) else [])
            if isinstance(element, ui.table):
                column_labels.extend(column["label"] for column in element.columns)
                for row in element.rows:
                    texts.extend(str(row[name]) for name in ("status", "trigger", "action") if name in row)

    assert "<<History>>" in texts
    assert "<<Videos added>>" in column_labels
    assert len(column_labels) == 15
    assert [label for label in column_labels if not (label.startswith("<<") and label.endswith(">>"))] == []
    untranslated = [
        text
        for text in texts
        if not (text.startswith("<<") and text.endswith(">>")) and text not in data and not TIME.fullmatch(text)
    ]
    assert untranslated == []
