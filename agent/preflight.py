# -*- coding: utf-8 -*-
'''开播前自检（设计文档 04_acceptance.md 的 preflight 六项）：不通过就不开播。

六项（用户口径全选）：

1. game_window   游戏窗口在不在、能不能截到图
2. omni          Omni realtime 能不能连上（含额度）
3. gamescaling   手模型服务连通 + 版本
4. danmaku       弹幕服务在不在（读得到 /stats）
5. graphiti      长期记忆连通 + 上一场摘要可读
6. sticky        便利贴合法（两块各 ≤1024、词表内）
7. disk          录播磁盘余量与推流合成

每项都带**可执行提示**（hint）：失败时人要知道下一步做什么，不是只知道「失败了」。
超时也算失败——自检不许挂死，所以每项都在独立线程里跑并有截止时间。
'''
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional

DEFAULT_CHECK_TIMEOUT_MS = 3000


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ''
    hint: str = ''
    elapsed_ms: int = 0
    skipped: bool = False
    check_id: str = ''

    def to_dict(self) -> Dict[str, Any]:
        return {
            'name': self.name,
            'check_id': self.check_id,
            'ok': bool(self.ok),
            'detail': self.detail,
            'hint': self.hint,
            'elapsed_ms': int(self.elapsed_ms),
            'skipped': bool(self.skipped),
        }


@dataclass
class PreflightReport:
    results: List[CheckResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(r.ok or r.skipped for r in self.results)

    @property
    def failures(self) -> List[CheckResult]:
        return [r for r in self.results if not r.ok and not r.skipped]

    def to_dict(self) -> Dict[str, Any]:
        return {
            'ok': self.ok,
            'checks': [r.to_dict() for r in self.results],
            'failures': [r.name for r in self.failures],
        }

    def markdown(self) -> str:
        lines = ['# 开播前自检', '']
        lines.append('- 结论：' + ('通过，可以开播' if self.ok else '不通过，不许开播'))
        lines.append('')
        lines.append('| 项 | 结果 | 耗时 | 说明 |')
        lines.append('|---|---|---|---|')
        for item in self.results:
            mark = '跳过' if item.skipped else ('通过' if item.ok else '失败')
            lines.append('| %s | %s | %dms | %s |' % (item.name, mark, item.elapsed_ms, (item.detail or '').replace('|', '/'))),
        bad = self.failures
        if bad:
            lines += ['', '## 要先修的']
            for item in bad:
                lines.append('- **%s**：%s' % (item.name, item.detail or '（无说明）'))
                if item.hint:
                    lines.append('  - 下一步：' + item.hint)
        return chr(10).join(lines)


def _run_one(name: str, fn: Callable[[], Any], timeout_ms: int) -> CheckResult:
    '''跑一项：异常/超时都变成失败结果，不往上抛。'''

    started = time.monotonic()
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='preflight-' + name)
    try:
        future = pool.submit(fn)
        try:
            value = future.result(timeout=max(1, int(timeout_ms)) / 1000.0)
        except FutureTimeout:
            future.cancel()
            return CheckResult(name=name, ok=False, detail='超过 %dms 没回应' % timeout_ms,
                               hint='服务没起或卡死：先看该服务进程与端口，再看它的日志',
                               elapsed_ms=int((time.monotonic() - started) * 1000))
        except Exception as error:  # noqa: BLE001 - 自检要把异常变结论
            return CheckResult(name=name, ok=False, detail='%s: %s' % (type(error).__name__, error),
                               hint='按上面的异常去查该服务；自检信息不足时先手工验证一遍',
                               elapsed_ms=int((time.monotonic() - started) * 1000))
    finally:
        pool.shutdown(wait=False)
    elapsed = int((time.monotonic() - started) * 1000)
    if isinstance(value, CheckResult):
        value.elapsed_ms = elapsed
        # 清单里给的是人看的标签（弹幕服务/便利贴/磁盘），检查函数自己带的是模块名（danmaku/sticky）。
        # 标签为准，模块名留到 check_id —— 报告里出现两个 danmaku 时人分不清是哪一项。
        if name and value.name != name:
            value.check_id = value.check_id or value.name
            value.name = name
        return value
    if isinstance(value, dict):
        return CheckResult(name=name, ok=bool(value.get('ok')), detail=str(value.get('detail') or ''),
                           hint=str(value.get('hint') or ''), elapsed_ms=elapsed,
                           skipped=bool(value.get('skipped')))
    if isinstance(value, tuple):
        ok, detail = (bool(value[0]), str(value[1]) if len(value) > 1 else '')
        hint = str(value[2]) if len(value) > 2 else ''
        return CheckResult(name=name, ok=ok, detail=detail, hint=hint, elapsed_ms=elapsed)
    return CheckResult(name=name, ok=bool(value), elapsed_ms=elapsed)


