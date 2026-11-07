from __future__ import annotations

import copy
import ctypes
import json
import os
import sys
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import betteragi as cli  # noqa: E402


@pytest.mark.parametrize("name", list(cli.COMMANDS))
def test_subcommand_passes_arguments_and_exit_code(monkeypatch, name):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=37)

    monkeypatch.setattr(cli.subprocess, "run", run)
    arguments = ["--first", "含空格的值", "--", "--not-an-option", "*;echo unsafe"]
    assert cli.main([name, *arguments]) == 37
    expected = [sys.executable, str(cli.COMMANDS[name])]
    if name == "journey":
        expected.append("--journey")
    assert calls == [(expected + arguments, {"check": False})]


def test_wrapper_delimiter_is_removed_once(monkeypatch):
    calls = []
    monkeypatch.setattr(cli.subprocess, "run", lambda command, **kwargs: (
        calls.append(command) or SimpleNamespace(returncode=0)
    ))
    assert cli.main(["agent", "--", "status", "--", "literal"]) == 0
    assert calls[0][2:] == ["status", "--", "literal"]


@pytest.fixture
def host_files(tmp_path):
    token = tmp_path / "token.txt"
    token.write_text("secret-value-not-to-log", encoding="utf-8")
    executable = tmp_path / "BetterGI.exe"
    executable.touch()
    return tmp_path / "headless.json", token, executable


def init_args(config, token, *extra):
    return ["host", "init", "--config", str(config), "--token-file", str(token), *extra]


def test_init_creates_only_explicit_unauthorized_config(host_files, capsys):
    config, token, _ = host_files
    assert cli.main(init_args(config, token)) == 0
    raw = config.read_text(encoding="utf-8")
    data = json.loads(raw)
    external = data["externalAccessConfig"]
    assert external == {"httpApiEnabled": True, "webSocketEnabled": True,
                        "agentControlEnabled": True, "mcpEnabled": False, "port": 30648}
    assert all(data[key]["enabled"] is False for key in (
        "autoPickConfig", "autoSkipConfig", "autoFishingConfig", "autoEatConfig", "quickTeleportConfig"
    ))
    assert "secret-value-not-to-log" not in raw
    captured = capsys.readouterr()
    assert "secret-value-not-to-log" not in captured.out + captured.err


def test_init_never_overwrites_existing_config(host_files):
    config, token, _ = host_files
    config.write_bytes(b"existing configuration")
    assert cli.main(init_args(config, token)) == 2
    assert config.read_bytes() == b"existing configuration"


def make_fragments(tmp_path):
    database = str(tmp_path / "events.sqlite")
    evidence = {"PlayerEvidence": {"DatabasePath": database, "PlayerScope": "player-" + "a" * 64}}
    message = {"X": 0.1, "Y": 0.1, "Width": 0.3, "Height": 0.2}
    options = {"X": 0.5, "Y": 0.2, "Width": 0.3, "Height": 0.3}
    full = {"X": 0.0, "Y": 0.0, "Width": 1.0, "Height": 1.0}
    advance = tmp_path / "advance.png"
    main = tmp_path / "main.png"
    advance.touch()
    main.touch()
    calibration = {"ExternalAccessConfig": {
        **evidence,
        "DialogueCandidate": {"Enabled": True, "Message": message, "Options": options, "ExitFrames": 3},
        "DialogueActionAuthorization": {"AllowSelect": True, "AllowAdvance": True,
            "AdvanceTemplatePath": str(advance), "AdvanceTemplateCertified": True,
            "AdvanceRegion": full, "PossibleOptionsRegion": options, "OptionCoverageConfirmed": True},
        "DialogueExitAuthorization": {"Enabled": True, "TemplatePath": str(main),
            "MainTemplateCertified": True, "Region": full, "PossibleDialogueRegion": full,
            "CoverageConfirmed": True, "RequiredFrames": 3},
    }}
    return evidence, calibration


