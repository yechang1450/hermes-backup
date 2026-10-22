# -*- coding: utf-8 -*-
"""恢复会话层：把 sessions/<档>/ 的每会话文件（.jsonl / .part-NNNN.jsonl / 旧 .jsonl.gz）恢复进本机 state.db。

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
import sqlite3
import sys
import time
from pathlib import Path

# 只维护 display_order/display_identity 的四个触发器。导入期摘掉它们可消除官方导入路径的
# O(n²) 开销（归档批 UPDATE 逐行触发「重写同会话同展示组全部可见兄弟行」）；导入完成后
# 用官方 SessionDB._ensure_display_order 逐会话重算，再原样重建这四个触发器。
DISPLAY_TRIGGERS = (
    "messages_display_order_insert",
    "messages_display_visibility_update",
    "messages_display_identity_update",
    "messages_display_identity_delete",
)


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


def group_sessions(src: Path) -> dict[str, list[Path]]:
    """按会话分组：<sid>.jsonl（原样单件）/ <sid>.part-NNNN.jsonl（分片）/ <sid>.jsonl.gz（旧整包）。"""
    groups: dict[str, list[Path]] = {}
    for p in src.iterdir():
        if not p.is_file():
            continue
        n = p.name
        if n.endswith(".jsonl.gz"):
            groups.setdefault(n[:-len(".jsonl.gz")], []).append(p)
        elif ".part-" in n and n.endswith(".jsonl"):
            groups.setdefault(n.split(".part-", 1)[0], []).append(p)
        elif n.endswith(".jsonl"):
            groups.setdefault(n[:-len(".jsonl")], []).append(p)
    return groups


def load_session(files: list[Path]) -> dict:
    """单件/分片/旧 gz 皆可：分片按文件名序拼接后整体解析（与原样存储逐字节一致）。"""
    files = sorted(files, key=lambda p: p.name)
    data = b""
    for p in files:
        if p.name.endswith(".gz"):
            with gzip.open(p, "rb") as f:
                data = f.read()          # 旧格式：单文件即整包
        else:
            data += p.read_bytes()
    return json.loads(data)


def _display_triggers_ddl(db_path: Path) -> list[tuple[str, str]]:
    """读取目标库里四个 display 触发器的原文 DDL（逐字保留，重建时原样执行）。"""
    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        q = ("SELECT name, sql FROM sqlite_master WHERE type='trigger' AND name IN (%s)"
             % ",".join("?" * len(DISPLAY_TRIGGERS)))
        rows = conn.execute(q, DISPLAY_TRIGGERS).fetchall()
    finally:
        conn.close()
    return [(n, s) for n, s in rows if s]


def _drop_display_triggers(db_path: Path) -> list[tuple[str, str]]:
    ddl = _display_triggers_ddl(db_path)
    if not ddl:
        return []
    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        for name, _ in ddl:
            conn.execute(f'DROP TRIGGER IF EXISTS "{name}"')
        conn.commit()
    finally:
        conn.close()
    return ddl


def _restore_display_triggers(db_path: Path, ddl: list[tuple[str, str]]) -> None:
    if not ddl:
        return
    conn = sqlite3.connect(str(db_path), timeout=60)
    try:
        for name, sql in ddl:
            conn.execute(f'DROP TRIGGER IF EXISTS "{name}"')
            conn.execute(sql)
        conn.commit()
    finally:
        conn.close()


def _rebuild_display(db, sids: list[str]) -> int:
    """官方 SessionDB._ensure_display_order：仅对缺索引的可见行分批（1000/批）重算并写回。

    与触发器逐行维护的最终不变量一致（display_order=同展示组首个可见行 id、
    display_identity=sha256(展示键)）；官方读路径本身也会自愈，故此处失败不影响正确性。
    """
    fn = getattr(db, "_ensure_display_order", None)
    healed = 0
    for sid in sids:
        try:
            if fn is not None and fn(sid):
                healed += 1
        except Exception as exc:
            print(f"   !! display 重建跳过 {sid}: {exc}")
    return healed


def _fill_invisible_orders(db_path: Path) -> int:
    """不可见行（active=0 且 compacted=0）官方原本由 INSERT 触发器补 order=自身 id；此处一次性补齐。"""
    conn = sqlite3.connect(str(db_path), timeout=60)
    try:
        cur = conn.execute("UPDATE messages SET display_order = id WHERE display_order IS NULL")
        conn.commit()
        return int(cur.rowcount or 0)
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", default=str(default_home()), help="目标 HERMES_HOME")
    ap.add_argument("--profile", default="main", help="main 或档名")
    ap.add_argument("--all", action="store_true", help="恢复 sessions/ 下全部档")
    ap.add_argument("--source", default="", help="会话文件目录（默认用仓库的 sessions/）")
    ap.add_argument("--repo", default="", help="仓库根（默认=本脚本上一级）")
    ap.add_argument("--strict-caps", action="store_true",
                    help="不放宽官方导入尺寸上限（默认放宽；恢复完整历史所需）")
    ap.add_argument("--fast-import", action="store_true",
                    help="导入提速：导入期摘除 display 触发器，导入后官方重算并按原 DDL 重建（大型历史约 3–4 倍速）")
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
        groups = group_sessions(src)
        files = sorted(groups.items(),
                       key=lambda kv: sum(p.stat().st_size for p in kv[1]))  # 小档优先：进度可见、大档最后
        print(f"[{prof}] 目标库: {db_path} | 会话: {len(files)} 个")
        db = SessionDB(db_path)
        suppressed: list[tuple[str, str]] = []
        touched: list[str] = []
        try:
            if args.fast_import:
                suppressed = _drop_display_triggers(db_path)
                print(f"[{prof}] fast-import：摘除 {len(suppressed)} 个 display 触发器"
                      if suppressed else f"[{prof}] fast-import：目标库无 display 触发器，跳过抑制")
            for key, parts in files:
                try:
                    sess = load_session(parts)
                except Exception as exc:
                    total["failed"] += 1
                    print(f"   !! 读取失败 {key}: {exc}")
                    continue
                sid = sess.get("id", key)
                try:
                    res = db.import_sessions([sess])
                except Exception as exc:
                    total["failed"] += 1
                    print(f"   !! {sid} 导入异常: {exc}")
                    continue
                if res.get("ok"):
                    touched.append(sid)
                    if res.get("imported"):
                        total["imported"] += 1
                        print(f"   + {sid}  已导入")
                    else:
                        total["skipped"] += 1
                        print(f"   = {sid}  已存在，跳过")
                else:
                    total["failed"] += 1
                    print(f"   !! {sid} 被拒: {res.get('errors')}")
            if args.fast_import:
                t0 = time.time()
                healed = _rebuild_display(db, touched)
                try:
                    db.close()
                except Exception:
                    pass
                filled = _fill_invisible_orders(db_path)
                _restore_display_triggers(db_path, suppressed)
                print(f"[{prof}] display 重建：{healed} 会话重算 / 不可见行补序 {filled} 行 / "
                      f"触发器恢复 {len(suppressed)} 个 / 用时 {time.time() - t0:.1f}s")
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
