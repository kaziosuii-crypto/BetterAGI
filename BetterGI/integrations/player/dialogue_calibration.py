#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成本机对话标定配置片段；不修改现有配置或上传任何数据。"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path
from typing import Sequence


def _region(values: Sequence[float], name: str) -> dict[str, float]:
    x, y, width, height = values
    if (not all(math.isfinite(value) for value in values) or x < 0 or y < 0
            or width < 0.02 or height < 0.02 or x + width > 1 or y + height > 1):
        raise ValueError(f"{name} 必须是画面内的归一化 X、Y、宽、高，宽高至少 0.02")
    return {"X": x, "Y": y, "Width": width, "Height": height}


def _covers(outer: dict[str, float], inner: dict[str, float]) -> bool:
    return (outer["X"] <= inner["X"] and outer["Y"] <= inner["Y"]
            and outer["X"] + outer["Width"] >= inner["X"] + inner["Width"]
            and outer["Y"] + outer["Height"] >= inner["Y"] + inner["Height"])


def _local_file(value: str, name: str, *, image: bool = False) -> str:
    path = Path(value)
    if not path.is_absolute() or str(path).startswith(("\\\\", "//")):
        raise ValueError(f"{name} 必须是本机绝对文件路径，不能使用网络路径")
    if not path.is_file():
        raise ValueError(f"{name} 必须是已存在的本机文件")
    if image and path.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
        raise ValueError(f"{name} 仅支持 PNG 或 JPEG")
    return str(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="生成独立的 BetterGI 对话标定片段，不直接修改配置；必须自行使用真实画面标定。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--out", required=True, help="新建 JSON 片段路径；不覆盖已有文件")
    parser.add_argument("--database", required=True, help="现有 Player SQLite 数据库的本机绝对路径")
    parser.add_argument("--player-scope", required=True, help="player- 加 64 位小写 SHA-256")
    parser.add_argument("--message-region", type=float, nargs=4, required=True, metavar=("X", "Y", "W", "H"))
    parser.add_argument("--options-region", type=float, nargs=4, required=True, metavar=("X", "Y", "W", "H"))
    parser.add_argument("--candidate-exit-frames", type=int, required=True, choices=range(2, 11))
    parser.add_argument("--allow-select", action="store_true", help="显式授权匹配 Player 决策的选项点击")
    parser.add_argument("--confirm-select", action="store_true", help="确认选项点击授权及区域已由本人标定")
    parser.add_argument("--allow-advance", action="store_true", help="显式启用仅推进模板授权")
    parser.add_argument("--advance-template", default="", help="仅推进 ROI 的本机 PNG/JPEG 绝对路径")
    parser.add_argument("--advance-region", type=float, nargs=4, metavar=("X", "Y", "W", "H"))
    parser.add_argument("--possible-options-region", type=float, nargs=4, metavar=("X", "Y", "W", "H"))
    parser.add_argument("--confirm-advance-only", action="store_true", help="确认模板来自不可选项的仅推进实机画面")
    parser.add_argument("--confirm-options-coverage", action="store_true", help="确认选项区域覆盖所有可能选项")
    parser.add_argument("--enable-exit", action="store_true", help="显式启用返回主界面模板")
    parser.add_argument("--exit-template", default="", help="无对话主界面 ROI 的本机 PNG/JPEG 绝对路径")
    parser.add_argument("--exit-region", type=float, nargs=4, metavar=("X", "Y", "W", "H"),
                        help="静态主界面 HUD 的归一化区域；模板像素尺寸必须与该区域一致")
    parser.add_argument("--possible-dialogue-region", type=float, nargs=4, metavar=("X", "Y", "W", "H"))
    parser.add_argument("--required-frames", type=int, choices=range(2, 11))
    parser.add_argument("--confirm-main-without-dialogue", action="store_true", help="确认模板来自没有对话的实机主界面")
    parser.add_argument("--confirm-dialogue-coverage", action="store_true", help="确认区域覆盖所有可能对话与选项")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        database = _local_file(args.database, "Player 数据库")
        if not re.fullmatch(r"player-[0-9a-f]{64}", args.player_scope):
            raise ValueError("Player scope 必须是 player- 加 64 位小写十六进制")
        message = _region(args.message_region, "对话消息区域")
        options = _region(args.options_region, "对话选项区域")
        if args.allow_select and not args.confirm_select:
            raise ValueError("启用选项点击需要 --confirm-select")
        if args.confirm_select and not args.allow_select:
            raise ValueError("--confirm-select 只能与 --allow-select 一起使用")

        action: dict[str, object] = {"AllowSelect": args.allow_select, "AllowAdvance": False}
        if args.allow_advance:
            if not (args.advance_template and args.advance_region and args.possible_options_region
                    and args.confirm_advance_only and args.confirm_options_coverage):
                raise ValueError("启用推进必须提供模板、两个区域及两项显式确认")
            advance = _region(args.advance_region, "推进模板区域")
            possible_options = _region(args.possible_options_region, "全部潜在选项区域")
            if not (_covers(advance, message) and _covers(advance, options)
                    and _covers(advance, possible_options) and _covers(possible_options, options)):
                raise ValueError("推进模板区域须覆盖消息、选项和全部潜在选项；潜在选项区域须覆盖选项区域")
            action.update({"AllowAdvance": True,
                           "AdvanceTemplatePath": _local_file(args.advance_template, "推进模板", image=True),
                           "AdvanceRegion": advance, "PossibleOptionsRegion": possible_options,
                           "AdvanceTemplateCertified": True, "OptionCoverageConfirmed": True})
        elif any((args.advance_template, args.advance_region, args.possible_options_region,
                  args.confirm_advance_only, args.confirm_options_coverage)):
            raise ValueError("推进模板与确认只能与 --allow-advance 一起使用")

        exit_config: dict[str, object] = {"Enabled": False}
        if args.enable_exit:
            if not (args.exit_template and args.exit_region and args.possible_dialogue_region
                    and args.required_frames and args.confirm_main_without_dialogue
                    and args.confirm_dialogue_coverage):
                raise ValueError("启用退出必须提供模板、两个区域、帧数及两项显式确认")
            exit_region = _region(args.exit_region, "主界面模板区域")
            possible_dialogue = _region(args.possible_dialogue_region, "全部潜在对话区域")
            # 退出模板是独立的静态主界面 HUD 区域，不要求覆盖对话区域；
            # 但潜在对话区域必须覆盖消息与选项，避免标定漏掉可能的对话位置。
            if not (_covers(possible_dialogue, message) and _covers(possible_dialogue, options)):
                raise ValueError("潜在对话区域须覆盖消息和选项")
            exit_config = {"Enabled": True, "TemplatePath": _local_file(args.exit_template, "主界面模板", image=True),
                           "Region": exit_region, "PossibleDialogueRegion": possible_dialogue,
                           "CoverageConfirmed": True, "MainTemplateCertified": True,
                           "RequiredFrames": args.required_frames}
        elif any((args.exit_template, args.exit_region, args.possible_dialogue_region, args.required_frames,
                  args.confirm_main_without_dialogue, args.confirm_dialogue_coverage)):
            raise ValueError("退出模板与确认只能与 --enable-exit 一起使用")

        fragment = {"ExternalAccessConfig": {
            "PlayerEvidence": {"DatabasePath": database, "PlayerScope": args.player_scope},
            "DialogueCandidate": {"Enabled": True, "Message": message, "Options": options,
                                  "ExitFrames": args.candidate_exit_frames},
            "DialogueActionAuthorization": action,
            "DialogueExitAuthorization": exit_config,
        }}
        output = Path(args.out)
        if str(output).startswith(("\\\\", "//")):
            raise ValueError("输出片段必须写入本机路径")
        with output.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(fragment, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
    except (OSError, ValueError) as exc:
        print(f"无法生成标定片段：{exc}", file=sys.stderr)
        return 1
    print(f"已创建独立配置片段：{output}。现有设置未修改；模板准确性仍需用户实机标定。")
    print("请将片段中的指定字段合并到本机配置，保留其他设置；配置更改后需重启宿主。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
