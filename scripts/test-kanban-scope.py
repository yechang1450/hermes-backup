"""回归：kanban 备份**只**取 `boards/<board>/kanban.db`，任务工作区里的 db 一律不备。

背景（2026-10-25）：旧实现 `rglob("*.db")` 把工作区/证据/备份目录里的 73 个 `school80.db`
也备进了云（镜像 84 个 db / 36MB），违背"工作区属过程记录"的政策。

判据：
  S1 只在 boards/<board>/ 下的 kanban.db 被复制
  S2 工作区里的同名 db（workspaces/t_x/...) 不被复制
  S3 顶层 kanban.db 若存在则照常备份
  S4 复制出来的是 SQLite 快照（能打开、表结构与行数一致）
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sync  # noqa: E402

fails = 0


def check(name: str, ok: bool, extra: str = "") -> None:
    global fails
    print(("PASS" if ok else "FAIL") + f"  {name}" + (f"  [{extra}]" if extra else ""))
    if not ok:
        fails += 1


def make_db(p: Path, rows: int) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p))
    con.execute("PRAGMA journal_mode=wal")
    con.execute("CREATE TABLE tasks(id INTEGER PRIMARY KEY, title TEXT)")
    con.executemany("INSERT INTO tasks(title) VALUES (?)", [("t%d" % i,) for i in range(rows)])
    con.commit()
    con.close()


def count(p: Path) -> int:
    con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True, timeout=5)
    try:
        return con.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    finally:
        con.close()


def main() -> int:
    base = Path(tempfile.mkdtemp(prefix="hcb-kanban-scope-"))
    src = base / "kanban"
    make_db(src / "boards" / "b1" / "kanban.db", 3)
    make_db(src / "boards" / "b2" / "kanban.db", 5)
    make_db(src / "boards" / "b1" / "workspaces" / "t_x" / "repo" / "data" / "school80.db", 7)
    make_db(src / "boards" / "b2" / "workspaces" / "t_y" / "run" / "proj.db", 9)
    make_db(src / "kanban.db", 1)

    dst = base / "mirror" / "kanban"
    n = sync.copy_kanban_dbs(src, dst)
    got = sorted(p.relative_to(dst).as_posix() for p in dst.rglob("*.db"))
    want = ["boards/b1/kanban.db", "boards/b2/kanban.db", "kanban.db"]

    check("S1/S2/S3 只备 board 库 + 顶层库", got == want, f"got={got}")
    check("S4a 行数一致", all(count(dst / w) == count(src / w) for w in want))
    check("S4b 工作区 db 没被复制", not any("workspaces" in g for g in got), f"{len(got)} 个")
    print(f"  （复制 {n} 个库）")

    shutil.rmtree(base, ignore_errors=True)
    print(f"\n== 4 项，失败 {fails} ==")
    return 0 if fails == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
