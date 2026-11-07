# -*- coding: utf-8 -*-
"""GameScaling 训练词表（README §5，74 tokens，case-sensitive）。

这份词表是硬约束：模型输出里出现词表之外的 token 会被直接丢弃（Action.dropped），
一步动作就退化成空动作。历史 bug：我们一直往外送小写的 z / up / enter，这三个都
不在词表里（词表是大写 Z、Up、Enter），于是模型算出来的东西全被扔光，表现为"手永远全零"。

所以这里只做两件事：
* to_token：把 profile 里的任意写法（up / Up / 空格）规整成词表原生 token。
* tokens_for_profile：把 keymap 里的物理键翻成词表 token，给系统提示词拼
  "- <key> -> <effect>" 行用。
"""
from __future__ import annotations

import re
from typing import Dict, Iterable, List, Sequence

#: README §5 的 74 个 token，原样照抄，不要"优化"大小写。
LETTERS = tuple(chr(code) for code in range(ord("A"), ord("Z") + 1))
NUMBERS = ("zero", "one", "two", "three", "four", "five",
           "six", "seven", "eight", "nine")
FUNCTION_KEYS = ("One", "Two", "Three", "Four", "Five", "Six",
                 "Seven", "Eight", "Nine", "Ten", "Eleven", "Twelve")
MOUSE_BUTTONS = ("LB", "RB", "MB")
ARROWS = ("Up", "Down", "Left", "Right")
OTHER = ("Shift", "Ctrl", "Alt", "Tab", "Caps", "Esc", "Space", "Enter",
         "Back", "Delete", "Insert", "Home", "End", "Pause",
         "Equal", "Minus", "Period", "Slash", "Quote")

KEY_VOCAB = frozenset(LETTERS + NUMBERS + FUNCTION_KEYS + MOUSE_BUTTONS + ARROWS + OTHER)

_ALIASES: Dict[str, str] = {
    "up": "Up", "down": "Down", "left": "Left", "right": "Right",
    "arrowup": "Up", "arrowdown": "Down", "arrowleft": "Left", "arrowright": "Right",
    "esc": "Esc", "escape": "Esc",
    "enter": "Enter", "return": "Enter", "submit": "Enter",
    "space": "Space", "spacebar": "Space",
    "backspace": "Back", "back": "Back",
    "shift": "Shift", "ctrl": "Ctrl", "control": "Ctrl", "alt": "Alt",
    "tab": "Tab", "caps": "Caps", "capslock": "Caps",
    "delete": "Delete", "del": "Delete", "insert": "Insert", "ins": "Insert",
    "home": "Home", "end": "End", "pause": "Pause",
    "lb": "LB", "rb": "RB", "mb": "MB",
}
for _index, _word in enumerate(NUMBERS):
    _ALIASES[str(_index)] = _word
for _index, _word in enumerate(FUNCTION_KEYS, start=1):
    _ALIASES["f%d" % _index] = _word


def to_token(name: str) -> str:
    """把任意写法收敛成训练词表里的 token；认不出来就原样（字母则大写化）返回。

    不做"映射到别的键"的猜测：认不出来就交给调用方丢弃，免得静默换键把游戏按坏。
    """
    text = str(name or "").strip()
    if not text:
        return ""
    if text in KEY_VOCAB:
        return text
    lowered = text.lower()
    if lowered in _ALIASES:
        return _ALIASES[lowered]
    if len(text) == 1 and text.isalpha():
        return text.upper()
    return text


def in_vocab(name: str) -> bool:
    return to_token(name) in KEY_VOCAB


def tokens_for_profile(physical: Sequence[str]) -> List[str]:
    """物理键列表 → 词表 token 列表（去重保序）；词表外的直接丢，不猜。"""
    out: List[str] = []
    for key in physical:
        token = to_token(key)
        if token in KEY_VOCAB and token not in out:
            out.append(token)
    return out


def sanitize_steps_report(steps: Iterable[Iterable[str]]) -> tuple[List[List[str]], List[str]]:
    """清洗成词表 token，并把被丢掉的原始写法一起返回。

    为什么要返回 dropped：模型如果输出 `confirm`/`interact` 这种语义名，清洗后整组
    变成空动作，日志里只看到"空动作"，根本分不清是模型不想动还是我们把它扔了。
    README §4 也说得很清楚——服务端会静默丢弃词表外 token，不记录就等于瞎。
    """
    clean: List[List[str]] = []
    dropped: List[str] = []
    for step in steps:
        row: List[str] = []
        for key in step:
            token = to_token(key)
            if token in KEY_VOCAB:
                if token not in row:
                    row.append(token)
            elif str(key).strip() and str(key).strip() not in dropped:
                dropped.append(str(key).strip())
        clean.append(row)
    return clean, dropped


def sanitize_steps(steps: Iterable[Iterable[str]]) -> List[List[str]]:
    """把模型输出（或历史动作）里的键名清洗成词表 token，丢掉词表外的。"""
    return sanitize_steps_report(steps)[0]

#: Playwright / 浏览器键盘库的名字 -> 训练 token（官方 gamescaling/keymaps.py:49 原样）。
#: 我们的 profile 是人写的（对应游戏里显示的键名），跟这份后端命名不是一套；
#: 写成 `ArrowLeft` / `Escape` 送进提示词就是词表外 token。
PLAYWRIGHT_TO_TOKEN: Dict[str, str] = {
    "ArrowLeft": "Left", "ArrowRight": "Right", "ArrowUp": "Up", "ArrowDown": "Down",
    "Escape": "Esc", "CapsLock": "Caps", "Control": "Ctrl", "Backspace": "Back",
}

