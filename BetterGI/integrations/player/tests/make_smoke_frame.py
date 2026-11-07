#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 fake_live_hub 用的最小合法 JPEG 帧（Base64 文本）。

用法::

    python integrations/player/tests/make_smoke_frame.py
"""

from __future__ import annotations

import base64
from pathlib import Path

TARGET = Path(__file__).with_name("_smoke_frame.b64")


def main() -> int:
    try:
        from PIL import Image
    except ImportError:
        print("需要 Pillow：pip install pillow")
        return 2
    import io

    buffer = io.BytesIO()
    Image.new("RGB", (64, 64), (120, 180, 220)).save(buffer, format="JPEG", quality=70)
    TARGET.write_text(base64.b64encode(buffer.getvalue()).decode("ascii"), encoding="ascii")
    print("已写入", TARGET.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
