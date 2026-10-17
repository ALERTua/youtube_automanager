"""The access control of the web service: a check of the ``Host`` header and an optional password.

The check of the ``Host`` header stops a DNS rebinding attack. A web page of an attacker can make the browser send
requests to the service under the domain name of the attacker, and such a request has that name as ``Host``. The
default names that pass are the names of a home network, which an attacker on the internet does not control.

The password asks for HTTP basic auth on each path except ``/health``, also on the websocket of NiceGUI.
"""

import base64
import binascii
import ipaddress
import logging
import secrets

from starlette.types import ASGIApp, Receive, Scope, Send

LOG = logging.getLogger(__name__)

HEALTH_PATH = "/health"
# A host name under one of these domains passes without a setting.
DEFAULT_SUFFIXES = (".ts.net", ".local", ".lan", ".home.arpa", ".internal")
# The refused names that the log keeps, so a flood of new names cannot fill the memory.
MAX_LOGGED_HOSTS = 256
REALM = "YouTube AutoManager"


def parse_allowed_hosts(value: str) -> tuple[frozenset[str], tuple[str, ...], bool]:
    """Split ``ALLOWED_HOSTS`` into the exact names, the suffixes of the names that start with a dot, and whether
    ``*`` turns the check off."""
    names = {item.strip().lower().rstrip(".") for item in value.split(",")}
    names.discard("")
    if "*" in names:
        return frozenset(), (), True
    exact = {name.lstrip(".") for name in names}
    suffixes = tuple(sorted(name for name in names if name.startswith(".")))
    return frozenset(exact), suffixes, False


def host_name(header: str) -> str:
    """Return the host name of a ``Host`` header in lower case, without the port, the brackets of an IPv6 address
    and a trailing dot."""
    header = header.strip().lower()
    if header.startswith("["):
        return header[1 : header.find("]")] if "]" in header else ""
    name, colon, port = header.rpartition(":")
    # Only one colon separates a port. More colons are an IPv6 address without brackets.
    if colon and ":" not in name and port.isdigit():
        header = name
    return header.rstrip(".")


def _is_ip_address(name: str) -> bool:
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return False
    return True


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope["headers"]:
        if key == name:
            return value.decode("latin-1")
    return None


class AccessGuard:
    """A pure ASGI middleware that refuses a foreign ``Host`` with HTTP 400 and asks for the password when one is set.

    A refused websocket gets closed before its accept. ``allowed_hosts`` is the value of ``ALLOWED_HOSTS``, and an
    empty ``password`` turns the password off.
    """

    def __init__(self, app: ASGIApp, *, allowed_hosts: str = "", password: str = "") -> None:
        self.app = app
        self.exact, self.suffixes, self.any_host = parse_allowed_hosts(allowed_hosts)
        self.password = password.encode()
        self.logged_hosts: set[str] = set()

    def host_allowed(self, name: str) -> bool:
        if self.any_host:
            return True
        if not name:
            return False
        if _is_ip_address(name) or name == "localhost" or "." not in name:
            return True
        if name in self.exact:
            return True
        return name.endswith(DEFAULT_SUFFIXES + self.suffixes)

    def authorized(self, header: str | None) -> bool:
        scheme, _, credentials = (header or "").partition(" ")
        if scheme.lower() != "basic":
            return False
        try:
            decoded = base64.b64decode(credentials.strip(), validate=True)
        except binascii.Error:
            return False
        # Any user name passes, so only the part after the first colon counts.
        _user, colon, password = decoded.partition(b":")
        return bool(colon) and secrets.compare_digest(password, self.password)

    def _log_refused(self, name: str) -> None:
        if name in self.logged_hosts or len(self.logged_hosts) >= MAX_LOGGED_HOSTS:
            return
        self.logged_hosts.add(name)
        LOG.warning(
            "Refused a request with the host name %r. To allow it, add it to ALLOWED_HOSTS. A DNS rebinding attack "
            "sends a foreign host name",
            name,
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        name = host_name(_header(scope, b"host") or "")
        if not self.host_allowed(name):
            self._log_refused(name)
            await _refuse(
                scope, receive, send, 400, "The host name of this request is not allowed. Add it to ALLOWED_HOSTS."
            )
            return
        if self.password and scope["path"] != HEALTH_PATH and not self.authorized(_header(scope, b"authorization")):
            await _refuse(
                scope,
                receive,
                send,
                401,
                "The service needs the password of UI_PASSWORD.",
                ((b"www-authenticate", f'Basic realm="{REALM}", charset="UTF-8"'.encode()),),
            )
            return
        await self.app(scope, receive, send)


async def _refuse(
    scope: Scope, receive: Receive, send: Send, status: int, text: str, headers: tuple[tuple[bytes, bytes], ...] = ()
) -> None:
    if scope["type"] == "websocket":
        # The first message of a websocket is its connect. A close before the accept refuses the handshake.
        await receive()
        await send({"type": "websocket.close", "code": 1008})
        return
    body = f"{text}\n".encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
                *headers,
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
