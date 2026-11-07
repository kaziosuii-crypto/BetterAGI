# -*- coding: utf-8 -*-
'''慢系统（设计文档 02_schemas.md §4 SlowPlan）：只出一段稍规整的 markdown，程序负责拼装。

三条硬口径（用户拍板）：

1. 慢系统**不写 JSON、不写函数调用参数**——那段 markdown 只有三节：这一轮做什么 / 就算成了 / 别做。
2. 『这一轮做什么』超过 400 字就是「在写剧本」，不是任务，必须退回重写（不是截断）。
3. 慢系统有预算（≤2s）：超时不是重试，是编排层认定「这轮没想出来」，画面贴「大肥鱼CPU烧了」。

thinking 分级（用户口径）：常规轮关 thinking；**同一处卡关超过 N 次**才开 5-10 秒，并用「发呆」圆场。
'''
from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

SECTION_GOAL = '这一轮做什么'
SECTION_DONE = '就算成了'
SECTION_AVOID = '别做'
SECTIONS = (SECTION_GOAL, SECTION_DONE, SECTION_AVOID)

GOAL_MAX_CHARS = 400
#: 慢系统最多看几张画面：看多了既是钱也是延迟，三张足够判断「我在哪、有没有变化」。
MAX_SLOW_FRAMES = 3
#: None = 不设截止。用户 2026-11-04：慢系统思考超时设为永久。
DEFAULT_BUDGET_MS: Optional[int] = None
THINKING_BUDGET_MS: Optional[int] = None
DEFAULT_BLOCKED_THRESHOLD = 3


class SlowPlannerError(Exception):
    '''慢系统调用本身的错误。'''


class SlowPlannerTimeout(SlowPlannerError):
    '''慢系统超预算。'''

    def __init__(self, waited_ms: int, budget_ms: int) -> None:
        super().__init__('慢系统等了 %dms，超过预算 %dms' % (waited_ms, budget_ms))
        self.waited_ms = waited_ms
        self.budget_ms = budget_ms


@dataclass
class SlowPlan:
    goal: str = ''
    success: str = ''
    avoid: str = ''
    raw: str = ''
    warnings: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        '''能不能拿去执行：必须有一段非空的「这一轮做什么」。'''

        return bool(self.goal.strip())

    @property
    def needs_rewrite(self) -> bool:
        '''超 400 字：不是在写任务，是在写剧本。'''

        return len(self.goal) > GOAL_MAX_CHARS

    def to_dict(self) -> Dict[str, Any]:
        return {
            'goal': self.goal,
            'success': self.success,
            'avoid': self.avoid,
            'needs_rewrite': self.needs_rewrite,
            'warnings': list(self.warnings),
        }


def parse_slow_plan(text: str) -> SlowPlan:
    '''把慢系统那段 markdown 解成三节。缺节只记 warning，不编内容。'''

    plan = SlowPlan(raw=text or '')
    if not (text or '').strip():
        plan.warnings.append('慢系统返回空文本')
        return plan
    current: Optional[str] = None
    bucket: Dict[str, List[str]] = {name: [] for name in SECTIONS}
    for raw_line in str(text).splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        heading = stripped.lstrip('#').strip()
        if stripped.startswith('#') and heading in SECTIONS:
            current = heading
            continue
        if current is None:
            if stripped:
                # 没写标题就开写：当成「这一轮做什么」，但要提醒
                bucket[SECTION_GOAL].append(stripped)
                current = SECTION_GOAL
                if '缺少标题' not in ' '.join(plan.warnings):
                    plan.warnings.append('缺少标题，首段被当成「%s」' % SECTION_GOAL)
            continue
        bucket[current].append(line)
    plan.goal = chr(10).join(bucket[SECTION_GOAL]).strip()
    plan.success = chr(10).join(bucket[SECTION_DONE]).strip()
    plan.avoid = chr(10).join(bucket[SECTION_AVOID]).strip()
    if not plan.goal.strip():
        plan.warnings.append('缺「%s」：这轮没法执行' % SECTION_GOAL)
    if not plan.success.strip():
        plan.warnings.append('缺「%s」：交棒没有判据，快系统只能自己主观判断' % SECTION_DONE)
    if plan.needs_rewrite:
        plan.warnings.append('「%s」%d 字，超过 %d 字上限：退回重写' % (SECTION_GOAL, len(plan.goal), GOAL_MAX_CHARS))
    return plan


# ---- 慢系统 prompt ------------------------------------------------------