_DIGIT_TO_WORD = {str(i): word for i, word in enumerate(NUMBERS)}
_FKEY_TO_WORD = {"F%d" % i: word for i, word in enumerate(FUNCTION_KEYS, start=1)}

_PW_KEY_RE = re.compile(r"\b(" + "|".join(re.escape(k) for k in PLAYWRIGHT_TO_TOKEN) + r")\b")
_DIGIT_RANGE_RE = re.compile(r"(?<![A-Za-z0-9])(\d)\s*-\s*(\d)(?=\s*:)")
_LIST_DIGIT_RE = re.compile(r"(?m)^(\s*-\s*)(\d)(?=\s*[:/])")
_COMBO_SLASH_RE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z])\s*/\s*([A-Za-z])(?![A-Za-z0-9])")
_COMBO_PLUS_RE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z])\s*\+\s*([A-Za-z])(?![A-Za-z0-9])")
_LIST_LETTER_RE = re.compile(r"(?m)^(\s*-\s*)([a-z])(?=\s*[:/+])")
_COLON_LETTER_RE = re.compile(r"(?<![A-Za-z0-9])([a-z])(?=\s*:)")


def from_playwright(name: str) -> str:
    '''后端键名 -> 训练 token；认不出来返回空串（调用方决定丢还是留）。'''
    text = str(name or "").strip()
    if not text:
        return ""
    if text in PLAYWRIGHT_TO_TOKEN:
        return PLAYWRIGHT_TO_TOKEN[text]
    if text in _DIGIT_TO_WORD:
        return _DIGIT_TO_WORD[text]
    if text in _FKEY_TO_WORD:
        return _FKEY_TO_WORD[text]
    token = to_token(text)
    return token if token in KEY_VOCAB else ""


#: 训练 token -> ut_harness.KEYMAP 里的物理键名。执行层靠它把模型的动作落到真键盘上。
#: 反过来的方向（物理键名 -> token）在 to_token 里，别混：
#: 提示词要 token，按键要 VK 名，两边大小写规则不同。
TOKEN_TO_KEYMAP: Dict[str, str] = {}
for _letter in LETTERS:
    TOKEN_TO_KEYMAP[_letter] = _letter.lower()
for _index, _word in enumerate(NUMBERS):
    TOKEN_TO_KEYMAP[_word] = str(_index)
for _index, _word in enumerate(FUNCTION_KEYS, start=1):
    TOKEN_TO_KEYMAP[_word] = "f%d" % _index
TOKEN_TO_KEYMAP.update({
    "Up": "up", "Down": "down", "Left": "left", "Right": "right",
    "Esc": "esc", "Space": "space", "Enter": "enter", "Back": "backspace",
    "Shift": "shift", "Ctrl": "ctrl", "Alt": "alt", "Tab": "tab",
    "Caps": "capslock", "Delete": "delete", "Insert": "insert",
    "Home": "home", "End": "end", "Pause": "pause",
    "Equal": "equal", "Minus": "minus", "Period": "period",
    "Slash": "slash", "Quote": "quote",
})


def keymap_name(token: str) -> str:
    '''训练 token -> KEYMAP 物理键名；不是键盘键（LB/RB/MB）或认不出来返回空串。'''
    text = str(token or "").strip()
    if not text:
        return ""
    if text in TOKEN_TO_KEYMAP:
        return TOKEN_TO_KEYMAP[text]
    lowered = text.lower()
    if lowered in KEY_VOCAB:
        return lowered
    if len(text) == 1 and text.isalpha():
        return text.lower()
    return ""


def is_mouse_button(token: str) -> bool:
    '''LB/RB/MB 是鼠标键，走鼠标通道而不是键盘通道。'''
    return str(token or "").strip() in MOUSE_BUTTONS

def translate_controls(text: str) -> str:
    '''把「给浏览器键盘 API 写的」操作说明改写成训练 token 写法。

    官方 prompt.py:113 的 translate_controls 逐条对齐：ArrowLeft -> Left、
    Escape -> Esc……；单个字母只在**当键用**的位置大写（`a/d`、`w + a`、`- r:`、`1-9:`），
    正文里的普通单词不动。

    为什么必须有：profile 的规则是人写的自然语言，里面混着游戏说明书式的键名；
    不翻译就等于把词表外的东西塞进提示词，模型照着抄出来又被服务端丢掉。
    '''
    if not text:
        return ""
    out = _PW_KEY_RE.sub(lambda m: PLAYWRIGHT_TO_TOKEN[m.group(1)], str(text))
    out = _DIGIT_RANGE_RE.sub(
        lambda m: _DIGIT_TO_WORD[m.group(1)] + "-" + _DIGIT_TO_WORD[m.group(2)], out)
    out = _LIST_DIGIT_RE.sub(lambda m: m.group(1) + _DIGIT_TO_WORD[m.group(2)], out)
    out = _COMBO_SLASH_RE.sub(lambda m: m.group(1).upper() + "/" + m.group(2).upper(), out)
    out = _COMBO_PLUS_RE.sub(
        lambda m: m.group(1).upper() + " + " + m.group(2).upper(), out)
    out = _LIST_LETTER_RE.sub(lambda m: m.group(1) + m.group(2).upper(), out)
    out = _COLON_LETTER_RE.sub(lambda m: m.group(1).upper(), out)
    return out


