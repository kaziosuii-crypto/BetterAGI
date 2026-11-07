# -*- coding: utf-8 -*-
'''事故留证（设计文档 02_schemas.md §11）：出问题先留证，再断流下播。

用户的硬口径：**手模型解析失败 = 程序错误**。程序错误不许静默重试、不许用假动作掩盖，
要做的是把「那一刻的完整现场」原样落盘，然后断流下播。

一个 bundle 就是 incidents/<ts>_<intent_id>/ 下的 8 个东西：

    README.md            自动生成：触发点、时间线摘要、结论占位
    request.json         发给模型的完整请求（system prompt 原文）
    raw_reply.txt        原始返回字节，不裁剪
    parsed.json          解析中间态：tokens、解析器输出、为什么判失败
    frames/              失败前后各 3 帧（调用方负责拷，这里给位置）
    timeline_slice.jsonl ±10 秒的时间轴切片
    proc.json            线程数 / 内存 / active_sessions / 队列深度
    env.json             build、模型版本、GPU、驱动、checkpoint 路径

保留策略：最近 20 个或 2GB 先删最旧；目录里有 keep 文件的不删。
'''
from __future__ import annotations

import json
import os
import platform
import shutil
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

KEEP_MARKER = 'keep'
DEFAULT_KEEP_COUNT = 20
DEFAULT_MAX_BYTES = 2 * 1024 * 1024 * 1024
#: 时间轴切片半径（毫秒）：事故前后各 10 秒。
DEFAULT_SLICE_MS = 10000


#: 事故现场采集里最危险的事：为了写一个环境快照把进程卡死。
#: 平台探测在 Windows 上会去调外部命令（cmd /c ver 一类），系统一有状况就能
#: 几分钟不返回；而这条路径是在崩溃处理里跑的，卡住等于连事故现场一起丢了。
#: 所以每个可能阻塞的探针都套一层上界：超时就写 unknown 并记一笔，绝不等待。
SNAPSHOT_TIMEOUT_S = 2.0
UNKNOWN = 'unknown'


def _bounded(fn: Any, default: Any = None, timeout_s: float = SNAPSHOT_TIMEOUT_S) -> Any:
    '''在守护线程里跑 fn，超时返回 default（不抛异常、不阻塞调用方）。'''

    box: Dict[str, Any] = {}

    def _run() -> None:
        try:
            box['value'] = fn()
        except Exception:  # noqa: BLE001 - 探针失败不影响留证
            box['failed'] = True

    worker = threading.Thread(target=_run, name='incident-probe', daemon=True)
    worker.start()
    worker.join(timeout_s)
    if worker.is_alive():
        return default
    return box.get('value', default)


#: torch / CUDA 探针的预算：慢，但值得等一会儿；卡死就更不值得。
TORCH_PROBE_TIMEOUT_S = 5.0


def _torch_info() -> Dict[str, Any]:
    '''torch / CUDA 概况；没装就返回空字典（不编版本号）。'''

    info: Dict[str, Any] = {}
    try:
        import torch  # type: ignore

        info['torch'] = torch.__version__
        info['cuda'] = bool(torch.cuda.is_available())
        if info['cuda']:
            info['gpu'] = torch.cuda.get_device_name(0)
    except Exception:  # noqa: BLE001 - 不一定装了 torch
        return {}
    return info


def _now_tag() -> str:
    return time.strftime('%Y%m%d-%H%M%S')


def _memory_mb() -> Optional[float]:
    '''当前进程常驻内存（MB）；拿不到就返回 None，不编数字。'''

    try:
        import ctypes
        from ctypes import wintypes

        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [
                ('cb', wintypes.DWORD),
                ('PageFaultCount', wintypes.DWORD),
                ('PeakWorkingSetSize', ctypes.c_size_t),
                ('WorkingSetSize', ctypes.c_size_t),
                ('QuotaPeakPagedPoolUsage', ctypes.c_size_t),
                ('QuotaPagedPoolUsage', ctypes.c_size_t),
                ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t),
                ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                ('PagefileUsage', ctypes.c_size_t),
                ('PeakPagefileUsage', ctypes.c_size_t),
            ]

        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(counters)
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetCurrentProcess()
        if ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            return round(counters.WorkingSetSize / (1024 * 1024), 1)
    except Exception:  # noqa: BLE001 - 非 Windows 或 API 不可用
        return None