SLOW_SYSTEM = '''你是一个游戏主播的大脑（慢系统）。你的输出会被程序直接拼进操作模型的 system prompt。

只输出三段 markdown，不要任何别的话：

## 这一轮做什么
（一句到三句，**英文**，可执行的自然语言目标；不超过 400 字）

## 就算成了
（画面出现什么就该交棒）

## 别做
（可选：避免剧透/避免耗资源/避免某些行为）

你手上有一块便利贴（sticky），它是操作模型的全部知识来源。**它的全文已经附在你的输入里**，不用去读。
**便利贴只放两类内容，别的一律不许写（用户 2026-11-05 明确斥责）**：
① key_binding —— 这个游戏的键位表，一行一个，形如 `- Z -> confirm / advance dialogue`；
② game_rules —— 这个游戏的规则与操作要领，一行一条，形如 `- Dialogue advances with Z.`。
不许写：进度、战况、剧情、角色名、观众、你的计划/猜想/备注、时间戳、「我刚发现……」、「Current state:」这类叙述。
那些属于记忆（write_memory）和你说的话，不属于便利贴；写进去会污染操作模型的 system 提示词，
它会把叙述当规则照做。判断标准：这一行是「按什么键」「什么规则」，才留下；否则删掉。
写它的门槛很高（用户 2026-11-04 明确）：**默认不要写**，绝大多数轮次一个字都不该动。
只有这一轮真的试出了以前没有的键位/规则、并且它对下一轮有用，才 write_sticky 一次；内容跟现在一样就一个字都别动。
write_sticky 是**后台任务**（用户 2026-11-05 明确）：调用后立即返回，写盘在工作线程里做。
提交完就立刻继续规划下一步，不要等结果、不要重复提交、不要为了「确认写进去」再调一次。
- 操作模型每一轮能看到的只有三样：游戏名、你写进便利贴的 game_rules、你写进便利贴的 key_binding。你的目标、观众弹幕、记忆，它都看不到。
- 这两块一开始是空的。空着的时候它只能瞎猜或者干脆不按键——教会它这个游戏怎么玩，是你的工作，不是它的。
- 不要用 read_sticky 去「确认现状」——现状就在你输入里。要写就整块重写，key_binding 一行一个：`- Z -> confirm / advance dialogue`。
- 键名必须用它认识的 token：字母大写（Z X C A S D W），方向键 Up/Down/Left/Right，F1 写 One、F2 写 Two，Enter、Esc、Space、Tab、Shift，鼠标 LB/RB/MB。写小写 z 或「确认键」它收不到。
- 规则会变：进菜单、进战斗、换场景、换存档，game_rules 就要跟着改。覆盖不是浪费，覆盖就是学习。
- 「这一轮做什么」只是给程序的任务说明，它不会进操作模型的 prompt。要它做某件事，必须写成 game_rules 里一句可执行的动作。

语言（用户 2026-11-05 明确，操作模型只吃英文）：
- 「## 这一轮做什么」的目标、便利贴的 key_binding 与 game_rules —— **必须全用英文写**。
  中文会直接把操作模型打坏：它会用中文思考，中文 reasoning 里的换行让输出变成非法 JSON，
  整步解析失败（实测 3 次里 2 次）。
- **调用 lumine / run_script 时的参数也必须是英文**：lumine 的 goal、done_when 一律用英文写
  （程序会拦下中文 goal 并要求你改写，白跑一轮）。屏幕上的中文原样保留在「」里没关系。
- 只有「## 就算成了」「## 别做」和你自己的备注可以用中文。

观察与验证：以最新画面和实际执行结果为准，先辨认可见对话文字、菜单、角色和可行走空间；没有对话框不能假定还在对话。操作后重新看画面，按键发送成功不等于目标达成；多次无变化时停止重复，重新判断是否已可探索，不能直接断言输入失效。状态不明或可能切场景时避免长串盲按；可探索的移动交给 lumine。

怎么执行（**先看这一条再选工具**）：
- **能写死成固定流程的，就用 run_script**（用户 2026-11-05 最新口径）：推进对话、过场、确认已知位置的菜单项、
  取消/返回、在字母网格里挪光标——这些不看画面也能确定，交给脚本批处理，几秒钟就能过掉，别让手模型一步步试。
- **只有必须边看边改的，才用 lumine**：走路/找路/探索、靠近 NPC、进出门、战斗、闪避、3D 转视角、
  需要知道「光标现在停在哪」「角色走到哪了」「按完有没有生效」的动作。
- **lumine 的 goal 同样只写「要达成什么」，禁止指导怎么按键**（不写按哪个键、按几次、先上后下）；
  程序会在真正调用前再单开一次思考，把目标重写得更明确并补上完成条件，你写的 goal 只是草稿。
- lumine 回来时会带 script 字段：这一段操作器实际按下的键，已自动压成脚本形式（如 `Down*3,Z`，
  `wait` 表示那一步没按键）。据此判断做到哪一步，不要自己猜它按了什么。
- 连续两次 run_script 之间必须 look 一次（确认上一段脚本真的生效了）。
- 一轮只挑一个通道：要么一次 run_script，要么 lumine 干到底，不要把两者混着塞。
- run_script 只收一句**自然语言要求**（goal），而且写「要做到什么」，**禁止指导怎么按键**
  （用户 2026-11-05 明确）：不要写按哪个键、按几次、先 Down 再 Right 这类操作细节，
  也不要写 DSL / 脚本串 / preset（已全部删除）。按键步骤由程序另发一次思考请求生成，
  你在这里写按键指令只会和生成脚本的那次请求打架。
  好例子：「翻过这段开场旁白」「在字母网格里把名字打完并提交」「把菜单光标移到第二个选项并确认」；
  坏例子：「按 Z 五次」「按 Down 到按钮行再按 Right 两次」——这是程序自己决定的事。
- 便利贴全文已经附在你的输入里（## 你的便利贴），**不要为了看它去调 read_sticky**；
  只有要改它时才写 write_sticky，而且它是异步的：提交即走，别等、别重复提交。

铁律：
- 不要写 JSON，不要写函数调用参数。
- 不要在这里重复键位表：程序会把便利贴原样附给操作模型。
- 记忆就是你的全部已知：不许引入记忆之外的游戏知识（防剧透）。
'''.strip()

