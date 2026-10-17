"""Installs yt-dlp releases from PyPI into a data folder that a child process imports before the yt-dlp of the image.

A yt-dlp that pip or uv installed cannot update itself, and a new version of an imported module takes effect only in a
new process. So an update installs the release into its own folder, and each sync runs in a child process that gets
that folder first on its import path from ``child_env``.

A new release replaces the active one only after a check of the new folder passed. The replaced release stays in the
sibling folder ``<target>.previous``, so ``go_back`` can put it back. ``use_image`` moves the active release there, so
the child imports the yt-dlp of the image.
"""

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
from collections.abc import Callable, Mapping
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
    ``ok`` is True when the target folder holds that version at the end of the call, or when ``skipped`` is True.
    ``skipped`` is True when the latest version is not newer than the version that the user skipped, so the folder
    stays as it was.
    """

    ok: bool
    channel: Channel
    old_version: str | None
    new_version: str | None
    error: str | None
    skipped: bool = False


# Checks the release of a version in a folder before it replaces the active release. It returns None when the release
# works, or else the cause, and it changes nothing in the folder.
type Smoke = Callable[[Path, str], str | None]


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


def previous_dir(target_dir: Path) -> Path:
    """Return the sibling folder that keeps the release that the last change of ``target_dir`` replaced."""
    return target_dir.with_name(f"{target_dir.name}.previous")


def _old_dir(target_dir: Path) -> Path:
    return target_dir.with_name(f"{target_dir.name}.old")


def _is_newer(version: str, than: str) -> bool:
    """Return True when ``version`` is a higher PEP 440 version than ``than``, or when one of them is not one."""
    try:
        return Version(version) > Version(than)
    except InvalidVersion:
        return True


def _replace_folder(target_dir: Path, new_dir: Path) -> str | None:
    """Put ``new_dir`` in place of ``target_dir``, and keep the replaced release in ``previous_dir``.

    ``new_dir`` can be ``previous_dir`` itself: then the two releases change places.

    Return None on success, else the error text. Windows cannot rename a folder over another folder, so
    ``target_dir`` first moves to the sibling folder ``<target_dir>.old``. A failure moves it back, so ``target_dir``
    stays as it was. The release in ``previous_dir`` changes only after a success.
    """
    old_dir = _old_dir(target_dir)
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
    if moved_aside:
        _keep_as_previous(old_dir, previous_dir(target_dir))
    return None


def _keep_as_previous(old_dir: Path, keep_dir: Path) -> None:
    """Move the replaced release in ``old_dir`` to ``keep_dir``, in place of the release that ``keep_dir`` held.

    A folder without a release goes. A failure leaves the new release in place and only writes a warning.
    """
    if installed_version(old_dir) is None:
        if cleanup_error := _remove_tree(old_dir):
            LOG.warning("The new yt-dlp is in place, but a folder stays behind. %s", cleanup_error)
        return
    if cleanup_error := _remove_tree(keep_dir):
        LOG.warning("The new yt-dlp is in place, but the replaced one is not kept. %s", cleanup_error)
        return
    try:
        old_dir.rename(keep_dir)
    except OSError as error:
        LOG.warning("The new yt-dlp is in place, but the replaced one is not kept. Cannot move %s: %s", old_dir, error)


def _smoke_error(smoke: Smoke, new_dir: Path, version: str) -> str | None:
    """Run the check of the new release. Return None when it passed, else the error text of the update."""
    try:
        cause = smoke(new_dir, version)
    except Exception as error:
        # Any failure of the check keeps the active release, also a defect of the check itself.
        LOG.exception("The check of yt-dlp %s raised an error", version)
        cause = f"the check raised {type(error).__name__}"
    if cause is None:
        return None
    return f"yt-dlp {version} failed the check of a new release, so the service keeps the active yt-dlp: {cause}"


def update(
    target_dir: Path, channel: Channel, *, smoke: Smoke | None = None, skipped: str | None = None
) -> UpdateResult:
    """Install the latest yt-dlp of ``channel`` into ``target_dir``, unless the folder already holds that version.

    The release goes into the sibling folder ``<target_dir>.new`` first. ``smoke`` then checks it in that folder, and
    only a release that passed replaces ``target_dir``. The replaced release stays in ``previous_dir``. A failure
    leaves ``target_dir`` as it was. When the latest version is not newer than ``skipped``, the call installs nothing.
    Only one update runs at a time in this process.
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
        if skipped is not None and not _is_newer(new_version, skipped):
            LOG.info(
                "yt-dlp %s (%s) is not newer than the skipped version %s, so the update installs nothing",
                new_version,
                channel,
                skipped,
            )
            return UpdateResult(
                ok=True, channel=channel, old_version=old_version, new_version=new_version, error=None, skipped=True
            )

        new_dir = target_dir.with_name(f"{target_dir.name}.new")
        error = _remove_tree(new_dir) or _install(new_dir, new_version, channel)
        if not error and smoke is not None:
            error = _smoke_error(smoke, new_dir, new_version)
        if not error:
            error = _replace_folder(target_dir, new_dir)
        if error:
            if cleanup_error := _remove_tree(new_dir):
                LOG.warning("%s", cleanup_error)
            LOG.warning("The yt-dlp update to %s (%s) failed: %s", new_version, channel, error)
            return UpdateResult(
                ok=False, channel=channel, old_version=old_version, new_version=new_version, error=error
            )
        LOG.info("Updated yt-dlp from %s to %s (%s)", old_version, new_version, channel)
        return UpdateResult(ok=True, channel=channel, old_version=old_version, new_version=new_version, error=None)


def go_back(target_dir: Path) -> str | None:
    """Put the release of ``previous_dir`` in place of ``target_dir``, and keep the replaced release there instead.

    Without a release in ``target_dir``, the previous release only moves in. Return None on success, else the error
    text. A failure leaves both folders as they were.
    """
    keep_dir = previous_dir(target_dir)
    with _update_lock:
        if installed_version(keep_dir) is None:
            return "The data folder keeps no previous yt-dlp."
        if error := _replace_folder(target_dir, keep_dir):
            return error
        LOG.info("yt-dlp %s of the data folder is active again", installed_version(target_dir))
        return None


def use_image(target_dir: Path) -> str | None:
    """Move the release of ``target_dir`` to ``previous_dir``, so that a child process imports the yt-dlp of the image.

    The release in ``previous_dir`` goes. Return None on success, else the error text. A failure leaves
    ``target_dir`` as it was.
    """
    keep_dir = previous_dir(target_dir)
    with _update_lock:
        if installed_version(target_dir) is None:
            return "The data folder holds no yt-dlp, so the service already uses the yt-dlp of the image."
        if cleanup_error := _remove_tree(keep_dir):
            return cleanup_error
        try:
            target_dir.rename(keep_dir)
        except OSError as error:
            return f"Cannot move {target_dir} to {keep_dir}: {error}"
        LOG.info("The service uses the yt-dlp of the image, and keeps the release of the data folder in %s", keep_dir)
        return None


def child_env(target_dir: Path, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return a copy of ``base`` (by default ``os.environ``) with ``target_dir`` first in ``PYTHONPATH``.

    A child Python with this environment imports yt-dlp from ``target_dir`` before the copy of the image.
    """
    env = dict(os.environ if base is None else base)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = os.pathsep.join([str(target_dir), existing]) if existing else str(target_dir)
    return env