def save_fragment(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    return str(path)


def test_init_merges_explicit_fragments_and_normalizes_keys(host_files, tmp_path):
    config, token, _ = host_files
    evidence, calibration = make_fragments(tmp_path)
    ev_path = save_fragment(tmp_path / "evidence.json", evidence)
    cal_path = save_fragment(tmp_path / "calibration.json", calibration)
    assert cli.main(init_args(config, token, "--player-evidence", ev_path, "--dialogue-calibration", cal_path)) == 0
    data = json.loads(config.read_text(encoding="utf-8"))["externalAccessConfig"]
    assert data["playerEvidence"]["playerScope"] == "player-" + "a" * 64
    assert data["dialogueCandidate"]["message"] == {"x": 0.1, "y": 0.1, "width": 0.3, "height": 0.2}
    assert data["dialogueActionAuthorization"]["advanceTemplateCertified"] is True
    assert data["dialogueExitAuthorization"]["mainTemplateCertified"] is True


@pytest.mark.parametrize("problem", ["token", "unknown", "wrong-type", "coverage", "unconfirmed", "mismatch"])
def test_init_rejects_invalid_fragments_without_creating_file(host_files, tmp_path, capsys, problem):
    config, token, _ = host_files
    evidence, calibration = make_fragments(tmp_path)
    cal = calibration["ExternalAccessConfig"]
    if problem == "token":
        cal["AccessToken"] = "secret-value-not-to-log"
    elif problem == "unknown":
        cal["DialogueCandidate"]["Unknown"] = True
    elif problem == "wrong-type":
        cal["DialogueCandidate"]["ExitFrames"] = True
    elif problem == "coverage":
        cal["DialogueActionAuthorization"]["PossibleOptionsRegion"] = {
            "X": 0.0, "Y": 0.0, "Width": 0.1, "Height": 0.1
        }
    elif problem == "unconfirmed":
        cal["DialogueExitAuthorization"]["MainTemplateCertified"] = False
    else:
        evidence["PlayerEvidence"] = {**evidence["PlayerEvidence"], "PlayerScope": "player-" + "b" * 64}
    ev_path = save_fragment(tmp_path / "evidence.json", evidence)
    cal_path = save_fragment(tmp_path / "calibration.json", calibration)
    assert cli.main(init_args(config, token, "--player-evidence", ev_path, "--dialogue-calibration", cal_path)) == 2
    assert not config.exists()
    captured = capsys.readouterr()
    assert "secret-value-not-to-log" not in captured.out + captured.err


def test_init_rejects_case_variant_duplicate_fields(host_files, tmp_path):
    config, token, _ = host_files
    fragment = tmp_path / "evidence.json"
    fragment.write_text('{"PlayerEvidence":{},"playerEvidence":{}}', encoding="utf-8")
    assert cli.main(init_args(config, token, "--player-evidence", str(fragment))) == 2
    assert not config.exists()


def test_start_rejects_missing_marker_and_never_launches(host_files, monkeypatch):
    config, _, executable = host_files
    config.write_text("{}", encoding="utf-8")
    calls = []
    monkeypatch.setattr(cli.subprocess, "run", lambda *args, **kwargs: calls.append(args))
    assert cli.main(["host", "start", "--exe", str(executable), "--config", str(config)]) == 2
    assert not calls


@pytest.mark.parametrize("marker", [b"bettergi-headless-cli-v1", b"\xef\xbb\xbfbettergi-headless-cli-v1\r\n"])
def test_start_forwards_explicit_files_and_exit_code(host_files, monkeypatch, marker, capsys):
    config, token, executable = host_files
    config.write_text("{}", encoding="utf-8")
    executable.with_name("BetterGI.headless-v1").write_bytes(marker)
    calls = []
    monkeypatch.setattr(cli.subprocess, "run", lambda command, **kwargs: (
        calls.append((command, kwargs)) or SimpleNamespace(returncode=23)
    ))
    assert cli.main(["host", "start", "--exe", str(executable), "--config", str(config),
                     "--token-file", str(token)]) == 23
    assert calls == [([str(executable.resolve()), "--headless", "--headless-config", str(config.resolve()),
                       "--headless-token-file", str(token.resolve())], {"check": False})]
    captured = capsys.readouterr()
    assert "secret-value-not-to-log" not in captured.out + captured.err


class FakeKernel:
    def __init__(self, results):
        self.results = list(results)
        self.waits = []
        self.closed = []

    def WaitForSingleObject(self, handle, timeout):
        self.waits.append((handle, timeout))
        return self.results.pop(0)

    def CloseHandle(self, handle):
        self.closed.append(handle)


def status(pid=123, instance="host-instance"):
    return {"headless": True, "state": "ready", "processId": pid, "instanceId": instance}


def receipt(pid=123, instance="host-instance"):
    return {"status": "drained", "processId": pid, "instanceId": instance,
            "liveStopped": True, "runtimeStopped": True, "shutdownScheduled": True}


def setup_stop(monkeypatch, kernel, responses, calls):
    monkeypatch.setattr(cli.sys, "platform", "win32")
    monkeypatch.setenv("BETTERGI_AGENT_TOKEN", "secret-value-not-to-log")

    def request(opener, origin, token, path, *, body=None, timeout):
        calls.append((origin, token, path, copy.deepcopy(body)))
        result = responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(cli, "_host_request", request)
    monkeypatch.setattr(cli, "_open_process", lambda pid: (kernel, 777))


def test_stop_binds_pid_and_instance_and_waits_for_exit(monkeypatch, capsys):
    kernel = FakeKernel([258, 0])
    calls = []
    setup_stop(monkeypatch, kernel, [status(), receipt()], calls)
    assert cli.main(["host", "stop"]) == 0
    assert calls[0][2].endswith("/headless/status")
    assert calls[1][2].endswith("/headless/shutdown")
    assert calls[1][3] == {"expectedProcessId": 123, "expectedInstanceId": "host-instance"}
    assert len(kernel.waits) == 2 and kernel.waits[0] == (777, 0) and kernel.waits[1][1] > 0
    assert kernel.closed == [777]
    captured = capsys.readouterr()
    assert "secret-value-not-to-log" not in captured.out + captured.err


@pytest.mark.parametrize("bad_receipt", [receipt(pid=124), receipt(instance="other"),
                                        {**receipt(), "status": "accepted"},
                                        {**receipt(), "runtimeStopped": False},
                                        {**receipt(), "shutdownScheduled": False}])
def test_stop_rejects_unbound_or_incomplete_receipt(monkeypatch, bad_receipt):
    kernel = FakeKernel([258])
    calls = []
    setup_stop(monkeypatch, kernel, [status(), bad_receipt], calls)
    assert cli.main(["host", "stop"]) == 1
    assert kernel.waits == [(777, 0)] and kernel.closed == [777]


def test_stop_rejects_boolean_pid_even_when_numeric_value_matches(monkeypatch):
    kernel = FakeKernel([258])
    setup_stop(monkeypatch, kernel, [status(pid=1), receipt(pid=True)], [])
    assert cli.main(["host", "stop"]) == 1
    assert kernel.waits == [(777, 0)] and kernel.closed == [777]


def test_stop_does_not_request_shutdown_without_process_handle(monkeypatch):
    calls = []
    setup_stop(monkeypatch, FakeKernel([]), [status()], calls)
    monkeypatch.setattr(cli, "_open_process", lambda pid: (_ for _ in ()).throw(OSError("denied")))
    assert cli.main(["host", "stop"]) == 1
    assert len(calls) == 1 and calls[0][2].endswith("/headless/status")


def test_stop_timeout_is_not_success(monkeypatch, capsys):
    kernel = FakeKernel([258, 258])
    setup_stop(monkeypatch, kernel, [status(), receipt()], [])
    assert cli.main(["host", "stop"]) == 1
    assert kernel.closed == [777]
    assert "清理完成但退出未确认" in capsys.readouterr().err


def test_stop_network_failure_after_request_is_unknown(monkeypatch, capsys):
    kernel = FakeKernel([258])
    setup_stop(monkeypatch, kernel, [status(), urllib.error.URLError("unavailable")], [])
    assert cli.main(["host", "stop"]) == 1
    assert kernel.closed == [777]
    assert "关闭未确认" in capsys.readouterr().err


def test_stop_rejects_non_loopback_without_request(monkeypatch):
    monkeypatch.setattr(cli.sys, "platform", "win32")
    monkeypatch.setenv("BETTERGI_AGENT_TOKEN", "secret-value-not-to-log")
    monkeypatch.setattr(cli, "_host_request", lambda *args, **kwargs: pytest.fail("must not connect"))
    assert cli.main(["host", "stop", "--base-url", "http://example.org:30648"]) == 2


def test_game_status_reports_only_window_and_focus(monkeypatch, capsys):
    monkeypatch.setattr(cli.sys, "platform", "win32")
    monkeypatch.setattr(cli, "_scan_game_windows", lambda: ([100], False))
    monkeypatch.setattr(cli, "_foreground_window", lambda: 100)
    assert cli.main(["game", "status"]) == 0
    assert json.loads(capsys.readouterr().out) == {"gameWindowPresent": True, "gameForeground": True}


def test_game_status_unknown_does_not_claim_absence(monkeypatch, capsys):
    monkeypatch.setattr(cli.sys, "platform", "win32")
    monkeypatch.setattr(cli, "_scan_game_windows", lambda: ([], True))
    assert cli.main(["game", "status"]) == 1
    assert json.loads(capsys.readouterr().out) == {"gameWindowPresent": None, "gameForeground": None}


def test_game_start_uses_explicit_path_without_secrets_or_shell(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli.sys, "platform", "win32")
    monkeypatch.setattr(cli, "_scan_game_windows", lambda: ([], False))
    monkeypatch.setattr(cli, "_running_process_names", lambda: set())
    monkeypatch.setenv("BETTERGI_AGENT_TOKEN", "secret-value-not-to-log")
    monkeypatch.setenv("LLM_API_KEY", "secret-value-not-to-log")
    executable = tmp_path / "YuanShen.exe"
    executable.touch()
    calls = []
    monkeypatch.setattr(cli.subprocess, "Popen", lambda command, **kwargs: calls.append((command, kwargs)))
    assert cli.main(["game", "start", "--exe", str(executable)]) == 0
    command, kwargs = calls[0]
    assert command == [str(executable.resolve())]
    assert kwargs["cwd"] == str(executable.parent.resolve()) and "shell" not in kwargs
    assert "BETTERGI_AGENT_TOKEN" not in kwargs["env"] and "LLM_API_KEY" not in kwargs["env"]
    captured = capsys.readouterr()
    assert "secret-value-not-to-log" not in captured.out + captured.err
    assert "画面状态尚未确认" in captured.out


@pytest.mark.parametrize("windows,uncertain,names", [([100], False, set()), ([], True, set()),
                                                     ([], False, {"yuanshen.exe"}),
                                                     ([], False, {"launcher.exe"})])
def test_game_start_refuses_existing_or_unknown(monkeypatch, tmp_path, windows, uncertain, names):
    monkeypatch.setattr(cli.sys, "platform", "win32")
    monkeypatch.setattr(cli, "_scan_game_windows", lambda: (windows, uncertain))
    monkeypatch.setattr(cli, "_running_process_names", lambda: names)
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("must not launch"))
    executable = tmp_path / "launcher.exe"
    executable.touch()
    assert cli.main(["game", "start", "--exe", str(executable)]) == 1


