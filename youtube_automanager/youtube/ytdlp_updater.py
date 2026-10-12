"""Installs yt-dlp releases from PyPI into a data folder that a child process imports before the yt-dlp of the image.

A yt-dlp that pip or uv installed cannot update itself, and a new version of an imported module takes effect only in a
new process. So an update installs the release into its own folder, and each sync runs in a child process that gets
that folder first on its import path from ``child_env``.
"""

import ast
import http.client
import importlib.metadata
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from packaging.version import InvalidVersion, Version

LOG = logging.getLogger(__name__)

# What a failed PyPI request or an unexpected PyPI answer raises: network and HTTP errors, bad JSON, a missing key,
# and a value of the wrong type.
_PYPI_ERRORS = (OSError, http.client.HTTPException, ValueError, LookupError, TypeError, AttributeError)

Channel = Literal["stable", "nightly"]
# Where the yt-dlp of the next child process comes from: the data folder, or the copy that came with the service.
Source = Literal["data_folder", "image"]

DISTRIBUTION = "yt-dlp"
PYPI_URL = "https://pypi.org/pypi/yt-dlp/json"
PYPI_TIMEOUT_SECONDS = 30
INSTALL_TIMEOUT_SECONDS = 300

_update_lock = threading.Lock()


@dataclass(frozen=True)
class UpdateResult:
    """The outcome of ``update``.

    ``new_version`` is the latest version of the channel on PyPI, or None when PyPI gave no answer.
    ``ok`` is True when the target folder holds that version at the end of the call.
    """

    ok: bool
    channel: Channel
    old_version: str | None
    new_version: str | None
    error: str | None


def installed_version(target_dir: Path) -> str | None:
    """Return the version of the yt-dlp distribution in ``target_dir`` as PyPI writes it, or None when there is none.

    The version comes from the metadata of the distribution, so ``yt_dlp`` is not imported in this process.
    The ``__version__`` of yt-dlp differs from the PyPI version: ``2026.08.19`` for ``2026.8.19``, and no ``.dev0``
    for a nightly build.
    """
    try:
        distributions = list(importlib.metadata.distributions(name=DISTRIBUTION, path=[str(target_dir)]))
        # Distribution.version warns on a missing Version line, and a future Python raises KeyError there.
        version = distributions[0].metadata.get("Version") if distributions else None
    except (OSError, LookupError, ValueError) as error:
        LOG.warning("Cannot read the yt-dlp metadata in %s: %s", target_dir, error)
        return None
    return version or None


def image_version() -> str | None:
    """Return the version of the yt-dlp that came with the service, or None when the service has none.

    The version comes from the metadata on the import path of this process, which does not hold the data folder, so
    ``yt_dlp`` is not imported.
    """
    try:
        return importlib.metadata.version(DISTRIBUTION)
    except importlib.metadata.PackageNotFoundError:
        return None


def active_version(target_dir: Path) -> tuple[str | None, Source]:
    """Return the version of the yt-dlp that the next child process imports, and where it comes from.

    A release in ``target_dir`` comes first on the import path of the child. Without one, the child imports the copy
    that came with the service.
    """
    version = installed_version(target_dir)
    if version:
        return version, "data_folder"
    return image_version(), "image"


def installed_channel(target_dir: Path) -> str | None:
    """Return ``CHANNEL`` of ``yt_dlp/version.py`` in ``target_dir``, or None when it cannot be read. For display only.

    The file is parsed, not imported.
    """
    version_file = target_dir / "yt_dlp" / "version.py"
    try:
        tree = ast.parse(version_file.read_bytes(), filename=str(version_file))
    except FileNotFoundError:
        return None
    except (OSError, SyntaxError, ValueError) as error:
        LOG.warning("Cannot parse %s: %s", version_file, error)
        return None
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "CHANNEL" for target in node.targets)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            return node.value.value
    return None


def _fetch_pypi_json() -> dict[str, Any]:
    request = urllib.request.Request(PYPI_URL, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=PYPI_TIMEOUT_SECONDS) as response:
        return json.load(response)


def latest_version(channel: Channel) -> str:
    """Return the latest version of yt-dlp on PyPI for ``channel``, as PyPI writes it.

    Stable is the version that PyPI shows as current. Nightly builds are PEP 440 dev releases, so nightly is the highest
    PEP 440 version among the releases that have files and are not yanked. This order also holds when a stable release
    comes out after a nightly build of the same day: ``2026.8.19.233000.dev0`` is higher than ``2026.8.19``.
    """
    data = _fetch_pypi_json()
    if channel == "stable":
        return data["info"]["version"]
    if channel == "nightly":
        return _highest_release(data["releases"])
    raise ValueError(f"Unknown yt-dlp channel: {channel!r}")


def _highest_release(releases: Mapping[str, list[Mapping[str, Any]]]) -> str:
    # PyPI lists the releases in string order, so "2026.10.1" comes before "2026.9.1".
    candidates = []
    for name, files in releases.items():
        if all(file.get("yanked", False) for file in files):
            continue
        try:
            candidates.append((Version(name), name))
        except InvalidVersion:
            LOG.debug("Skipping the yt-dlp release %r: it is not a PEP 440 version", name)
    if not candidates:
        raise ValueError("PyPI lists no installable release of yt-dlp")
    return max(candidates)[1]


