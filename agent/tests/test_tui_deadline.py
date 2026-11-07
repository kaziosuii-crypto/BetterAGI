# -*- coding: utf-8 -*-
'''TUI 的慢系统超预算处理：超时就收手，并且界面上看得见（不是静默截断）。'''
from __future__ import annotations

import tui


class FakeConsole:
    def __init__(self):
        self.notes = []
        self.pieces = []

    def note(self, text):
        self.notes.append(text)

    def reasoning(self, piece):
        self.pieces.append(piece)


def test_reasoning_pieces_are_printed():
    console = FakeConsole()
    aborted = {'hit': False, 'why': ''}
    handler = tui.slow_delta_handler(console, aborted)

    handler('reasoning', '先看左边')

    assert console.pieces == ['先看左边']
    assert aborted['hit'] is False


def test_abort_marks_state_and_warns_once_per_event():
    console = FakeConsole()
    aborted = {'hit': False, 'why': ''}
    handler = tui.slow_delta_handler(console, aborted)

    handler('abort', 'streaming deadline 2.0s exceeded after 2.3s')

    assert aborted['hit'] is True
    assert '2.3s' in aborted['why']
    assert console.notes and '收手' in console.notes[0]


def test_text_deltas_are_ignored_here():
    console = FakeConsole()
    aborted = {'hit': False, 'why': ''}
    handler = tui.slow_delta_handler(console, aborted)

    handler('text', '随便走走')

    assert console.pieces == [] and console.notes == []