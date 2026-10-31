# -*- coding: utf-8 -*-
"""恢复文件层：把备份仓的 home-overlay/ 与 profiles-overlay/ 覆盖到本机 HERMES_HOME。

覆盖前先把将被覆盖的旧文件备份到 <repo>/restore-out/pre-restore-backup/（可 --no-backup 关闭）。
纯标准库，任意 python 均可运行。

用法:
    python restore_files.py --repo ~/hermes-backup --dry-run          # 先看会写哪些，不动任何东西
    python restore_files.py --repo ~/hermes-backup --home <HERMES_HOME>
    python restore_files.py --repo ~/hermes-backup --what profiles --no-backup

安全约定（2026-10-26 加）：
  · `--dry-run` 只列计划：不建目录、不写文件、不做备份。
  · 目标 HERMES_HOME 不存在时默认**拒绝**（新机请先启动一次 Hermes，让它建骨架）；确需新建加 `--create`。
  · 目标目录存在但不像 Hermes home（无 hermes-agent/、config.yaml、memories）→ 打警告但仍继续。
  · 只覆盖、不删除：本地有而备份没有的文件一律保留。
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

HERMES_MARKERS = ("hermes-agent", "config.yaml", "memories", "profiles")


def default_home() -> Path:
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    return Path(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")) / "hermes"


def overlay_dir(src: Path, dst: Path, backup_root: Path | None,
                dry_run: bool = False, show: int = 10) -> tuple[int, int, int, list[str]]:
    """把 src 树覆盖到 dst；返回 (覆盖文件数, 备份旧文件数, 失败数, 样例路径)。

    dry_run=True 时只统计、不写盘（也不建目录、不备份）。
    """
    n = b = fails = 0
    sample: list[str] = []
    for root, dirs, files in os.walk(src):
        rel = Path(root).relative_to(src)
        if not dry_run:
            (dst / rel).mkdir(parents=True, exist_ok=True)
        for f in files:
            s = Path(root) / f
            d = dst / rel / f
            if dry_run:
                n += 1
                if len(sample) < show:
                    sample.append(str(rel / f))
                continue
            try:
                if backup_root is not None and d.is_file():
                    bdst = backup_root / rel / f
                    bdst.parent.mkdir(parents=True, exist_ok=True)
                    if not bdst.exists():
                        shutil.copy2(d, bdst)
                        b += 1
                shutil.copy2(s, d)
                n += 1
            except OSError as exc:
                fails += 1
                print(f"   !! 覆盖失败 {rel / f}: {exc}")
    return n, b, fails, sample


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, help="备份仓 clone 目录")
    ap.add_argument("--home", default=str(default_home()), help="目标 HERMES_HOME")
    ap.add_argument("--what", default="home,profiles", help="home,profiles（逗号分隔）")
    ap.add_argument("--no-backup", action="store_true", help="不备份被覆盖的旧文件")
    ap.add_argument("--dry-run", action="store_true", help="只列出将要覆盖的文件，不写任何东西")
    ap.add_argument("--create", action="store_true", help="目标 HERMES_HOME 不存在时允许新建（默认拒绝）")
    a = ap.parse_args()

    repo = Path(a.repo)
    home = Path(a.home)
    if not (repo / "home-overlay").is_dir():
        print(f"不是备份仓目录（缺 home-overlay/）: {repo}")
        return 1

    if not home.is_dir():
        if a.dry_run:
            print(f"（dry-run）目标目录尚不存在，真恢复时会新建: {home}")
        elif not a.create:
            print(f"目标 HERMES_HOME 不存在: {home}")
            print("  新机请先启动一次 Hermes（生成骨架）再恢复；确需在此路径新建，加 --create。")
            return 2
        else:
            home.mkdir(parents=True, exist_ok=True)
    elif not any((home / m).exists() for m in HERMES_MARKERS):
        print(f"⚠ 目标目录不像 Hermes home（无 hermes-agent/config.yaml/memories）: {home}")

    backup = None if (a.no_backup or a.dry_run) else repo / "restore-out" / "pre-restore-backup"
    _allowed = {"home", "profiles"}
    _bad = [w for w in (x.strip() for x in a.what.split(",")) if w and w not in _allowed]
    if _bad:
        # 拼错层名原来会「0 个文件 + rc=0」——看起来恢复成功，其实什么都没做（2026-10-31 审计发现）
        print(f"!! --what 里有未知层名 {_bad}（只认 home / profiles）")
        return 2
    what = [w.strip() for w in a.what.split(",") if w.strip()]
    files = backed = failed = 0
    tag = "[dry-run] " if a.dry_run else ""

    if "home" in what:
        n, b, x, sample = overlay_dir(repo / "home-overlay", home, backup, a.dry_run)
        files += n
        backed += b
        failed += x
        print(f"{tag}home-overlay -> {home}: {'将覆盖' if a.dry_run else '覆盖'} {n} 文件"
              + ("" if a.dry_run else f"（旧件备份 {b}）"))
        for s in sample:
            print(f"    · {s}" + (" …" if a.dry_run and n > len(sample) else ""))
    if "profiles" in what:
        proot = repo / "profiles-overlay"
        if proot.is_dir():
            for p in sorted(x for x in proot.iterdir() if x.is_dir()):
                sub_backup = (backup / "profiles" / p.name) if backup else None
                n, b, x, _ = overlay_dir(p, home / "profiles" / p.name, sub_backup, a.dry_run)
                files += n
                backed += b
                failed += x
                print(f"{tag}profiles-overlay/{p.name} -> {home / 'profiles' / p.name}: "
                      f"{'将覆盖' if a.dry_run else '覆盖'} {n} 文件"
                      + ("" if a.dry_run else f"（备份 {b}）"))

    print(f"== {'[dry-run] 合计将覆盖' if a.dry_run else '完成：覆盖'} {files} 文件"
          + ("" if a.dry_run else f"，旧件备份 {backed}，失败 {failed}")
          + (f"（于 {backup}）" if backup else "") + " ==")
    if a.dry_run:
        print("   （dry-run 未写入任何文件；去掉 --dry-run 才会真覆盖）")
    return 0 if failed == 0 else 3


if __name__ == "__main__":
    sys.exit(main())
