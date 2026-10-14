# -*- coding: utf-8 -*-
"""恢复文件层：把备份仓的 home-overlay/ 与 profiles-overlay/ 覆盖到本机 HERMES_HOME。

覆盖前先把将被覆盖的旧文件备份到 <repo>/restore-out/pre-restore-backup/（可 --no-backup 关闭）。
纯标准库，任意 python 均可运行。

用法:
    python restore_files.py --repo D:\\hermes-backup --home <HERMES_HOME>
    python restore_files.py --repo D:\\hermes-backup --what profiles --no-backup
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path


def default_home() -> Path:
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    return Path(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")) / "hermes"


def overlay_dir(src: Path, dst: Path, backup_root: Path | None) -> tuple[int, int, int]:
    """把 src 树覆盖到 dst；返回 (覆盖文件数, 备份旧文件数, 失败数)。"""
    n = b = fails = 0
    for root, dirs, files in os.walk(src):
        rel = Path(root).relative_to(src)
        (dst / rel).mkdir(parents=True, exist_ok=True)
        for f in files:
            s = Path(root) / f
            d = dst / rel / f
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
    return n, b, fails


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, help="备份仓 clone 目录")
    ap.add_argument("--home", default=str(default_home()), help="目标 HERMES_HOME")
    ap.add_argument("--what", default="home,profiles", help="home,profiles（逗号分隔）")
    ap.add_argument("--no-backup", action="store_true", help="不备份被覆盖的旧文件")
    a = ap.parse_args()

    repo = Path(a.repo)
    home = Path(a.home)
    if not (repo / "home-overlay").is_dir():
        print(f"不是备份仓目录（缺 home-overlay/）: {repo}")
        return 1
    backup = None if a.no_backup else repo / "restore-out" / "pre-restore-backup"
    what = [w.strip() for w in a.what.split(",") if w.strip()]
    files = backed = failed = 0

    if "home" in what:
        n, b, x = overlay_dir(repo / "home-overlay", home, backup)
        files += n
        backed += b
        failed += x
        print(f"home-overlay -> {home}: 覆盖 {n} 文件（旧件备份 {b}）")
    if "profiles" in what:
        proot = repo / "profiles-overlay"
        if proot.is_dir():
            for p in sorted(x for x in proot.iterdir() if x.is_dir()):
                sub_backup = (backup / "profiles" / p.name) if backup else None
                n, b, x = overlay_dir(p, home / "profiles" / p.name, sub_backup)
                files += n
                backed += b
                failed += x
                print(f"profiles-overlay/{p.name} -> {home / 'profiles' / p.name}: 覆盖 {n} 文件（备份 {b}）")

    print(f"== 完成：覆盖 {files} 文件，旧件备份 {backed}，失败 {failed}"
          + (f"（于 {backup}）" if backup else "") + " ==")
    return 0 if failed == 0 else 3


if __name__ == "__main__":
    sys.exit(main())
