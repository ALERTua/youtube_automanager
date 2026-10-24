"""Tell when a recorded fixture of the tests is older than ``MAX_AGE_DAYS`` days.

The tests replay recorded answers of YouTube and of PyPI from the JSON files under this folder. A recording ages
without a sign: the tests still pass while YouTube answers differently today. This script reads the date of the last
commit of each JSON file from git, and exits with 1 when the oldest one is older than the limit. The weekly workflow
of the newest yt-dlp runs it, so an old recording shows up in that run.

Run it from the repository root with ``uv run python tests/fixtures/check_age.py``. It needs the git history of the
files, so a shallow clone of depth 1 gives no dates. Give ``--now YYYY-MM-DD`` to check against another day.

The public yt-dlp fixtures come from ``record.py`` next to this script. The account fixtures were recorded and
anonymized by hand.
"""

import argparse
import datetime
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MAX_AGE_DAYS = 90


def last_commit_date(path: Path) -> datetime.date | None:
    """Return the day of the last commit that changed ``path``, or None when git knows no commit of it."""
    result = subprocess.run(
        ["git", "log", "-1", "--format=%cs", "--", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        cwd=HERE,
    )
    text = result.stdout.strip()
    return datetime.date.fromisoformat(text) if text else None


def fixture_ages(now: datetime.date) -> list[tuple[int, Path]]:
    """Return the age in days of each recorded JSON file under this folder, the oldest first."""
    ages = []
    for path in sorted(HERE.rglob("*.json")):
        day = last_commit_date(path)
        if day is None:
            raise SystemExit(f"git knows no commit of {path.relative_to(HERE)}: a shallow clone gives no dates")
        ages.append(((now - day).days, path))
    return sorted(ages, key=lambda age: -age[0])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--now", type=datetime.date.fromisoformat, default=datetime.date.today(), help="YYYY-MM-DD")
    parser.add_argument("--max-age-days", type=int, default=MAX_AGE_DAYS)
    args = parser.parse_args(argv)
    ages = fixture_ages(args.now)
    old = [(days, path) for days, path in ages if days > args.max_age_days]
    for days, path in ages:
        mark = "OLD " if days > args.max_age_days else "ok  "
        print(f"{mark} {days:4d} days  {path.relative_to(HERE).as_posix()}")
    if old:
        print(
            f"\n{len(old)} recorded fixture(s) are older than {args.max_age_days} days. Record the public ones again "
            f"with tests/fixtures/ytdlp/record.py, and the account ones by hand.",
            file=sys.stderr,
        )
        return 1
    print(f"\nEach recorded fixture is at most {args.max_age_days} days old.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
