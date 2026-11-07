#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一条命令跑完一场录制：启动实时会话 → 导演/操作 → 录制 → 收尾。

录制器在本进程内运行，导演与 AutoDL 桥作为子进程启动；Ctrl+C 会让录制器
先正常写盘收尾，再结束后台进程。

示例::

    python run_episode.py --token-file E:/BetterAGI/tools/bettergi_token.txt \
        --duration 1800 \
        --director-config ../lumine/director_config.json \
        --lumine-config ../lumine/bridge_config.json

只录画面、不跑智能体::

    python run_episode.py --token-file E:/BetterAGI/tools/bettergi_token.txt --no-agents

同时启动 Phase 1 shadow 玩家（只读、不操作）::

    python run_episode.py --token-file E:/BetterAGI/tools/bettergi_token.txt \
        --director-config ../lumine/director_config.json \
        --player-shadow-config ../player/player_config.example.json
"""

from __future__ import annotations

import argparse
import os
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import List, Optional

import record_session


def build_recorder_args(options: argparse.Namespace) -> argparse.Namespace:
    argv = [
        "--url",
        options.ws_url,
        "--http-base",
        options.http_base,
        "--out",
        options.out,
        "--capture",
        options.capture,
        "--fps",
        str(options.fps),
        "--window-title",
        options.window_title,
        "--reconnect-ms",
        str(options.reconnect_ms),
    ]
    if options.audio_device:
        argv += ["--audio-device", options.audio_device]
    if options.name:
        argv += ["--name", options.name]
    if options.duration:
        argv += ["--duration", str(options.duration)]
    if options.start:
        argv.append("--start")
    if options.player_mode != "observer":
        argv.append("--strict-safety")
    if options.keep_live:
        pass
    else:
        argv.append("--stop-on-exit")
    if options.player_speech_endpoint:
        argv += ["--player-speech-endpoint", options.player_speech_endpoint]
    argv += ["--speech-mode", options.speech_mode]
    args = record_session.build_parser().parse_args(argv)
    args.token = options.token
    args.player_speech_secret = getattr(options, "player_speech_secret", "")
    args.exclusive_producer_required = (
        options.player_mode == "full_active" and options.speech_mode == "player-exclusive"
        and bool(options.player_shadow_config) and bool(options.player_speech_endpoint)
        and bool(args.player_speech_secret) and not options.director_config and not options.no_agents
    )
    if args.exclusive_producer_required:
        args.formal_chain_id = secrets.token_hex(16)
        player_config = options.player_shadow_config
        player_script = options.player_shadow_script
        speech_endpoint = options.player_speech_endpoint
        bound_process = None

        def formal_chain_guard(pid: int) -> bool:
            nonlocal bound_process
            process = getattr(options, "player_shadow_process", None)
            if bound_process is None:
                bound_process = process
            return (options.player_mode == "full_active" and options.speech_mode == "player-exclusive"
                    and options.player_shadow_config == player_config and options.player_shadow_script == player_script
                    and options.player_speech_endpoint == speech_endpoint
                    and not options.director_config and not options.no_agents
                    and process is not None and process is bound_process
                    and process.pid == pid and process.poll() is None)

        args.formal_chain_guard = formal_chain_guard
    return args


def _spawn_process(
    processes: List[subprocess.Popen], command: List[str], env: dict, label: str
) -> subprocess.Popen:
    """启动一个子进程；失败时先收掉已启动的兄弟进程，避免留下孤儿。"""
    print(f"启动{label}：{' '.join(command)}", flush=True)
    try:
        process = subprocess.Popen(command, env=env)
    except OSError:
        stop_processes(processes)
        raise
    processes.append(process)
    return process


def start_agent_processes(options: argparse.Namespace) -> List[subprocess.Popen]:
    processes: List[subprocess.Popen] = []
    options.player_shadow_process = None
    options.player_shadow_stop_file = None
    # 令牌走环境变量，避免出现在子进程命令行与进程列表里。
    env = os.environ.copy()
    env.pop("BETTERGI_PLAYER_SPEECH_SECRET", None)
    env.pop("BETTERGI_PLAYER_SPEECH_ENDPOINT", None)
    env["BETTERGI_AGENT_TOKEN"] = options.token
    # 只有显式改了默认地址时才覆盖子进程的 WS 配置，避免抢掉配置文件里的远端地址。
    if options.ws_url and options.ws_url != record_session.DEFAULT_WS_URL:
        env["BETTERGI_LIVE_WS"] = options.ws_url
    if options.director_config:
        command = [
            sys.executable,
            str(Path(options.director_script)),
            "--config",
            str(options.director_config),
        ]
        if options.director_no_llm:
            command.append("--no-llm")
        director_env = env.copy()
        director_env.pop("BRIDGE_INTENT_TOKEN", None)
        director_env.pop("LUMINE_SERVER_TOKEN", None)
        _spawn_process(processes, command, director_env, "导演")
    if options.lumine_config:
        command = [
            sys.executable,
            str(Path(options.lumine_script)),
            "--config",
            str(options.lumine_config),
        ]
        motor_env = env.copy()
        for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "LLM_API_KEY", "DEEPSEEK_API_KEY"):
            motor_env.pop(key, None)
        _spawn_process(processes, command, motor_env, "操作桥")
    if options.player_shadow_config:
        # 模式显式传给玩家；observer 不获得 speech 或动作能力。
        command = [
            sys.executable,
            str(Path(options.player_shadow_script)),
            "--config",
            str(options.player_shadow_config),
            "--mode",
            options.player_mode,
            "--observe-http",
            options.http_base,
        ]
        if options.player_intent_url:
            command.extend(["--intent-url", options.player_intent_url])
        if getattr(options, "controlled_mvp", False):
            command.append("--controlled-mvp")
        options.player_shadow_stop_file = Path(tempfile.gettempdir()) / ("betteragi-shadow-stop-" + uuid.uuid4().hex)
        command.extend(["--stop-file", str(options.player_shadow_stop_file)])
        if options.player_observe_ocr:
            command.append("--observe-ocr")
        if options.player_ocr_roi:
            command.extend(["--ocr-roi", *map(str, options.player_ocr_roi)])
        for region in options.player_named_ocr or []:
            command.extend(["--named-ocr", *region])
        for name in ("dialogue", "choice", "speaker", "quest", "hud"):
            roi = getattr(options, "player_" + name + "_roi")
            if roi:
                command.extend(["--" + name + "-roi", *map(str, roi)])
        if options.player_embedding_model:
            command.extend(["--embedding-model", options.player_embedding_model])
        if getattr(options, "player_trusted_motor_config", ""):
            command.extend(["--trusted-motor-config", options.player_trusted_motor_config])
        shadow_env = env.copy()
        shadow_env.pop("LUMINE_SERVER_TOKEN", None)
        if options.player_mode == "full_active" and options.speech_mode == "player-exclusive":
            shadow_env["BETTERGI_PLAYER_SPEECH_ENDPOINT"] = options.player_speech_endpoint
            shadow_env["BETTERGI_PLAYER_SPEECH_SECRET"] = options.player_speech_secret
        options.player_shadow_process = _spawn_process(
            processes, command, shadow_env, "shadow 玩家"
        )
    return processes


def check_agent_processes(processes: List[subprocess.Popen], wait: float = 1.0) -> None:
    """短暂等待后报告启动即退出的子进程；不因此中断录制。"""
    if not processes:
        return
    time.sleep(wait)
    for process in processes:
        code = process.poll()
        if code is not None:
            print(
                f"警告：智能体进程已退出（exit={code}），请检查对应配置/密钥。",
                flush=True,
            )


def stop_processes(
    processes: List[subprocess.Popen],
    shadow_process: Optional[subprocess.Popen] = None,
    grace_seconds: float = 8.0,
    shadow_stop_file: Optional[Path] = None,
) -> None:
    # shadow 会在 live.stopped 或停止文件后自行保存 resume 并写 shadow_end；
    # 先给它优雅退出窗口，再强制结束。Windows 的 terminate 不执行 finally。
    if shadow_stop_file is not None:
        try:
            shadow_stop_file.write_text("stop", encoding="utf-8")
        except OSError as exc:
            print(f"写入 shadow 停止信号失败：{exc}", flush=True)
    if shadow_process is not None and shadow_process.poll() is None:
        try:
            shadow_process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            pass
    for process in processes:
        if process.poll() is None and process is not shadow_process:
            process.terminate()
    for process in processes:
        if process.poll() is None:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()


def apply_no_agents(options: argparse.Namespace) -> None:
    """--no-agents 时清空所有智能体配置，确保只录制。"""
    if not options.no_agents:
        return
    options.director_config = ""
    options.lumine_config = ""
    options.player_shadow_config = ""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="启动实时会话、导演与操作桥，并录制为会话文件",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--token", default="", help="访问令牌（建议用 --token-file）")
    parser.add_argument("--token-file", default="", help="从文件读取访问令牌")
    parser.add_argument("--ws-url", default=record_session.DEFAULT_WS_URL)
    parser.add_argument("--http-base", default=record_session.DEFAULT_HTTP_BASE)
    parser.add_argument("--out", default="sessions", help="会话根目录")
    parser.add_argument("--name", default="", help="会话子目录名")
    parser.add_argument(
        "--duration", type=float, default=0.0, help="录制秒数；0 = 手动停止"
    )
    parser.add_argument("--fps", type=float, default=15.0, help="视频帧率")
    parser.add_argument(
        "--capture",
        choices=["frames", "window", "none"],
        default="frames",
        help="frames = 实时帧合成；window = 直录窗口；none = 只记事件",
    )
    parser.add_argument("--window-title", default="原神")
    parser.add_argument("--audio-device", default="", help="游戏原音的 dshow 音频设备")
    parser.add_argument("--reconnect-ms", type=int, default=2000)
    parser.add_argument(
        "--start", action="store_true", default=True, help="先启动实时会话"
    )
    parser.add_argument(
        "--no-start", dest="start", action="store_false", help="不自动启动实时会话"
    )
    parser.add_argument(
        "--keep-live", action="store_true", help="录制结束后不停止实时会话"
    )
    parser.add_argument(
        "--director-config", default="", help="导演 JSON 配置；为空则不启动导演"
    )
    parser.add_argument("--lumine-config", default="", help="AutoDL 操作桥 JSON 配置")
    parser.add_argument(
        "--player-shadow-config",
        default="",
        help="Phase 1 shadow 玩家 JSON 配置；为空则不启动",
    )
    parser.add_argument(
        "--player-mode", choices=["observer", "motor_active", "full_active"],
        default="observer", help="玩家模式；主动模式须通过全部安全证明",
    )
    parser.add_argument(
        "--controlled-mvp", action="store_true",
        help="启用需人工确认的受控模式；不能证明游戏响应或其他输入被隔离",
    )
    parser.add_argument(
        "--player-intent-url", default="", help="主动玩家的本机 Bridge 意图总线地址",
    )
    parser.add_argument(
        "--player-observe-ocr", action="store_true",
        help="为玩家启用屏幕 OCR（写入 BetterGI 观察事件）",
    )
    parser.add_argument("--player-ocr-roi", type=int, nargs=4, metavar=("X", "Y", "WIDTH", "HEIGHT"),
                        help="玩家 OCR 原帧像素区域，与具名区域互斥")
    parser.add_argument("--player-named-ocr", action="append", nargs=5,
                        metavar=("NAME", "X", "Y", "WIDTH", "HEIGHT"), help="玩家具名 OCR 原帧像素区域，可重复")
    for name in ("dialogue", "choice", "speaker", "quest", "hud"):
        parser.add_argument("--player-" + name + "-roi", type=float, nargs=4,
                            metavar=("LEFT", "TOP", "RIGHT", "BOTTOM"), help="玩家 " + name + " 归一化感知区域")
    parser.add_argument("--player-embedding-model", default="", help="本机玩家经历 embedding 模型目录")
    parser.add_argument("--player-trusted-motor-config", default="",
                        help="玩家目录内显式信任的 Motor 端点配置 JSON")
    parser.add_argument(
        "--director-script",
        default=str(Path(__file__).resolve().parent.parent / "lumine" / "live_director.py"),
    )
    parser.add_argument(
        "--lumine-script",
        default=str(Path(__file__).resolve().parent.parent / "lumine" / "lumine_bridge.py"),
    )
    parser.add_argument(
        "--player-shadow-script",
        default=str(Path(__file__).resolve().parent.parent / "player" / "run_shadow.py"),
    )
    parser.add_argument(
        "--director-no-llm", action="store_true", help="导演使用无 LLM 的占位输出"
    )
    parser.add_argument("--player-speech-endpoint", default="", help="显式启用 player speech 的本机会话描述文件")
    parser.add_argument("--speech-mode", choices=["legacy", "player-exclusive"], default="legacy",
                        help="player-exclusive 排除旧 commentary 解说")
    parser.add_argument(
        "--no-agents", action="store_true", help="只录制，不启动导演与操作桥"
    )
    parser.add_argument("--render-on-exit", action="store_true", help="录制成功结束后生成成片")
    parser.add_argument("--render-tts", choices=["feiyu", "auto", "sapi", "none"], default="feiyu",
                        help="后期配音方式；默认使用飞鱼")
    parser.add_argument("--render-feiyu-style", choices=["cute", "gentle"], default="cute",
                        help="飞鱼音色")
    parser.add_argument("--render-out", default="final.mp4", help="成片路径，相对会话目录")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    options = parser.parse_args(argv)
    if options.player_ocr_roi and options.player_named_ocr:
        print("单一区域与具名 OCR 区域不能同时配置。", file=sys.stderr)
        return 2
    if (options.player_ocr_roi or options.player_named_ocr) and not options.player_observe_ocr:
        print("配置 OCR 区域时必须启用玩家屏幕观察。", file=sys.stderr)
        return 2
    region_options = any(getattr(options, "player_" + name + "_roi") for name in
                         ("dialogue", "choice", "speaker", "quest", "hud"))
    if ((options.player_observe_ocr or options.player_embedding_model or region_options)
            and (options.no_agents or not options.player_shadow_config)):
        print("玩家感知或记忆选项需要配置并启动玩家。", file=sys.stderr)
        return 2
    if options.controlled_mvp and options.player_mode == "observer":
        print("受控 MVP 模式需要主动玩家。", file=sys.stderr)
        return 2
    if options.player_mode == "observer" and options.player_intent_url:
        print("observer 禁止配置意图总线。", file=sys.stderr)
        return 2
    if options.player_mode != "observer" and (options.no_agents or not options.player_shadow_config):
        print("主动模式需要启用并配置玩家；不能退回 observer。", file=sys.stderr)
        return 2
    if options.player_mode != "observer" and options.director_config:
        print("主动玩家不能与旧导演同时控制或生成正式解说。", file=sys.stderr)
        return 2
    if options.player_mode == "full_active" and (options.speech_mode != "player-exclusive"
                                                  or not options.player_speech_endpoint):
        print("full_active 需要 player-exclusive 和 speech IPC 端点。", file=sys.stderr)
        return 2
    if options.speech_mode == "player-exclusive" and options.player_mode != "full_active":
        print("player-exclusive 需要 full_active 玩家。", file=sys.stderr)
        return 2
    options.token = record_session._read_token(options)  # type: ignore[attr-defined]
    options.player_speech_secret = secrets.token_hex(32) if options.player_speech_endpoint else ""
    apply_no_agents(options)

    recorder = record_session.SessionRecorder(build_recorder_args(options))
    recording_errors: list[Exception] = []
    recording_results: list[int] = []
    player_failed = False
    def run_recorder() -> None:
        """将录制线程的错误传回主线程，避免启动失败仍报告成功。"""
        try:
            recording_results.append(recorder.run())
        except Exception as exc:  # noqa: BLE001 - 主线程负责清理所有子进程
            recording_errors.append(exc)
            print(f"录制失败：{exc}", file=sys.stderr, flush=True)
    thread = threading.Thread(target=run_recorder, name="session-recorder")
    processes: List[subprocess.Popen] = []
    try:
        thread.start()
        # 等实时会话出现第一帧后再启动智能体，避免导演读不到画面。
        deadline = time.time() + 20
        while (
            time.time() < deadline and recorder.frame_count == 0 and thread.is_alive()
        ):
            time.sleep(0.2)
        processes = start_agent_processes(options) if thread.is_alive() else []
        player = getattr(options, "player_shadow_process", None)
        if options.speech_mode == "player-exclusive" and player is not None:
            recorder.bind_expected_speech_producer(player.pid)
        if processes:
            check_agent_processes(processes)
        if options.speech_mode == "player-exclusive":
            player = getattr(options, "player_shadow_process", None)
            ready_deadline = time.monotonic() + 30.0
            while (thread.is_alive() and player is not None and player.poll() is None
                   and not recorder.speech_producer_ready.is_set() and time.monotonic() < ready_deadline):
                recorder.speech_producer_ready.wait(timeout=0.1)
            if (not thread.is_alive() or player is None or player.poll() is not None
                    or not recorder.speech_producer_ready.is_set()
                    or recorder.speech_producer_pid != player.pid):
                print("Player 语音生产者未完成会话就绪交接，停止录制。", file=sys.stderr, flush=True)
                player_failed = True
                recorder.stopped = True
        while thread.is_alive():
            player = getattr(options, "player_shadow_process", None)
            if (options.player_mode != "observer" and not player_failed
                    and player is not None and player.poll() is not None):
                print("主动玩家已退出，停止录制；请查看安全门拒绝或撤销原因。", file=sys.stderr, flush=True)
                player_failed = True
                recorder.stopped = True
            thread.join(timeout=0.5)
    except KeyboardInterrupt:
        print("收到中断，正在收尾…", flush=True)
        recorder.stopped = True
        thread.join(timeout=30)
    except Exception:
        # 启动子进程失败时先停录制线程，避免非守护线程让进程挂住。
        recorder.stopped = True
        thread.join(timeout=30)
        raise
    finally:
        stop_file = getattr(options, "player_shadow_stop_file", None)
        stop_processes(
            processes,
            getattr(options, "player_shadow_process", None),
            shadow_stop_file=stop_file,
        )
        if stop_file is not None:
            try:
                stop_file.unlink()
            except OSError:
                pass
    exclusive_failed = (options.speech_mode == "player-exclusive" and
                        (recorder.exclusive_chain_failed.is_set() or recorder._live_session_changed
                         or not recorder.speech_producer_ready.is_set()))
    if player_failed or recording_errors or thread.is_alive() or exclusive_failed:
        return 1
    if not options.render_on_exit:
        return 0
    if recording_results != [0] or recorder.session_dir is None:
        print("录制未成功结束或未创建会话，无法渲染。", file=sys.stderr, flush=True)
        return 1
    import render_session

    try:
        return render_session.main([
            str(recorder.session_dir), "--out", options.render_out,
            "--tts", options.render_tts, "--feiyu-style", options.render_feiyu_style,
        ])
    except Exception as exc:  # noqa: BLE001 - 录制已完成，但不能把渲染异常报告为成功
        print(f"渲染失败：{exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