def test_game_start_rejects_unknown_executable(monkeypatch, tmp_path):
    monkeypatch.setattr(cli.sys, "platform", "win32")
    monkeypatch.setattr(cli, "_scan_game_windows", lambda: ([], False))
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("must not launch"))
    executable = tmp_path / "unrelated.exe"
    executable.touch()
    assert cli.main(["game", "start", "--exe", str(executable)]) == 2


def test_game_rejects_password_argument_without_echoing(monkeypatch, capsys):
    monkeypatch.setattr(cli.sys, "platform", "win32")
    monkeypatch.setattr(cli, "_scan_game_windows", lambda: pytest.fail("must not scan"))
    assert cli.main(["game", "start", "--exe", "ignored", "--password", "secret-value-not-to-log"]) == 2
    captured = capsys.readouterr()
    assert "secret-value-not-to-log" not in captured.out + captured.err


@pytest.mark.parametrize("windows,uncertain", [([], False), ([100, 200], False), ([100], True)])
def test_game_focus_refuses_missing_ambiguous_or_unknown(monkeypatch, windows, uncertain):
    monkeypatch.setattr(cli.sys, "platform", "win32")
    monkeypatch.setattr(cli, "_scan_game_windows", lambda: (windows, uncertain))
    monkeypatch.setattr(cli, "_focus_game_window", lambda hwnd: pytest.fail("must not focus"))
    assert cli.main(["game", "focus"]) == 1


