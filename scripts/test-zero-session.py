# -*- coding: utf-8 -*-
"""零产出保护回归：导出"成功"却 0 个会话时，绝不能把该档已有历史 prune 掉。

2026-10-26 实案：16:34 轮 `export main: sessions=0 files=0`（内存不足），
prune_stale 把 sessions/main 的 1,470 个文件整目录删除，且整轮报 exit 0 并推送。
"""
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import sync  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, ok: bool) -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"PASS  {name}")
    else:
        FAIL += 1
        print(f"FAIL  {name}")


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        d = root / "main"
        d.mkdir()
        for i in range(3):
            (d / f"s{i}.jsonl").write_text("{}\n", encoding="utf-8")

        # S1：0 个会话但历史有文件 ⇒ 判失败、旧文件全部保留
        entry = {"ok": True, "sessions": 0, "files": 0}
        keep: dict = {"main": set()}
        hit = sync.guard_zero_sessions("main", entry, keep, root)
        check("S1 触发保护", hit and entry["ok"] is False and entry.get("error") == "zero_sessions")
        check("S1 旧文件全部进 keep（3 个）", keep["main"] == {"s0.jsonl", "s1.jsonl", "s2.jsonl"})

        # S2：0 个会话但该档本来就没有文件 ⇒ 不算失败（合法的空档）
        empty = root / "emptyprof"
        empty.mkdir()
        e2 = {"ok": True, "sessions": 0, "files": 0}
        k2: dict = {"emptyprof": set()}
        hit2 = sync.guard_zero_sessions("emptyprof", e2, k2, root)
        check("S2 空档不误判", (not hit2) and e2["ok"] is True and k2["emptyprof"] == set())

        # S3：正常有产出 ⇒ 不干预
        e3 = {"ok": True, "sessions": 5, "files": 5}
        k3: dict = {"main": {"new1.jsonl", "new2.jsonl"}}
        hit3 = sync.guard_zero_sessions("main", e3, k3, root)
        check("S3 有产出不干预", (not hit3) and e3["ok"] is True and k3["main"] == {"new1.jsonl", "new2.jsonl"})

        # S4：prune 用保护后的 keep 跑一遍，文件必须还在
        sync.prune_stale(root, keep, allow_delete=set())
        left = {f.name for f in d.iterdir() if f.is_file()}
        check("S4 prune 后旧文件仍在", left == {"s0.jsonl", "s1.jsonl", "s2.jsonl"})

    print(f"\n== {PASS + FAIL} 项，失败 {FAIL} ==")
    return 0 if FAIL == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
