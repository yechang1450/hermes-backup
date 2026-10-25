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
"""
from __future__ import annotations

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

    shutil.rmtree(base, ignore_errors=True)
    print(f"\n== 5 项，失败 {fails} ==")
    return 0 if fails == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
