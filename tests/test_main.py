import datetime

import pytest
from fastapi import FastAPI
from nicegui import app as nicegui_app

from youtube_automanager import __main__ as cli
from youtube_automanager.engine.rule_preview import DraftRule
from youtube_automanager.settings import Settings


@pytest.fixture
def settings(tmp_path, monkeypatch):
    settings = Settings(home=tmp_path, host="127.0.0.1", port=8123, tz="UTC", verbose=True)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    return settings


@pytest.fixture
def logging_setups(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "setup_logging", calls.append)
    return calls


def test_run_once_runs_the_sync_body_with_the_run_id(settings, logging_setups, monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "run_once", lambda run_id, given: calls.append((run_id, given)) or 7)

    assert cli.main(["run-once", "--run-id", "42"]) == 7

    assert calls == [(42, settings)]
    assert logging_setups == [True]


def test_check_session_runs_the_session_check_body(settings, logging_setups, monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "check_session_once", lambda given: calls.append(given) or 3)

    assert cli.main(["check-session"]) == 3

    assert calls == [settings]
    assert logging_setups == [True]


def test_refresh_account_runs_the_refresh_body_with_the_settings_and_gives_its_exit_code(
    settings, logging_setups, monkeypatch
):
    calls = []
    monkeypatch.setattr(cli, "refresh_once", lambda given: calls.append(given) or 4)

    assert cli.main(["refresh-account"]) == 4

    assert calls == [settings]
    assert logging_setups == [True]


@pytest.mark.parametrize(
    ("argv", "query"),
    [
        (["find-channels", "--query", "Deep Look"], "Deep Look"),
        (["find-channels", "--query=-starts with a dash"], "-starts with a dash"),
        (["find-channels", "--query=@misha_katsurin"], "@misha_katsurin"),
    ],
)
def test_find_channels_runs_the_search_body_with_the_query_and_gives_its_exit_code(
    settings, logging_setups, monkeypatch, argv, query
):
    calls = []
    monkeypatch.setattr(cli, "find_channels_once", lambda given: calls.append(given) or 5)

    assert cli.main(argv) == 5

    assert calls == [query]
    assert logging_setups == [True]


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["subscribe", "--channel-id=UCx", "--title=- A title"], ("UCx", True, "- A title")),
        (["subscribe", "--channel-id", "UCx"], ("UCx", True, None)),
        (["unsubscribe", "--channel-id=UCx"], ("UCx", False, None)),
    ],
)
def test_subscribe_and_unsubscribe_run_the_subscription_body_and_give_its_exit_code(
    settings, logging_setups, monkeypatch, argv, expected
):
    calls = []

    def change(given, channel_id, *, subscribe, title=None):
        calls.append((given, channel_id, subscribe, title))
        return 4

    monkeypatch.setattr(cli, "change_subscription_once", change)

    assert cli.main(argv) == 4

    assert calls == [(settings, *expected)]
    assert logging_setups == [True]


DRAFT = DraftRule({"regex": [{"var": "title"}, "-x", "i"]}, ("UCx",), True)


@pytest.mark.parametrize(
    ("argv", "since", "rule_id", "draft"),
    [
        (["preview-rule", "--since=2026-10-05", "--rule-id=3"], datetime.date(2026, 10, 5), 3, None),
        (
            ["preview-rule", "--since", "2026-10-05", f"--draft={DRAFT.to_json()}"],
            datetime.date(2026, 10, 5),
            None,
            DRAFT,
        ),
        (
            ["preview-rule", "--since=2026-10-05", "--rule-id", "3", f"--draft={DRAFT.to_json()}"],
            datetime.date(2026, 10, 5),
            3,
            DRAFT,
        ),
        (["preview-rule", f"--draft={DRAFT.to_json()}"], None, None, DRAFT),
        (["preview-rule", "--rule-id=3"], None, 3, None),
    ],
)
def test_preview_rule_runs_the_preview_body_and_gives_its_exit_code(
    settings, logging_setups, monkeypatch, argv, since, rule_id, draft
):
    calls = []
    monkeypatch.setattr(cli, "preview_once", lambda *given: calls.append(given) or 5)

    assert cli.main(argv) == 5

    assert calls == [(settings, since, rule_id, draft)]
    assert logging_setups == [True]


def test_apply_rule_runs_the_apply_body_and_gives_its_exit_code(settings, logging_setups, monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "apply_once", lambda *given: calls.append(given) or 1)

    assert cli.main(["apply-rule", "--run-id=7", "--rule-id=3", "--since=2026-10-05"]) == 1

    assert calls == [(7, 3, datetime.date(2026, 10, 5), settings)]
    assert logging_setups == [True]


def test_serve_starts_uvicorn_with_the_app_and_the_address_of_the_settings(
    settings, logging_setups, monkeypatch, nicegui_globals
):
    calls = []
    monkeypatch.setattr("uvicorn.run", lambda app, **options: calls.append((app, options)))

    assert cli.main(["serve"]) == 0

    ((app, options),) = calls
    assert isinstance(app, FastAPI)
    assert options == {"host": "127.0.0.1", "port": 8123, "log_config": None}
    assert logging_setups == [True]
    # The pages of NiceGUI answer at the root, after the routes of the service.
    assert app.routes[-1].path == "" and app.routes[-1].app is nicegui_app
    assert "/" in {route.path for route in nicegui_app.routes}


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["run-once"],
        ["run-once", "--run-id", "x"],
        ["refresh-account", "--run-id", "1"],
        ["unknown"],
        ["find-channels"],
        ["subscribe"],
        ["unsubscribe", "--title=x"],
        ["unsubscribe", "--channel-id=UCx", "--title=x"],
        ["preview-rule", "--since=2026-10-05"],
        ["preview-rule"],
        ["preview-rule", "--since=yesterday", "--rule-id=3"],
        ["preview-rule", "--since=2026-10-05", "--rule-id=x"],
        ["preview-rule", "--since=2026-10-05", "--draft=[]"],
        ["apply-rule", "--run-id=7", "--rule-id=3"],
        ["apply-rule", "--run-id=7", "--since=2026-10-05"],
    ],
)
def test_wrong_command_line_exits_with_code_2(argv, capsys):
    with pytest.raises(SystemExit) as caught:
        cli.main(argv)

    assert caught.value.code == 2
    assert "usage: python -m youtube_automanager" in capsys.readouterr().err
