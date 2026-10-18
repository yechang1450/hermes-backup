# -*- coding: utf-8 -*-
"""hb-autosync.py — hermes-cloud-backup 周期同步（四机制之 ③：每 2 小时）。

由 Hermes 官方 cron 调用（--no-agent 纯脚本）。成功＝空输出（静默）；
失败输出摘要（会被 cron 投递）。若距上次实际同步 <20 分钟则静默跳过（去重）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
HOME = Path(os.environ.get("HERMES_HOME") or (LOCALAPPDATA / "hermes"))
SYNC_SCRIPT = HOME / "skills" / "hermes-cloud-backup" / "scripts" / "sync.py"
MIN_SYNC_GAP = 20 * 60
SYNC_TIMEOUT = 3300


def mirror_meta() -> Path:
    base = os.environ.get("HERMES_BACKUP_MIRROR")
    cands = [Path(base)] if base else [Path("D:/hermes-backup"), Path.home() / "hermes-backup"]
    for c in cands:
        if (c / "meta" / "sync-state.json").is_file():
            return c / "meta" / "sync-state.json"
    return cands[0] / "meta" / "sync-state.json"


def git_env() -> dict:
    env = os.environ.copy()
    tools = HOME / "tools"
    if tools.is_dir():
        cands = sorted(p for p in tools.glob("git-*") if (p / "cmd" / "git.exe").is_file())
        if cands:
            env["PATH"] = str(cands[-1] / "cmd") + os.pathsep + env.get("PATH", "")
    return env


def main() -> int:
    meta = mirror_meta()
    try:
        m = json.loads(meta.read_text(encoding="utf-8"))
        fin = m.get("finished_at")
        if fin:
            age = (datetime.now().astimezone() - datetime.fromisoformat(fin)).total_seconds()
            if age < MIN_SYNC_GAP:
                return 0  # 刚同步过，静默跳过
    except Exception:
        pass
    try:
        p = subprocess.run([sys.executable, str(SYNC_SCRIPT)], capture_output=True, text=True,
                           timeout=SYNC_TIMEOUT, env=git_env(), encoding="utf-8", errors="replace")
    except Exception as e:
        print("[hb-autosync] 同步异常：%r" % e, flush=True)
        return 1
    tail = ((p.stdout or "") + (p.stderr or "")).strip()
    if p.returncode == 0 and ("sync 完成" in tail or "已有另一轮同步" in tail):
        return 0
    print("[hb-autosync] 同步失败 rc=%s\n%s" % (p.returncode, tail[-800:]), flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