def test_game_focus_invokes_only_window_focus(monkeypatch):
    monkeypatch.setattr(cli.sys, "platform", "win32")
    monkeypatch.setattr(cli, "_scan_game_windows", lambda: ([100], False))
    calls = []
    monkeypatch.setattr(cli, "_focus_game_window", lambda hwnd: calls.append(hwnd) or True)
    assert cli.main(["game", "focus"]) == 0
    assert calls == [100]


class FakeWinCall:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        return self.result


@pytest.mark.parametrize("iconic,accepted,foreground,expected", [(0, 1, 100, True), (1, 1, 100, False),
                                                                 (0, 0, 100, False), (0, 1, 200, False)])
def test_focus_uses_standard_api_without_bypassing_denial(monkeypatch, iconic, accepted, foreground, expected):
    user32 = SimpleNamespace(IsIconic=FakeWinCall(iconic), SetForegroundWindow=FakeWinCall(accepted),
                             GetForegroundWindow=FakeWinCall(foreground))
    monkeypatch.setattr(cli.ctypes, "WinDLL", lambda *args, **kwargs: user32)
    assert cli._focus_game_window(100) is expected
    assert user32.IsIconic.calls == [(100,)]
    assert user32.SetForegroundWindow.calls == ([] if iconic else [(100,)])


