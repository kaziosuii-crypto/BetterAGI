#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""shadow 玩家的配置解析：CLI > 环境变量 > JSON 配置 > 默认值。

令牌只从 CLI/环境变量/令牌文件读取，不写入 JSON 配置、profile、state 或日志。
"""

from __future__ import annotations

import json
import os
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

DEFAULT_WS_URL = "ws://127.0.0.1:30648/ws/v1/agent/live"
DEFAULT_SESSION_ID = "vtuber"
DEFAULT_INTERVAL = 12.0
DEFAULT_RECONNECT_DELAY = 3.0
DEFAULT_MAX_FRAME_AGE_MS = 5000
DEFAULT_STATE_SAVE_INTERVAL_MS = 30000


def default_player_home() -> Path:
    """默认玩家数据目录：E:\\BetterAGI\\player（与重构文档一致）。"""
    return Path(__file__).resolve().parents[3] / "player"


def build_ws_url(ws_url: str, token: str) -> str:
    """附加有效令牌；URL 里遗留的 token 参数一律移除，避免旧令牌覆盖新令牌。"""
    try:
        parts = urllib.parse.urlsplit(ws_url)
        query = [
            (key, value)
            for key, value in urllib.parse.parse_qsl(
                parts.query, keep_blank_values=True
            )
            if key.lower() not in ("access_token", "token")
        ]
        if token:
            query.append(("access_token", token))
        return urllib.parse.urlunsplit(
            (
                parts.scheme,
                parts.netloc,
                parts.path,
                urllib.parse.urlencode(query),
                parts.fragment,
            )
        )
    except ValueError:
        if not token:
            return ws_url
        separator = "&" if "?" in ws_url else "?"
        return ws_url + separator + urllib.parse.urlencode({"access_token": token})


def redact_url(url: str) -> str:
    """从日志 URL 中移除令牌查询参数与 userinfo。"""
    try:
        parts = urllib.parse.urlsplit(url)
        query = [
            (key, value)
            for key, value in urllib.parse.parse_qsl(
                parts.query, keep_blank_values=True
            )
            if key.lower() not in ("access_token", "token")
        ]
        netloc = parts.netloc.rsplit("@", 1)[-1]
        return urllib.parse.urlunsplit(
            (
                parts.scheme,
                netloc,
                parts.path,
                urllib.parse.urlencode(query),
                parts.fragment,
            )
        )
    except ValueError:
        return "<invalid-url>"


def load_config_file(path: Path | str) -> Dict[str, Any]:
    """读取 JSON 配置；文件不存在时明确报错。"""
    source = Path(path)
    if not source.exists():
        raise FileNotFoundError("配置文件不存在：{0}".format(source))
    with open(source, "r", encoding="utf-8-sig") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("配置文件必须是 JSON 对象：{0}".format(source))
    return data


def read_token_file(path: Path | str) -> str:
    """从文件读取令牌；文件缺失或为空都明确报错。"""
    source = Path(path)
    try:
        token = source.read_text(encoding="utf-8-sig").strip()
    except OSError as exc:
        raise ValueError("读取令牌文件失败：{0}（{1}）".format(source, exc))
    if not token:
        raise ValueError("令牌文件为空：{0}".format(source))
    return token


def _env_nonempty(environ: Dict[str, str], name: str) -> Optional[str]:
    """空白环境变量视为未设置，避免覆盖配置文件里的显式开关。"""
    value = environ.get(name)
    if isinstance(value, str) and value.strip():
        return value
    return None


def _pick(cli_value: Any, env: Dict[str, str], env_name: str, config: Dict[str, Any], keys: tuple, default: Any = None) -> Any:
    if cli_value is not None and cli_value != "":
        return cli_value
    if env_name:
        value = env.get(env_name)
        if value not in (None, ""):
            return value
    for key in keys:
        if key in config and config[key] is not None:
            return config[key]
    return default


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on", "enabled"):
        return True
    if text in ("0", "false", "no", "off", "disabled"):
        return False
    return default


def _as_float(value: Any, default: float, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return min(high, max(low, number))


def _as_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return min(high, max(low, number))


def _resolve_path(value: Any, base: Path) -> Optional[Path]:
    if value in (None, ""):
        return None
    path = Path(str(value))
    if not path.is_absolute():
        path = base / path
    return path


@dataclass(frozen=True)
class PlayerConfig:
    home: Path
    profile_path: Path
    state_path: Path
    shadow_dir: Path
    memory_dir: Path
    episodes_dir: Path
    semantic_path: Path
    working_path: Path
    recap_path: Path
    belief_path: Path
    relationship_path: Path
    emotion_path: Path
    thread_path: Path
    ws_url: str
    token: str
    session_id: str
    interval: float
    reconnect_delay: float
    max_frame_age_ms: int
    state_save_interval_ms: int
    enabled: bool
    no_llm: bool
    shadow_mode: bool
    context_builder: str
    profile_is_default: bool
    config_path: Optional[Path] = None

    def describe(self) -> str:
        """可安全写日志的配置摘要（不含令牌）。"""
        return (
            "ws={0} session={1} interval={2:.1f}s shadow={3} state={4} "
            "memory={5} context={6} enabled={7}"
        ).format(
            redact_url(build_ws_url(self.ws_url, self.token)),
            self.session_id,
            self.interval,
            self.shadow_dir,
            self.state_path,
            self.memory_dir,
            self.context_builder,
            self.enabled,
        )


def resolve_config(
    config_path: Optional[Path | str] = None,
    *,
    env: Optional[Dict[str, str]] = None,
    home: Optional[Path | str] = None,
    profile: Optional[Path | str] = None,
    state: Optional[Path | str] = None,
    shadow_dir: Optional[Path | str] = None,
    memory_dir: Optional[Path | str] = None,
    episodes_dir: Optional[Path | str] = None,
    semantic_path: Optional[Path | str] = None,
    working_path: Optional[Path | str] = None,
    recap_path: Optional[Path | str] = None,
    belief_path: Optional[Path | str] = None,
    relationship_path: Optional[Path | str] = None,
    emotion_path: Optional[Path | str] = None,
    thread_path: Optional[Path | str] = None,
    context_builder: Optional[str] = None,
    ws_url: Optional[str] = None,
    token: Optional[str] = None,
    token_file: Optional[Path | str] = None,
    session_id: Optional[str] = None,
    interval: Optional[float] = None,
    reconnect_delay: Optional[float] = None,
    max_frame_age_ms: Optional[int] = None,
    state_save_interval_ms: Optional[int] = None,
    enabled: Optional[bool] = None,
    no_llm: Optional[bool] = None,
    shadow_mode: Optional[bool] = None,
) -> PlayerConfig:
    """合并各层配置；相对路径按配置文件所在目录（无配置时按当前目录）解析。"""
    environ = dict(os.environ if env is None else env)
    config: Dict[str, Any] = {}
    config_file: Optional[Path] = None
    if config_path:
        config_file = Path(config_path).resolve()
        config = load_config_file(config_file)
    base_dir = config_file.parent if config_file else Path.cwd()

    home_value = home or environ.get("PLAYER_HOME")
    home_path = Path(home_value).expanduser() if home_value else default_player_home()
    home_path = home_path.resolve()

    profile_path = _resolve_path(profile, base_dir) or _resolve_path(
        environ.get("PLAYER_PROFILE"), base_dir
    ) or _resolve_path(config.get("profile"), base_dir)
    default_profile_path = home_path / "profile" / "profile.json"
    if profile_path is None:
        profile_path = default_profile_path
    # 只有指向默认位置时才允许自动播种；自定义路径缺失必须显式报错，
    # 避免路径写错后静默换成另一套人格。
    profile_is_default = profile_path == default_profile_path

    state_path = _resolve_path(state, base_dir) or _resolve_path(
        environ.get("PLAYER_STATE"), base_dir
    ) or _resolve_path(config.get("state") or config.get("resumeState"), base_dir)
    if state_path is None:
        state_path = home_path / "state" / "resume_state.json"

    shadow_dir_path = _resolve_path(shadow_dir, base_dir) or _resolve_path(
        environ.get("PLAYER_SHADOW_DIR"), base_dir
    ) or _resolve_path(config.get("shadowDir") or config.get("shadow_dir"), base_dir)
    if shadow_dir_path is None:
        shadow_dir_path = home_path / "state" / "shadow"

    memory_dir_path = _resolve_path(memory_dir, base_dir) or _resolve_path(
        environ.get("PLAYER_MEMORY_DIR"), base_dir
    ) or _resolve_path(config.get("memoryDir") or config.get("memory_dir"), base_dir)
    if memory_dir_path is None:
        memory_dir_path = home_path / "memory"

    episodes_dir_path = _resolve_path(episodes_dir, base_dir) or _resolve_path(
        environ.get("PLAYER_EPISODES_DIR"), base_dir
    ) or _resolve_path(
        config.get("episodesDir") or config.get("episodes_dir"), base_dir
    )
    if episodes_dir_path is None:
        episodes_dir_path = memory_dir_path / "episodes"

    semantic_path_value = _resolve_path(semantic_path, base_dir) or _resolve_path(
        environ.get("PLAYER_SEMANTIC_PATH"), base_dir
    ) or _resolve_path(
        config.get("semanticPath") or config.get("semantic_path"), base_dir
    )
    if semantic_path_value is None:
        semantic_path_value = memory_dir_path / "semantic" / "facts.json"

    working_path_value = _resolve_path(working_path, base_dir) or _resolve_path(
        environ.get("PLAYER_WORKING_PATH"), base_dir
    ) or _resolve_path(
        config.get("workingPath") or config.get("working_path"), base_dir
    )
    if working_path_value is None:
        working_path_value = home_path / "state" / "working.json"

    recap_path_value = _resolve_path(recap_path, base_dir) or _resolve_path(
        environ.get("PLAYER_RECAP_PATH"), base_dir
    ) or _resolve_path(config.get("recapPath") or config.get("recap_path"), base_dir)
    if recap_path_value is None:
        recap_path_value = home_path / "state" / "session_recap.json"

    def _state_path(cli, env_name, key, name):
        return _resolve_path(cli, base_dir) or _resolve_path(
            environ.get(env_name), base_dir
        ) or _resolve_path(config.get(key), base_dir) or (home_path / "state" / name)

    belief_path_value = _state_path(belief_path, "PLAYER_BELIEF_PATH", "beliefPath", "belief_state.json")
    relationship_path_value = _state_path(relationship_path, "PLAYER_RELATIONSHIP_PATH", "relationshipPath", "relationships.json")
    emotion_path_value = _state_path(emotion_path, "PLAYER_EMOTION_PATH", "emotionPath", "emotion_state.json")
    thread_path_value = _state_path(thread_path, "PLAYER_THREAD_PATH", "threadPath", "narrative_threads.json")

    builder_value = _pick(
        context_builder,
        environ,
        "CONTEXT_BUILDER",
        config,
        ("contextBuilder", "context_builder"),
        "v2",
    )
    resolved_builder = str(builder_value or "v2").strip().lower()
    if resolved_builder not in ("legacy", "v2"):
        resolved_builder = "v2"

    resolved_ws = _pick(
        ws_url,
        environ,
        "BETTERGI_LIVE_WS",
        config,
        ("bettergiWs", "wsUrl", "ws"),
        DEFAULT_WS_URL,
    )
    resolved_token = token or (read_token_file(token_file) if token_file else "")
    if not resolved_token:
        resolved_token = (environ.get("BETTERGI_AGENT_TOKEN") or "").strip()
    else:
        resolved_token = str(resolved_token).strip()

    resolved_session = _pick(
        session_id,
        environ,
        "BETTERGI_SESSION_ID",
        config,
        ("sessionId", "session_id"),
        DEFAULT_SESSION_ID,
    )
    resolved_interval = _as_float(
        _pick(interval, environ, "PLAYER_INTERVAL", config, ("interval",), DEFAULT_INTERVAL),
        DEFAULT_INTERVAL,
        low=1.0,
        high=600.0,
    )
    resolved_reconnect = _as_float(
        _pick(
            reconnect_delay,
            environ,
            "PLAYER_RECONNECT_DELAY",
            config,
            ("reconnectDelay", "reconnect_delay"),
            DEFAULT_RECONNECT_DELAY,
        ),
        DEFAULT_RECONNECT_DELAY,
        low=0.0,
        high=120.0,
    )
    resolved_frame_age = _as_int(
        _pick(
            max_frame_age_ms,
            environ,
            "PLAYER_MAX_FRAME_AGE_MS",
            config,
            ("maxFrameAgeMs", "max_frame_age_ms"),
            DEFAULT_MAX_FRAME_AGE_MS,
        ),
        DEFAULT_MAX_FRAME_AGE_MS,
        low=0,
        high=60000,
    )
    resolved_state_save = _as_int(
        _pick(
            state_save_interval_ms,
            environ,
            "PLAYER_STATE_SAVE_INTERVAL_MS",
            config,
            ("stateSaveIntervalMs", "state_save_interval_ms"),
            DEFAULT_STATE_SAVE_INTERVAL_MS,
        ),
        DEFAULT_STATE_SAVE_INTERVAL_MS,
        low=1000,
        high=600000,
    )

    enabled_value = (
        enabled
        if enabled is not None
        else _env_nonempty(environ, "PLAYER_AGENT_ENABLED")
    )
    if enabled_value is None:
        enabled_value = config.get("enabled", True)
    resolved_enabled = _as_bool(enabled_value, True)

    no_llm_value = (
        no_llm
        if no_llm is not None
        else _env_nonempty(environ, "PLAYER_NO_LLM")
    )
    resolved_no_llm = _as_bool(no_llm_value, False)

    shadow_value = (
        shadow_mode
        if shadow_mode is not None
        else _env_nonempty(environ, "PLAYER_AGENT_SHADOW")
    )
    if shadow_value is None:
        shadow_value = config.get("shadow", config.get("shadowMode", True))
    resolved_shadow = _as_bool(shadow_value, True)

    return PlayerConfig(
        home=home_path,
        profile_path=profile_path,
        state_path=state_path,
        shadow_dir=shadow_dir_path,
        memory_dir=memory_dir_path,
        episodes_dir=episodes_dir_path,
        semantic_path=semantic_path_value,
        working_path=working_path_value,
        recap_path=recap_path_value,
        belief_path=belief_path_value,
        relationship_path=relationship_path_value,
        emotion_path=emotion_path_value,
        thread_path=thread_path_value,
        ws_url=str(resolved_ws),
        token=str(resolved_token or ""),
        session_id=str(resolved_session or DEFAULT_SESSION_ID),
        interval=resolved_interval,
        reconnect_delay=resolved_reconnect,
        max_frame_age_ms=resolved_frame_age,
        state_save_interval_ms=resolved_state_save,
        enabled=resolved_enabled,
        no_llm=resolved_no_llm,
        shadow_mode=resolved_shadow,
        context_builder=resolved_builder,
        profile_is_default=profile_is_default,
        config_path=config_file,
    )


def ensure_directories(config: PlayerConfig) -> None:
    """创建运行所需目录；profile 文件本身由 runner 决定是否播种。"""
    config.profile_path.parent.mkdir(parents=True, exist_ok=True)
    config.state_path.parent.mkdir(parents=True, exist_ok=True)
    config.shadow_dir.mkdir(parents=True, exist_ok=True)
    config.memory_dir.mkdir(parents=True, exist_ok=True)
    config.episodes_dir.mkdir(parents=True, exist_ok=True)
    config.semantic_path.parent.mkdir(parents=True, exist_ok=True)
    config.working_path.parent.mkdir(parents=True, exist_ok=True)
    config.recap_path.parent.mkdir(parents=True, exist_ok=True)
    for path in (
        config.belief_path,
        config.relationship_path,
        config.emotion_path,
        config.thread_path,
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
