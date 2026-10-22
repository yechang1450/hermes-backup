# -*- coding: utf-8 -*-
"""audit-coverage.py — 备份覆盖率审计：镜像（会话导出文件） vs 活库（state.db）逐会话对账。

回答的问题：**镜像里装的是不是活库的全部内容**（不是"能不能恢复"，那是演练的事）。

用法：
    python scripts/audit-coverage.py --repo D:/hermes-backup --home %LOCALAPPDATA%/hermes
    python scripts/audit-coverage.py --repo ... --home ... --profile main --json out.json

判据（退出码）：
    0 = 无硬伤（镜像不缺会话、不多会话、且没有任何会话的镜像条数**多于**活库）
    1 = 有硬伤（缺会话 / 多会话 / 镜像条数多于活库——后者意味着数据错位或库被回滚）
    镜像条数**少于**活库是正常：镜像只是上一轮快照，之后的增量消息还没进镜像（报告里单列）。

注意：只读活库（mode=ro）；解析镜像文件用的是与恢复侧相同的分组规则（单件/分片/旧 gz）。
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


def default_home() -> Path:
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return Path(base) / "hermes"


def group_sessions(src: Path) -> dict[str, list[Path]]:
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


def mirror_counts(src: Path) -> tuple[dict[str, int], list[str]]:
    """返回 ({sid: 消息条数}, [解析失败的 sid])。0 字节文件记为 0 条（空会话是合法导出）。"""
    counts: dict[str, int] = {}
    bad: list[str] = []
    for sid, files in group_sessions(src).items():
        files = sorted(files, key=lambda p: p.name)
        data = b""
        for p in files:
            if p.name.endswith(".gz"):
                with gzip.open(p, "rb") as f:
                    data = f.read()
            else:
                data += p.read_bytes()
        if not data.strip():
            counts[sid] = 0
            continue
        try:
            sess = json.loads(data)
            counts[sid] = len(sess.get("messages") or [])
        except Exception:
            bad.append(sid)
    return counts, bad


def live_counts(db: Path) -> tuple[dict[str, int], int, dict[str, int]]:
    """返回 ({session_id: 消息条数}, 会话总数, {孤立 session_id: 条数})。

    孤立 = messages 里有行、sessions 里没有会话行（导出按会话走，这类内容不进镜像；
    它们不是备份漏洞，是活库自身的历史残留，报告里单列）。
    """
    con = sqlite3.connect("file:%s?mode=ro" % db.as_posix(), uri=True, timeout=10)
    try:
        msg_counts = {sid: n for sid, n in con.execute(
            "SELECT session_id, COUNT(*) FROM messages GROUP BY session_id")}
        session_ids = {sid for (sid,) in con.execute("SELECT id FROM sessions")}
        total_sessions = len(session_ids)
        orphans = {sid: n for sid, n in msg_counts.items() if sid not in session_ids}
        counts = {sid: msg_counts.get(sid, 0) for sid in session_ids}
    finally:
        con.close()
    return counts, total_sessions, orphans


def last_round_ts(repo: Path) -> float | None:
    """镜像 meta/sync-state.json 的**本轮开始时间** → epoch。

    豁免线必须取 started_at（不是 finished_at）：一轮先采集、最后才写 meta，
    采集之后新建的会话本来就不该出现在这一轮的镜像里。
    """
    try:
        meta = json.loads((repo / "meta" / "sync-state.json").read_text(encoding="utf-8"))
        for key in ("started_at", "finished_at"):
            val = meta.get(key)
            if not val:
                continue
            from datetime import datetime as _dt
            return _dt.fromisoformat(val).timestamp()
        return None
    except Exception:
        return None


def session_started(db: Path, sid: str) -> float | None:
    con = sqlite3.connect("file:%s?mode=ro" % db.as_posix(), uri=True, timeout=10)
    try:
        row = con.execute("SELECT started_at FROM sessions WHERE id = ?", (sid,)).fetchone()
        return float(row[0]) if row and row[0] is not None else None
    except Exception:
        return None
    finally:
        con.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, help="镜像仓库根（含 sessions/）")
    ap.add_argument("--home", default=str(default_home()), help="活库所在 HERMES_HOME")
    ap.add_argument("--profile", action="append", default=[], help="只审这些档（默认全部）")
    ap.add_argument("--json", default="", help="把明细写到这个 JSON 文件")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except Exception:
        pass
    repo, home = Path(args.repo), Path(args.home)
    src_root = repo / "sessions"
    if not src_root.is_dir():
        print("镜像里没有 sessions/：%s" % src_root)
        return 1
    profiles = args.profile or sorted(p.name for p in src_root.iterdir() if p.is_dir())

    t0 = time.time()
    report: dict = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "profiles": {}}
    hard: list[str] = []
    grand = {"live_s": 0, "live_m": 0, "mirror_s": 0, "mirror_m": 0, "ahead_msgs": 0, "behind_msgs": 0}

    for prof in profiles:
        db = home / ("state.db" if prof == "main" else f"profiles/{prof}/state.db")
        mdir = src_root / prof
        if not db.is_file():
            print("[%s] 跳过：活库不存在 %s" % (prof, db))
            continue
        lc, l_sessions, orphans = live_counts(db)
        mc, bad = mirror_counts(mdir)
        missing = sorted(set(lc) - set(mc))          # 活库有、镜像没有
        # 豁免：上一轮同步之后新建的会话，镜像里本来就不该有
        cut = last_round_ts(repo)
        missing_new = [] if cut is None else [s for s in missing
                                              if (session_started(db, s) or 0) > cut]
        missing = [s for s in missing if s not in set(missing_new)]   # 剩下的是硬伤
        extra = sorted(set(mc) - set(lc))            # 镜像有、活库没有 → 硬伤（应为 0，prune 会收）
        ahead = {s: mc[s] - lc[s] for s in mc if s in lc and mc[s] > lc[s]}      # 镜像多于活库 → 硬伤
        behind = {s: lc[s] - mc[s] for s in mc if s in lc and lc[s] > mc[s]}    # 镜像落后 → 正常（增量未入库）

        rep = {
            "live_sessions": l_sessions, "live_messages": sum(lc.values()),
            "mirror_sessions": len(mc), "mirror_messages": sum(mc.values()),
            "missing_in_mirror": missing[:20], "missing_count": len(missing),
            "missing_new_after_round": len(missing_new),
            "mirror_extra": extra[:20], "mirror_extra_count": len(extra),
            "mirror_ahead": {k: v for k, v in list(ahead.items())[:10]}, "mirror_ahead_count": len(ahead),
            "mirror_behind_count": len(behind), "mirror_behind_messages": sum(behind.values()),
            "orphan_groups": len(orphans), "orphan_messages": sum(orphans.values()),
            "parse_failed": bad[:10], "parse_failed_count": len(bad),
        }
        report["profiles"][prof] = rep
        grand["live_s"] += rep["live_sessions"]; grand["live_m"] += rep["live_messages"]
        grand["mirror_s"] += rep["mirror_sessions"]; grand["mirror_m"] += rep["mirror_messages"]
        grand["behind_msgs"] += rep["mirror_behind_messages"]

        flag = ""
        if missing or extra or ahead or bad:
            flag = "  ⚠ 硬伤"
            for label, items in (("缺会话", missing), ("多会话", extra)):
                if items:
                    hard.append("%s: %s %d 个（例 %s）" % (prof, label, len(items), items[:3]))
            if ahead:
                hard.append("%s: 镜像条数多于活库 %d 个会话（例 %s）" % (prof, len(ahead), list(ahead)[:3]))
            if bad:
                hard.append("%s: %d 个会话文件解析失败（例 %s）" % (prof, len(bad), bad[:3]))
        print("[%s] 活库 %d 会话/%d 消息 ⇒ 镜像 %d/%d | 缺 %d(新建豁免 %d) 多 %d 超前 %d 落后 %d(%d 条) 孤立 %d(%d 条) 解析失败 %d%s"
              % (prof, rep["live_sessions"], rep["live_messages"], rep["mirror_sessions"], rep["mirror_messages"],
                 rep["missing_count"], rep["missing_new_after_round"], rep["mirror_extra_count"], rep["mirror_ahead_count"],
                 rep["mirror_behind_count"], rep["mirror_behind_messages"],
                 rep["orphan_groups"], rep["orphan_messages"], rep["parse_failed_count"], flag),
              flush=True)

    report["totals"] = grand
    report["hard_findings"] = hard
    print()
    print("== 合计（%.0fs）==" % (time.time() - t0))
    print("活库 %d 会话 / %d 消息 ⇒ 镜像 %d 会话 / %d 消息" % (grand["live_s"], grand["live_m"],
                                                              grand["mirror_s"], grand["mirror_m"]))
    print("镜像落后（上一轮快照之后的增量，正常）：%d 条" % grand["behind_msgs"])
    print("硬伤：%d 项" % len(hard))
    for h in hard[:20]:
        print("  -", h)
    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        print("明细已写：%s" % out)
    print("AUDIT-%s" % ("PASS" if not hard else "FAIL"))
    return 0 if not hard else 1


if __name__ == "__main__":
    sys.exit(main())
