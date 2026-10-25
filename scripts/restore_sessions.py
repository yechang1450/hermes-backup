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

# 导入期摘除的两类"索引维护"触发器：
#  1) display 投影（display_order/display_identity）——归档批 UPDATE 逐行触发「重写同组全部可见兄弟行」＝O(n²)；
#  2) FTS 索引（含 trigram）——逐行 tokenize 巨量正文，且导入路径的剪枝删除还要逐行从索引里摘除。
# 导入完成后：display 用官方 SessionDB._ensure_display_order 重算；FTS 用官方 FTS5 'rebuild' 一次性重建；
# 两类触发器最后都用捕获的原文 DDL 原样恢复。
INDEX_TRIGGERS = (
    "messages_display_order_insert",
    "messages_display_visibility_update",
    "messages_display_identity_update",
    "messages_display_identity_delete",
    "messages_fts_insert",
    "messages_fts_delete",
    "messages_fts_update",
    "messages_fts_trigram_insert",
    "messages_fts_trigram_delete",
    "messages_fts_trigram_update",
)
FTS_TABLES = ("messages_fts", "messages_fts_trigram")


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
    """读取目标库里display/FTS 触发器的原文 DDL（逐字保留，重建时原样执行）。"""
    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        q = ("SELECT name, sql FROM sqlite_master WHERE type='trigger' AND name IN (%s)"
             % ",".join("?" * len(INDEX_TRIGGERS)))
        rows = conn.execute(q, INDEX_TRIGGERS).fetchall()
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


def _check_uid_nulls(db_path: Path) -> tuple[int, int]:
    """兜底自检：0.22.5 起官方新增 `messages_message_uid_insert` 触发器（导入期我们**不摘它**，
    由官方在插入时填 uid）。这里统计 `message_uid` 空值行——空值 >0 说明该触发器改名/失效
    （schema 变动信号），需要核对官方 schema 后再更新 INDEX_TRIGGERS。

    返回 (总行数, 空值行数)；没有该列时返回 (-1, -1)。
    """
    try:
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=30)
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
            if "message_uid" not in cols:
                return -1, -1
            total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            nulls = conn.execute("SELECT COUNT(*) FROM messages WHERE message_uid IS NULL").fetchone()[0]
            return int(total), int(nulls)
        finally:
            conn.close()
    except Exception:
        return -1, -1


def _rebuild_fts(db_path: Path) -> float:
    """用官方 FTS5 'rebuild' 一次性重建外部内容索引（等价于触发器逐行维护的最终状态）。"""
    t0 = time.time()
    conn = sqlite3.connect(str(db_path), timeout=600)
    try:
        for table in FTS_TABLES:
            try:
                conn.execute(f"INSERT INTO {table}({table}) VALUES('rebuild')")
                conn.commit()
            except sqlite3.OperationalError as exc:
                print(f"   !! FTS 重建跳过 {table}: {exc}")
    finally:
        conn.close()
    return time.time() - t0


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
                    help="导入提速：导入期摘除 display 与 FTS 索引触发器，导入后官方重算/重建（大型历史约 2–4 倍速）")
    ap.add_argument("--allow-empty", action="store_true",
                    help="源目录里没有会话文件时也按成功退出（默认判失败）")
    ap.add_argument("--verify", action="store_true",
                    help="导入后逐会话比对消息条数与镜像一致（只比本轮真正导入的会话；不一致则退出码 3）")
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

    total = {"imported": 0, "skipped": 0, "failed": 0, "verify_failed": 0}
    empty_sources: list[str] = []
    for prof in profiles:
        src = src_root / prof
        if not src.is_dir():
            print(f"[{prof}] 源目录不存在: {src}")
            empty_sources.append(prof)
            continue
        db_path = home / ("state.db" if prof == "main" else f"profiles/{prof}/state.db")
        db_path.parent.mkdir(parents=True, exist_ok=True)
        groups = group_sessions(src)
        if not groups:
            # 指错目录时不该"安静成功"：0 个会话等于什么都没恢复（2026-10-26 实测踩到）
            print(f"[{prof}] 源目录里没有会话文件: {src}")
            empty_sources.append(prof)
            continue
        files = sorted(groups.items(),
                       key=lambda kv: sum(p.stat().st_size for p in kv[1]))  # 小档优先：进度可见、大档最后
        print(f"[{prof}] 目标库: {db_path} | 会话: {len(files)} 个")
        db = SessionDB(db_path)
        suppressed: list[tuple[str, str]] = []
        touched: list[str] = []
        src_counts: dict[str, int] = {}
        imported_sids: list[str] = []
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
                src_counts[sid] = len(sess.get("messages") or [])
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
                        imported_sids.append(sid)
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
                fts_s = _rebuild_fts(db_path)
                _restore_display_triggers(db_path, suppressed)
                uid_total, uid_nulls = _check_uid_nulls(db_path)
                uid_note = ("uid 空值 0/%d" % uid_total) if uid_total >= 0 and uid_nulls == 0 else (
                    "uid 空值 %d/%d ⚠" % (uid_nulls, uid_total) if uid_total >= 0 else "uid 检查跳过")
                print(f"[{prof}] 索引重建：display {healed} 会话 / 不可见行补序 {filled} 行 / "
                      f"FTS {fts_s:.1f}s / 触发器恢复 {len(suppressed)} 个 / {uid_note} / 总用时 {time.time() - t0:.1f}s")
                if uid_total >= 0 and uid_nulls > 0:
                    print(f"   !! {prof}: {uid_nulls} 行 message_uid 为空——官方 messages_message_uid_insert "
                          f"触发器可能改名/失效（schema 变动信号），请核对官方 schema 后更新 INDEX_TRIGGERS 自检")
            if args.verify and imported_sids:
                bad: list[tuple[str, int, int]] = []
                try:
                    vcon = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=30)
                    try:
                        for sid in imported_sids:
                            want = src_counts.get(sid)
                            if want is None:
                                continue
                            got = vcon.execute("SELECT COUNT(*) FROM messages WHERE session_id = ?",
                                               (sid,)).fetchone()[0]
                            if got != want:
                                bad.append((sid, want, got))
                    finally:
                        vcon.close()
                except Exception as exc:
                    print(f"   !! [{prof}] 校验读库异常：{exc!r}")
                    total["verify_failed"] += 1
                total["verify_failed"] += len(bad)
                print(f"[{prof}] 校验：{len(imported_sids)} 个导入会话逐条比对，不一致 {len(bad)} 个")
                for sid, want, got in bad[:10]:
                    print(f"   !! {sid}: 镜像 {want} / 恢复 {got}")
        finally:
            try:
                db.close()
            except Exception:
                pass
    print(f"== 完成: imported={total['imported']}  skipped(已存在)={total['skipped']}  "
          f"failed={total['failed']}  verify_failed={total['verify_failed']} ==")
    if empty_sources and not args.allow_empty:
        print(f"!! {len(empty_sources)} 个档没有任何会话文件（{empty_sources}）——"
              f"请检查 --source / --profile 是否指对；确属预期请加 --allow-empty")
        return 2
    return 0 if total["failed"] == 0 and total["verify_failed"] == 0 else 3


if __name__ == "__main__":
    sys.exit(main())
