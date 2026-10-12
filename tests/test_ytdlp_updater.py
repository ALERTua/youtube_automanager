import http.client
import io
import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from youtube_automanager.youtube import ytdlp_updater
from youtube_automanager.youtube.ytdlp_updater import (
    child_env,
    installed_channel,
    installed_version,
    latest_version,
    update,
)

FIXTURES = Path(__file__).parent / "fixtures"
STABLE = "2026.8.19"
NIGHTLY = "2026.10.10.233141.dev0"
OLD = "2026.7.1"


class FakePyPI:
    """Replaces urllib.request.urlopen: serves the recorded PyPI JSON and records each request."""

    def __init__(self, data):
        self.data = data
        self.requests = []
        self.error = None

    def urlopen(self, request, timeout=None):
        self.requests.append((request.full_url, timeout))
        if self.error:
            raise self.error
        return io.BytesIO(json.dumps(self.data).encode("utf-8"))


class FakeUv:
    """Replaces subprocess.run for the uv command: records each call and writes a fake install into the --target folder."""

    def __init__(self, make_install):
        self.make_install = make_install
        self.calls = []
        self.target_existed = []
        self.returncode = 0
        self.stderr = ""
        self.error = None
        self.writes_install = True
        self.before_install = None

    def run(self, command, **kwargs):
        self.calls.append((command, kwargs))
        target = Path(command[command.index("--target") + 1])
        self.target_existed.append(target.exists())
        if self.before_install:
            self.before_install()
        if self.error:
            raise self.error
        if self.returncode == 0 and self.writes_install:
            channel = "nightly" if "--prerelease" in command else "stable"
            self.make_install(target, command[-1].removeprefix("yt-dlp=="), channel)
        return subprocess.CompletedProcess(command, self.returncode, stdout="", stderr=self.stderr)


