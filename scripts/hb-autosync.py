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
CHILD_LOG = HOME / "state" / "hb-sync-child.log"


def run_sync() -> subprocess.CompletedProcess:
    """跑 sync.py，子进程输出**流式落盘**（超时被掐也留有阶段证据）。"""
    CHILD_LOG.parent.mkdir(parents=True, exist_ok=True)
    try:
        if CHILD_LOG.exists() and CHILD_LOG.stat().st_size > 5 * 1024 * 1024:
            CHILD_LOG.write_text("", encoding="utf-8")
    except OSError:
        pass
    with open(CHILD_LOG, "a", encoding="utf-8", errors="replace") as lf:
        lf.write("\n===== %s 开始 =====\n" % datetime.now().astimezone().isoformat(timespec="seconds"))
        lf.flush()
        return subprocess.run([sys.executable, str(SYNC_SCRIPT)], stdout=lf,
                              stderr=subprocess.STDOUT, timeout=SYNC_TIMEOUT, env=git_env(),
                              creationflags=getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0))


def child_log_tail(n: int = 4000) -> str:
    try:
        return CHILD_LOG.read_text(encoding="utf-8", errors="replace")[-n:]
    except OSError:
        return ""


HEALTH = HOME / "state" / "hb-sync-health.json"
MARKER = HOME / "state" / "hb-sync-needs-attention"


def bump_unfinished(start_ts: float, reason: str) -> int:
    """**只在 sync.py 没机会记账时**补记一次失败（超时/被杀/起不来）。

    正常跑完的轮次由 sync.py 自己写（单一来源，哨兵与 cron 皆同）。这里用
    「健康文件的 updated_at 是否晚于本子进程启动时刻」判重，避免重复计数。
    """
    try:
        h = json.loads(HEALTH.read_text(encoding="utf-8"))
        if not isinstance(h, dict):
            h = {}
    except Exception:
        h = {}
    try:
        updated = datetime.fromisoformat(str(h.get("updated_at") or "")).timestamp()
    except Exception:
        updated = 0.0
    if updated >= start_ts:
        return int(h.get("consecutive_failures") or 0)
    n = int(h.get("consecutive_failures") or 0) + 1
    h["consecutive_failures"] = n
    h["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    h["last_exit_code"] = "unfinished"
    h["last_error"] = reason[:200]
    try:
        HEALTH.parent.mkdir(parents=True, exist_ok=True)
        HEALTH.write_text(json.dumps(h, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    try:
        if n >= 3:
            MARKER.write_text("连续 %d 轮同步失败（最后一轮未跑完：%s）；日志：%s\n"
                              % (n, reason[:120], CHILD_LOG), encoding="utf-8")
    except OSError:
        pass
    return n


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
        started = time.time()
        p = run_sync()
    except subprocess.TimeoutExpired:
        bump_unfinished(started, "timeout %ss" % SYNC_TIMEOUT)
        print("[hb-autosync] 同步超时（%ss）——子进程已终止；阶段日志尾：\n%s"
              % (SYNC_TIMEOUT, child_log_tail(1200)), flush=True)
        return 1
    except Exception as e:
        bump_unfinished(started, repr(e))
        print("[hb-autosync] 同步异常：%r" % e, flush=True)
        return 1
    tail = child_log_tail()
    if p.returncode == 0 and ("sync 完成" in tail or "已有另一轮同步" in tail):
        return 0
    print("[hb-autosync] 同步失败 rc=%s\n%s" % (p.returncode, tail[-800:]), flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
