import base64
import logging

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from youtube_automanager.settings import Settings
from youtube_automanager.web.access import AccessGuard, host_name
from youtube_automanager.web.app import create_app

PASSWORD = "fake-pässword"


async def page(request):
    return PlainTextResponse("page")


async def socket(websocket: WebSocket):
    await websocket.accept()
    await websocket.send_text("hello")
    await websocket.close()


def guarded(**options) -> TestClient:
    app = Starlette(
        routes=[Route("/", page), Route("/health", page), Route("/other", page), WebSocketRoute("/ws", socket)]
    )
    return TestClient(AccessGuard(app, **options))


def basic(user: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.mark.parametrize(
    ("header", "name"),
    [
        ("Mini:8080", "mini"),
        ("192.168.1.5:8080", "192.168.1.5"),
        ("[::1]:8080", "::1"),
        ("[fe80::1]", "fe80::1"),
        ("fe80::1", "fe80::1"),
        ("example.com.", "example.com"),
        ("", ""),
    ],
)
def test_host_name_drops_the_port_and_the_brackets(header, name):
    assert host_name(header) == name


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "192.168.1.5:8080",
        "[::1]:8080",
        "localhost:8080",
        "mini",
        "MINI:8080",
        "server.tail1234.ts.net",
        "nas.local",
        "nas.lan",
        "nas.home.arpa",
        "nas.internal",
    ],
)
def test_default_hosts_pass(host):
    response = guarded().get("/", headers={"Host": host})

    assert response.status_code == 200
    assert response.text == "page"


@pytest.mark.parametrize("host", ["evil.example.com", "attacker.com:8080", "ts.net", "evil.ts.net.com"])
def test_a_foreign_host_gets_400_that_names_the_setting(host):
    response = guarded().get("/", headers={"Host": host})

    assert response.status_code == 400
    assert "ALLOWED_HOSTS" in response.text
    assert response.headers["content-type"].startswith("text/plain")


def test_a_request_without_a_host_gets_400():
    client = guarded()

    response = client.get("/", headers={"Host": ""})

    assert response.status_code == 400


def test_allowed_hosts_add_exact_names_and_subdomains_case_insensitive():
    client = guarded(allowed_hosts=" YouTube.Example.com , .home.example.org")

    assert client.get("/", headers={"Host": "youtube.example.com"}).status_code == 200
    assert client.get("/", headers={"Host": "a.b.home.example.org:443"}).status_code == 200
    assert client.get("/", headers={"Host": "home.example.org"}).status_code == 200
    assert client.get("/", headers={"Host": "other.example.com"}).status_code == 400
    assert client.get("/", headers={"Host": "evilhome.example.org"}).status_code == 400


def test_a_star_turns_the_host_check_off():
    response = guarded(allowed_hosts="example.com, *").get("/", headers={"Host": "evil.example.com"})

    assert response.status_code == 200


def test_each_refused_host_name_is_logged_once(caplog):
    client = guarded()

    with caplog.at_level(logging.WARNING, logger="youtube_automanager.web.access"):
        for _ in range(3):
            client.get("/", headers={"Host": "evil.example.com"})
        client.get("/", headers={"Host": "EVIL.example.com:81"})
        client.get("/", headers={"Host": "other.example.com"})

    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 2
    assert "'evil.example.com'" in messages[0]
    assert "'other.example.com'" in messages[1]
    assert all("ALLOWED_HOSTS" in message for message in messages)


def test_a_foreign_host_closes_the_websocket_before_its_accept():
    client = guarded()

    with (
        pytest.raises(WebSocketDisconnect) as refused,
        client.websocket_connect("/ws", headers={"Host": "evil.example.com"}),
    ):
        pass

    assert refused.value.code == 1008


def test_a_default_host_opens_the_websocket():
    with guarded().websocket_connect("/ws", headers={"Host": "mini:8080"}) as websocket:
        assert websocket.receive_text() == "hello"


def test_without_a_password_no_credentials_are_needed():
    assert guarded().get("/other").status_code == 200


def test_the_password_asks_for_basic_auth():
    response = guarded(password=PASSWORD).get("/other")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == 'Basic realm="YouTube AutoManager", charset="UTF-8"'
    assert "UI_PASSWORD" in response.text
    assert PASSWORD not in response.text


@pytest.mark.parametrize("user", ["", "anybody", "Ім'я"])
def test_the_password_passes_with_any_user_name(user):
    response = guarded(password=PASSWORD).get("/other", headers=basic(user, PASSWORD))

    assert response.status_code == 200


@pytest.mark.parametrize(
    "authorization",
    [
        basic("user", "wrong")["Authorization"],
        basic("user", PASSWORD + "x")["Authorization"],
        basic("user", "")["Authorization"],
        "Basic " + base64.b64encode(PASSWORD.encode()).decode(),
        "Basic not-base64!",
        "Bearer fake-token",
    ],
)
def test_a_wrong_password_gets_401(authorization):
    response = guarded(password=PASSWORD).get("/other", headers={"Authorization": authorization})

    assert response.status_code == 401


def test_health_needs_no_password():
    assert guarded(password=PASSWORD).get("/health").status_code == 200


def test_a_foreign_host_gets_400_also_with_the_password():
    response = guarded(password=PASSWORD).get("/other", headers={"Host": "evil.example.com", **basic("u", PASSWORD)})

    assert response.status_code == 400


def test_the_password_covers_the_websocket():
    client = guarded(password=PASSWORD)

    with pytest.raises(WebSocketDisconnect) as refused, client.websocket_connect("/ws"):
        pass
    with client.websocket_connect("/ws", headers=basic("u", PASSWORD)) as websocket:
        assert websocket.receive_text() == "hello"

    assert refused.value.code == 1008


def test_the_password_is_never_logged(caplog):
    client = guarded(password=PASSWORD)

    with caplog.at_level(logging.DEBUG):
        client.get("/other", headers=basic("u", "wrong"))
        client.get("/other", headers={"Host": "evil.example.com", **basic("u", PASSWORD)})

    assert PASSWORD not in caplog.text


def test_the_service_checks_the_host_and_the_password_of_its_settings(tmp_path):
    settings = Settings(
        home=tmp_path, allowed_hosts="automanager.example.com", ui_password=SecretStr(PASSWORD), tz="UTC"
    )
    # Without a "with" block, the client does not start the lifespan, so the database stays untouched.
    client = TestClient(create_app(settings))

    assert client.get("/api/status", headers={"Host": "evil.example.com"}).status_code == 400
    assert client.get("/api/status", headers={"Host": "automanager.example.com"}).status_code == 401
    assert client.get("/api/unknown", headers=basic("u", PASSWORD)).status_code == 404


def test_the_service_needs_no_password_by_default(tmp_path):
    client = TestClient(create_app(Settings(home=tmp_path, tz="UTC")))

    assert client.get("/api/unknown").status_code == 404
    assert client.get("/api/unknown", headers={"Host": "evil.example.com"}).status_code == 400