@pytest.fixture
def pypi_data():
    return json.loads((FIXTURES / "pypi_yt_dlp.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def pypi(monkeypatch, pypi_data):
    """Every test of this module talks to the fake PyPI, so no test makes a network request."""
    fake = FakePyPI(pypi_data)
    monkeypatch.setattr(urllib.request, "urlopen", fake.urlopen)
    return fake


@pytest.fixture
def make_install():
    """Writes what uv leaves in a --target folder, reduced to the files that the updater reads."""

    def factory(folder, version, channel="stable", module_version=None):
        dist_info = folder / f"yt_dlp-{version}.dist-info"
        dist_info.mkdir(parents=True)
        metadata = f"Metadata-Version: 2.4\nName: yt-dlp\nVersion: {version}\n"
        (dist_info / "METADATA").write_text(metadata, encoding="utf-8")
        package = folder / "yt_dlp"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        version_py = f"__version__ = {module_version or version!r}\nCHANNEL = {channel!r}\n"
        (package / "version.py").write_text(version_py, encoding="utf-8")

    return factory


@pytest.fixture
def uv(monkeypatch, make_install):
    fake = FakeUv(make_install)
    monkeypatch.setattr(subprocess, "run", fake.run)
    return fake


@pytest.fixture
def target_dir(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    return data / "yt-dlp"


@pytest.fixture
def old_install(make_install, target_dir):
    make_install(target_dir, OLD)
    (target_dir / "marker").write_text("from the old install", encoding="utf-8")
    return target_dir


def sibling(target_dir, suffix):
    return target_dir.with_name(target_dir.name + suffix)


def test_installed_version_is_the_pypi_version_from_the_metadata(make_install, target_dir, monkeypatch):
    make_install(target_dir, STABLE, module_version="2026.08.19")
    # Other test modules import yt_dlp in this process. Hide it, so the check below shows an import by this call.
    for name in [name for name in sys.modules if name == "yt_dlp" or name.startswith("yt_dlp.")]:
        monkeypatch.delitem(sys.modules, name)

    assert installed_version(target_dir) == STABLE
    assert "yt_dlp" not in sys.modules


def test_installed_version_of_a_missing_folder_is_none(target_dir):
    assert installed_version(target_dir) is None


def test_installed_version_of_a_folder_without_yt_dlp_is_none(target_dir):
    target_dir.mkdir()

    assert installed_version(target_dir) is None


@pytest.mark.parametrize(
    "metadata",
    [b"Metadata-Version: 2.4\nName: yt-dlp\n", b"\xff\xfe\x00 not text"],
    ids=["no-version-line", "not-utf8"],
)
def test_installed_version_with_malformed_metadata_is_none(target_dir, metadata):
    dist_info = target_dir / "yt_dlp-1.0.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").write_bytes(metadata)

    assert installed_version(target_dir) is None


def test_installed_channel_reads_the_channel_of_version_py(make_install, target_dir):
    make_install(target_dir, NIGHTLY, channel="nightly")

    assert installed_channel(target_dir) == "nightly"


def test_installed_channel_of_a_missing_folder_is_none(target_dir):
    assert installed_channel(target_dir) is None


@pytest.mark.parametrize(
    "source",
    ["CHANNEL = \n", "__version__ = '2026.08.19'\n"],
    ids=["syntax-error", "no-channel"],
)
def test_installed_channel_of_a_malformed_version_py_is_none(target_dir, source):
    package = target_dir / "yt_dlp"
    package.mkdir(parents=True)
    (package / "version.py").write_text(source, encoding="utf-8")

    assert installed_channel(target_dir) is None


def test_latest_stable_is_the_current_version_of_pypi(pypi):
    assert latest_version("stable") == STABLE
    assert pypi.requests == [(ytdlp_updater.PYPI_URL, ytdlp_updater.PYPI_TIMEOUT_SECONDS)]


def test_latest_nightly_is_the_highest_version_not_the_last_listed_one(pypi_data):
    assert list(pypi_data["releases"])[-1] == "2026.9.27.232945.dev0"

    assert latest_version("nightly") == NIGHTLY


def test_latest_nightly_skips_yanked_releases_releases_without_files_and_invalid_names(pypi_data):
    pypi_data["releases"]["2026.10.11.1.dev0"] = [{"yanked": True}, {"yanked": True}]
    pypi_data["releases"]["2026.10.12.1.dev0"] = []
    pypi_data["releases"]["not-a-version"] = [{"yanked": False}]

    assert latest_version("nightly") == NIGHTLY


def test_latest_nightly_keeps_the_version_order_when_stable_comes_after_a_nightly_of_the_same_day(pypi_data):
    del pypi_data["releases"][NIGHTLY]
    del pypi_data["releases"]["2026.9.27.232945.dev0"]

    assert latest_version("nightly") == "2026.8.19.233000.dev0"


@pytest.mark.parametrize(
    ("channel", "installed", "module_version"),
    [("stable", STABLE, "2026.08.19"), ("nightly", NIGHTLY, "2026.10.10.233141")],
)
def test_update_does_not_install_when_the_folder_holds_the_latest_version(
    uv, make_install, target_dir, channel, installed, module_version
):
    make_install(target_dir, installed, channel=channel, module_version=module_version)

    result = update(target_dir, channel)

    assert result == ytdlp_updater.UpdateResult(
        ok=True, channel=channel, old_version=installed, new_version=installed, error=None
    )
    assert uv.calls == []


def test_successful_update_replaces_the_folder_and_removes_the_old_one(uv, old_install):
    result = update(old_install, "stable")

    assert result == ytdlp_updater.UpdateResult(
        ok=True, channel="stable", old_version=OLD, new_version=STABLE, error=None
    )
    assert installed_version(old_install) == STABLE
    assert not (old_install / "marker").exists()
    assert not sibling(old_install, ".new").exists()
    assert not sibling(old_install, ".old").exists()


def test_first_update_creates_the_folder(uv, target_dir):
    result = update(target_dir, "nightly")

    assert result.ok
    assert result.old_version is None
    assert installed_version(target_dir) == NIGHTLY
    assert installed_channel(target_dir) == "nightly"


def test_update_installs_into_a_clean_new_folder_and_removes_leftovers(uv, old_install):
    for suffix in (".new", ".old"):
        sibling(old_install, suffix).mkdir()
        (sibling(old_install, suffix) / "leftover").write_text("from a crash", encoding="utf-8")

    result = update(old_install, "stable")

    assert result.ok, result.error
    assert uv.target_existed == [False]
    assert installed_version(old_install) == STABLE
    assert not sibling(old_install, ".new").exists()
    assert not sibling(old_install, ".old").exists()


@pytest.mark.parametrize(
    ("change", "expected_error"),
    [
        ({"returncode": 2, "stderr": "error: No solution found\n"}, "uv exited with code 2: error: No solution found"),
        ({"error": subprocess.TimeoutExpired(["uv"], 300)}, "uv did not run: "),
        ({"error": FileNotFoundError("uv is missing")}, "uv did not run: uv is missing"),
        ({"writes_install": False}, "holds yt-dlp None, not 2026.8.19"),
    ],
    ids=["exit-code", "timeout", "no-uv", "nothing-installed"],
)
def test_failed_install_keeps_the_folder_and_its_version(uv, old_install, change, expected_error):
    for name, value in change.items():
        setattr(uv, name, value)

    result = update(old_install, "stable")

    assert not result.ok
    assert (result.old_version, result.new_version) == (OLD, STABLE)
    assert expected_error in result.error
    assert installed_version(old_install) == OLD
    assert (old_install / "marker").exists()
    assert not sibling(old_install, ".new").exists()


@pytest.mark.parametrize("failing_source", ["yt-dlp", "yt-dlp.new"], ids=["move-aside", "move-in"])
def test_failed_rename_keeps_the_folder_and_its_version(uv, old_install, monkeypatch, failing_source):
    rename = Path.rename

    def fake_rename(self, target):
        if self.name == failing_source:
            raise PermissionError(f"{self.name} is in use")
        return rename(self, target)

    monkeypatch.setattr(Path, "rename", fake_rename)

    result = update(old_install, "stable")

    assert not result.ok
    assert f"{failing_source} is in use" in result.error
    assert installed_version(old_install) == OLD
    assert (old_install / "marker").exists()
    assert not sibling(old_install, ".new").exists()
    assert not sibling(old_install, ".old").exists()


@pytest.mark.parametrize(
    ("change", "expected_error"),
    [
        ({"error": urllib.error.URLError("network is down")}, "network is down"),
        ({"error": http.client.IncompleteRead(b"{")}, "1 bytes read"),
        ({"data": {"releases": {}}}, "'info'"),
    ],
    ids=["network", "cut-off", "malformed"],
)
def test_pypi_failure_returns_a_failed_result_without_an_install(uv, old_install, pypi, change, expected_error):
    for name, value in change.items():
        setattr(pypi, name, value)

    result = update(old_install, "stable")

    assert not result.ok
    assert result.new_version is None
    assert expected_error in result.error
    assert uv.calls == []
    assert installed_version(old_install) == OLD


@pytest.mark.parametrize(
    ("channel", "tail"),
    [
        ("stable", ["--no-deps", "yt-dlp==2026.8.19"]),
        ("nightly", ["--no-deps", "--prerelease", "allow", "yt-dlp==2026.10.10.233141.dev0"]),
    ],
)
def test_uv_command_line(uv, old_install, channel, tail):
    update(old_install, channel)

    [(command, kwargs)] = uv.calls
    new_dir = str(sibling(old_install, ".new"))
    assert command == ["uv", "pip", "install", "--no-config", "--python", sys.executable, "--target", new_dir, *tail]
    assert kwargs["timeout"] == 300


def test_two_updates_do_not_run_at_the_same_time(uv, target_dir):
    inside = threading.Event()
    release = threading.Event()
    counter_lock = threading.Lock()
    active = 0
    max_active = 0

    def slow_install():
        nonlocal active, max_active
        with counter_lock:
            active += 1
            max_active = max(max_active, active)
        inside.set()
        release.wait(timeout=10)
        with counter_lock:
            active -= 1

    uv.before_install = slow_install
    results = []
    first = threading.Thread(target=lambda: results.append(update(target_dir, "stable")))
    second = threading.Thread(target=lambda: results.append(update(target_dir, "stable")))

    first.start()
    assert inside.wait(timeout=10)
    second.start()
    second.join(timeout=0.5)
    release.set()
    first.join(timeout=10)
    second.join(timeout=10)

    assert max_active == 1
    assert len(uv.calls) == 1
    assert [result.ok for result in results] == [True, True]


@pytest.mark.parametrize(
    ("base", "expected"),
    [
        ({"PATH": "bin"}, "{target}"),
        ({"PATH": "bin", "PYTHONPATH": ""}, "{target}"),
        ({"PATH": "bin", "PYTHONPATH": "lib"}, "{target}{sep}lib"),
    ],
    ids=["no-pythonpath", "empty-pythonpath", "pythonpath"],
)
def test_child_env_puts_the_target_folder_first(target_dir, base, expected):
    original = dict(base)

    env = child_env(target_dir, base)

    assert env == {**original, "PYTHONPATH": expected.format(target=target_dir, sep=os.pathsep)}
    assert base == original


def test_child_env_copies_os_environ_by_default(target_dir, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "lib")
    monkeypatch.setenv("YAM_TEST_MARKER", "1")

    env = child_env(target_dir)

    assert env["PYTHONPATH"] == f"{target_dir}{os.pathsep}lib"
    assert env["YAM_TEST_MARKER"] == "1"
    assert os.environ["PYTHONPATH"] == "lib"


def test_child_python_imports_yt_dlp_from_the_target_folder(tmp_path):
    package = tmp_path / "yt-dlp" / "yt_dlp"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "version.py").write_text("__version__ = '2099.01.02'\n", encoding="utf-8")
    workdir = tmp_path / "work"
    workdir.mkdir()
    code = "import yt_dlp, yt_dlp.version\nprint(yt_dlp.version.__version__)\nprint(yt_dlp.__file__)\n"

    result = subprocess.run(
        [sys.executable, "-c", code],
        env=child_env(tmp_path / "yt-dlp"),
        cwd=workdir,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    version, module_file = result.stdout.splitlines()
    assert version == "2099.01.02"
    assert Path(module_file).resolve() == (package / "__init__.py").resolve()
