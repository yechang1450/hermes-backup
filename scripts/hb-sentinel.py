# -*- coding: utf-8 -*-
"""hb-sentinel.py — hermes-backup 自动同步哨兵（watchdog）。

由 Hermes 官方 cron 每 2 分钟调用（--no-agent 纯脚本、不过模型零 token）。
职责 = 四机制之 ①②④：
  ① 网关/Hermes 新启动                    → 触发一次同步
  ② 全部对话静默 ≥5 分钟                  → 触发一次同步（每个静默期只触发一次）
  ④ 长静默（≥1小时）结束、恢复活动的那一刻 → 触发一次同步
  ③（每 2 小时）由另一个 cron 任务 hb-autosync.py 负责，不在本脚本内。

静默判据（全部满足才算静默）：
  · gateway_state.json：active_agents==0 且 active_work 为空
  · processes.json：无活的后台任务
  · 主档＋全部档 state.db：无未过期回合租约、无未完成委派
  · 所有 state.db 的 messages.max(timestamp) 距今 ≥5 分钟

触发约束：距上次实际完成同步须 ≥20 分钟；同步进行中（锁在）直接跳过。
成功＝空输出（静默）；异常/失败输出摘要（会被 cron 投递）。
调试：python hb-sentinel.py --debug （只读判读、不触发、不写状态）
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
HOME = Path(os.environ.get("HERMES_HOME") or (LOCALAPPDATA / "hermes"))
STATE_FILE = HOME / "state" / "hb-sentinel-state.json"
SYNC_LOCK = HOME / "state" / "hermes-backup-sync.lock"
SYNC_SCRIPT = HOME / "skills" / "hermes-backup" / "scripts" / "sync.py"

QUIET_SECONDS = 300          # ② 静默阈值：5 分钟（按全库最后一条消息计时）
LONG_QUIET_SECONDS = 3600    # ④ 长静默阈值：1 小时
OVERDUE_SECONDS = 150 * 60   # ⑤ 超期兜底：>2.5 小时没有成功轮 ⇒ 补一轮（周期 cron 漂移/漏跑时）
MIN_SYNC_GAP = 20 * 60       # 距上次实际同步 ≥20 分钟才允许再触发
SYNC_TIMEOUT = 1800    # 与 hb-autosync 一致（2026-11-01 按本机实测 88–459s 重算，留 4× 余量）
GUARD_EXIT = 6               # sync.py 的 shrink guard：活库比备份少太多 ⇒ 拒绝同步（新机/未恢复）
MASS_DELETE_EXIT = 7         # sync.py 的批量删除闸：按失败处理（不写 24h 静默标记）
GUARD_MARKER = HOME / "state" / "hb-sync-guard-blocked.json"
GUARD_QUIET_S = 24 * 3600    # 被拦下后静默一天：这是配置问题，重试无意义，但不能永远不提

NOTES: list[str] = []


def note(msg: str) -> None:
    NOTES.append("[hb-sentinel %s] %s" % (datetime.now().strftime("%H:%M:%S"), msg))


def guard_marker_age() -> float | None:
    """上次被 shrink guard 拦下距今多少秒（无标记/读不到 ⇒ None）。"""
    try:
        d = json.loads(GUARD_MARKER.read_text(encoding="utf-8"))
        return max(0.0, time.time() - float(d["ts"]))
    except Exception:
        return None


def mark_guard_blocked(reason: str) -> None:
    try:
        GUARD_MARKER.parent.mkdir(parents=True, exist_ok=True)
        GUARD_MARKER.write_text(json.dumps(
            {"ts": time.time(),
             "at": datetime.now().astimezone().isoformat(timespec="seconds"),
             "reason": reason[:500]}, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError:
        pass


def clear_guard_marker() -> None:
    try:
        GUARD_MARKER.unlink(missing_ok=True)
    except OSError:
        pass


def mirror_meta() -> Path:
    base = os.environ.get("HERMES_BACKUP_MIRROR")
    cands = [Path(base)] if base else [(Path.home() / "hermes-backup"), Path.home() / "hermes-backup"]
    for c in cands:
        if (c / "meta" / "sync-state.json").is_file():
            return c / "meta" / "sync-state.json"
    return cands[0] / "meta" / "sync-state.json"


SYNC_STATE = mirror_meta()


def _pid_alive(pid: int) -> bool:
    """进程是否还活着——**不依赖 psutil**。

    为什么必须自带一份（2026-10-31 冷机审计）：原实现是
    `import psutil ... except Exception: return True`，在没装 psutil 的机器上
    （Hermes 自带的 tools python 默认就没有）一律当成「锁还活着」⇒
    崩溃残留的锁会永久静默挡住哨兵，正好是这个函数要修的病。
    Windows 用 OpenProcess+GetExitCodeProcess，POSIX 用 os.kill(pid, 0)。
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            QUERY = 0x1000          # PROCESS_QUERY_LIMITED_INFORMATION
            SYNC = 0x00100000       # SYNCHRONIZE
            STILL_ACTIVE = 259
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            h = k32.OpenProcess(QUERY | SYNC, False, int(pid))
            if not h:
                return False        # 打不开＝进程不存在或无权（无权按死处理，宁可重跑）
            try:
                code = wintypes.DWORD()
                if k32.GetExitCodeProcess(h, ctypes.byref(code)):
                    return code.value == STILL_ACTIVE
                return True
            finally:
                k32.CloseHandle(h)
        except Exception:           # noqa: BLE001
            return True             # 探针本身出错时保守：当作还活着
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def lock_is_live() -> bool:
    """同步锁是否真的有人持有：文件存在但持有进程已死 ⇒ 视为没在跑。

    崩溃残留的锁不该永久挡住哨兵——原实现只看 `SYNC_LOCK.exists()`，
    一旦某轮被杀留下锁，哨兵就再也不会触发，直到人工删锁（2026-10-26 审查发现）。
    """
    try:
        txt = SYNC_LOCK.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    pid = 0
    try:
        pid = int(json.loads(txt)["pid"]) if txt.startswith("{") else int(txt)
    except Exception:
        # 0 字节 / 写到一半的锁解析不出 pid。原来「读不懂就当它在跑」——独装哨兵时会永久卡住，
        # 而且 rc=0 完全看不出问题（2026-10-31 审计发现）。按陈旧锁处理，并在下次失败投递里留痕。
        note("!! 同步锁文件解析不出 pid（%d 字节），按陈旧锁处理：%r" % (len(txt), txt[:40]))
        return False
    if pid <= 0:
        note("!! 同步锁 pid<=0（%r），按陈旧锁处理" % txt[:40])
        return False
    try:
        import psutil
        return bool(psutil.pid_exists(pid))
    except Exception:               # noqa: BLE001  没装 psutil 也要能判活
        return _pid_alive(pid)


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(st: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, STATE_FILE)          # 原子：半截 JSON 会让下次判定全部重来