SPEAK_POLICY = '''- 说话：持续碎嘴，但剧情演出允许长静默；其余情况静默不超过 5 秒。
- 失败：小失败自嘲，大失败红温；不演「我没有情绪」。
- 遇到不懂的就说不知道，再试。'''.strip()


def _user_content(text: str, frames: Optional[List[str]]) -> Any:
    '''有图就拼多模态 content：一段文字 + 若干张画面；没图就返回纯字符串。

    只认 data URL（`data:image/jpeg;base64,...`）：慢系统看到的必须是**真帧**，
    不接受本地路径（换台机器就失效）也不接受「图在 run/xxx.png」这种需要它自己
    去读文件的写法。
    '''

    images = [str(x).strip() for x in (frames or []) if str(x).strip()]
    images = [x for x in images if x.startswith('data:image/')]
    if not images:
        return text
    content: List[Dict[str, Any]] = [{'type': 'text', 'text': text}]
    for url in images[:MAX_SLOW_FRAMES]:
        content.append({'type': 'image_url', 'image_url': {'url': url}})
    return content


def _sticky_block(sticky: Any) -> str:
    '''把便利贴原样贴进慢系统输入。

    2026-11-04 用户指出「llm 根本没真正读到 sticky」：原来只有模型主动 read_sticky
    才看得到，它经常凭记忆瞎按、反复试。现在每轮都把全文塞进来，省一次工具调用。
    '''

    if not sticky:
        return '（便利贴还是空的：先 write_sticky 教会操作模型这个游戏怎么玩）'
    if isinstance(sticky, str):
        return sticky.strip() or '（空）'
    try:
        kb = str(sticky.get('key_binding') or '').strip()
        gr = str(sticky.get('game_rules') or '').strip()
    except AttributeError:
        return str(sticky)
    if not kb and not gr:
        return '（便利贴还是空的：先 write_sticky 教会操作模型这个游戏怎么玩）'
    parts = []
    if kb:
        parts.append('### key_binding' + chr(10) + kb)
    if gr:
        parts.append('### game_rules' + chr(10) + gr)
    return (chr(10) * 2).join(parts)