@dataclass
class IncidentBundle:
    '''一个事故目录：建目录、写证、按策略清理。'''

    root: Path
    kind: str
    intent_id: str = ''
    reason: str = ''
    extra: Dict[str, Any] = field(default_factory=dict)
    path: Path = field(init=False)

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        name = '%s_%s' % (_now_tag(), (self.intent_id or 'no-intent').strip() or 'no-intent')
        self.path = self.root / name
        self.path.mkdir(parents=True, exist_ok=True)
        (self.path / 'frames').mkdir(exist_ok=True)

    # ---- 写证 ----------------------------------------------------------

    def write_json(self, name: str, payload: Any) -> Path:
        target = self.path / name
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        return target

    def write_text(self, name: str, text: str) -> Path:
        target = self.path / name
        target.write_text(str(text or ''), encoding='utf-8')
        return target

    def copy_frames(self, paths: Iterable[Any]) -> List[str]:
        '''把失败前后的帧拷进 frames/；源文件丢了就记进 README，不假装有。'''

        saved: List[str] = []
        missing: List[str] = []
        for item in paths:
            source = Path(str(item))
            try:
                if not source.is_file():
                    missing.append(str(source))
                    continue
                target = self.path / 'frames' / source.name
                shutil.copy2(source, target)
                saved.append(target.name)
            except Exception:  # noqa: BLE001
                missing.append(str(source))
        if missing:
            (self.path / 'frames' / 'MISSING.txt').write_text(chr(10).join(missing), encoding='utf-8')
        return saved

    def timeline_slice(self, rows: Iterable[Dict[str, Any]], anchor_ms: int, radius_ms: int = DEFAULT_SLICE_MS) -> Path:
        '''±radius 的时间轴切片（anchor 是事故点的 t_ms）。'''

        lo, hi = int(anchor_ms) - int(radius_ms), int(anchor_ms) + int(radius_ms)
        picked = [r for r in rows if lo <= int(r.get('t_ms') or 0) <= hi]
        target = self.path / 'timeline_slice.jsonl'
        with target.open('w', encoding='utf-8') as handle:
            for row in picked:
                handle.write(json.dumps(row, ensure_ascii=False) + chr(10))
        return target

    def proc_snapshot(self, extra: Optional[Dict[str, Any]] = None) -> Path:
        threads = [t.name for t in threading.enumerate()]
        payload: Dict[str, Any] = {
            'ts': time.time(),
            'thread_count': threading.active_count(),
            'threads': threads,
            'memory_mb': _bounded(_memory_mb),
            'pid': os.getpid(),
        }
        payload.update(extra or {})
        return self.write_json('proc.json', payload)

    def env_snapshot(self, extra: Optional[Dict[str, Any]] = None) -> Path:
        # 这三个探针里 platform.platform() 在 Windows 上可能去调外部命令，
        # 崩溃路径上不能等它：超时就留 unknown，并在 slow_probes 里记一笔。
        probes: Dict[str, Any] = {
            'platform': _bounded(platform.platform, UNKNOWN),
            'machine': _bounded(platform.machine, UNKNOWN),
            'cpu_count': _bounded(os.cpu_count, None),
        }
        payload: Dict[str, Any] = {
            'ts': time.time(),
            'python': sys.version.split()[0],
            'platform': probes['platform'],
            'machine': probes['machine'],
            'cpu_count': probes['cpu_count'],
            'slow_probes': sorted(name for name, value in probes.items() if value is UNKNOWN),
            'cwd': _bounded(os.getcwd, UNKNOWN),
            'env': {k: v for k, v in os.environ.items() if k.startswith(('BETTERAGI_', 'GAMESCALING_', 'OMNI_', 'DASHSCOPE_'))},
        }
        # 导入 torch / 初始化 CUDA 在崩溃现场也可能卡几秒，同样要上界。
        torch_info = _bounded(_torch_info, {}, timeout_s=TORCH_PROBE_TIMEOUT_S)
        payload.update(torch_info or {})
        if not torch_info:
            payload['slow_probes'] = sorted(payload.get('slow_probes', []) + ['torch'])
        payload.update(extra or {})
        return self.write_json('env.json', payload)

    def readme(self, summary: str = '') -> Path:
        body = [
            '# 事故留证 ' + self.kind,
            '',
            '- 时间：' + time.strftime('%Y-%m-%d %H:%M:%S'),
            '- intent_id：' + (self.intent_id or '（无）'),
            '- 触发原因：' + (self.reason or '（未填写）'),
            '',
            '## 现场',
            '',
            '见 request.json / raw_reply.txt / parsed.json / frames/ / timeline_slice.jsonl / proc.json / env.json。',
            '',
            '## 摘要',
            '',
            summary or '（待补）',
            '',
            '## 结论（占位，复盘后填）',
            '',
            '- 根因：',
            '- 影响：',
            '- 修法：',
            '',
        ]
        if self.extra:
            body += ['## 附加上下文', '', '```json', json.dumps(self.extra, ensure_ascii=False, indent=2), '```', '']
        return self.write_text('README.md', chr(10).join(body))

    def capture_error(self, error: BaseException, summary: str = '') -> Path:
        text = ''.join(traceback.format_exception(type(error), error, error.__traceback__))
        self.write_text('parsed.json', json.dumps({
            'error_type': type(error).__name__,
            'error': str(error),
            'diagnostics': {k: v for k, v in vars(error).items() if not k.startswith('_')} if hasattr(error, '__dict__') else {},
        }, ensure_ascii=False, indent=2))
        return self.readme(summary=summary + chr(10) + chr(10) + '```' + chr(10) + text + '```')