def _install_command(target_dir: Path, version: str, channel: Channel) -> list[str]:
    # --no-deps: the folder comes first on the import path of the child process, so a dependency of yt-dlp there would
    # replace the copy of the image for every other library.
    # --no-config: the exclude-newer of the project configuration would refuse a release younger than its cutoff.
    # uv accepts a pinned pre-release by default, but UV_PRERELEASE=disallow in the environment would refuse a nightly.
    command = [
        "uv",
        "pip",
        "install",
        "--no-config",
        "--python",
        sys.executable,
        "--target",
        str(target_dir),
        "--no-deps",
    ]
    if channel == "nightly":
        command += ["--prerelease", "allow"]
    command.append(f"{DISTRIBUTION}=={version}")
    return command


def _install(target_dir: Path, version: str, channel: Channel) -> str | None:
    """Install yt-dlp ``version`` into the new folder ``target_dir``. Return None on success, else the error text."""
    LOG.info("Installing yt-dlp %s (%s) into %s", version, channel, target_dir)
    try:
        result = subprocess.run(
            _install_command(target_dir, version, channel),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=INSTALL_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return f"uv did not run: {error}"
    if result.returncode != 0:
        return f"uv exited with code {result.returncode}: {result.stderr.strip()}"
    found = installed_version(target_dir)
    if found != version:
        return f"uv exited with code 0, but {target_dir} holds yt-dlp {found}, not {version}"
    return None


def _remove_tree(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        shutil.rmtree(path)
    except OSError as error:
        return f"Cannot remove {path}: {error}"
    return None


def _replace_folder(target_dir: Path, new_dir: Path, old_dir: Path) -> str | None:
    """Put ``new_dir`` in place of ``target_dir``. Return None on success, else the error text.

    Windows cannot rename a folder over another folder, so ``target_dir`` first moves to ``old_dir``.
    A failure moves it back, so ``target_dir`` stays as it was.
    """
    if stale_error := _remove_tree(old_dir):
        return stale_error
    moved_aside = False
    try:
        if target_dir.exists():
            target_dir.rename(old_dir)
            moved_aside = True
        new_dir.rename(target_dir)
    except OSError as error:
        if moved_aside:
            try:
                old_dir.rename(target_dir)
            except OSError as restore_error:
                LOG.error("Cannot move %s back to %s: %s", old_dir, target_dir, restore_error)
        return f"Cannot replace {target_dir}: {error}"
    if cleanup_error := _remove_tree(old_dir):
        LOG.warning("The update is in place, but the previous version stays behind. %s", cleanup_error)
    return None


def update(target_dir: Path, channel: Channel) -> UpdateResult:
    """Install the latest yt-dlp of ``channel`` into ``target_dir``, unless the folder already holds that version.

    The release goes into the sibling folder ``<target_dir>.new`` first and then replaces ``target_dir``.
    A failure leaves ``target_dir`` as it was. Only one update runs at a time in this process.
    """
    with _update_lock:
        old_version = installed_version(target_dir)
        try:
            new_version = latest_version(channel)
        except _PYPI_ERRORS as error:
            LOG.warning("Cannot get the latest %s version of yt-dlp from PyPI: %s", channel, error)
            return UpdateResult(
                ok=False,
                channel=channel,
                old_version=old_version,
                new_version=None,
                error=f"Cannot get the latest version from PyPI: {error}",
            )
        if new_version == old_version:
            LOG.info("yt-dlp %s (%s) is up to date", old_version, channel)
            return UpdateResult(ok=True, channel=channel, old_version=old_version, new_version=new_version, error=None)

        new_dir = target_dir.with_name(f"{target_dir.name}.new")
        old_dir = target_dir.with_name(f"{target_dir.name}.old")
        error = _remove_tree(new_dir) or _install(new_dir, new_version, channel)
        if not error:
            error = _replace_folder(target_dir, new_dir, old_dir)
        if error:
            if cleanup_error := _remove_tree(new_dir):
                LOG.warning("%s", cleanup_error)
            LOG.warning("The yt-dlp update to %s (%s) failed: %s", new_version, channel, error)
            return UpdateResult(
                ok=False, channel=channel, old_version=old_version, new_version=new_version, error=error
            )
        LOG.info("Updated yt-dlp from %s to %s (%s)", old_version, new_version, channel)
        return UpdateResult(ok=True, channel=channel, old_version=old_version, new_version=new_version, error=None)


def child_env(target_dir: Path, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return a copy of ``base`` (by default ``os.environ``) with ``target_dir`` first in ``PYTHONPATH``.

    A child Python with this environment imports yt-dlp from ``target_dir`` before the copy of the image.
    """
    env = dict(os.environ if base is None else base)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = os.pathsep.join([str(target_dir), existing]) if existing else str(target_dir)
    return env
