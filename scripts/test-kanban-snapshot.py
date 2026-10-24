"""回归：WAL 模式下"直接拷 .db"会丢最近事务，`snapshot_sqlite()` 不会。

判据：
  S1 造一个 WAL 库、写入后**不 checkpoint**（-wal 里有已提交事务）：
     - 直接复制 .db 到别处 → 打开后看不到那条记录（证明风险真实存在）
     - `sync.snapshot_sqlite()` 的快照 → 记录在
  S2 快照后源库继续写入，快照不受影响（隔离）
  S3 目标目录不存在时自动创建
"""
from __future__ import annotations

import os
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


def rows(db: Path) -> list:
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=5)
    try:
        return con.execute("SELECT v FROM t ORDER BY v").fetchall()
    except sqlite3.Error:
        return []          # 普通拷贝甚至可能连表都没有（CREATE 还在 WAL 里）
    finally:
        con.close()


def main() -> int:
    base = Path(tempfile.mkdtemp(prefix="hcb-wal-"))
    src = base / "kanban.db"
    con = sqlite3.connect(str(src))
    con.execute("PRAGMA journal_mode=wal")
    con.execute("CREATE TABLE t(v TEXT)")
    con.execute("INSERT INTO t VALUES ('committed-in-wal')")
    con.commit()
    # 关键：保持连接打开、不 checkpoint，模拟"活库正在被写"
    con.execute("INSERT INTO t VALUES ('second')")
    con.commit()

    naive = base / "naive-copy.db"
    shutil.copy2(src, naive)          # 普通拷贝：-wal 没带上
    ok1 = rows(naive) == []           # 期望：看不到记录（证明风险）
    check("S1a 直接拷 .db 会丢未 checkpoint 的事务", ok1, f"naive={rows(naive)}")

    snap = base / "deep" / "dir" / "snap.db"
    ok2 = sync.snapshot_sqlite(src, snap)
    got = rows(snap) if snap.is_file() else []
    check("S1b snapshot_sqlite 带上 WAL 里的已提交事务", ok2 and got == [("committed-in-wal",), ("second",)],
          f"snap={got}")

    con.execute("INSERT INTO t VALUES ('after-snapshot')")
    con.commit()
    got2 = rows(snap)
    check("S2 快照与源库隔离（后续写入不影响快照）", got2 == [("committed-in-wal",), ("second",)], f"snap={got2}")
    con.close()

    check("S3 目标目录自动创建", snap.is_file(), str(snap.parent))

    shutil.rmtree(base, ignore_errors=True)
    print(f"\n== 3 项，失败 {fails} ==")
    return 0 if fails == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