def build_slow_messages(
    report_summary: str,
    memory_hits: Optional[List[str]] = None,
    danmaku: Optional[List[str]] = None,
    history: Optional[List[str]] = None,
    constraints: Optional[List[str]] = None,
    blocked_count: int = 0,
    last_plan: Optional[str] = None,
    frames: Optional[List[str]] = None,
    sticky: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    '''拼慢系统输入。空项写「（无）」，不留空槽让模型自己脑补。

    `frames` 是**原始帧**（data:image/... 的 data URL）：慢系统自己看图，
    不经过快系统转述——转述会丢细节，也会多一次串台的调用。
    '''

    def block(title: str, lines: Optional[List[str]]) -> str:
        body = [str(x).strip() for x in (lines or []) if str(x).strip()]
        if not body:
            return '## %s' % title + chr(10) + '（无）'
        return '## %s' % title + chr(10) + chr(10).join('- ' + x for x in body)

    parts = [
    '## 你的便利贴（操作模型现在看到的全文，逐字）',
    _sticky_block(sticky),
    '',
    '## 刚才发生了什么（快系统的见闻总结）',
    (report_summary or '（无）').strip(),
    '',
    block('记忆检索', memory_hits),
    '',
    block('观众在说什么', danmaku),
    '',
    block('最近几轮意图与结果', history),
    '',
    block('全局约束', constraints),
    ]
    if last_plan:
        parts += ['', '## 上一轮的安排（参考，不必照做）', str(last_plan).strip()]
    if blocked_count:
        parts += ['', '## 卡关', '同一个地方已经卡了 %d 次：换思路，别再重复同一套动作。' % int(blocked_count)]
    return [
        {'role': 'system', 'content': SLOW_SYSTEM},
        {'role': 'user', 'content': _user_content(chr(10).join(parts).strip(), frames)},
    ]


_CJK_RE = re.compile('[' + chr(92) + 'u3400-' + chr(92) + 'u4dbf' + chr(92) + 'u4e00-' + chr(92) + 'u9fff]')
FIX_ENGLISH_PROMPT = (
    'Your objective under the heading for this round contains Chinese characters. The hand model '
    'only understands English: a Chinese objective makes it think in Chinese and emit broken JSON. '
    'Rewrite the whole reply now. Keep the same three headings and the same plan, but write the '
    'objective as one to three English sentences (imperative, what to do next). Chinese is allowed '
    'only in the success and avoid sections.'
)


def _has_cjk(text: Any) -> bool:
    '''目标里是否有中文字符（决定要不要让慢系统重写成英文）。'''

    return bool(_CJK_RE.search(str(text or '')))


TRANSLATE_PROMPT = (
    'Translate the user objective into English. Output only the English sentence, no quotes, '
    'no explanation, no Chinese characters except names of on-screen buttons kept in parentheses.'
)


def _cjk_ratio(text: Any) -> float:
    '''中文占非空白字符的比例。

    只用来判断「这句目标是不是用中文写的」：英文句子里引用几个中文 UI 字样
    （比如「完成」按钮）完全正常，不能因为一个汉字就退回重写。
    '''

    body = ''.join(str(text or '').split())
    if not body:
        return 0.0
    return len(_CJK_RE.findall(body)) / float(len(body))


QUOTE_PAIRS = (('「', '」'), ('『', '』'), ('“', '”'), ('"', '"'))


def _strip_quoted(text: Any) -> str:
    '''去掉引号内的片段，只留引号外的正文（引号里是屏幕原文，留中文是可以的）。'''

    body = str(text or '')
    out = []
    i = 0
    while i < len(body):
        ch = body[i]
        hit = None
        for open_ch, close_ch in QUOTE_PAIRS:
            if ch == open_ch:
                hit = close_ch
                break
        if not hit:
            out.append(ch)
            i += 1
            continue
        end = body.find(hit, i + 1)
        if end < 0:
            out.append(ch)
            i += 1
            continue
        out.append(' ')
        i = end + 1
    return ''.join(out)


def _needs_english(text: Any) -> bool:
    '''目标是否必须退回重写成英文：引号外的中文占比 > 15% 或汉字 ≥ 12 个。'''

    body = ''.join(_strip_quoted(text).split())
    if not body:
        return False
    return _cjk_ratio(body) > 0.15 or len(_CJK_RE.findall(body)) >= 12


def thinking_for(blocked_count: int = 0, threshold: int = DEFAULT_BLOCKED_THRESHOLD) -> bool:
    '''慢系统是否开思考模式。

    2026-11-04 曾经拍板「永久打开」，2026-11-05 用户改口：**关掉**——思考链让
    每一轮规划都明显变长（他还要求便利贴写入改成异步）。参数保留是为了不破坏
    既有调用点；返回值恒为 False。
    '''

    # 用户 2026-11-05 再改口：思考要发生在**真正输出动作的那一次请求里**
    # （tui 的 stream_complete 那一腿已开 thinking=True，脚本就在那次回复里出）。
    # 规划腿只负责给一句「这一轮做什么」，不需要再想一遍，白等十几秒。
    return False


class SlowPlanner:
    '''调慢系统并守预算。client 可以是 callable 或带 complete() 的对象。'''

    def __init__(self, client: Any, budget_ms: Optional[int] = DEFAULT_BUDGET_MS,
                 thinking_budget_ms: Optional[int] = THINKING_BUDGET_MS,
                 blocked_threshold: int = DEFAULT_BLOCKED_THRESHOLD) -> None:
        self.client = client
        #: None/0 = 不设截止（用户 2026-11-04：慢系统思考超时设为永久）。
        self.budget_ms = None if budget_ms in (None, 0) else int(budget_ms)
        self.thinking_budget_ms = None if thinking_budget_ms in (None, 0) else int(thinking_budget_ms)
        self.blocked_threshold = int(blocked_threshold)
        self.last_error: Optional[str] = None

    def plan(
        self,
        report_summary: str,
        memory_hits: Optional[List[str]] = None,
        danmaku: Optional[List[str]] = None,
        history: Optional[List[str]] = None,
        constraints: Optional[List[str]] = None,
        blocked_count: int = 0,
        last_plan: Optional[str] = None,
        frames: Optional[List[str]] = None,
        sticky: Any = None,
    ) -> SlowPlan:
        thinking = thinking_for(blocked_count, self.blocked_threshold)
        #: None = 不设截止（用户 2026-11-04：超时设为永久）。0/负数同样当不设。
        budget = self.thinking_budget_ms if thinking else self.budget_ms
        if budget is not None and int(budget) <= 0:
            budget = None
        #: 有帧就发帧，不看客户端脸色——「慢系统自己看图」是需求，不是可选项。
        messages = build_slow_messages(report_summary, memory_hits, danmaku, history, constraints,
                                       blocked_count=blocked_count, last_plan=last_plan,
                                       frames=frames, sticky=sticky)
        started = time.monotonic()
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='slow-planner')
        try:
            future = pool.submit(self._call, messages, thinking)
            try:
                # budget=None 表示永久等（不设截止）。
                timeout = None if budget is None else float(budget) / 1000.0
                text = future.result() if timeout is None else future.result(timeout=timeout)
            except FutureTimeout:
                future.cancel()
                waited = int((time.monotonic() - started) * 1000)
                self.last_error = 'slow_timeout'
                raise SlowPlannerTimeout(waited, budget)
            except Exception as error:  # noqa: BLE001 - 统一成自己的异常类型
                self.last_error = '%s: %s' % (type(error).__name__, error)
                raise SlowPlannerError(self.last_error) from error
        finally:
            pool.shutdown(wait=False)
        plan = parse_slow_plan(text)
        if _needs_english(plan.goal):
            # 用户 2026-11-05：手的输入必须全英文。中文目标会让 4B 用中文思考，
            # 中文 reasoning 里的换行直接产出非法 JSON（实测 3 次里 2 次解析失败）。
            plan = self._rewrite_english(messages, thinking, plan)
        if thinking:
            plan.warnings.append('本轮开了 thinking（永久开启），预算 %s' % ('无上限' if budget is None else '%dms' % budget))
        return plan

    def _rewrite_english(self, messages: List[Dict[str, str]], thinking: bool,
                         plan: 'SlowPlan') -> 'SlowPlan':
        '''目标里带中文就再要一次：只把目标改写成英文，别改策略。

        只重试一次。还不行就沿用原计划（附 warning），因为把整轮丢掉更糟。
        '''

        fix = [dict(item) for item in messages]
        fix.append({'role': 'user', 'content': FIX_ENGLISH_PROMPT})
        try:
            text = self._call(fix, thinking)
        except Exception as error:  # noqa: BLE001 - 重写失败不该毁掉这一轮
            plan.warnings.append('目标含中文，英文重写失败（%s：%s）' % (type(error).__name__, error))
            return plan
        fixed = parse_slow_plan(str(text or ''))
        if not fixed.goal:
            plan.warnings.append('目标含中文，英文重写给了空目标，沿用原目标')
            return plan
        fixed.warnings = list(plan.warnings) + list(fixed.warnings)
        if not _needs_english(fixed.goal):
            fixed.warnings.append('目标已自动改写成英文')
            return fixed
        # 慢系统仍然用中文回：单独要一次翻译。目标是一句话，翻译比「重写整段计划」
        # 稳定得多，也不会动到「就算成了 / 别做」这两段。
        translated = self._translate_goal(fixed.goal or plan.goal)
        if translated:
            fixed.goal = translated
            fixed.warnings.append('目标已自动翻译成英文')
        else:
            fixed.warnings.append('目标仍是中文：手模型可能因此解析失败')
        return fixed

    def _translate_goal(self, goal: str) -> str:
        '''把一句中文目标翻成英文；失败返回空串（不抛，别毁掉这一轮）。'''

        messages = [
            {'role': 'system', 'content': TRANSLATE_PROMPT},
            {'role': 'user', 'content': str(goal or '').strip()},
        ]
        try:
            text = str(self._call(messages, False) or '').strip()
        except Exception:  # noqa: BLE001
            return ''
        text = text.strip().strip('"').strip("'").strip()
        if not text or _needs_english(text):
            return ''
        return text

    def _call(self, messages: List[Dict[str, str]], thinking: bool) -> str:
        client = self.client
        if callable(client) and not hasattr(client, 'complete'):
            return str(client(messages, thinking=thinking) or '')
        try:
            return str(client.complete(messages, thinking=thinking) or '')
        except TypeError:
            return str(client.complete(messages) or '')