class FakeNativeCall:
    def __init__(self, callback):
        self.callback = callback
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        return self.callback(*args)


@pytest.mark.skipif(not hasattr(ctypes, "WINFUNCTYPE"), reason="Windows API binding")
def test_game_window_scan_queries_only_process_metadata(monkeypatch):
    def text(hwnd, buffer, length):
        buffer.value = "原神"
        return len(buffer.value)

    def process_id(hwnd, pointer):
        ctypes.cast(pointer, ctypes.POINTER(cli.wintypes.DWORD)).contents.value = 42
        return 1

    def image_name(handle, flags, buffer, length):
        buffer.value = r"C:\Games\Genshin Impact\YuanShen.exe"
        return 1

    user32 = SimpleNamespace(
        EnumWindows=FakeNativeCall(lambda callback, extra: callback(100, 0)),
        GetWindowTextLengthW=FakeNativeCall(lambda hwnd: 2),
        GetWindowTextW=FakeNativeCall(text),
        GetWindowThreadProcessId=FakeNativeCall(process_id),
    )
    kernel32 = SimpleNamespace(OpenProcess=FakeNativeCall(lambda access, inherit, pid: 77),
                               QueryFullProcessImageNameW=FakeNativeCall(image_name),
                               CloseHandle=FakeNativeCall(lambda handle: 1))
    monkeypatch.setattr(cli.ctypes, "WinDLL", lambda name, **kwargs: user32 if name == "user32" else kernel32)
    assert cli._scan_game_windows() == ([100], False)
    assert kernel32.OpenProcess.calls == [(0x1000, False, 42)]
    assert kernel32.CloseHandle.calls == [(77,)]


@pytest.mark.skipif(not hasattr(ctypes, "WINFUNCTYPE"), reason="Windows API binding")
def test_game_window_scan_marks_process_query_failure_unknown(monkeypatch):
    def text(hwnd, buffer, length):
        buffer.value = "原神"
        return len(buffer.value)

    def process_id(hwnd, pointer):
        ctypes.cast(pointer, ctypes.POINTER(cli.wintypes.DWORD)).contents.value = 42
        return 1

    user32 = SimpleNamespace(
        EnumWindows=FakeNativeCall(lambda callback, extra: callback(100, 0)),
        GetWindowTextLengthW=FakeNativeCall(lambda hwnd: 2),
        GetWindowTextW=FakeNativeCall(text),
        GetWindowThreadProcessId=FakeNativeCall(process_id),
    )
    kernel32 = SimpleNamespace(OpenProcess=FakeNativeCall(lambda access, inherit, pid: 0),
                               QueryFullProcessImageNameW=FakeNativeCall(lambda *args: 1),
                               CloseHandle=FakeNativeCall(lambda handle: 1))
    monkeypatch.setattr(cli.ctypes, "WinDLL", lambda name, **kwargs: user32 if name == "user32" else kernel32)
    assert cli._scan_game_windows() == ([], True)
    assert not kernel32.QueryFullProcessImageNameW.calls


