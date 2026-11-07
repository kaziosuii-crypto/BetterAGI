from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dsh"))
import agent_client as client  # noqa: E402


@pytest.mark.parametrize("suffix", [b"", b"\n", b"\r\n"])
def test_reads_single_line_token_with_optional_terminator(tmp_path, suffix):
    token_file = tmp_path / "token.txt"
    token_file.write_bytes(b"\xef\xbb\xbfsafe-token_123" + suffix)
    assert client._read_token_file(token_file) == "safe-token_123"


@pytest.mark.parametrize("payload", [b"", b"\n", b"abc\ndef", b"abc\n\n", b"abc\r", b"abc\tdef",
                                      b"abc def", b"abc\x00def", b"\xff", b"a" * 4097])
def test_rejects_invalid_token_file(tmp_path, payload):
    token_file = tmp_path / "token.txt"
    token_file.write_bytes(payload)
    with pytest.raises((ValueError, UnicodeError)):
        client._read_token_file(token_file)


def test_token_file_overrides_environment_and_explicit_token_without_output(tmp_path, monkeypatch, capsys):
    token_file = tmp_path / "token.txt"
    token_file.write_text('file-secret"with\\escapes\n', encoding="utf-8")
    monkeypatch.setenv("BETTERGI_AGENT_TOKEN", "environment-secret")
    observed = {}

    class FakeClient:
        def __init__(self, **options):
            observed.update(options)

        def status(self):
            return {"enabled": True, "echo": observed["token"]}

    monkeypatch.setattr(client, "DshAgentClient", FakeClient)
    assert client.main(["--token", "explicit-secret", "--token-file", str(token_file), "status"]) == 0
    assert observed["token"] == 'file-secret"with\\escapes'
    assert observed["token_file_mode"] is True
    out = capsys.readouterr()
    combined = out.out + out.err
    assert observed["token"] not in combined
    assert json.dumps(observed["token"])[1:-1] not in combined
    assert "environment-secret" not in combined
    assert "explicit-secret" not in combined
    assert json.loads(out.out)["echo"] == "[REDACTED]"


def test_invalid_file_fails_closed_without_echoing_path_or_token(tmp_path, monkeypatch, capsys):
    token_file = tmp_path / "secret-name.txt"
    token_file.write_text("hidden-secret\nsecond-line", encoding="utf-8")
    monkeypatch.setenv("BETTERGI_AGENT_TOKEN", "environment-secret")
    monkeypatch.setattr(client, "DshAgentClient", lambda **kwargs: pytest.fail("invalid file started client"))
    assert client.main(["--token-file", str(token_file), "status"]) == 2
    output = capsys.readouterr()
    assert str(token_file) not in output.out + output.err
    assert "hidden-secret" not in output.out + output.err
    assert "environment-secret" not in output.out + output.err


@pytest.mark.parametrize("path", [None, ""])
def test_missing_file_fails_closed(tmp_path, monkeypatch, capsys, path):
    monkeypatch.setenv("BETTERGI_AGENT_TOKEN", "environment-secret")
    missing = str(tmp_path / "missing") if path is None else path
    assert client.main(["--token-file", missing, "status"]) == 2
    output = capsys.readouterr()
    assert "environment-secret" not in output.out + output.err
    assert "missing" not in output.out + output.err


def test_status_preserves_existing_environment_behavior(monkeypatch, capsys):
    monkeypatch.setenv("BETTERGI_AGENT_TOKEN", "environment-secret")
    observed = {}

    class FakeClient:
        def __init__(self, base_url, token, timeout):
            observed.update(base_url=base_url, token=token, timeout=timeout)

        def status(self):
            return {"enabled": True}

    monkeypatch.setattr(client, "DshAgentClient", FakeClient)
    assert client.main(["status"]) == 0
    assert observed["token"] == "environment-secret"
    assert json.loads(capsys.readouterr().out) == {"enabled": True}


def test_server_error_is_redacted_in_file_mode(tmp_path, monkeypatch, capsys):
    token_file = tmp_path / "token.txt"
    token_file.write_text("hidden-secret", encoding="utf-8")

    class FakeClient:
        def __init__(self, **options):
            self.token = options["token"]

        def status(self):
            raise client.DshAgentError(500, "TEST", "server echoed " + self.token)

    monkeypatch.setattr(client, "DshAgentClient", FakeClient)
    assert client.main(["--token-file", str(token_file), "status"]) == 1
    out = capsys.readouterr()
    assert "hidden-secret" not in out.out + out.err
    assert "[REDACTED]" in out.err


def test_file_transport_sends_bearer_only_in_header(monkeypatch):
    calls = []

    class Response:
        headers = {}
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def read(self):
            return b'{"enabled":true}'

    class Opener:
        def open(self, request, timeout):
            calls.append((request, timeout))
            return Response()

    instance = client.DshAgentClient(token="file-secret", token_file_mode=True)
    instance._opener = Opener()
    assert instance.status() == {"enabled": True}
    request, _ = calls[0]
    assert request.get_header("Authorization") == "Bearer file-secret"
    assert "file-secret" not in request.full_url


def test_file_transport_blocks_redirects_and_untrusted_plain_http():
    assert client._RejectRedirect().redirect_request(None, None, 302, "", {}, "https://elsewhere.test") is None
    for origin in ("http://example.test:30648", "http://user:password@127.0.0.1:30648",
                   "http://127.0.0.1:30648/?token=secret", "http://127.0.0.1:30648/path"):
        with pytest.raises(ValueError):
            client.DshAgentClient(base_url=origin, token="secret", token_file_mode=True)
    assert client.DshAgentClient(base_url="http://example.test", token="secret").base_url == "http://example.test"
    assert isinstance(client.DshAgentClient(base_url="https://example.test", token="secret",
                                            token_file_mode=True)._opener, urllib.request.OpenerDirector)