class LithosSlowClient:
    '''把 lithos.LithosClient 包成慢系统要的形状：callable(messages, thinking) -> str。'''

    def __init__(self, client: Any = None) -> None:
        if client is None:
            import lithos  # type: ignore - 惰性：没有 key 的环境不该在 import 期就炸

            client = lithos.LithosClient()
        self.client = client
        self.last_error: Optional[str] = None

    def available(self) -> bool:
        config = getattr(self.client, 'config', None)
        fn = getattr(config, 'available', None)
        try:
            return bool(fn()) if callable(fn) else True
        except Exception:  # noqa: BLE001
            return False

    def describe(self) -> Dict[str, Any]:
        fn = getattr(self.client, 'describe', None)
        if callable(fn):
            try:
                return dict(fn())
            except Exception:  # noqa: BLE001
                pass
        return {'available': self.available()}

    def __call__(self, messages: List[Dict[str, str]], thinking: bool = False) -> str:
        # 2026-11-06：规划腿的提示词与原文也进追踪日志（用户要「除图片外的全部提示词」）。
        started = time.monotonic()
        trace = getattr(self, 'trace', None)
        if trace is not None:
            try:
                import trace_log as _trace_log
                trace.event('slow_request', channel='planner',
                            messages=_trace_log.describe_messages(messages),
                            thinking=bool(thinking))
            except Exception:  # noqa: BLE001 - 日志坏了不能拦规划
                pass
        reply = self.client.complete(list(messages), thinking=bool(thinking))
        text = getattr(reply, 'text', reply)
        if trace is not None:
            try:
                trace.event('slow_response', channel='planner', text=text,
                            reasoning=str(getattr(reply, 'reasoning', '') or ''),
                            usage=dict(getattr(reply, 'usage', {}) or {}),
                            total_ms=int((time.monotonic() - started) * 1000))
            except Exception:  # noqa: BLE001
                pass
        text = '' if text is None else str(text).strip()
        if text or not thinking:
            return text
        # deepseek-flash 开思考时经常只回 reasoning_content、content 为空（lithos.complete
        # 里也有同样的注记）。规划腿拿不到正文等于这一轮白跑，所以补一次关思考的调用：
        # 先让它想，想完拿不到答案就闭嘴再答一遍。
        retry = self.client.complete(list(messages), thinking=False)
        again = getattr(retry, 'text', retry)
        return '' if again is None else str(again).strip()


def build_client(client: Any = None, config: Any = None) -> LithosSlowClient:
    '''慢系统客户端：默认 DeepSeek flash（lithos 的 deepseek provider）。没 key 就明确报错。'''

    if client is not None:
        return LithosSlowClient(client)
    try:
        import lithos  # type: ignore
    except Exception as error:  # noqa: BLE001
        raise SlowPlannerError('接不上慢系统模型：%s' % error) from error
    cfg = config or lithos.LithosConfig.from_env()
    if not cfg.available():
        raise SlowPlannerError(
            '慢系统没 key：LITHOS_API_KEY / DEEPSEEK_API_KEY 都没有，DSH 凭据也读不到（不会编一个客户端出来）')
    return LithosSlowClient(lithos.LithosClient(cfg))


def build_slow_planner(client: Any = None, **kwargs: Any) -> SlowPlanner:
    '''client 不给就接 lithos（慢系统模型）。接不上就明确报错，不假装有慢系统。'''

    if client is None:
        client = build_client()
    return SlowPlanner(client, **kwargs)