def test_running_process_names_uses_standard_process_snapshot(monkeypatch):
    def first(snapshot, pointer):
        pointer._obj.name = "YuanShen.exe"
        return 1

    kernel32 = SimpleNamespace(CreateToolhelp32Snapshot=FakeNativeCall(lambda flags, pid: 77),
                               Process32FirstW=FakeNativeCall(first), Process32NextW=FakeNativeCall(lambda *args: 0),
                               CloseHandle=FakeNativeCall(lambda handle: 1))
    monkeypatch.setattr(cli.ctypes, "WinDLL", lambda name, **kwargs: kernel32)
    monkeypatch.setattr(cli.ctypes, "get_last_error", lambda: 18)
    monkeypatch.setattr(cli.ctypes, "set_last_error", lambda code: None)
    assert cli._running_process_names() == {"yuanshen.exe"}
    assert kernel32.CreateToolhelp32Snapshot.calls == [(2, 0)]
    assert kernel32.CloseHandle.calls == [(77,)]


def tunnel_args(*extra):
    return ["tunnel", "--host", "private-host.example", "--ssh-port", "2222", "--user", "private-user",
            "--local-port", "3001", "--remote-port", "3002", *extra]


class FakeTunnelProcess:
    def __init__(self, results):
        self.results = list(results)
        self.terminated = False
        self.killed = False
        self.wait_timeouts = []

    def wait(self, timeout=None):
        self.wait_timeouts.append(timeout)
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


def test_tunnel_forwards_only_loopback_and_preserves_exit_code(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_ssh_binary", lambda: "C:/Windows/System32/OpenSSH/ssh.exe")
    monkeypatch.setenv("BETTERGI_AGENT_TOKEN", "secret-value-not-to-log")
    monkeypatch.setenv("SSH_ASKPASS", "unsafe-external-prompt")
    monkeypatch.setenv("SSH_ASKPASS_REQUIRE", "force")
    calls = []
    process = FakeTunnelProcess([17])
    monkeypatch.setattr(cli.subprocess, "Popen", lambda command, **kwargs: calls.append((command, kwargs)) or process)
    assert cli.main(tunnel_args()) == 17
    command, kwargs = calls[0]
    assert command[:6] == ["C:/Windows/System32/OpenSSH/ssh.exe", "-F", "none", "-N", "-T", "-q"]
    assert command[command.index("-L") + 1] == "127.0.0.1:3001:127.0.0.1:3002"
    assert command[command.index("-p") + 1] == "2222"
    assert command[command.index("-l") + 1] == "private-user"
    assert command[-2:] == ["--", "private-host.example"]
    for option in ("StrictHostKeyChecking=yes", "UpdateHostKeys=no", "ExitOnForwardFailure=yes", "GatewayPorts=no"):
        assert option in command
    assert "shell" not in kwargs and "stdin" not in kwargs and "stderr" not in kwargs
    assert "BETTERGI_AGENT_TOKEN" not in kwargs["env"] and "SSH_ASKPASS" not in kwargs["env"]
    assert kwargs["env"]["SSH_ASKPASS_REQUIRE"] == "never"
    assert "BatchMode=yes" in command
    captured = capsys.readouterr()
    assert all(value not in captured.out + captured.err for value in (
        "private-host.example", "private-user", "secret-value-not-to-log", "unsafe-external-prompt"
    ))


def test_tunnel_explicit_files_use_strict_known_hosts(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "_ssh_binary", lambda: "C:/Windows/System32/OpenSSH/ssh.exe")
    identity = tmp_path / "private key"
    known_hosts = tmp_path / "known hosts"
    identity.touch()
    known_hosts.touch()
    calls = []
    monkeypatch.setattr(cli.subprocess, "Popen", lambda command, **kwargs: (
        calls.append(command) or FakeTunnelProcess([0])
    ))
    assert cli.main(tunnel_args("--identity", str(identity), "--known-hosts", str(known_hosts))) == 0
    command = calls[0]
    assert command[command.index("-i") + 1] == str(identity.resolve())
    assert 'UserKnownHostsFile="' + str(known_hosts.resolve()).replace("\\", "/") + '"' in command
    assert "GlobalKnownHostsFile=none" in command and "StrictHostKeyChecking=yes" in command


@pytest.mark.parametrize("field,value", [("--host", "-evil"), ("--host", "user@host"),
                                         ("--host", "host/path"), ("--host", "host\nname"),
                                         ("--user", "-oProxyCommand"), ("--user", "user@host"),
                                         ("--ssh-port", "0"), ("--local-port", "65536"),
                                         ("--remote-port", "22;evil")])
def test_tunnel_rejects_argument_injection_without_logging(monkeypatch, capsys, field, value):
    monkeypatch.setattr(cli, "_ssh_binary", lambda: "C:/Windows/System32/OpenSSH/ssh.exe")
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("must not connect"))
    args = tunnel_args()
    index = args.index(field)
    args[index:index + 2] = [field + "=" + value]
    assert cli.main(args) == 2
    captured = capsys.readouterr()
    assert "private-host.example" not in captured.out + captured.err
    assert "private-user" not in captured.out + captured.err
    assert value not in captured.out + captured.err


