# -*- coding: utf-8 -*-
"""hb-sentinel 分支回归测试：用假 HERMES_HOME + 桩 sync.py，把 ①②④、新鲜度闸、锁跳过、失败上报逐条点一遍。

用法：`python scripts/test-hb-sentinel.py`（纯标准库；不碰真实数据，不跑真同步）
      `HB_TEST_BASE=D:/some/dir python scripts/test-hb-sentinel.py` 可指定工作目录（默认 mkdtemp）。
通过判据：末行 `== 总判定：PASS ==`，退出码 0。

场景：
  S1  冷启动+近期有活动            → 不触发，只记账 gw_start
  S1b 冷启动+库已静默 1 小时       → 触发 quiet-5min（设计行为：本来就该同步）
  S2  网关重启 ①                   → 触发 gateway-restart
  S3  静默 5 分钟 ②                → 触发 quiet-5min
  S4  20 分钟内刚同步过（新鲜度闸） → 不触发
  S5  同步进行中（锁在）           → 不触发
  S6  长静默 ≥1h 后恢复活动 ④      → 触发 resume-after-long-quiet，并复位静默状态
  S7  同步失败                     → rc=1 + 打印摘要（cron 投递路径）+ last_trigger_ok=False
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SENTINEL = SCRIPT_DIR / "hb-sentinel.py"
BASE = Path(os.environ.get("HB_TEST_BASE") or tempfile.mkdtemp(prefix="hb-sentinel-test-"))


def make_home(name: str, *, gw_start="A", msg_age=3600.0, busy=False, fail_stub=False):
    shutil.rmtree(BASE / name, ignore_errors=True)   # 每个场景独立：清掉上一轮残留（含 state 文件）
    home = BASE / name / "home"
    (home / "state").mkdir(parents=True, exist_ok=True)
    (home / "skills" / "hermes-cloud-backup" / "scripts").mkdir(parents=True, exist_ok=True)
    stub = home / "skills" / "hermes-cloud-backup" / "scripts" / "sync.py"
    stub.write_text('import sys\nprint("sync 完成：stub ok")\nsys.exit(%d)\n' % (1 if fail_stub else 0),
                    encoding="utf-8")
    (home / "gateway.pid").write_text(json.dumps({"start_time": gw_start}), encoding="utf-8")
    (home / "gateway_state.json").write_text(json.dumps(
        {"active_agents": 1 if busy else 0, "active_work": ["turn"] if busy else []}), encoding="utf-8")
    (home / "processes.json").write_text("[]", encoding="utf-8")
    db = home / "state.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, timestamp REAL)")
    con.execute("INSERT INTO messages(timestamp) VALUES (?)", (time.time() - msg_age,))
    con.commit()
    con.close()
    mirror = BASE / name / "mirror"
    (mirror / "meta").mkdir(parents=True, exist_ok=True)
    return home, mirror


def set_last_sync(mirror: Path, age_s: float | None):
    p = mirror / "meta" / "sync-state.json"
    if age_s is None:
        p.write_text("{}", encoding="utf-8")
        return
    p.write_text(json.dumps({"finished_at": time.strftime("%Y-%m-%dT%H:%M:%S",
                                                          time.localtime(time.time() - age_s))}),
                 encoding="utf-8")


def run(home: Path, mirror: Path):
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    env["HERMES_BACKUP_MIRROR"] = str(mirror)
    p = subprocess.run([sys.executable, str(SENTINEL)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env, timeout=180)
    st = {}
    sp = home / "state" / "hb-sentinel-state.json"
    if sp.is_file():
        st = json.loads(sp.read_text(encoding="utf-8"))
    child = home / "state" / "hb-sync-child.log"
    spawned = child.is_file() and "哨兵触发" in child.read_text(encoding="utf-8", errors="replace")
    return p, st, spawned


def check(name, cond, detail=""):
    print(("  ✓ " if cond else "  ✗ ") + name + (" | " + detail if detail else ""))
    return bool(cond)


def main() -> int:
    ok_all = True
    print("工作目录：%s" % BASE)
    print("== S1 冷启动（无状态、近期有活动）→ 只记账、不触发 ==")
    h, m = make_home("s1", msg_age=30); set_last_sync(m, None)
    p, st, spawned = run(h, m)
    ok_all &= check("无触发", st.get("last_trigger_tag") is None and not spawned, f"tag={st.get('last_trigger_tag')}")
    ok_all &= check("gw_start 已记账", st.get("gw_start") == "A")

    print("== S1b 冷启动 + 库已静默 1 小时 → 按设计触发 quiet-5min ==")
    h, m = make_home("s1b", msg_age=3600); set_last_sync(m, None)
    p, st, spawned = run(h, m)
    ok_all &= check("触发 quiet-5min", st.get("last_trigger_tag") == "quiet-5min" and spawned,
                    f"tag={st.get('last_trigger_tag')}")

    print("== S2 网关重启 ① ==")
    h, m = make_home("s2", gw_start="B"); set_last_sync(m, 3600)
    (h / "state" / "hb-sentinel-state.json").write_text(json.dumps({"gw_start": "A"}), encoding="utf-8")
    p, st, spawned = run(h, m)
    ok_all &= check("触发 gateway-restart", st.get("last_trigger_tag") == "gateway-restart" and spawned,
                    f"tag={st.get('last_trigger_tag')} ok={st.get('last_trigger_ok')}")

    print("== S3 静默 5 分钟 ② ==")
    h, m = make_home("s3", msg_age=1800); set_last_sync(m, 3600)
    (h / "state" / "hb-sentinel-state.json").write_text(json.dumps({"gw_start": "A"}), encoding="utf-8")
    p, st, spawned = run(h, m)
    ok_all &= check("触发 quiet-5min", st.get("last_trigger_tag") == "quiet-5min" and spawned,
                    f"tag={st.get('last_trigger_tag')} msg_age={st.get('last_msg_age_s')}")

    print("== S4 新鲜度闸（20 分钟内刚同步过）→ 不触发 ==")
    h, m = make_home("s4", msg_age=1800); set_last_sync(m, 300)
    (h / "state" / "hb-sentinel-state.json").write_text(json.dumps({"gw_start": "A"}), encoding="utf-8")
    p, st, spawned = run(h, m)
    ok_all &= check("不触发", st.get("last_trigger_tag") is None and not spawned, f"tag={st.get('last_trigger_tag')}")

    print("== S5 同步进行中（锁在）→ 不触发 ==")
    h, m = make_home("s5", msg_age=1800); set_last_sync(m, 3600)
    (h / "state" / "hb-sentinel-state.json").write_text(json.dumps({"gw_start": "A"}), encoding="utf-8")
    (h / "state" / "hermes-backup-sync.lock").write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
    p, st, spawned = run(h, m)
    ok_all &= check("不触发", st.get("last_trigger_tag") is None and not spawned, f"tag={st.get('last_trigger_tag')}")

    print("== S5b 残留死锁（持有进程已死）→ 不挡哨兵，照常触发 ==")
    h, m = make_home("s5b", msg_age=1800); set_last_sync(m, 3600)
    (h / "state" / "hb-sentinel-state.json").write_text(json.dumps({"gw_start": "A"}), encoding="utf-8")
    (h / "state" / "hermes-backup-sync.lock").write_text(json.dumps({"pid": 999999}), encoding="utf-8")
    p, st, spawned = run(h, m)
    ok_all &= check("死锁不阻塞（触发 quiet-5min）", st.get("last_trigger_tag") == "quiet-5min" and spawned,
                    f"tag={st.get('last_trigger_tag')}")

    print("== S10 从未成功过 + 上次触发已超 2.5h → 补一轮（overdue-no-state）==")
    h, m = make_home("s10", msg_age=60, busy=True); set_last_sync(m, None)
    (h / "state" / "hb-sentinel-state.json").write_text(json.dumps(
        {"gw_start": "A", "quiet_from": time.time() - 60, "synced_this_quiet": True,
         "last_trigger_ts": time.time() - 3 * 3600}), encoding="utf-8")
    p, st, spawned = run(h, m)
    ok_all &= check("触发 overdue-no-state", st.get("last_trigger_tag") == "overdue-no-state" and spawned,
                    f"tag={st.get('last_trigger_tag')}")

    print("== S6 长静默（≥1h）结束、恢复活动 ④ ==")
    h, m = make_home("s6", msg_age=60, busy=True); set_last_sync(m, 3600)
    (h / "state" / "hb-sentinel-state.json").write_text(json.dumps(
        {"gw_start": "A", "quiet_from": time.time() - 7200, "synced_this_quiet": True}), encoding="utf-8")
    p, st, spawned = run(h, m)
    ok_all &= check("触发 resume-after-long-quiet", st.get("last_trigger_tag") == "resume-after-long-quiet" and spawned,
                    f"tag={st.get('last_trigger_tag')}")
    ok_all &= check("静默状态已复位", st.get("quiet_from") is None and st.get("synced_this_quiet") is False)

    print("== S7 同步失败 → rc=1 且打印摘要（cron 投递路径）==")
    h, m = make_home("s7", msg_age=1800, fail_stub=True); set_last_sync(m, 3600)
    (h / "state" / "hb-sentinel-state.json").write_text(json.dumps({"gw_start": "A"}), encoding="utf-8")
    p, st, spawned = run(h, m)
    ok_all &= check("rc=1", p.returncode == 1, f"rc={p.returncode}")
    ok_all &= check("has 失败摘要输出", "!!" in p.stdout and "同步失败" in p.stdout, p.stdout.strip()[:80])
    ok_all &= check("last_trigger_ok=False 且已复位 synced_this_quiet",
                    st.get("last_trigger_ok") is False and st.get("synced_this_quiet") is False)

    print()
    print("== S8 超期兜底 ⑤（>2.5h 无成功轮、当下不忙）→ 触发 overdue ==")
    h, m = make_home("s8", msg_age=60); set_last_sync(m, 3 * 3600)
    (h / "state" / "hb-sentinel-state.json").write_text(
        json.dumps({"gw_start": "A", "synced_this_quiet": True}), encoding="utf-8")
    p, st, spawned = run(h, m)
    ok_all &= check("触发 overdue", st.get("last_trigger_tag") == "overdue" and spawned,
                    f"tag={st.get('last_trigger_tag')}")

    print("== S9 刚同步过（<20 分钟）→ 不触发 ==")
    h, m = make_home("s9", msg_age=60); set_last_sync(m, 600)
    (h / "state" / "hb-sentinel-state.json").write_text(
        json.dumps({"gw_start": "A", "synced_this_quiet": True}), encoding="utf-8")
    p, st, spawned = run(h, m)
    ok_all &= check("不触发", st.get("last_trigger_tag") is None and not spawned,
                    f"tag={st.get('last_trigger_tag')}")

    print("== 总判定：%s ==" % ("PASS" if ok_all else "FAIL"))
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
