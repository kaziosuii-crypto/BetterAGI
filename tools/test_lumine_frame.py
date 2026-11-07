#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 AutoDL 本机给 Lumine 玩家服务发一帧，验证真实模型推理。"""

import base64
import json
import os
import sys
import time

from websockets.sync.client import connect

TOKEN = os.environ["LUMINE_TOKEN"]
FRAME = "/root/autodl-tmp/frame.jpg"
URL = f"ws://127.0.0.1:8010/ws/player?token={TOKEN}"

with open(FRAME, "rb") as handle:
    jpeg = base64.b64encode(handle.read()).decode("ascii")

with connect(URL, max_size=16 * 1024 * 1024, open_timeout=15) as ws:
    hello = ws.recv(timeout=15)
    print("hello:", hello[:300])
    for seq in range(1, 4):
        started = time.time()
        ws.send(
            json.dumps(
                {
                    "type": "frame",
                    "seq": seq,
                    "jpeg": jpeg,
                    "guidance": "沿路向前，注意前方",
                }
            )
        )
        deadline = time.time() + 180
        while time.time() < deadline:
            raw = ws.recv(timeout=max(1.0, deadline - time.time()))
            message = json.loads(raw)
            kind = message.get("type")
            if kind in ("action", "error"):
                print(
                    "seq",
                    seq,
                    kind,
                    "serverLatency=",
                    message.get("latencyMs"),
                    "wall=",
                    round(time.time() - started, 2),
                )
                print(json.dumps(message, ensure_ascii=False)[:1200])
                break
        else:
            print("timeout", file=sys.stderr)
            sys.exit(2)

