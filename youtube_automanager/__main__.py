"""The command line of the service: ``serve`` starts the web service. ``run-once`` is one sync, ``check-session`` is one
session check, ``refresh-account`` is one refresh of the account data, ``find-channels`` is one channel search, and
``subscribe`` and ``unsubscribe`` change one subscription. The service starts each command except ``serve`` in a child
process.
"""

import argparse
import sys
from collections.abc import Sequence

from youtube_automanager.account_refresh import refresh_once
from youtube_automanager.channel_actions import change_subscription_once, find_channels_once
from youtube_automanager.log_setup import setup_logging
from youtube_automanager.runner import check_session_once, run_once
from youtube_automanager.settings import get_settings


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m youtube_automanager", description="Adds YouTube videos to your playlists by rules."
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    commands.add_parser("serve", help="start the web service and the scheduler")
    run_once_parser = commands.add_parser(
        "run-once", help="do one sync; the service starts this command in a child process"
    )
    run_once_parser.add_argument("--run-id", type=int, required=True, help="the id of the run row of this sync")
    commands.add_parser(
        "check-session",
        help="check the session of the stored YouTube cookies; the service starts it in a child process",
    )
    commands.add_parser(
        "refresh-account",
        help="read the subscriptions and the playlists of the account once; the service starts this command in a "
        "child process",
    )
    find_parser = commands.add_parser(
        "find-channels",
        help="find channels on YouTube without cookies; the service starts this command in a child process",
    )
    find_parser.add_argument(
        "--query",
        required=True,
        help="a name, an @handle, a channel id or a channel URL; give a query that starts with '-' as --query=TEXT",
    )
    subscribe_parser = commands.add_parser(
        "subscribe", help="subscribe the account to a channel; the service starts this command in a child process"
    )
    subscribe_parser.add_argument("--channel-id", required=True, help="the UC... id of the channel")
    subscribe_parser.add_argument("--title", help="the title of a new channel row")
    unsubscribe_parser = commands.add_parser(
        "unsubscribe",
        help="unsubscribe the account from a channel; the service starts this command in a child process",
    )
    unsubscribe_parser.add_argument("--channel-id", required=True, help="the UC... id of the channel")
    return parser


def _serve() -> int:
    # The web modules load only here, so the child of a run starts faster.
    import uvicorn

    from youtube_automanager.web.app import create_app
    from youtube_automanager.web.ui import mount_ui

    settings = get_settings()
    # Logging starts before uvicorn, so the first lines of uvicorn get the format of the service.
    # With log_config=None, uvicorn keeps that setup.
    setup_logging(settings.verbose)
    app = create_app(settings)
    mount_ui(app)
    uvicorn.run(app, host=settings.host, port=settings.port, log_config=None)
    return 0


def _run_once(run_id: int) -> int:
    settings = get_settings()
    setup_logging(settings.verbose)
    return run_once(run_id, settings)


def _check_session() -> int:
    settings = get_settings()
    setup_logging(settings.verbose)
    return check_session_once(settings)


def _refresh_account() -> int:
    settings = get_settings()
    setup_logging(settings.verbose)
    return refresh_once(settings)


def _find_channels(query: str) -> int:
    settings = get_settings()
    setup_logging(settings.verbose)
    return find_channels_once(query)


def _change_subscription(channel_id: str, *, subscribe: bool, title: str | None = None) -> int:
    settings = get_settings()
    setup_logging(settings.verbose)
    return change_subscription_once(settings, channel_id, subscribe=subscribe, title=title)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "serve":
        return _serve()
    if args.command == "check-session":
        return _check_session()
    if args.command == "refresh-account":
        return _refresh_account()
    if args.command == "find-channels":
        return _find_channels(args.query)
    if args.command == "subscribe":
        return _change_subscription(args.channel_id, subscribe=True, title=args.title)
    if args.command == "unsubscribe":
        return _change_subscription(args.channel_id, subscribe=False)
    return _run_once(args.run_id)


if __name__ == "__main__":
    sys.exit(main())
