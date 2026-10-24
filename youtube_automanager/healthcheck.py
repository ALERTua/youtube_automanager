"""The health check of the container: exit 0 when the service answers ``/health`` with HTTP 200, else exit 1.

The image has no curl, so the HEALTHCHECK of the Dockerfile runs this module with the Python of the service.
"""

import sys
import urllib.error
import urllib.request

from youtube_automanager.settings import get_settings

TIMEOUT_SECONDS = 5


def health_url(host: str, port: int) -> str:
    # A service that listens on all interfaces answers on the loopback address too.
    if host in ("", "0.0.0.0", "::"):
        host = "127.0.0.1"
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{port}/health"


def main() -> int:
    settings = get_settings()
    url = health_url(settings.host, settings.port)
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS) as response:
            return 0 if response.status == 200 else 1
    # An answer other than 2xx raises HTTPError. The error holds the open answer of the server, so it needs a close
    # like a response.
    except urllib.error.HTTPError as error:
        with error:
            print(f"The service does not answer {url}: {error}", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"The service does not answer {url}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
