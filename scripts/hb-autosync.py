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
import time
from datetime import datetime
from pathlib import Path

LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
HOME = Path(os.environ.get("HERMES_HOME") or (LOCALAPPDATA / "hermes"))
SYNC_SCRIPT = HOME / "skills" / "hermes-cloud-backup" / "scripts" / "sync.py"
MIN_SYNC_GAP = 20 * 60
SYNC_TIMEOUT = 5400   # 外层兜底：collect+export+commit 最坏 ~1900s，推送预算 1500s
CHILD_LOG = HOME / "state" / "hb-sync-child.log"


def _kill_tree(pid: int) -> int:
    """杀整棵进程树（含孙子）。`subprocess.run` 超时只杀直接子进程，孙子会留成孤儿。"""
    n = 0
    try:
        import psutil
    except Exception:
        return n
    try:
        kids = psutil.Process(pid).children(recursive=True)
    except Exception:
        kids = []
    for c in kids:
        try:
            c.kill()
            n += 1
        except Exception:
            pass
    try:
        psutil.Process(pid).kill()
        n += 1
    except Exception:
        pass
    return n


def run_sync() -> int:
    """跑 sync.py，子进程输出**流式落盘**（超时被掐也留有阶段证据）；超时杀整树。"""
    CHILD_LOG.parent.mkdir(parents=True, exist_ok=True)
    try:
        if CHILD_LOG.exists() and CHILD_LOG.stat().st_size > 5 * 1024 * 1024:
            CHILD_LOG.write_text("", encoding="utf-8")
    except OSError:
        pass
    with open(CHILD_LOG, "a", encoding="utf-8", errors="replace") as lf:
        lf.write("\n===== %s 开始 =====\n" % datetime.now().astimezone().isoformat(timespec="seconds"))
        lf.flush()
        proc = subprocess.Popen([sys.executable, str(SYNC_SCRIPT)], stdout=lf,
                                stderr=subprocess.STDOUT, env=git_env(),
                                creationflags=getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0))
        try:
            return proc.wait(timeout=SYNC_TIMEOUT)
        except subprocess.TimeoutExpired:
            killed = _kill_tree(proc.pid)
            try:
                proc.wait(timeout=30)
            except Exception:
                pass
            lf.write("[hb-autosync] 超时 %ss，已杀进程树 %d 个\n" % (SYNC_TIMEOUT, killed))
            lf.flush()
            raise


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
    # 契约是「每轮一行」：被 SIGKILL 的轮次 sync.py 没机会写，这里补一行（带 note 标明来源）。
    try:
        RUNS = HOME / "state" / "hb-sync-runs.jsonl"
        RUNS.parent.mkdir(parents=True, exist_ok=True)
        end = datetime.now().astimezone()
        row = {
            "start_ts": datetime.fromtimestamp(start_ts).astimezone().isoformat(timespec="seconds"),
            "end_ts": end.isoformat(timespec="seconds"),
            "duration_s": round(end.timestamp() - start_ts, 1),
            "exit_code": "unfinished",
            "note": reason[:200],
            "via": "wrapper",
        }
        with open(RUNS, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return n


def mirror_meta() -> Path:
    base = os.environ.get("HERMES_BACKUP_MIRROR")
    cands = [Path(base)] if base else [(Path.home() / "hermes-backup"), Path.home() / "hermes-backup"]
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
    # 防呆（2026-10-26）：`--help` 落到这里会真跑一轮（实测 rc=1 并尝试真实同步）
    if "-h" in sys.argv[1:] or "--help" in sys.argv[1:]:
        print("用法: python hb-autosync.py\n"
              "  周期同步入口：调用 sync.py 跑一轮，超时杀整树并写健康账本。无参数。")
        return 0
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
    started = time.time()
    try:
        rc = run_sync()
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
    if rc == 0 and ("sync 完成" in tail or "已有另一轮同步" in tail):
        return 0
    # 子进程非零退出（被杀/崩）时 sync.py 可能没机会记账 ⇒ 补一次，避免"这轮没发生"
    bump_unfinished(started, "child rc=%s" % rc)
    print("[hb-autosync] 同步失败 rc=%s\n%s" % (rc, tail[-800:]), flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
