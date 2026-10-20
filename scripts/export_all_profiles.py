# -*- coding: utf-8 -*-
"""export_all_profiles.py — 在官方运行时内一次性导出全部档（sync 提速件，v0.3.5）

替代「每档一次 hermes CLI 启动」的逐个调用：本脚本在单个官方运行时进程里，
对 main 与所有已注册档各做一次与 `hermes sessions export --format jsonl`
完全相同的导出（官方 export_projection / export_all / render_sessions_export），
输出 <out>/<档名>.jsonl（一行 = 一个会话）。由 sync.py 经 run_in_runtime.py 调用。
"""
import argparse
import sys
from pathlib import Path

from hermes_state import SessionDB, resolved_max_export_messages
from hermes_cli.session_export import (SAVE_TRANSCRIPT_FORMATS,
                                       export_projection,
                                       render_sessions_export)


def export_one(name: str, db_path: Path, out_dir: Path) -> tuple[int, int]:
    db = SessionDB(db_path)
    # 复刻官方 `hermes sessions export` 的尺寸守卫（本项目配置 sessions.max_export_messages=0＝关闭）
    limit = resolved_max_export_messages()
    if limit:
        db.assert_exports_safe([s["id"] for s in db.search_sessions(source=None, limit=100000)],
                               max_messages=limit)
    shown = "jsonl" in SAVE_TRANSCRIPT_FORMATS          # 与 CLI _cmd_export 的投影选择完全一致
    projection = export_projection(shown)
    sessions = db.export_all(source=None, **projection)
    content = render_sessions_export(sessions, fmt="jsonl")
    p = out_dir / f"{name}.jsonl"
    with open(p, "w", encoding="utf-8") as fh:          # 与 CLI 相同的换行（Windows=CRLF），保证逐字节一致
        fh.write(content)
    return len(sessions), len(content.encode("utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", required=True, help="HERMES_HOME")
    ap.add_argument("--out", required=True, help="输出目录（每档一个 .jsonl）")
    a = ap.parse_args()
    home = Path(a.home)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    jobs = [("main", home / "state.db")]
    pdir = home / "profiles"
    if pdir.is_dir():
        for d in sorted(pdir.iterdir()):
            if d.is_dir() and not d.name.startswith(".") and (d / "profile.yaml").is_file():
                jobs.append((d.name, d / "state.db"))
    rc = 0
    for name, dbp in jobs:
        try:
            n, sz = export_one(name, dbp, out)
            print(f"[bulk-export] {name}: {n} sessions, {sz / 1048576:.1f}MB", flush=True)
        except Exception as e:
            rc = 1
            print(f"[bulk-export] {name}: ERROR {type(e).__name__}: {e}", flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
