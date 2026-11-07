#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BetterAGI 实时屏幕解说器 (Live Commentator)

使用 MiniCPM-o-4.5-Realtime 大模型实时盯住屏幕/游戏窗口进行连续流式解说。
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import os
from pathlib import Path
import sys
import time

# 强制标准输出为 UTF-8，防止 Windows 终端中文乱码
if sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import minicpm_realtime
import screen_capture

BANNER = r"""
======================================================================
  ____       _   _              _    ____ ___ 
 | __ )  ___| |_| |_ ___ _ __  / \  / ___|_ _|
 |  _ \ / _ \ __| __/ _ \ '__|/ _ \| |  _ | | 
 | |_) |  __/ |_| ||  __/ |  / ___ \ |_| || | 
 |____/ \___|\__|\__\___|_| /_/   \_\____|___|
                                              
  MiniCPM-o-4.5-Realtime 实时盯屏实况解说系统
======================================================================
"""


def parse_args():
    parser = argparse.ArgumentParser(description="MiniCPM-o 4.5 实时屏幕解说")
    parser.add_argument(
        "--window", "-w",
        default="",
        help="目标窗口标题关键词（例如 '原神' 或 'UNDERTALE'）。留空则盯住整个电脑主屏幕。"
    )
    parser.add_argument(
        "--interval", "-i",
        type=float,
        default=2.5,
        help="每轮解说之间的观察停顿时间（秒），默认 2.5 秒"
    )
    parser.add_argument(
        "--key", "-k",
        default="",
        help="ModelBest API Key（默认优先从 keys/minicpm.key 读取）"
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=120,
        help="每轮解说文本最大 token 数，默认 120"
    )
    parser.add_argument(
        "--log-dir",
        default="logs",
        help="解说历史日志输出目录，默认 logs/"
    )
    return parser.parse_args()


async def run_live_loop(args):
    print(BANNER)

    # 1. 初始化日志文件
    log_dir = Path(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"commentary_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    print(f"[*] 解说历史记录将同步写入: {log_file}")

    # 2. 检查窗口目标
    target_hwnd = None
    if args.window:
        print(f"[*] 正在寻找标题包含 '{args.window}' 的目标窗口...")
        target_hwnd = screen_capture.find_window(args.window)
        if target_hwnd:
            print(f"[+] 成功捕获目标窗口 (HWND: {target_hwnd})")
        else:
            print(f"[!] 未找到匹配 '{args.window}' 的可见窗口，将回退为盯着【全屏幕】")
    else:
        print("[*] 正在盯住【电脑主屏幕桌面】...")

    # 3. 初始化 MiniCPM-o 客户端
    client = minicpm_realtime.MiniCPMRealtimeClient(
        api_key=args.key,
        max_new_tokens=args.max_tokens
    )
    
    print("[*] 正在连接 ModelBest Realtime 服务...")
    try:
        await client.connect()
        print(f"[+] 会话连接成功！会话 ID: {client.session_id}")
    except Exception as e:
        print(f"[-] 连接失败: {e}")
        print("[!] 请检查网络连接或 API Key 是否正确。")
        return

    print("\n" + "=" * 50)
    print(" >>> 实时解说已启动！按 Ctrl + C 可随时退出 <<<")
    print("=" * 50 + "\n")

    round_idx = 0
    try:
        while True:
            round_idx += 1
            now_str = datetime.now().strftime("%H:%M:%S")

            # 4. 截帧
            try:
                # 动态刷新一次窗口状态
                if args.window:
                    current_hwnd = screen_capture.find_window(args.window)
                    if current_hwnd:
                        target_hwnd = current_hwnd

                img, jpeg_bytes = screen_capture.capture_screen(hwnd=target_hwnd)
            except Exception as e:
                print(f"[{now_str}] 截帧失败: {e}")
                await asyncio.sleep(args.interval)
                continue

            # 5. 模型解说
            print(f"[{now_str}] [第 {round_idx} 轮] 👁️  主播正在盯屏幕并实时解说:")
            print("  ▶ ", end="", flush=True)

            commentary_text = ""
            start_time = time.monotonic()
            try:
                async for delta in client.generate_commentary_stream(jpeg_bytes):
                    print(delta, end="", flush=True)
                    commentary_text += delta
            except Exception as err:
                print(f"\n[!] 本轮解说异常: {err}")
                await asyncio.sleep(1.0)
                continue

            latency = (time.monotonic() - start_time) * 1000
            print(f"  \n  (耗时 {latency:.0f}ms)")
            print("-" * 50)

            # 6. 同步写入日志
            if commentary_text:
                with open(log_file, "a", encoding="utf-8") as f:
                    f.write(f"[{now_str}] {commentary_text.strip()}\n")

            # 停顿指定间隔
            await asyncio.sleep(args.interval)

    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\n\n[*] 收到退出信号，正在安全关闭会话...")
    finally:
        await client.close()
        print("[*] 解说系统已关闭。感谢使用！")


def main():
    args = parse_args()
    try:
        asyncio.run(run_live_loop(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