def git_env() -> dict:
    env = os.environ.copy()
    tools = HOME / "tools"
    if tools.is_dir():
        cands = sorted(p for p in tools.glob("git-*") if (p / "cmd" / "git.exe").is_file())
        if cands:
            env["PATH"] = str(cands[-1] / "cmd") + os.pathsep + env.get("PATH", "")
    # gh：sync.py 的私有性校验要用它；cron 环境的 PATH 往往没有它（换机实测：gh 装了但不在 PATH，
    # 于是每一轮都在「gh 不可用，拒绝推送」上静默失败）。标准安装位探测一次，别写死用户名。
    if not shutil.which("gh", path=env.get("PATH", "")):
        for c in (Path(r"C:\Program Files\GitHub CLI"),
                  Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "GitHub CLI",
                  Path(os.environ.get("LOCALAPPDATA", "")) / r"Programs\GitHub CLI"):
            if (c / "gh.exe").is_file():
                env["PATH"] = str(c) + os.pathsep + env.get("PATH", "")
                break
    return env


def gateway_start() -> object:
    try:
        return json.loads((HOME / "gateway.pid").read_text(encoding="utf-8")).get("start_time")
    except Exception:
        return None


def busy_signals() -> tuple[list, float]:
    """返回 (busy 原因列表, 全库最新消息时间戳)。"""
    b: list = []
    try:
        gs = json.loads((HOME / "gateway_state.json").read_text(encoding="utf-8"))
        if gs.get("active_agents"):
            b.append("active_agents=%s" % gs["active_agents"])
        if gs.get("active_work"):
            b.append("active_work")
    except Exception:
        pass
    try:
        procs = json.loads((HOME / "processes.json").read_text(encoding="utf-8"))
        if isinstance(procs, list) and procs:
            b.append("background_procs=%d" % len(procs))
    except Exception:
        pass
    now = time.time()
    newest = 0.0
    dbs = [HOME / "state.db"] + sorted((HOME / "profiles").glob("*/state.db"))
    for db in dbs:
        if not db.is_file():
            continue
        try:
            con = sqlite3.connect("file:%s?mode=ro" % db.as_posix(), uri=True, timeout=5)
            try:
                try:
                    ts = con.execute("SELECT max(timestamp) FROM messages").fetchone()[0]
                    if ts:
                        newest = max(newest, float(ts))
                except Exception:
                    pass
                try:
                    n = con.execute("SELECT COUNT(*) FROM session_turn_leases WHERE expires_at > ?",
                                    (now,)).fetchone()[0]
                    if n:
                        b.append("turn_leases=%d(%s)" % (n, db.parent.name))
                except Exception:
                    pass
                try:
                    n = con.execute(
                        "SELECT COUNT(*) FROM async_delegations WHERE completed_at IS NULL "
                        "AND lower(COALESCE(state,'')) NOT IN "
                        "('completed','failed','delivered','cancelled','canceled','expired','done')"
                    ).fetchone()[0]
                    if n:
                        b.append("delegations=%d(%s)" % (n, db.parent.name))
                except Exception:
                    pass
            finally:
                con.close()
        except Exception:
            pass
    if newest and now - newest <= QUIET_SECONDS:
        b.append("recent_msg=%.0fs" % (now - newest))
    return b, newest