def prune(root: Path, keep_count: int = DEFAULT_KEEP_COUNT, max_bytes: int = DEFAULT_MAX_BYTES) -> List[str]:
    '''只保留最近 keep_count 个且总量不超过 max_bytes；带 keep 标记的不删。返回删掉的目录名。'''

    root = Path(root)
    if not root.is_dir():
        return []
    bundles = sorted([p for p in root.iterdir() if p.is_dir()], key=lambda p: p.name)
    sizes: Dict[str, int] = {}
    for bundle in bundles:
        total = 0
        for item in bundle.rglob('*'):
            try:
                if item.is_file():
                    total += item.stat().st_size
            except OSError:
                continue
        sizes[bundle.name] = total
    removable = [b for b in bundles if not (b / KEEP_MARKER).exists()]
    keep_set = {b.name for b in bundles if (b / KEEP_MARKER).exists()}
    # 先按数量：最新 keep_count 个保底留下
    survivors = {b.name for b in bundles[-keep_count:]}
    survivors |= keep_set
    deleted: List[str] = []
    for bundle in removable:
        if bundle.name in survivors:
            continue
        shutil.rmtree(bundle, ignore_errors=True)
        deleted.append(bundle.name)
    # 再按体积：从最旧开始删，直到总量达标
    total_bytes = sum(sizes.get(name, 0) for name in survivors)
    for bundle in removable:
        if total_bytes <= max_bytes:
            break
        if bundle.name in keep_set or not bundle.exists():
            continue
        total_bytes -= sizes.get(bundle.name, 0)
        shutil.rmtree(bundle, ignore_errors=True)
        deleted.append(bundle.name)
    return deleted
