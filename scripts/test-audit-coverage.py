#!/usr/bin/env python3
"""负向对照测试：证明 audit-coverage.py 不是"永远 PASS"。

用合成数据（假 HERMES_HOME + 假镜像仓库）逐场景断言退出码与输出，完全不碰真库。
场景：全等 / 缺会话 / 多会话 / 镜像超前 / 镜像落后 / 轮内新建豁免 / 孤立消息组 / 解析失败。

用法：python scripts/test-audit-coverage.py [--keep]
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

AUDIT = Path(__file__).resolve().parent / "audit-coverage.py"
BASE = Path(os.environ.get("AC_TEST_BASE") or (Path(os.environ.get("TMP") or ".") / "ac-audit-test"))
HOME = BASE / "home"
REPO = BASE / "repo"
ROUND = 1792688714.0            # 假"本轮开始时间"（2026-10-23 00:56 量级）


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).isoformat()


def make_db(path: Path, sessions: dict[str, float], messages: dict[str, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, started_at REAL, title TEXT)")
    con.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "session_id TEXT, role TEXT, content TEXT)")
    for sid, st in sessions.items():
        con.execute("INSERT INTO sessions VALUES (?,?,?)", (sid, st, "t-" + sid))
    for sid, n in messages.items():
        for i in range(n):
            con.execute("INSERT INTO messages (session_id, role, content) VALUES (?,?,?)",
                        (sid, "user", "m%d" % i))
    con.commit()
    con.close()


def write_session(path: Path, n: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"session_id": path.stem,
                                "messages": [{"role": "user", "content": "x"}] * n},
                               ensure_ascii=False), encoding="utf-8")


def reset(extra_sessions: dict[str, float] | None = None) -> None:
    """重建夹具：main 档 s1/s2/s3 + 孤立组 orph；p1 档 t1；镜像有 s1/s2/p1-t1。"""
    shutil.rmtree(REPO, ignore_errors=True)
    shutil.rmtree(HOME, ignore_errors=True)
    sessions = {"s1": ROUND - 500, "s2": ROUND - 400, "s3": ROUND - 300}
    if extra_sessions:
        sessions.update(extra_sessions)
    make_db(HOME / "state.db", sessions,
            {"s1": 5, "s2": 3, "s3": 2, "orph": 4})
    make_db(HOME / "profiles" / "p1" / "state.db", {"t1": ROUND - 200}, {"t1": 2})
    write_session(REPO / "sessions" / "main" / "s1.jsonl", 5)
    write_session(REPO / "sessions" / "main" / "s2.jsonl", 3)
    write_session(REPO / "sessions" / "main" / "s3.jsonl", 2)
    write_session(REPO / "sessions" / "p1" / "t1.jsonl", 2)
    (REPO / "meta").mkdir(parents=True, exist_ok=True)
    (REPO / "meta" / "sync-state.json").write_text(
        json.dumps({"started_at": iso(ROUND), "finished_at": iso(ROUND + 300)}), encoding="utf-8")


def run() -> tuple[int, str]:
    p = subprocess.run([sys.executable, str(AUDIT), "--repo", str(REPO), "--home", str(HOME)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout or "") + (p.stderr or "")


CASES: list[tuple[str, callable]] = []


def case(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


@case("S1 全等 ⇒ PASS")
def _(out: dict) -> None:
    reset()
    rc, o = run()
    assert rc == 0 and "AUDIT-PASS" in o, "全等应 PASS，实际 rc=%d" % rc
    assert "硬伤：0 项" in o


@case("S2 缺会话 ⇒ FAIL 且点名")
def _(out: dict) -> None:
    reset()
    (REPO / "sessions" / "main" / "s3.jsonl").unlink()      # 镜像少一个会话
    rc, o = run()
    assert rc == 1 and "AUDIT-FAIL" in o, "缺会话应 FAIL，实际 rc=%d" % rc
    assert "缺 1(新建豁免 0)" in o and "s3" in o


@case("S3 轮内新建 ⇒ 豁免 PASS")
def _(out: dict) -> None:
    reset(extra_sessions={"s4": ROUND + 600})               # 本轮开始后新建
    rc, o = run()
    assert rc == 0 and "AUDIT-PASS" in o, "轮内新建应豁免，实际 rc=%d\n%s" % (rc, o[-400:])
    assert "缺 0(新建豁免 1)" in o


@case("S4 镜像多会话 ⇒ PASS + 警告（不算硬伤）")
def _(out: dict) -> None:
    # 口径变更（0.4.48）：活库删掉的会话（子代理临时会话/人工删除/库回滚）在镜像里保留到下一轮 prune，
    # 这属于「备份比活库多」，不是备份缺陷；只作警告，不判 AUDIT-FAIL。
    reset()
    write_session(REPO / "sessions" / "main" / "ghost.jsonl", 1)
    rc, o = run()
    assert rc == 0 and "AUDIT-PASS" in o, "多会话应只警告，实际 rc=%d\n%s" % (rc, o[-400:])
    assert "多 1" in o and "ghost" in o and "警告" in o


@case("S5 镜像落后（增量未入库）⇒ PASS")
def _(out: dict) -> None:
    reset()
    write_session(REPO / "sessions" / "main" / "s1.jsonl", 2)   # 活库 5、镜像 2
    rc, o = run()
    assert rc == 0 and "落后" in o and "(3 条)" in o, "落后应 PASS，实际 rc=%d" % rc


@case("S6 镜像超前（条数多于活库）⇒ FAIL")
def _(out: dict) -> None:
    reset()
    write_session(REPO / "sessions" / "main" / "s1.jsonl", 9)
    rc, o = run()
    assert rc == 1 and "超前 1" in o


@case("S7 孤立消息组 ⇒ 计数但不计硬伤")
def _(out: dict) -> None:
    reset()
    rc, o = run()
    assert rc == 0, "孤立组不该判红"
    assert "孤立 1(4 条)" in o, "孤立组计数不对：%s" % [ln for ln in o.splitlines() if "孤立" in ln]


@case("S8 会话文件坏 JSON ⇒ FAIL")
def _(out: dict) -> None:
    reset()
    (REPO / "sessions" / "main" / "bad.jsonl").write_text('{"messages": [', encoding="utf-8")
    rc, o = run()
    assert rc == 1 and "解析失败" in o


def main() -> int:
    keep = "--keep" in sys.argv
    failed = 0
    for name, fn in CASES:
        try:
            fn({})
            print("PASS  %s" % name)
        except AssertionError as exc:
            failed += 1
            print("FAIL  %s -> %s" % (name, exc))
        except Exception as exc:                       # noqa: BLE001
            failed += 1
            print("ERROR %s -> %r" % (name, exc))
    if not keep:
        shutil.rmtree(BASE, ignore_errors=True)
    print("== %d 场景，失败 %d ==" % (len(CASES), failed))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