@pytest.mark.parametrize("extra", [["--password", "secret-value-not-to-log"],
                                    ["--token", "secret-value-not-to-log"],
                                    ["--local-bind", "0.0.0.0"], ["--remote-host", "example.org"]])
def test_tunnel_rejects_credentials_and_external_bind_options(monkeypatch, capsys, extra):
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("must not connect"))
    assert cli.main(tunnel_args(*extra)) == 2
    captured = capsys.readouterr()
    assert all(value not in captured.out + captured.err for value in extra)


def test_tunnel_missing_configuration_never_connects(monkeypatch):
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("must not connect"))
    with pytest.raises(SystemExit) as exc:
        cli.main(["tunnel"])
    assert exc.value.code == 2


@pytest.mark.parametrize("option", ["--identity", "--known-hosts"])
def test_tunnel_rejects_relative_or_missing_files(monkeypatch, tmp_path, option):
    monkeypatch.setattr(cli, "_ssh_binary", lambda: "C:/Windows/System32/OpenSSH/ssh.exe")
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("must not connect"))
    assert cli.main(tunnel_args(option, "relative-file")) == 2
    assert cli.main(tunnel_args(option, str(tmp_path / "missing-file"))) == 2


def test_tunnel_does_not_require_batch_mode_on_interactive_terminal(monkeypatch):
    monkeypatch.setattr(cli, "_ssh_binary", lambda: "C:/Windows/System32/OpenSSH/ssh.exe")
    tty = SimpleNamespace(isatty=lambda: True)
    monkeypatch.setattr(cli, "sys", SimpleNamespace(stdin=tty, stderr=tty))
    calls = []
    monkeypatch.setattr(cli.subprocess, "Popen", lambda command, **kwargs: calls.append(command) or FakeTunnelProcess([0]))
    assert cli._tunnel(tunnel_args()[1:]) == 0
    assert "BatchMode=yes" not in calls[0]


def test_tunnel_ctrl_c_terminates_only_owned_process(monkeypatch):
    monkeypatch.setattr(cli, "_ssh_binary", lambda: "C:/Windows/System32/OpenSSH/ssh.exe")
    process = FakeTunnelProcess([KeyboardInterrupt(), 0])
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *args, **kwargs: process)
    assert cli.main(tunnel_args()) == 130
    assert process.terminated and not process.killed and process.wait_timeouts == [None, 5]


def test_tunnel_ctrl_c_kills_unresponsive_owned_process(monkeypatch):
    monkeypatch.setattr(cli, "_ssh_binary", lambda: "C:/Windows/System32/OpenSSH/ssh.exe")
    process = FakeTunnelProcess([KeyboardInterrupt(), cli.subprocess.TimeoutExpired("ssh", 5), 0])
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *args, **kwargs: process)
    assert cli.main(tunnel_args()) == 130
    assert process.terminated and process.killed and process.wait_timeouts == [None, 5, 5]



