#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用环境变量里的 AutoDL SSH 凭据执行一条远程命令并转发输出。

凭据只从环境变量读取，不写入文件：
  AUTODL_HOST / AUTODL_PORT / AUTODL_USER / AUTODL_PASS
"""

from __future__ import annotations

import os
import sys

import paramiko


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: autodl_run.py <command>", file=sys.stderr)
        return 2
    host = os.environ["AUTODL_HOST"]
    port = int(os.environ.get("AUTODL_PORT", "22"))
    user = os.environ.get("AUTODL_USER", "root")
    password = os.environ["AUTODL_PASS"]
    command = " ".join(sys.argv[1:])

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        host,
        port=port,
        username=user,
        password=password,
        timeout=25,
        banner_timeout=40,
        auth_timeout=40,
        look_for_keys=False,
        allow_agent=False,
    )
    try:
        _stdin, stdout, stderr = client.exec_command(
            command, timeout=None, get_pty=True
        )
        for line in stdout:
            print(line, end="", flush=True)
        for line in stderr:
            print(line, end="", file=sys.stderr, flush=True)
        return stdout.channel.recv_exit_status()
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
