"""Tests of the HTTP API of the rules, the schedule and the runs. A fake child stands in for the child of a run, so no test
reads YouTube."""

import base64
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from ui_support import running_service, store

from youtube_automanager import notify
from youtube_automanager.db.models import (
    Channel,
    Playlist,
    Rule,
    RuleChannel,
    Run,
    RunAction,
    RunActionKind,
    RunStatus,
    RunTrigger,
    Schedule,
    ScheduleKind,
)
from youtube_automanager.engine.rules import MATCH_ALL
from youtube_automanager.rules_store import MAX_DAYS_BACK
from youtube_automanager.runner import BUSY_TEXT
from youtube_automanager.scheduler import MIN_INTERVAL_S
from youtube_automanager.web.api import router
from youtube_automanager.web.app import create_app

pytestmark = pytest.mark.usefixtures("logging_setups")

A = "UCaaaaaaaaaaaaaaaaaaaaaa"
B = "UCbbbbbbbbbbbbbbbbbbbbbb"
LEVELS = {"regex": [{"var": "title"}, "Levels of", "i"]}
CHANGED_AT = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
KYIV = ZoneInfo("Europe/Kyiv")
PASSWORD = "fake-api-pässword"
WAIT_SECONDS = 10

# Each route of this step, with its method and a path that has an id.
NEW_ROUTES = [
    ("GET", "/api/rules"),
    ("POST", "/api/rules"),
    ("GET", "/api/rules/{rule_id}"),
    ("PUT", "/api/rules/{rule_id}"),
    ("DELETE", "/api/rules/{rule_id}"),
    ("GET", "/api/schedule"),
    ("PUT", "/api/schedule"),
    ("POST", "/api/runs"),
    ("GET", "/api/runs/{run_id}"),
]


@pytest.fixture
def account(sessions):
    """The playlists and the channels of the account copy, and two rules. The channel titles come in the opposite order
    to the ids."""
    store(
        sessions,
        Playlist(playlist_id="WL", title="Watch Later", is_own=True),
        Playlist(playlist_id="PLhome", title="Home", is_own=True),
        Channel(channel_id=A, title="Zeta", handle="@zeta", is_subscribed=True),
        Channel(channel_id=B, title="alpha", handle=None, is_subscribed=False),
        rule_row(1, "First", 2),
        rule_row(2, "Second", 1, catch_up_since=date(2026, 9, 1)),
        RuleChannel(rule_id=2, channel_id=A),
        RuleChannel(rule_id=2, channel_id=B),
    )
    return sessions


@pytest.fixture
def client(settings, account, fake_starter):
    with TestClient(create_app(settings, start=fake_starter)) as client:
        yield client


def rule_row(rule_id: int, name: str, position: int, **columns) -> Rule:
    return Rule(
        id=rule_id,
        name=name,
        enabled=True,
        position=position,
        condition=MATCH_ALL,
        target_playlist_id="WL",
        no_shorts=False,
        created_at=CHANGED_AT,
        updated_at=CHANGED_AT,
        **columns,
    )


def body(**fields) -> dict:
    return {
        "name": "Levels",
        "enabled": True,
        "channel_ids": [A],
        "playlist_id": "PLhome",
        "no_shorts": True,
        "condition": LEVELS,
        "catch_up_since": None,
        **fields,
    }


def today() -> datetime:
    return datetime.now(KYIV)


def rule_rows(sessions) -> dict[int, tuple]:
    with sessions() as session:
        links = {}
        for rule_id, channel_id in session.execute(select(RuleChannel.rule_id, RuleChannel.channel_id)):
            links.setdefault(rule_id, set()).add(channel_id)
        return {
            rule.id: (rule.name, rule.target_playlist_id, rule.no_shorts, rule.condition, links.get(rule.id, set()))
            for rule in session.scalars(select(Rule))
        }


