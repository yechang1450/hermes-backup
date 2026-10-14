# -*- coding: utf-8 -*-
"""恢复会话层：把 sessions/<档>/*.jsonl.gz 恢复进本机 state.db。

走官方 `SessionDB.import_sessions()` 管线（与桌面端"导入"按钮同一条代码路径）。
官方对单次导入有尺寸上限（≤500 会话 / 单会话 ≤1 万条、≤5MB / 单次 ≤5 万条、≤25MB），
该上限服务于 Web 请求场景；本工具在**本进程内**放宽这些常量（不改官方文件），
以便完整恢复历史（含超大档 QQ/微信会话）。已存在的会话自动跳过（幂等，可重复跑）。

用法（在本仓 clone 目录内）：
    python tools/restore_sessions.py                      # 恢复 main 档
    python tools/restore_sessions.py --profile sslb-coordinator
    python tools/restore_sessions.py --all                # main + sessions/ 全部档
    python tools/restore_sessions.py --source <目录> --profile main
    python tools/restore_sessions.py --home D:\\some\\hermes-home   # 指定目标 HERMES_HOME
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
from pathlib import Path


def default_home() -> Path:
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return Path(base) / "hermes"


def find_agent_src(home: Path) -> Path:
    for cand in (home / "hermes-agent", home.parent / "hermes-agent",
                 Path(os.environ.get("LOCALAPPDATA", "")) / "hermes" / "hermes-agent"):
        if (cand / "hermes_state.py").is_file():
            return cand
    raise SystemExit(f"找不到 hermes-agent 源码目录（尝试过 {home}、{home.parent} 下）")


def load_session(path: Path) -> dict:
    if path.name.endswith(".gz"):
        with gzip.open(path, "rb") as f:
            data = f.read()
    else:
        data = path.read_bytes()
    return json.loads(data)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", default=str(default_home()), help="目标 HERMES_HOME")
    ap.add_argument("--profile", default="main", help="main 或档名")
    ap.add_argument("--all", action="store_true", help="恢复 sessions/ 下全部档")
    ap.add_argument("--source", default="", help="会话文件目录（默认用仓库的 sessions/）")
    ap.add_argument("--repo", default="", help="仓库根（默认=本脚本上一级）")
    ap.add_argument("--strict-caps", action="store_true",
                    help="不放宽官方导入尺寸上限（默认放宽；恢复完整历史所需）")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)  # noqa: 实时进度
    except Exception:
        pass
    home = Path(args.home)
    repo = Path(args.repo) if args.repo else Path(__file__).resolve().parent.parent
    src_root = Path(args.source) if args.source else repo / "sessions"
    if not src_root.is_dir():
        print(f"源目录不存在: {src_root}")
        return 1

    if args.all:
        profiles = sorted(p.name for p in src_root.iterdir() if p.is_dir())
    else:
        profiles = [args.profile]

    agent_src = find_agent_src(home)
    sys.path.insert(0, str(agent_src))
    from hermes_state import SessionDB  # noqa: E402

    if not args.strict_caps:
        relaxed = []
        for attr in ("_IMPORT_MAX_SESSIONS", "_IMPORT_MAX_MESSAGES_PER_SESSION",
                     "_IMPORT_MAX_TOTAL_MESSAGES", "_IMPORT_MAX_SESSION_BYTES",
                     "_IMPORT_MAX_TOTAL_BYTES"):
            if hasattr(SessionDB, attr):
                setattr(SessionDB, attr, 1 << 60)
                relaxed.append(attr)
        print(f"已放宽导入上限（进程内）: {len(relaxed)} 项")

    total = {"imported": 0, "skipped": 0, "failed": 0}
    for prof in profiles:
        src = src_root / prof
        if not src.is_dir():
            print(f"[{prof}] 源目录不存在: {src}")
            continue
        db_path = home / ("state.db" if prof == "main" else f"profiles/{prof}/state.db")
        db_path.parent.mkdir(parents=True, exist_ok=True)
        files = sorted((p for p in src.iterdir()
                        if p.name.endswith((".jsonl.gz", ".jsonl"))),
                       key=lambda p: p.stat().st_size)  # 小档优先：进度可见、大档最后
        print(f"[{prof}] 目标库: {db_path} | 会话文件: {len(files)} 个")
        db = SessionDB(db_path)
        try:
            for p in files:
                try:
                    sess = load_session(p)
                except Exception as exc:
                    total["failed"] += 1
                    print(f"   !! 读取失败 {p.name}: {exc}")
                    continue
                sid = sess.get("id", p.name)
                try:
                    res = db.import_sessions([sess])
                except Exception as exc:
                    total["failed"] += 1
                    print(f"   !! {sid} 导入异常: {exc}")
                    continue
                if res.get("ok"):
                    if res.get("imported"):
                        total["imported"] += 1
                        print(f"   + {sid}  已导入")
                    else:
                        total["skipped"] += 1
                        print(f"   = {sid}  已存在，跳过")
                else:
                    total["failed"] += 1
                    print(f"   !! {sid} 被拒: {res.get('errors')}")
        finally:
            try:
                db.close()
            except Exception:
                pass
    print(f"== 完成: imported={total['imported']}  skipped(已存在)={total['skipped']}  "
          f"failed={total['failed']} ==")
    return 0 if total["failed"] == 0 else 3


if __name__ == "__main__":
    sys.exit(main())
