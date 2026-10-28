import logging
import subprocess
import sys
from pathlib import Path

import pytest

from youtube_automanager.log_setup import setup_logging

ROOT = Path(__file__).parent.parent


@pytest.fixture
def restore_logging():
    root = logging.getLogger()
    package = logging.getLogger("youtube_automanager")
    handlers, root_level, package_level = root.handlers[:], root.level, package.level
    yield
    for handler in root.handlers[:]:
        root.removeHandler(handler)
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(root_level)
    package.setLevel(package_level)


def test_verbose_sets_debug_on_the_package_logger(restore_logging):
    setup_logging(True)

    assert logging.getLogger("youtube_automanager").level == logging.DEBUG
    assert logging.getLogger("youtube_automanager.engine.sync").isEnabledFor(logging.DEBUG)


def test_not_verbose_sets_info_on_the_package_logger(restore_logging):
    setup_logging(False)

    assert logging.getLogger("youtube_automanager").level == logging.INFO
    assert not logging.getLogger("youtube_automanager.engine.sync").isEnabledFor(logging.DEBUG)


@pytest.mark.parametrize("verbose", [True, False])
def test_root_logger_and_third_party_loggers_stay_at_info(restore_logging, verbose):
    setup_logging(verbose)

    assert logging.getLogger().level == logging.INFO
    assert not logging.getLogger("urllib3").isEnabledFor(logging.DEBUG)


def test_logs_go_to_stdout(restore_logging, capsys):
    setup_logging(False)

    logging.getLogger("youtube_automanager.db").info("a line for stdout")

    captured = capsys.readouterr()
    assert "INFO youtube_automanager.db: a line for stdout" in captured.out
    assert captured.err == ""


def test_calling_twice_leaves_one_handler(restore_logging):
    setup_logging(False)
    setup_logging(True)

    assert len(logging.getLogger().handlers) == 1


def test_importing_the_modules_neither_configures_logging_nor_reads_the_settings():
    code = (
        "import logging\n"
        "import youtube_automanager.runner\n"
        "import youtube_automanager.db\n"
        "import youtube_automanager.engine.sync\n"
        "from youtube_automanager.settings import get_settings\n"
        "assert logging.getLogger().handlers == [], logging.getLogger().handlers\n"
        "assert logging.getLogger().level == logging.WARNING\n"
        "assert get_settings.cache_info().currsize == 0\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
