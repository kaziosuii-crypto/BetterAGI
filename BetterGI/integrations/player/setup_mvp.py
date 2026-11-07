#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成本机 Player 和 Motor 的显式配置，不创建凭据或启动任何程序。"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import sys
import urllib.parse
from pathlib import Path

from player_profile import ProfileError, load_profile


def _loopback_origin(value: str) -> str:
    try:
        parts = urllib.parse.urlsplit(value)
        if (value != value.strip() or parts.scheme != "http" or not parts.hostname
                or parts.username is not None or parts.password is not None
                or parts.path not in ("", "/") or parts.query or parts.fragment
                or parts.port is None or not ipaddress.ip_address(parts.hostname).is_loopback):
            raise ValueError
        return value.rstrip("/")
    except ValueError as exc:
        raise ValueError("本机 HTTP 地址必须是带端口的 loopback IP origin") from exc


def _motor_origin(value: str) -> str:
    try:
        parts = urllib.parse.urlsplit(value)
        if (not value or value != value.strip() or any(ord(c) <= 32 or ord(c) == 127 for c in value)
                or parts.scheme not in ("ws", "wss") or not parts.hostname
                or parts.username is not None or parts.password is not None
                or parts.path not in ("", "/") or parts.query or parts.fragment or "%" in parts.hostname):
            raise ValueError
        host = parts.hostname.encode("idna").decode("ascii").lower()
        port = parts.port or (443 if parts.scheme == "wss" else 80)
        if not 1 <= port <= 65535 or (parts.scheme == "ws" and not ipaddress.ip_address(host).is_loopback):
            raise ValueError
        host = f"[{host}]" if ":" in host else host
        return f"{parts.scheme}://{host}:{port}"
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Motor origin 必须是 WSS 或 loopback WS，不能包含凭据、路径或查询参数") from exc


def _motor_url(value: str, expected_origin: str) -> str:
    try:
        parts = urllib.parse.urlsplit(value)
        if (not value or value != value.strip() or any(ord(c) <= 32 or ord(c) == 127 for c in value)
                or not parts.hostname or parts.username is not None or parts.password is not None
                or parts.query or parts.fragment or not parts.path.startswith("/") or "\\" in parts.path):
            raise ValueError
        origin = _motor_origin(urllib.parse.urlunsplit((parts.scheme, parts.netloc, "", "", "")))
        if origin != expected_origin:
            raise ValueError
        return value
    except ValueError as exc:
        raise ValueError("Motor WebSocket 地址必须含明确路径且与受信 origin 完全一致，不能含凭据或查询参数") from exc


def _region(value: list[int], width: int, height: int) -> dict[str, int]:
    x, y, w, h = value
    if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > width or y + h > height:
        raise ValueError("OCR 区域必须是完整位于原始画面内的正面积像素矩形")
    return {"x": x, "y": y, "width": w, "height": h}


def _powershell(value: object) -> str:
    text = str(value)
    if any(ord(c) < 32 or ord(c) == 127 for c in text):
        raise ValueError("路径或参数不能包含控制字符")
    return "'" + text.replace("'", "''") + "'"


