"""The pause of the requests with the cookies of the account, as the breaker of the request budget starts it.

A child process that stops at an open breaker ends with ``EXIT_PAUSED`` and writes the pause as one JSON line at the
end of its stderr: the reason as the value of ``BreakerReason``, the end as ISO 8601 text in UTC, and the time zone of
the service. The web process reads the line back, so a page can show its own text and let the browser format the end.

The module imports only the models, so the runner, the request budget and each child can use it.
"""

import datetime
import json
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from youtube_automanager.db.models import BreakerReason

# The exit code of a child that stopped at an open breaker. The other codes of a failed child are 3, 4 and 5.
EXIT_PAUSED = 6
# The answer of YouTube for each reason, in the English texts of the errors and of the notifications.
REASON_TEXTS = {
    BreakerReason.HTTP_429: "HTTP 429 (too many requests)",
    BreakerReason.TRY_AGAIN_LATER: 'a "try again later" page',
    BreakerReason.BOT_CHECK: 'a bot check ("Sign in to confirm you\'re not a bot")',
}
# The form of the end of a pause in an English text.
END_FORMAT = "%Y-%m-%d %H:%M %Z"


@dataclass(frozen=True)
class RequestPause:
    """A pause of the requests with cookies: the answer of YouTube that started it, its end, and the time zone of the
    service. ``until`` is a timezone-aware datetime."""

    reason: BreakerReason
    until: datetime.datetime
    zone: str

    def __str__(self) -> str:
        end = self.until.astimezone(ZoneInfo(self.zone)).strftime(END_FORMAT)
        return (
            f"The service sends no request with the cookies of the account until {end}, because YouTube answered a "
            f"request with {REASON_TEXTS[self.reason]}."
        )

    def to_line(self) -> str:
        """Return the pause as one JSON line, with the end in UTC."""
        until = self.until.astimezone(datetime.UTC).isoformat()
        return json.dumps({"reason": self.reason.value, "until": until, "zone": self.zone})

    @classmethod
    def from_line(cls, line: str | None) -> RequestPause | None:
        """Return the pause of a line of ``to_line``, or None when the line is not one."""
        try:
            value = json.loads(line or "")
            reason = BreakerReason(value["reason"])
            until = datetime.datetime.fromisoformat(value["until"])
            zone = value["zone"]
            # A name that is no valid file name raises OSError on Windows.
            ZoneInfo(zone)
        except ValueError, TypeError, KeyError, OSError:
            return None
        if until.tzinfo is None:
            return None
        return cls(reason, until, zone)