def last_sync_age() -> float | None:
    try:
        m = json.loads(SYNC_STATE.read_text(encoding="utf-8"))
        fin = m.get("finished_at")
        if fin:
            return time.time() - datetime.fromisoformat(fin).timestamp()
    except Exception:
        pass
    return None


CHILD_LOG = HOME / "state" / "hb-sync-child.log"
HEALTH = HOME / "state" / "hb-sync-health.json"
MARKER = HOME / "state" / "hb-sync-needs-attention"


def bump_unfinished(start_ts: float, reason: str) -> int:
    """**只在 sync.py 没机会记账时**补记一次失败（超时/被杀/起不来）。

    正常跑完的轮次由 sync.py 自己写（单一来源）。用「健康文件 updated_at 是否晚于
    本子进程启动时刻」判重，避免重复计数。
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
    h.update({"consecutive_failures": n,
              "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
              "last_exit_code": "unfinished", "last_error": reason[:200]})
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


def child_log_tail(n: int = 4000) -> str:
    try:
        return CHILD_LOG.read_text(encoding="utf-8", errors="replace")[-n:]
    except OSError:
        return ""


def run_sync(tag: str) -> str:
    """返回 "ok" / "fail" / "guard"（guard=shrink guard 主动拒绝，不是故障）。"""
    note("触发同步：%s" % tag)
    CHILD_LOG.parent.mkdir(parents=True, exist_ok=True)
    try:
        if CHILD_LOG.exists() and CHILD_LOG.stat().st_size > 5 * 1024 * 1024:
            CHILD_LOG.write_text("", encoding="utf-8")
    except OSError:
        pass
    try:
        started = time.time()
        with open(CHILD_LOG, "a", encoding="utf-8", errors="replace") as lf:
            lf.write("\n===== %s 开始（哨兵触发：%s）=====\n"
                     % (datetime.now().astimezone().isoformat(timespec="seconds"), tag))
            lf.flush()
            p = subprocess.run([sys.executable, str(SYNC_SCRIPT)], stdout=lf,
                               stderr=subprocess.STDOUT, timeout=SYNC_TIMEOUT, env=git_env(),
                               creationflags=getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0))
    except subprocess.TimeoutExpired:
        bump_unfinished(started, "timeout %ss" % SYNC_TIMEOUT)
        note("!! 同步超时（%ss）——阶段日志尾：\n%s" % (SYNC_TIMEOUT, child_log_tail(1200)))
        return "fail"
    except Exception as e:
        bump_unfinished(started, repr(e))
        note("!! 同步异常：%r" % e)
        return "fail"
    tail = child_log_tail()
    if p.returncode == 0 and ("sync 完成" in tail or "已有另一轮同步" in tail):
        note("同步完成 ok")
        clear_guard_marker()
        return "ok"
    if p.returncode == GUARD_EXIT:
        why = next((ln.strip() for ln in reversed(tail.splitlines())
                    if "活库" in ln or "shrink guard" in ln), "")
        mark_guard_blocked(why or tail[-500:])
        note("shrink guard 拦下本轮（备份比本机完整得多，拒绝清空备份）：%s\n"
             "   → 先按 RESTORE.md 恢复，或确认要覆盖后加 --allow-shrink；"
             "本提示 %d 小时内不再重复。" % (why or "（原因见子进程日志）", GUARD_QUIET_S // 3600))
        return "guard"
    note("!! 同步失败 rc=%s\n%s" % (p.returncode, tail[-800:]))
    return "fail"


def main() -> int:
    # 防呆（2026-10-26）：`--help` 落到这里会**真跑一轮同步**——实测子代理跑
    # `python hb-sentinel.py --help` 触发了一次真实同步。这里先短路。
    if "-h" in sys.argv[1:] or "--help" in sys.argv[1:]:
        print("用法: python hb-sentinel.py [--debug]\n"
              "  哨兵：判断是否需要补一轮同步；需要就调用 sync.py。\n"
              "  --debug  打印判定过程（默认静默：成功=空输出）")
        return 0
    if "--debug" in sys.argv:
        st = load_state()
        busy, newest = busy_signals()
        age = last_sync_age()
        print("gw_start:", gateway_start(), "| stored:", st.get("gw_start"))
        print("busy:", busy or "（无）",
              "| newest_msg_age:", ("%.0fs" % (time.time() - newest)) if newest else "n/a")
        print("quiet_from:", st.get("quiet_from"), "| synced_this_quiet:", st.get("synced_this_quiet"))
        print("last_sync_age:", ("%.0fs" % age) if age is not None else "n/a",
              "| fresh(20m):", age is not None and age < MIN_SYNC_GAP,
              "| sync_running:", lock_is_live())
        return 0

    ga = guard_marker_age()
    if ga is not None and ga < GUARD_QUIET_S:
        return 0        # 上一轮被 shrink guard 拦下：静默期不重试（否则每 20 分钟撞一次同一堵墙）

    st = load_state()
    now = time.time()
    gw = gateway_start()
    busy, newest = busy_signals()
    sync_running = lock_is_live()
    age = last_sync_age()
    fresh = age is not None and age < MIN_SYNC_GAP
    trigger = ""

    # ① 网关新启动
    if st.get("gw_start") is not None and gw is not None and gw != st.get("gw_start"):
        if not fresh and not sync_running:
            trigger = "gateway-restart"
    st["gw_start"] = gw

    # 静默期跟踪（②与④的共用状态）
    if busy:
        qf = st.get("quiet_from")
        if qf is not None and (now - qf) >= LONG_QUIET_SECONDS and not trigger:
            if not fresh and not sync_running:
                trigger = "resume-after-long-quiet"
        st["quiet_from"] = None
        st["synced_this_quiet"] = False
        st["last_busy_ts"] = now
    else:
        if st.get("quiet_from") is None:
            st["quiet_from"] = now
        qs = newest if newest else (st.get("quiet_from") or now)  # 5 分钟口径=自最后一条消息起算
        if (now - qs) >= QUIET_SECONDS and not st.get("synced_this_quiet") and not trigger:
            if not fresh and not sync_running:
                trigger = "quiet-5min"
                st["synced_this_quiet"] = True

    # ⑤ 超期兜底：周期 cron 因重启/漂移错过时（2026-10-25 实案：重启把 05:57 顺延到 06:29），
    # 只要超过 2.5 小时没有成功轮且当下没在同步，就补一轮。
    if not trigger and not sync_running and not fresh:
        if age is not None and age >= OVERDUE_SECONDS:
            trigger = "overdue"
        elif age is None:
            # 从未成功过（没有任何 sync-state）且上次触发已超过 2.5h ⇒ 说明那轮失败了、状态没落地。
            # 没有这条时：首轮失败后若一直安静，哨兵就再也不重试（2026-10-26 审查发现）。
            last_try = st.get("last_trigger_ts") or 0
            if last_try and (now - last_try) >= OVERDUE_SECONDS:
                trigger = "overdue-no-state"

    ok = True
    outcome = "ok"
    if trigger:
        outcome = run_sync(trigger)
        ok = outcome == "ok"
        st["last_trigger_ts"] = now
        st["last_trigger_tag"] = trigger
        st["last_trigger_ok"] = ok
        if not ok:
            st["synced_this_quiet"] = False   # 失败允许稍后重试（仍受 20 分钟新鲜度闸约束）
    st["updated_at"] = datetime.now().astimezone().isoformat()
    st["last_busy_reasons"] = busy[:6]
    st["last_msg_age_s"] = int(now - newest) if newest else None
    save_state(st)

    if outcome == "guard":
        for line in NOTES:
            print(line, flush=True)
        return 0        # 主动拒绝不算失败：不写失败计数、不让 cron 记红
    if trigger and not ok:
        for line in NOTES:
            print(line, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
