# -*- coding: utf-8 -*-
"""把 supplement/gateway_routing.json 写回本机 state.db 的 gateway_routing 表。

用途：重装/新机恢复时，恢复微信/QQ 等平台的"线程 -> 会话"映射，
让各平台旧对话线程能续接到恢复的历史会话。

用法：
    python apply_routing.py                          # 默认本机 HERMES_HOME
    python apply_routing.py --home <HERMES_HOME>     # 指定目标
    python apply_routing.py --repo <备份仓目录>      # 指定 supplement 来源
若写入时报 database is locked：等 Hermes 全退后的空窗期重跑。
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
    a = ap.parse_args()
    repo = Path(a.repo) if a.repo else Path(__file__).resolve().parent.parent
    src = repo / "supplement" / "gateway_routing.json"
    if not src.is_file():
        print(f"未找到 {src}")
        return 1
    data = json.loads(src.read_text(encoding="utf-8"))
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
        added = updated = same = 0
        for r in data.get("rows", []):
            cur = existing.get(r["session_key"])
            if cur is None:
                added += 1
                con.execute(
                    "INSERT INTO gateway_routing(scope, session_key, entry_json, updated_at)"
                    " VALUES (?,?,?,?)",
                    (scope, r["session_key"], r["entry_json"], r["updated_at"]))
            elif cur == (r["entry_json"], r["updated_at"]):
                same += 1
            else:
                updated += 1
                con.execute(
                    "UPDATE gateway_routing SET entry_json=?, updated_at=?"
                    " WHERE scope=? AND session_key=?",
                    (r["entry_json"], r["updated_at"], scope, r["session_key"]))
        con.commit()
    finally:
        con.close()
    print(f"applied: new={added} updated={updated} unchanged={same} -> {db} (scope={scope})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