def basic(password: str) -> dict[str, str]:
    token = base64.b64encode(f"api:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


# The rules.


def test_the_rules_come_in_the_order_of_their_evaluation_with_their_fields(client):
    rules = client.get("/api/rules")
    second = client.get("/api/rules/2")

    assert rules.status_code == 200
    assert [rule["id"] for rule in rules.json()] == [2, 1]
    assert second.status_code == 200
    assert second.json() == rules.json()[0]
    assert second.json() == {
        "id": 2,
        "name": "Second",
        "enabled": True,
        # In the order of the channel titles: "alpha" before "Zeta".
        "channel_ids": [B, A],
        "playlist_id": "WL",
        "no_shorts": False,
        "condition": MATCH_ALL,
        "catch_up_since": "2026-09-01",
        "updated_at": "2026-10-01T12:00:00Z",
    }


@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE"])
def test_an_unknown_rule_id_gets_404(client, account, method):
    answer = client.request(method, "/api/rules/99", json=body() if method == "PUT" else None)

    # The text of the route, not the "Not Found" of the catch-all of unknown API paths.
    assert (answer.status_code, answer.json()) == (404, {"detail": "No rule has this id."})
    assert set(rule_rows(account)) == {1, 2}


def test_a_post_creates_a_rule_at_the_end_with_the_name_stripped(client, account):
    since = (today() - timedelta(days=3)).date()

    answer = client.post("/api/rules", json=body(name="  Levels  ", channel_ids=[A, B], catch_up_since=str(since)))

    assert answer.status_code == 201
    created = answer.json()
    assert (created["name"], created["playlist_id"], created["channel_ids"]) == ("Levels", "PLhome", [B, A])
    assert (created["no_shorts"], created["condition"], created["catch_up_since"]) == (True, LEVELS, since.isoformat())
    assert rule_rows(account)[created["id"]] == ("Levels", "PLhome", True, LEVELS, {A, B})
    assert [rule["id"] for rule in client.get("/api/rules").json()] == [2, 1, created["id"]]


def test_a_post_without_channels_reads_the_subscriptions_feed(client, account):
    answer = client.post("/api/rules", json={"name": "Feed", "playlist_id": "WL", "condition": MATCH_ALL})

    assert answer.status_code == 201
    assert (answer.json()["channel_ids"], answer.json()["enabled"], answer.json()["no_shorts"]) == ([], True, False)


def test_a_put_replaces_the_rule_and_keeps_its_place(client, account):
    answer = client.put("/api/rules/2", json=body(name="Changed", enabled=False, channel_ids=[A]))

    assert answer.status_code == 200
    assert (answer.json()["id"], answer.json()["enabled"], answer.json()["catch_up_since"]) == (2, False, None)
    assert rule_rows(account)[2] == ("Changed", "PLhome", True, LEVELS, {A})
    assert [rule["id"] for rule in client.get("/api/rules").json()] == [2, 1]


def refused_bodies() -> list:
    now = today().date()
    return [
        pytest.param(body(name="   "), "Enter a name.", id="empty name"),
        pytest.param(body(playlist_id=""), "Choose a target playlist.", id="no playlist"),
        # The name comes first, as on the rules page.
        pytest.param(body(name="", playlist_id="", condition=[]), "Enter a name.", id="name before playlist"),
        pytest.param(body(playlist_id="", condition=[]), "Choose a target playlist.", id="playlist before tree"),
        pytest.param(body(condition=[]), "The root of a rule must be an object, not list", id="tree not an object"),
        pytest.param(body(condition={"var": "views"}), "views", id="unknown field"),
        pytest.param(body(catch_up_since=str(now + timedelta(days=1))), "not in the future", id="day in the future"),
        pytest.param(
            body(catch_up_since=str(now - timedelta(days=MAX_DAYS_BACK + 1))),
            f"at most {MAX_DAYS_BACK} days back",
            id="day too far back",
        ),
        pytest.param(body(playlist_id="PLother"), "The playlist PLother is not in the stored", id="unknown playlist"),
        pytest.param(
            body(channel_ids=[A, "UCnope"]), "The channels UCnope are not in the stored", id="unknown channel"
        ),
    ]


@pytest.mark.parametrize(("refused", "reason"), refused_bodies())
@pytest.mark.parametrize("method", ["POST", "PUT"])
def test_a_rule_that_cannot_be_saved_gets_422_with_the_reason(client, account, method, refused, reason):
    before = rule_rows(account)

    answer = client.request(method, "/api/rules" if method == "POST" else "/api/rules/1", json=refused)

    assert answer.status_code == 422
    assert reason in answer.json()["detail"]
    assert rule_rows(account) == before


@pytest.mark.parametrize("days", [0, MAX_DAYS_BACK])
def test_the_day_of_the_older_videos_can_be_today_or_the_last_day_of_the_range(client, days):
    since = (today() - timedelta(days=days)).date()

    answer = client.post("/api/rules", json=body(catch_up_since=since.isoformat()))

    assert (answer.status_code, answer.json()["catch_up_since"]) == (201, since.isoformat())


def test_a_body_with_an_unknown_or_a_missing_field_gets_the_422_of_the_model(client, account):
    unknown = client.post("/api/rules", json=body(playlist="WL"))
    missing = client.post("/api/rules", json={"name": "No playlist", "condition": MATCH_ALL})

    assert unknown.status_code == missing.status_code == 422
    assert unknown.json()["detail"][0]["type"] == "extra_forbidden"
    assert missing.json()["detail"][0]["loc"] == ["body", "playlist_id"]
    assert set(rule_rows(account)) == {1, 2}


def test_a_delete_removes_the_rule_and_its_channels(client, account):
    answer = client.delete("/api/rules/2")

    assert (answer.status_code, answer.content) == (204, b"")
    assert set(rule_rows(account)) == {1}
    assert client.get("/api/rules/2").status_code == 404


def test_a_delete_while_a_run_holds_the_lock_gets_409(client, account):
    with client.app.state.services.runner.lock_held(timeout=0):
        answer = client.delete("/api/rules/2")

    assert (answer.status_code, answer.json()) == (409, {"detail": BUSY_TEXT})
    assert set(rule_rows(account)) == {1, 2}


# The schedule.


def test_the_schedule_shows_its_fields(client):
    answer = client.get("/api/schedule")

    assert (answer.status_code, answer.json()) == (
        200,
        {
            "kind": "cron",
            "timezone": "Europe/Kyiv",
            "cron_expression": "0 18 * * *",
            "interval_s": None,
            "enabled": True,
        },
    )


def test_a_put_saves_the_schedule_and_the_scheduler_follows_it(client, account):
    before = datetime.now(UTC)
    schedule = {"kind": "interval", "timezone": "UTC", "interval_s": 3600, "cron_expression": "  "}

    answer = client.put("/api/schedule", json=schedule)
    status = client.get("/api/status").json()

    assert (answer.status_code, answer.json()) == (
        200,
        {"kind": "interval", "timezone": "UTC", "cron_expression": None, "interval_s": 3600, "enabled": True},
    )
    assert client.get("/api/schedule").json() == answer.json()
    next_run = datetime.fromisoformat(status["next_run_at"])
    assert before + timedelta(hours=1) <= next_run <= datetime.now(UTC) + timedelta(hours=1)
    with account() as session:
        row = session.get(Schedule, 1)
        assert (row.kind, row.interval_s, row.cron_expression, row.timezone) == (
            ScheduleKind.INTERVAL,
            3600,
            None,
            "UTC",
        )


def test_a_put_that_turns_the_schedule_off_pauses_it_and_tells_the_targets(client, monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "send_to_stored_targets", lambda sessions, notification: sent.append(notification))

    paused = client.put(
        "/api/schedule", json={"kind": "cron", "timezone": "UTC", "cron_expression": "0 6 * * *", "enabled": False}
    )
    state_after_pause = client.get("/api/status").json()["scheduler_state"]
    unchanged = client.put(
        "/api/schedule", json={"kind": "cron", "timezone": "UTC", "cron_expression": "0 7 * * *", "enabled": False}
    )
    resumed = client.put("/api/schedule", json={"kind": "cron", "timezone": "UTC", "cron_expression": "0 7 * * *"})

    assert (paused.status_code, unchanged.status_code, resumed.status_code) == (200, 200, 200)
    assert state_after_pause == "paused"
    assert client.get("/api/status").json()["scheduler_state"] == "running"
    assert [notification.title for notification in sent] == [
        notify.scheduler_state(True).title,
        notify.scheduler_state(False).title,
    ]


@pytest.mark.parametrize(
    ("schedule", "reason"),
    [
        pytest.param(
            {"kind": "cron", "timezone": "UTC", "cron_expression": "0 0 31 2 *"},
            "The schedule has no run time.",
            id="no run time",
        ),
        pytest.param(
            {"kind": "cron", "timezone": "UTC", "cron_expression": "0 0 31 2 *", "enabled": False},
            "The schedule has no run time.",
            id="no run time while off",
        ),
        pytest.param({"kind": "cron", "timezone": "UTC"}, "needs a cron expression", id="no cron expression"),
        pytest.param({"kind": "cron", "timezone": "UTC", "cron_expression": "0 18 * *"}, "5 fields", id="bad cron"),
        pytest.param(
            {"kind": "interval", "timezone": "UTC", "interval_s": MIN_INTERVAL_S - 1},
            f"{MIN_INTERVAL_S} s",
            id="short interval",
        ),
        pytest.param(
            {"kind": "cron", "timezone": "Mars/Base", "cron_expression": "0 18 * * *"}, "Mars/Base", id="zone"
        ),
        pytest.param(
            {"kind": "interval", "timezone": "UTC", "interval_s": 3600, "cron_expression": "bad"},
            "5 fields",
            id="bad unused cron",
        ),
        pytest.param(
            {"kind": "cron", "timezone": "UTC", "cron_expression": "0 18 * * *", "interval_s": 1},
            f"{MIN_INTERVAL_S} s",
            id="bad unused interval",
        ),
    ],
)
def test_a_wrong_schedule_gets_422_and_changes_nothing(client, account, schedule, reason):
    answer = client.put("/api/schedule", json=schedule)

    assert answer.status_code == 422
    assert reason in answer.json()["detail"]
    assert client.get("/api/schedule").json()["cron_expression"] == "0 18 * * *"
    assert client.get("/api/status").json()["scheduler_state"] == "running"


# The runs.


def test_a_post_starts_a_run_and_a_second_post_while_it_runs_gets_409(client, account, fake_starter):
    fake_starter.plan.append({"blocking": True})

    first = client.post("/api/runs")
    assert fake_starter.child_started.wait(WAIT_SECONDS)
    second = client.post("/api/runs")
    running = client.get(f"/api/runs/{first.json()['run_id']}").json()
    fake_starter.children[0].finish(0)

    assert first.status_code == 202
    assert (second.status_code, second.json()) == (409, {"detail": BUSY_TEXT})
    assert (running["status"], running["trigger"], running["actions"]) == ("running", "manual", [])
    with account() as session:
        assert session.scalars(select(Run.id)).all() == [first.json()["run_id"]]


def test_a_run_shows_its_actions_in_the_order_of_their_time_with_a_filter(client, account):
    at = datetime(2026, 10, 2, 18, 0, tzinfo=UTC)
    store(
        account,
        Run(
            id=7, status=RunStatus.SUCCEEDED, trigger=RunTrigger.SCHEDULE, started_at=at, finished_at=at, added_count=1
        ),
        RunAction(id=1, run_id=7, at=at + timedelta(seconds=2), action=RunActionKind.SKIPPED, video_id="v2"),
        RunAction(
            id=2,
            run_id=7,
            at=at + timedelta(seconds=1),
            action=RunActionKind.ADDED,
            video_id="v1",
            channel_id=A,
            rule_id=2,
            playlist_id="WL",
            detail="Matched",
        ),
        Run(id=8, status=RunStatus.SUCCEEDED, trigger=RunTrigger.SCHEDULE, started_at=at),
        RunAction(id=3, run_id=8, at=at, action=RunActionKind.ADDED, video_id="v3"),
    )

    run = client.get("/api/runs/7").json()
    added = client.get("/api/runs/7", params={"action": "added"}).json()
    errors = client.get("/api/runs/7", params={"action": "error"}).json()

    assert {key: run[key] for key in ("id", "status", "trigger", "added_count")} == {
        "id": 7,
        "status": "succeeded",
        "trigger": "schedule",
        "added_count": 1,
    }
    assert [action["id"] for action in run["actions"]] == [2, 1]
    assert added["actions"] == [
        {
            "id": 2,
            "at": "2026-10-02T18:00:01Z",
            "action": "added",
            "video_id": "v1",
            "channel_id": A,
            "rule_id": 2,
            "playlist_id": "WL",
            "detail": "Matched",
        }
    ]
    assert errors["actions"] == []


def test_an_unknown_run_gets_404_and_an_unknown_action_kind_gets_422(client):
    unknown = client.get("/api/runs/99")
    wrong_kind = client.get("/api/runs/99", params={"action": "removed"})

    assert (unknown.status_code, unknown.json()) == (404, {"detail": "No run has this id."})
    assert wrong_kind.status_code == 422


# The password, the order of the routes, and the documentation.


def test_each_route_needs_the_password(settings, account, fake_starter):
    settings = settings.model_copy(update={"ui_password": SecretStr(PASSWORD)})

    with TestClient(create_app(settings, start=fake_starter)) as client:
        refused = {
            (method, path): client.request(method, path.format(rule_id=1, run_id=1)).status_code
            for method, path in NEW_ROUTES
        }
        wrong = client.get("/api/rules", headers=basic("wrong")).status_code
        allowed = client.get("/api/rules", headers=basic(PASSWORD))

    assert set(refused.values()) == {401}
    assert wrong == 401
    assert (allowed.status_code, [rule["id"] for rule in allowed.json()]) == (200, [2, 1])
    assert set(rule_rows(account)) == {1, 2}


def test_each_new_route_comes_before_the_json_404_of_unknown_api_paths():
    routes = [route for route in router.routes if isinstance(route, APIRoute)]
    position = {(method, route.path): index for index, route in enumerate(routes) for method in route.methods or ()}

    for method, path in NEW_ROUTES:
        assert position[method, path] < position["GET", "/api/{path:path}"], (method, path)


def test_the_openapi_document_lists_the_new_routes(client):
    paths = client.get("/openapi.json").json()["paths"]

    listed = {(method.upper(), path) for path, operations in paths.items() for method in operations}
    assert set(NEW_ROUTES) <= listed
    assert "/api/{path}" not in paths
    assert set(paths["/api/rules"]["post"]["responses"]) >= {"201", "422"}
    assert set(paths["/api/rules/{rule_id}"]["delete"]["responses"]) >= {"204", "404", "409"}
    assert set(paths["/api/runs"]["post"]["responses"]) >= {"202", "409"}


@pytest.mark.anyio
@pytest.mark.usefixtures("nicegui_globals", "no_logged_errors")
async def test_the_documentation_answers_before_the_pages(settings, fake_starter):
    async with running_service(settings, fake_starter) as user:
        docs = await user.http_client.get("/docs")
        schema = await user.http_client.get("/openapi.json")
        rules = await user.http_client.get("/api/rules")

    assert docs.status_code == 200
    assert "swagger-ui" in docs.text
    assert "X-Nicegui-Content" not in docs.headers
    assert (schema.status_code, schema.json()["info"]["title"]) == (200, "YouTube AutoManager")
    assert (rules.status_code, rules.json()) == (200, [])
