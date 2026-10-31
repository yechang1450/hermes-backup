# -*- coding: utf-8 -*-
"""回归：sessions 目录的整档删除必须显式授权；坏 .db 的快照回退不得递归。

背景（2026-10-26 代码审查实测）：
  · `prune_stale` 原实现「keep 里没有的档就 rmtree」——某档导出失败/零产出时，
    整个 `sessions/<档>` 会被清掉，然后照常 commit+push。
  · `snapshot_sqlite` 失败时回退 `copy_file`，而 `copy_file` 对 `.db` 又调 `snapshot_sqlite`，
    两者互递归（喂坏库会递归到深层后返回 False，目标文件完全没写）。

判据：
  S1 keep 为空 + 无授权 ⇒ 什么都不删
  S2 keep 为空 + 授权 gone ⇒ 只删 gone，其余保留
  S3 keep 含 p ⇒ p 目录保留、目录内多余文件被清
  S4 损坏的 .db ⇒ snapshot_sqlite 不递归、能落下文件（True）或明确 False，且耗时 < 2s
  S5 prune_unvisited 的 keep_paths：保留子树原样不动，其余照常收敛
  S6 collect_profiles：活库 0 档时整层保留（2026-10-31 实案：8,284 个 profile 文件被清空）
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sync  # noqa: E402

fails = 0


def check(name: str, ok: bool, extra: str = "") -> None:
    global fails
    print(("PASS" if ok else "FAIL") + f"  {name}" + (f"  [{extra}]" if extra else ""))
    if not ok:
        fails += 1


def build(root: Path) -> None:
    for prof, files in (("p", ["a.jsonl", "b.jsonl"]), ("gone", ["x.jsonl"]), ("q", ["c.jsonl"])):
        d = root / prof
        d.mkdir(parents=True)
        for f in files:
            (d / f).write_text("{}", encoding="utf-8")


def main() -> int:
    base = Path(tempfile.mkdtemp(prefix="hcb-prune-"))

    # S1/S2
    r1 = base / "s1"
    build(r1)
    sync.prune_stale(r1, {}, allow_delete=frozenset())
    left = sorted(d.name for d in r1.iterdir() if d.is_dir())
    check("S1 无授权 ⇒ 一档不删", left == ["gone", "p", "q"], f"剩 {left}")

    r2 = base / "s2"
    build(r2)
    sync.prune_stale(r2, {}, allow_delete={"gone"})
    left2 = sorted(d.name for d in r2.iterdir() if d.is_dir())
    check("S2 仅授权 gone ⇒ 只删 gone", left2 == ["p", "q"], f"剩 {left2}")

    # S3
    r3 = base / "s3"
    build(r3)
    sync.prune_stale(r3, {"p": {"a.jsonl"}}, allow_delete={"gone"})
    inside = sorted(f.name for f in (r3 / "p").iterdir())
    check("S3 keep 内档：保留目录、清多余文件", inside == ["a.jsonl"], f"p/ 剩 {inside}")

    # S4 坏 .db
    bad = base / "bad.db"
    bad.write_bytes(b"not a sqlite file at all" * 100)
    dst = base / "out" / "bad.db"
    t0 = time.time()
    ok = sync.snapshot_sqlite(bad, dst)
    dt = time.time() - t0
    check("S4 坏库不递归、快速返回", dt < 2.0, f"{dt:.2f}s")
    check("S4 坏库回退后目标文件存在", (dst.exists() or not ok), f"ok={ok} exists={dst.exists()}")

    # S5 prune_unvisited(keep_paths=...)：保留子树不被收敛
    r5 = base / "s5"
    (r5 / "a").mkdir(parents=True)
    (r5 / "b").mkdir()
    (r5 / "a" / "1.txt").write_text("x", encoding="utf-8")   # 本轮「访问过」
    (r5 / "a" / "3.txt").write_text("x", encoding="utf-8")   # 未访问且不保
    (r5 / "b" / "2.txt").write_text("x", encoding="utf-8")   # 未访问但在 keep 子树
    old_copied = set(sync._COPIED)
    sync._COPIED.clear()
    sync._COPIED.add(str(r5 / "a" / "1.txt"))
    sync.prune_unvisited(r5, keep_paths=[r5 / "b"])
    check("S5 未访问且在 keep 外 ⇒ 删除", not (r5 / "a" / "3.txt").exists())
    check("S5 keep 子树 ⇒ 原样保留", (r5 / "b" / "2.txt").exists())
    check("S5 本轮访问过 ⇒ 保留", (r5 / "a" / "1.txt").exists())
    sync._COPIED.clear()
    sync._COPIED.update(old_copied)

    # S6 collect_profiles：活库 0 档时不得清空 profiles-overlay
    home6 = base / "home6"
    (home6 / "profiles").mkdir(parents=True)                  # 空 profiles = 新机/全退役
    mirror6 = base / "mirror6"
    for f, body in (("config.yaml", "x: 1"), ("SOUL.md", "soul")):
        d = mirror6 / "profiles-overlay" / "ceo"
        d.mkdir(parents=True, exist_ok=True)
        (d / f).write_text(body, encoding="utf-8")
    old_m, old_h = sync.MIRROR, sync.HERMES_HOME
    sync.MIRROR, sync.HERMES_HOME = mirror6, home6
    sync.collect_profiles({})
    n6 = sum(len(fs) for _, _, fs in os.walk(mirror6 / "profiles-overlay"))
    check("S6 活库零档 ⇒ profiles-overlay 整层保留（2 文件）", n6 == 2, f"{n6} 文件")

    # S6b 活库有档时，退役档仍被 keep_paths 护住、非退役档照常收敛
    (home6 / "profiles" / "live").mkdir()
    (home6 / "profiles" / "live" / "profile.yaml").write_text("name: live", encoding="utf-8")
    sync.collect_profiles({})
    n6b = sum(len(fs) for _, _, fs in os.walk(mirror6 / "profiles-overlay"))
    check("S6b 有活档时退役档仍在（ceo 2 + live 1）", n6b >= 3, f"{n6b} 文件")
    sync.MIRROR, sync.HERMES_HOME = old_m, old_h

    shutil.rmtree(base, ignore_errors=True)
    print(f"\n== {5 + 3 + 2} 项，失败 {fails} ==")
    return 0 if fails == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
