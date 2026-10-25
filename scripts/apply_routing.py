# -*- coding: utf-8 -*-
"""把 supplement/gateway_routing.json 写回本机 state.db 的 gateway_routing 表。

用途：重装/新机恢复时，恢复微信/QQ 等平台的"线程 -> 会话"映射，
让各平台旧对话线程能续接到恢复的历史会话。

用法：
    python apply_routing.py                          # 默认本机 HERMES_HOME
    python apply_routing.py --dry-run                # 只算 new/updated/unchanged，不写库
    python apply_routing.py --home <HERMES_HOME>     # 指定目标
    python apply_routing.py --repo <备份仓目录>      # 指定 supplement 来源
若写入时报 database is locked：等 Hermes 全退后的空窗期重跑。

安全约定（2026-10-26 加）：
  · `--dry-run` 只读库、只统计，不 INSERT/UPDATE。
  · 全部写入在**一个事务**里，末尾才 commit；中途异常 → 不 commit，SQLite 回滚，不留半截。
  · supplement 文件结构不对时给可读错误，不抛 traceback。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path


def default_home() -> Path:
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    return Path(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")) / "hermes"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", default=str(default_home()), help="目标 HERMES_HOME")
    ap.add_argument("--repo", default="", help="备份仓目录（含 supplement/）")
    ap.add_argument("--dry-run", action="store_true", help="只统计会新增/更新多少行，不写库")
    a = ap.parse_args()

    repo = Path(a.repo) if a.repo else Path(__file__).resolve().parent.parent
    src = repo / "supplement" / "gateway_routing.json"
    if not src.is_file():
        print(f"未找到 {src}")
        return 1
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
        rows = data.get("rows") or []
        for r in rows:
            if not isinstance(r, dict) or not {"session_key", "entry_json", "updated_at"} <= set(r):
                raise ValueError("每行必须含 session_key / entry_json / updated_at")
    except Exception as exc:
        print(f"supplement/gateway_routing.json 结构不对：{exc}")
        return 1

    home = Path(a.home)
    scope = str(home / "sessions")  # 目标机的 scope 恒为本机 HERMES_HOME
    db = home / "state.db"
    if not db.is_file():
        print(f"目标库不存在: {db}")
        return 1

    con = sqlite3.connect(db)
    try:
        existing = {r[0]: (r[1], r[2]) for r in con.execute(
            "SELECT session_key, entry_json, updated_at FROM gateway_routing WHERE scope=?",
            (scope,))}
        added = updated = same = skipped_dup = 0
        seen: set[str] = set()
        for r in rows:
            key = r["session_key"]
            if key in seen:
                skipped_dup += 1          # 同一 session_key 在文件里重复：以先出现的为准
                continue
            seen.add(key)
            cur = existing.get(key)
            if cur is None:
                added += 1
                if not a.dry_run:
                    con.execute(
                        "INSERT INTO gateway_routing(scope, session_key, entry_json, updated_at)"
                        " VALUES (?,?,?,?)"
                        " ON CONFLICT(scope, session_key) DO UPDATE SET"
                        " entry_json=excluded.entry_json, updated_at=excluded.updated_at",
                        (scope, key, r["entry_json"], r["updated_at"]))
            elif cur == (r["entry_json"], r["updated_at"]):
                same += 1
            else:
                updated += 1
                if not a.dry_run:
                    con.execute(
                        "UPDATE gateway_routing SET entry_json=?, updated_at=?"
                        " WHERE scope=? AND session_key=?",
                        (r["entry_json"], r["updated_at"], scope, key))
        if a.dry_run:
            con.rollback()          # 双保险：dry-run 绝不落库
        else:
            con.commit()
    finally:
        con.close()

    tag = "[dry-run] " if a.dry_run else ""
    dup = f" dup_skipped={skipped_dup}" if skipped_dup else ""
    print(f"{tag}applied: new={added} updated={updated} unchanged={same}{dup} -> {db} (scope={scope})")
    if a.dry_run:
        print("   （dry-run 未写库；去掉 --dry-run 才会真写）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