def run_preflight(checks: Iterable[Any], timeout_ms: int = DEFAULT_CHECK_TIMEOUT_MS) -> PreflightReport:
    '''checks 里每项可以是 (name, callable) 或 callable（名字取 __name__）。'''

    report = PreflightReport()
    for item in checks:
        if isinstance(item, tuple):
            name, fn = item[0], item[1]
        else:
            name, fn = getattr(item, '__name__', 'check'), item
        report.results.append(_run_one(str(name), fn, timeout_ms))
    return report


# ---- 默认六项（每个都只做「探一下」，不做副作用） --------------------------


def check_danmaku(feed: Any = None) -> CheckResult:
    if feed is None:
        import danmaku_feed

        feed = danmaku_feed.build_feed()
    stats = feed.stats()
    if not stats.get('ok'):
        return CheckResult('danmaku', False, '弹幕服务没回应：%s' % (stats.get('error') or '未知'),
                           '起本机弹幕 bus（GET /danmaku?since=<ms>&limit=50 要能返回 JSONL）')
    return CheckResult('danmaku', True, 'ok，在线 %s 人' % (stats.get('online') or '?'))


def check_sticky(game: str, save_id: str, root: Optional[str] = None) -> CheckResult:
    '''便利贴合不合法（A5 的 sticky 模块负责真正的读写）。'''

    try:
        import sticky  # type: ignore
    except Exception as error:  # noqa: BLE001 - 便利贴模块还没落地时要说清
        return CheckResult('sticky', False, '便利贴模块不可用：%s' % error, '先落地 agent/sticky.py，再开播')
    data = sticky.read_sticky(game, save_id, root=root)
    warnings = data.get('warnings') or []
    if warnings:
        return CheckResult('sticky', False, '便利贴有问题：' + '; '.join(warnings),
                           '用慢系统的 write_sticky 重写，或手工修 sticky/%s/%s.md' % (game, save_id))
    return CheckResult('sticky', True, 'key_binding %d 字 / game_rules %d 字' % (
        len(data.get('key_binding') or ''), len(data.get('game_rules') or '')))


def check_disk(path: str, min_free_mb: int = 5120) -> CheckResult:
    import shutil

    free_mb = shutil.disk_usage(path).free / (1024 * 1024)
    if free_mb < min_free_mb:
        return CheckResult('disk', False, '只剩 %.0fMB' % free_mb,
                           '清盘：删旧分片/旧 incidents，留够 %dMB' % min_free_mb)
    return CheckResult('disk', True, '剩 %.0fMB' % free_mb)


def check_motor(client: Any) -> CheckResult:
    '''手模型在不在。health 的写法有两种（ok / available），两种都要认，别把掉线的判成通过。'''

    health = client.health()
    if isinstance(health, dict):
        if 'ok' in health:
            healthy = bool(health.get('ok'))
        elif 'available' in health:
            healthy = bool(health.get('available'))
        else:
            healthy = not health.get('error')
        if not healthy:
            detail = health.get('error') or health.get('detail') or health
            return CheckResult('gamescaling', False, str(detail)[:300],
                               '起手模型服务（2B-S3 checkpoint 单卡），再验 /health')
    return CheckResult('gamescaling', True, str(health)[:200])

# ---- 补上设计里的其余项（游戏窗口 / Omni / 记忆 / 编码器） -------------------


