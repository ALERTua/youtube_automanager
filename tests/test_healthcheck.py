import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from youtube_automanager import healthcheck
from youtube_automanager.settings import get_settings


@pytest.mark.parametrize(
    ("host", "url"),
    [
        ("0.0.0.0", "http://127.0.0.1:8080/health"),
        ("", "http://127.0.0.1:8080/health"),
        ("::", "http://127.0.0.1:8080/health"),
        ("127.0.0.1", "http://127.0.0.1:8080/health"),
        ("::1", "http://[::1]:8080/health"),
        ("service.local", "http://service.local:8080/health"),
    ],
)
def test_health_url(host, url):
    assert healthcheck.health_url(host, 8080) == url


def serve(status):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(status if self.path == "/health" else 404)
            self.end_headers()

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def settings_env(monkeypatch):
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


@pytest.mark.parametrize(("status", "code"), [(200, 0), (503, 1)])
def test_main_gives_the_exit_code_of_the_answer(settings_env, status, code):
    server = serve(status)
    try:
        settings_env.setenv("HOST", "0.0.0.0")
        settings_env.setenv("PORT", str(server.server_address[1]))

        assert healthcheck.main() == code
    finally:
        server.shutdown()
        server.server_close()


def test_main_gives_1_when_nothing_listens(settings_env, capsys):
    server = serve(200)
    port = server.server_address[1]
    server.shutdown()
    server.server_close()
    settings_env.setenv("HOST", "127.0.0.1")
    settings_env.setenv("PORT", str(port))

    assert healthcheck.main() == 1
    assert "does not answer" in capsys.readouterr().err
