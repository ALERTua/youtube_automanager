"""The command line of the service: ``serve`` starts the web service. ``run-once`` is one sync, and ``check-session``
is one session check, each in a child process.
"""

import argparse
import sys
from collections.abc import Sequence

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


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "serve":
        return _serve()
    if args.command == "check-session":
        return _check_session()
    return _run_once(args.run_id)


if __name__ == "__main__":
    sys.exit(main())