def check_window(probe: Any = None) -> CheckResult:
    '''游戏窗口在不在、能不能截到图。

    probe 是一个无参可调用：返回 (ok, detail) / bool / dict 都收；返回体带 w/h 就写进
    detail。没给 probe 时**标跳过而不是通过**——「没查」不能伪装成「查过没问题」。
    '''

    if probe is None:
        return CheckResult('game_window', True, '没给窗口探针，跳过截图检查',
                           '传 probe=lambda: adapter.grab() 之类；或开播前手动确认窗口没被最小化',
                           skipped=True)
    try:
        value = probe()
    except Exception as error:  # noqa: BLE001 - 抓图失败就是不能开播
        return CheckResult('game_window', False, '抓图失败：%s: %s' % (type(error).__name__, error),
                           '把游戏窗口切到前台、确认没最小化，再重试一次抓图')
    if isinstance(value, dict):
        if 'ok' in value:
            value = (bool(value.get('ok')), str(value.get('detail') or ''))
        elif 'data' in value:
            size = '%sx%s' % (value.get('w') or '?', value.get('h') or '?')
            value = (bool(value.get('data')), '抓到一帧 %s' % size)
    if isinstance(value, tuple):
        ok = bool(value[0])
        detail = str(value[1]) if len(value) > 1 else ''
        return CheckResult('game_window', ok, detail, '' if ok else '窗口没画面：确认游戏没在过场/最小化')
    ok = bool(value)
    return CheckResult('game_window', ok, 'ok' if ok else '抓图返回空',
                       '' if ok else '窗口没画面：确认游戏没在过场/最小化')


def check_omni(key: Optional[str] = None, model: str = '') -> CheckResult:
    '''Omni 的凭证在不在。真实额度只能靠实连验证，这里不假装测过。'''

    if not key:
        try:
            import fast_system  # type: ignore

            key = fast_system.resolve_omni_key()
        except Exception as error:  # noqa: BLE001
            return CheckResult('omni', False, '拿不到 Omni 凭证：%s' % error,
                               '设置 MINICPM_API_KEY（或写 keys/minicpm.key）；'
                               '旧的 DashScope 路径仍可用 DASHSCOPE_API_KEY')
    if not key:
        return CheckResult('omni', False, '没有 Omni 凭证',
                           '设置 MINICPM_API_KEY（或写 keys/minicpm.key）；'
                           '旧的 DashScope 路径仍可用 DASHSCOPE_API_KEY')
    masked = (key[:4] + '…' + key[-2:]) if len(key) > 8 else '…'
    name = model or 'MiniCPM-o-4.5-Realtime'
    return CheckResult('omni', True, '凭证在（%s），模型 %s；额度只能实连验证' % (masked, name),
                       '')


def check_memory(memory: Any = None, query: str = '上一场 进度 这一局做到哪') -> CheckResult:
    '''长期记忆连通 + 上一场摘要可读（第一次开播没有记录算正常）。'''

    if memory is None:
        return CheckResult('memory', False, '没接记忆后端',
                           'run_stream 里 build_memory 失败时这里会空；先修记忆装配再开播')
    name = str(getattr(memory, 'name', '') or type(memory).__name__)
    group = str(getattr(memory, 'group_id', '') or '')
    try:
        hits = memory.search(query, top_k=1, layers=['episodic'])
    except TypeError:
        hits = memory.search(query, limit=1)
    except Exception as error:  # noqa: BLE001 - 读不了就是不能靠它防失忆
        return CheckResult('memory', False, '%s 读不了：%s: %s' % (name, type(error).__name__, error),
                           'Graphiti 看 Neo4j/FalkorDB 有没有在跑（GRAPHITI_URI）；本地后端看 memory 目录权限')
    count = len(hits or [])
    if count == 0:
        return CheckResult('memory', True, '%s 连通（group=%s），还没有上一场记录' % (name, group or '?'),
                           '')
    return CheckResult('memory', True, '%s 连通，上一场摘要可读（%d 条）' % (name, count), '')


def check_encoder() -> CheckResult:
    '''录播/推流合成要用的 ffmpeg 在不在。'''

    try:
        import record  # type: ignore
    except Exception as error:  # noqa: BLE001
        return CheckResult('encoder', False, '录播模块不可用：%s' % error, '先落地 agent/record.py')
    try:
        path = record.ffmpeg_path()
    except Exception as error:  # noqa: BLE001 - record 抛 RecordError 就是没装
        return CheckResult('encoder', False, '找不到 ffmpeg：%s' % error,
                           '装 ffmpeg 并加进 PATH，或用 BETTERAGI_FFMPEG 指定绝对路径')
    return CheckResult('encoder', True, str(path))

