#!/usr/bin/env python3
"""Kernel writer lock prevents two independent players using one home."""
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from player_instance import InstanceLockError, PlayerInstanceLock  # noqa: E402


def test_lock_refuses_second_process_and_releases(tmp_path):
    lockfile = tmp_path / "player.lock"
    code = ("import sys; from pathlib import Path; "
            "sys.path.insert(0,sys.argv[1]); "
            "from player_instance import PlayerInstanceLock; "
            "lock=PlayerInstanceLock(Path(sys.argv[2])); "
            "lock.__enter__(); lock.__exit__(None,None,None)")
    args = [sys.executable, "-c", code, str(Path(__file__).resolve().parents[1]), str(lockfile)]
    with PlayerInstanceLock(lockfile):
        blocked = subprocess.run(args, capture_output=True, timeout=10)
        assert blocked.returncode != 0
        with pytest.raises(InstanceLockError):
            with PlayerInstanceLock(lockfile):
                pass
    assert subprocess.run(args, capture_output=True, timeout=10).returncode == 0
