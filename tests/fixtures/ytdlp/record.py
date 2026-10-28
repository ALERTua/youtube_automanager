"""Record anonymous yt-dlp answers for the tests of ``youtube_automanager.youtube.public``.

Run this script with ``uv run python`` from the repository root. It makes live requests to YouTube without cookies.
It calls the public functions, catches each answer of ``public._extract`` and writes it as JSON next to this script.
A playlist or a search keeps only the entries that the function took, at most ``MAX_ENTRIES``.
Give names of ``CASES`` as arguments to record only those cases. Without arguments, it records all cases.
"""

import io
import itertools
import json
import sys
import unittest.mock
from pathlib import Path
from typing import Any

from yt_dlp import YoutubeDL
from yt_dlp.networking.exceptions import HTTPError
from yt_dlp.utils import DownloadError, ExtractorError

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from youtube_automanager.youtube import public

HERE = Path(__file__).resolve().parent
MAX_ENTRIES = 30

NASA = "UCLA_DiR1FfKNvjuUpBHmylQ"
EPICURIOUS = "UCcjhYlL1WRBjKaJsMH_h7Lg"
WIRED = "UCftwRNsjfRo08xYE31tkiyw"
MISSING_HANDLE = "@thishandledoesnotexist0x9q7z"
MISSING_CHANNEL_ID = "UCzzzzzzzzzzzzzzzzzzzzzz"
GOOGLE_TECH_TALKS = "UCtXKDgv1AVoG88PLl8nGXmw"

CASES = {
    "uploads_nasa": lambda: list(itertools.islice(public.iter_channel_uploads(NASA), MAX_ENTRIES)),
    "shorts_epicurious": lambda: public.shorts_video_ids(EPICURIOUS, limit=MAX_ENTRIES),
    "search_epicurious": lambda: public.search_channels("epicurious", limit=MAX_ENTRIES),
    "channel_epicurious_by_handle": lambda: public.resolve_channel("@Epicurious"),
    "channel_wired_by_id": lambda: public.resolve_channel(WIRED),
    "missing_handle": lambda: public.resolve_channel(MISSING_HANDLE),
    "missing_channel_id": lambda: public.resolve_channel(MISSING_CHANNEL_ID),
    "no_shorts_tab_google_tech_talks": lambda: public.shorts_video_ids(GOOGLE_TECH_TALKS),
}


def describe(error: DownloadError) -> dict:
    cause = error.exc_info[1] if error.exc_info else None
    described = {"message": error.msg, "cause": type(cause).__name__}
    if isinstance(cause, HTTPError):
        described |= {"status": cause.status, "url": cause.response.url}
    elif isinstance(cause, ExtractorError):
        described |= {
            "orig_msg": cause.orig_msg,
            "expected": cause.expected,
            "video_id": cause.video_id,
            "ie": cause.ie,
        }
    return described


def keep(entries, taken: list):
    for entry in entries:
        taken.append(entry)
        yield entry


def record(call) -> dict:
    real_extract = public._extract
    records = []

    def extract(url: str, **options) -> dict:
        rec: dict[str, Any] = {"url": url, "options": options}
        records.append(rec)
        try:
            info = real_extract(url, **options)
        except DownloadError as error:
            rec["error"] = describe(error)
            raise
        rec["info"] = info
        entries = info.get("entries")
        if isinstance(entries, list):
            # The tabs of a channel page. The public functions do not read them.
            info["entries"] = rec["taken"] = []
        elif entries is not None:
            info["entries"] = keep(entries, rec.setdefault("taken", []))
        return info

    with unittest.mock.patch.object(public, "_extract", extract):
        call()
    [rec] = records
    if "info" in rec:
        rec["info"] = YoutubeDL.sanitize_info({**rec["info"], "entries": rec.pop("taken")})
    return rec


def main() -> None:
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    for name in sys.argv[1:] or CASES:
        rec = record(CASES[name])
        path = HERE / f"{name}.json"
        path.write_text(json.dumps(rec, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
        summary = rec["error"]["message"] if "error" in rec else f"{len(rec['info']['entries'])} entries"
        print(f"{path.name}: {rec['url']} -> {summary}")


if __name__ == "__main__":
    main()
