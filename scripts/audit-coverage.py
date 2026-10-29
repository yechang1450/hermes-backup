# -*- coding: utf-8 -*-
"""audit-coverage.py — 备份覆盖率审计：镜像（会话导出文件） vs 活库（state.db）逐会话对账。

回答的问题：**镜像里装的是不是活库的全部内容**（不是"能不能恢复"，那是演练的事）。

用法：
    python scripts/audit-coverage.py --repo ~/hermes-backup --home %LOCALAPPDATA%/hermes
    python scripts/audit-coverage.py --repo ... --home ... --profile main --json out.json

判据（退出码）：
    0 = 无硬伤（镜像不缺会话、不多会话、且没有任何会话的镜像条数**多于**活库）
    1 = 有硬伤（缺会话 / 镜像条数多于活库 / 会话文件解析失败）；「镜像多会话」只作警告（活库删过东西）
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


def file_layer_report(home: Path, repo: Path) -> dict:
    """文件层覆盖：HERMES_HOME 顶层条目 vs 采集白名单（直接读 sync.py 的常量，单一来源）。

    分类：采集（白名单内）/ 排除(运行时)（缓存、二进制、库文件、锁——不该进仓）/ 未分类。
    未分类 = 既不在白名单、也不在已知运行时清单 → **可能的数据漏备**，需人工定性。
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import sync as _sync
        hl_files = set(_sync.HOME_FILES)
        hl_dirs = set(_sync.HOME_DIRS)
        pf = set(_sync.PROFILE_FILES)
        pd = set(_sync.PROFILE_DIRS)
    except Exception as exc:                                   # 拿不到白名单就别装懂
        return {"error": "无法读取 sync.py 白名单：%r" % (exc,)}

    known_runtime = {
        "cache", "logs", "installs", "tools", "hermes-agent", "gateway", "gateway-service",
        "bin", "bootstrap-cache", "checkpoints", "backups", "composer-pastes", "attachments",
        "audio_cache", "image_cache", "images", "desktop-plugins", "state.db", "state.db-wal",
        "state.db-shm", "kanban.db-wal", "kanban.db-shm", "shared-state.db-wal", "shared-state.db-shm",
        "gateway.pid", "gateway.lock", "gateway_state.json", "gateway-starts.log", "processes.json",
        "install_id", "models_dev_cache.json", "models_dev_cache.etag", "provider_models_cache.json",
        "spawn-ledger.json", "shell-hooks-allowlist.json", "hermes-setup.exe", "sessions",
        # 运行时/缓存/空壳目录（不进仓是正确的）
        "locks", "lsp", "pets", "plugins", "runtime", "sandboxes", "source-checks", "telemetry",
        "plugin-update-checks", "state-snapshots", "tui-theme-boot.json", ".scratch_tip_shown",
        ".skills_prompt_snapshot.json",
    }
    # profiles/ 由 collect_profiles() 单独落到 profiles-overlay/，不是漏备
    known_collected = {"profiles": "采集(profiles-overlay)", "sessions": "采集(按会话文件)"}

    def size_of(p: Path) -> str:
        try:
            if p.is_dir():
                n = sum(1 for _ in p.rglob("*") if _.is_file())
                b = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
                return "%d 件/%.1fMB" % (n, b / 1048576)
            return "%.1fMB" % (p.stat().st_size / 1048576)
        except OSError:
            return "?"

    rows, unclassified = [], []
    for p in sorted(home.iterdir(), key=lambda x: x.name.lower()):
        n = p.name
        if p.is_dir() and n in hl_dirs:
            cls = "采集"
        elif p.is_file() and n in hl_files:
            cls = "采集"
        elif n in known_collected:
            cls = known_collected[n]
        elif n == "state.db":
            cls = "排除(会话按导出承载)"
        elif n == "secrets":
            cls = "未分类(凭据)"
        elif any(n.startswith(k) for k in ("state.db", ".hermes-update", "models_dev_cache")) or \
                any(n.endswith(k) for k in (".lock", ".pid", "-wal", "-shm")):
            cls = "排除(运行时)"
        elif n in known_runtime:
            cls = "排除(运行时)"
        else:
            cls = "未分类"
        if cls.startswith("未分类"):
            unclassified.append((n, size_of(p), cls))
        if cls != "排除(运行时)":
            rows.append((n, "目录" if p.is_dir() else "文件", size_of(p), cls))

    mirror_top = sorted(x.name for x in (repo / "home-overlay").iterdir()) if (repo / "home-overlay").is_dir() else []

    def collectible_files(p: Path) -> int:
        """活库侧"真正会被采集"的文件数：排除锁/临时/缓存类，以及 __pycache__ / node_modules。"""
        pats = tuple(_sync.EXCLUDE_PATTERNS)
        skips = set(_sync.EXCLUDE_DIRNAMES)
        n = 0
        for f in p.rglob("*"):
            if not f.is_file():
                continue
            if any(part in skips for part in f.parts):
                continue
            if any(f.match(pat) for pat in pats):
                continue
            n += 1
        return n

    # "白名单内但镜像缺失"只有在该目录确有可采集内容时才算硬伤（git 不跟踪空目录）；
    # 但若这些内容是本轮采集之后才出现的（mtime > 本轮 started_at），下一轮自然会采到，
    # 和会话层的"轮内新建"同理，不算硬伤。
    def newest_mtime(p: Path) -> float:
        pats = tuple(_sync.EXCLUDE_PATTERNS)
        skips = set(_sync.EXCLUDE_DIRNAMES)
        newest = 0.0
        items = p.rglob("*") if p.is_dir() else [p]
        for f in items:
            try:
                if not f.is_file():
                    continue
                if any(part in skips for part in f.parts):
                    continue
                if any(f.match(pat) for pat in pats):
                    continue
                newest = max(newest, f.stat().st_mtime)
            except OSError:
                continue
        return newest

    cut = last_round_ts(repo)
    missing_all = [r[0] for r in rows
                   if r[3] == "采集" and r[0] not in mirror_top and r[0] != "sessions"
                   and collectible_files(home / r[0]) > 0]
    not_in_mirror, fresh_in_round = [], []
    for name in missing_all:
        if cut is not None and newest_mtime(home / name) > cut:
            fresh_in_round.append(name)
        else:
            not_in_mirror.append(name)
    return {"rows": rows, "unclassified": unclassified, "not_in_mirror": not_in_mirror,
            "fresh_in_round": fresh_in_round, "mirror_top_count": len(mirror_top)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, help="镜像仓库根（含 sessions/）")
    ap.add_argument("--home", default=str(default_home()), help="活库所在 HERMES_HOME")
    ap.add_argument("--profile", action="append", default=[], help="只审这些档（默认全部）")
    ap.add_argument("--json", default="", help="把明细写到这个 JSON 文件")
    ap.add_argument("--with-files", action="store_true",
                    help="同时审计文件层：HERMES_HOME 顶层条目 vs 采集白名单（列出未分类/漏备项）")
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
    warns: list[str] = []
    skipped: list[str] = []
    grand = {"live_s": 0, "live_m": 0, "mirror_s": 0, "mirror_m": 0, "ahead_msgs": 0, "behind_msgs": 0}

    for prof in profiles:
        db = home / ("state.db" if prof == "main" else f"profiles/{prof}/state.db")
        mdir = src_root / prof
        if not db.is_file():
            print("[%s] 跳过：活库不存在 %s" % (prof, db))
            skipped.append(prof)
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
        # 「镜像多」不算备份缺陷：活库删掉的会话（子代理临时会话/人工删除/库回滚）在镜像里会保留到下一轮 prune。
        # 2026-10-26 实测：5 个 `Subagent: …` 临时会话被判硬伤 → 每次都 AUDIT-FAIL，把信号淹没了。
        # 真硬伤只留三种：镜像缺会话 / 镜像条数多于活库（错位或回滚）/ 文件解析失败。
        if extra:
            flag = "  ⚠ 多会话"
            warns.append("%s: 镜像多 %d 个会话（活库已无；下一轮 prune 会收，若持续多轮存在请查活库是否回滚）例 %s"
                         % (prof, len(extra), extra[:3]))
        if missing or ahead or bad:
            flag = "  ⚠ 硬伤"
            if missing:
                hard.append("%s: 缺会话 %d 个（例 %s）" % (prof, len(missing), missing[:3]))
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
    if args.with_files:
        fl = file_layer_report(home, repo)
        report["file_layer"] = fl
        print()
        if "error" in fl:
            print("== 文件层审计：跳过（%s）==" % fl["error"])
        else:
            print("== 文件层审计（HERMES_HOME 顶层 vs 采集白名单）==")
            for name, kind, size, cls in fl["rows"]:
                mark = "✔" if cls.startswith("采集") else "⚠"
                print("  %s %-30s %-4s %-14s %s" % (mark, name, kind, size, cls))
            if fl["unclassified"]:
                print("  未分类 %d 项（既不在白名单、也不在已知运行时清单）：%s"
                      % (len(fl["unclassified"]),
                         "、".join("%s(%s,%s)" % (n, s, c) for n, s, c in fl["unclassified"][:10])))
            if fl["not_in_mirror"]:
                hard.append("文件层：白名单内但镜像缺失 %d 项（例 %s）"
                            % (len(fl["not_in_mirror"]), fl["not_in_mirror"][:5]))
                print("  ⚠ 白名单内但镜像缺失：%s" % fl["not_in_mirror"][:10])
            if fl.get("fresh_in_round"):
                print("  · 轮内新建、下一轮会采（不计硬伤）：%s" % fl["fresh_in_round"][:10])

    report["hard_findings"] = hard
    if warns:
        print("警告：%d 项（不算硬伤，附在此处备查）" % len(warns))
        for w in warns[:20]:
            print("  ·", w)
    print()
    print("== 合计（%.0fs）==" % (time.time() - t0))
    print("活库 %d 会话 / %d 消息 ⇒ 镜像 %d 会话 / %d 消息" % (grand["live_s"], grand["live_m"],
                                                              grand["mirror_s"], grand["mirror_m"]))
    print("镜像落后（上一轮快照之后的增量，正常）：%d 条" % grand["behind_msgs"])
    print("硬伤：%d 项" % len(hard))
    for h in hard[:20]:
        print("  -", h)
    report["warnings"] = warns
    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        print("明细已写：%s" % out)
    if skipped:
        # 档名拼错/库缺失时，原来会"跳过并 PASS"——对门禁是假绿（2026-10-26 修）
        print(f"!! {len(skipped)} 个档因活库缺失被跳过：{skipped}（检查 --profile 是否拼错）")
        print("AUDIT-SKIPPED")
        return 2
    print("AUDIT-%s" % ("PASS" if not hard else "FAIL"))
    return 0 if not hard else 1


if __name__ == "__main__":
    sys.exit(main())