def _write_json(path: Path, value: dict) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成本机 Player/Motor 配置；不写令牌、不启动游戏")
    parser.add_argument("--home", required=True, help="已存在的玩家目录")
    parser.add_argument("--profile", required=True, help="该玩家目录内已有的身份档案 JSON")
    parser.add_argument("--player-id", required=True, help="必须与档案中的 player_id 完全一致")
    parser.add_argument("--live-origin", required=True, help="本机 BetterGI HTTP loopback origin")
    parser.add_argument("--bridge-origin", required=True, help="本机 Bridge HTTP loopback origin")
    parser.add_argument("--motor-origin", required=True, help="明确受信的 Motor WSS 或 loopback WS origin")
    parser.add_argument("--motor-url", required=True, help="同一 Motor 的完整 WebSocket 地址（不含凭据）")
    parser.add_argument("--frame-size", required=True, nargs=2, type=int, metavar=("WIDTH", "HEIGHT"))
    parser.add_argument("--quest-roi", required=True, nargs=4, type=int, metavar=("X", "Y", "WIDTH", "HEIGHT"))
    parser.add_argument("--hud-roi", required=True, nargs=4, type=int, metavar=("X", "Y", "WIDTH", "HEIGHT"))
    parser.add_argument("--controlled-mvp", action="store_true",
                        help="生成需要交互式人工确认的受控模式启动参数")
    args = parser.parse_args(argv)
    try:
        for value in (args.home, args.profile, args.player_id, args.live_origin, args.bridge_origin,
                      args.motor_origin, args.motor_url):
            _powershell(value)
        home = Path(args.home).expanduser().resolve(strict=True)
        if not home.is_dir():
            raise ValueError("玩家目录必须是已存在的目录")
        profile_path = Path(args.profile).expanduser().resolve(strict=True)
        if not profile_path.is_file() or not profile_path.is_relative_to(home):
            raise ValueError("玩家档案必须是玩家目录内已有文件")
        profile = load_profile(profile_path)
        if profile.player_id != args.player_id:
            raise ValueError("player_id 与所选档案不一致")
        live = _loopback_origin(args.live_origin)
        bridge = _loopback_origin(args.bridge_origin)
        origin = _motor_origin(args.motor_origin)
        motor_url = _motor_url(args.motor_url, origin)
        width, height = args.frame_size
        if width <= 0 or height <= 0:
            raise ValueError("画面尺寸必须是正整数")
        quest = _region(args.quest_roi, width, height)
        hud = _region(args.hud_roi, width, height)
        if (quest["x"] < hud["x"] + hud["width"] and hud["x"] < quest["x"] + quest["width"]
                and quest["y"] < hud["y"] + hud["height"] and hud["y"] < quest["y"] + quest["height"]):
            raise ValueError("quest 与 hud OCR 区域不能重叠")
        live_parts = urllib.parse.urlsplit(live)
        live_ws = urllib.parse.urlunsplit(("ws", live_parts.netloc, "/ws/v1/agent/live", "", ""))
        bridge_parts = urllib.parse.urlsplit(bridge)
        setup = home / "mvp-setup"
        player_config = setup / "player.json"
        bridge_config = setup / "bridge.json"
        trust_config = setup / "motor-trust.json"
        speech_endpoint = home / "state" / "player-speech-endpoint.json"
        cli = Path(__file__).resolve().parent.parent / "betteragi.py"
        command = [sys.executable, str(cli), "episode", "--player-shadow-config", str(player_config),
                   "--lumine-config", str(bridge_config), "--player-mode", "full_active",
                   "--player-intent-url", bridge, "--http-base", live, "--ws-url", live_ws,
                   "--player-trusted-motor-config", str(trust_config), "--player-observe-ocr",
                   "--player-named-ocr", "quest", *map(str, args.quest_roi),
                   "--player-named-ocr", "hud", *map(str, args.hud_roi),
                   "--speech-mode", "player-exclusive", "--player-speech-endpoint", str(speech_endpoint),
                   "--render-on-exit"]
        if args.controlled_mvp:
            command.append("--controlled-mvp")
        for name, roi in (("quest", quest), ("hud", hud)):
            command += [f"--player-{name}-roi", str(roi["x"] / width), str(roi["y"] / height),
                        str((roi["x"] + roi["width"]) / width), str((roi["y"] + roi["height"]) / height)]
        rendered = "& " + " ".join(_powershell(arg) for arg in command)
        setup.mkdir(exist_ok=False)
        _write_json(player_config, {"enabled": True, "shadow": True, "contextBuilder": "v2",
                                    "profile": str(profile_path), "bettergiWs": live_ws})
        _write_json(bridge_config, {"bettergiWs": live_ws, "lumineWs": motor_url,
                                    "motorProtocol": "v2", "intentHost": bridge_parts.hostname,
                                    "intentPort": bridge_parts.port})
        _write_json(trust_config, {"trusted_motor_origin": origin})
        _write_json(setup / "host-player-evidence.json", {"PlayerEvidence": {
            "DatabasePath": str(home / "state" / "cognitive_events.sqlite"),
            "PlayerScope": "player-" + hashlib.sha256(args.player_id.encode("utf-8")).hexdigest(),
        }})
        print("已生成本机配置：", setup)
        print("host-player-evidence.json 是宿主 ExternalAccessConfig 的片段，需用户自行配置，不会自动修改宿主设置。")
        print("需要自行通过环境变量提供 BETTERGI_AGENT_TOKEN、LUMINE_SERVER_TOKEN、BRIDGE_INTENT_TOKEN 和 LLM 凭据；本工具不会读取或写入它们。")
        print("PowerShell 中设置玩家目录：")
        print("$env:PLAYER_HOME = " + _powershell(home))
        print("启动命令：")
        print(rendered)
        print("安全门仍然生效；宿主对话模板需用户真实标定与显式授权，实际运行和音画对齐仍需联调。")
        return 0
    except (OSError, ValueError, ProfileError) as exc:
        print(f"无法生成配置：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
