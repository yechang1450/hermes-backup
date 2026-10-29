#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check-paths.py — 恢复后检查/修正残留的「旧机器绝对路径」。

为什么需要：备份里 config.yaml（hook 路径）、cron/jobs.json（workdir）、各 bot 档 config、
以及 home 下的脚本都写死了旧机器的用户目录。新机若用户名不同（甚至只换了盘符/目录），
这些引用会在恢复后静默失效（hook 不加载、cron 找不到脚本）。本工具把这类残留一次性查出来，
需要时按新路径改写。

用法：
  python check-paths.py --home <HERMES_HOME>                     # 只报告（默认）
  python check-paths.py --home <HERMES_HOME> --json              # 机器可读
  python check-paths.py --home <HERMES_HOME> --fix --new-home <新 HERMES_HOME>
                                                                 # 真改写（先备份 .bak-<时间>）
  python check-paths.py --home <HERMES_HOME> --fix --new-home <...> --dry-run

改写范围（白名单，避免误伤历史记录）：
  HERMES_HOME/config.yaml、SOUL.md、.env、cron/jobs.json、
  profiles/<档>/config.yaml、profiles/<档>/SOUL.md、scripts/**（文本件）

不碰：*.db、sessions/、logs/、cron/output/、cache/、任何二进制。
退出码：0=无残留或已修好；2=发现残留（未修）；3=参数错误。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

# 形如 <盘>:\Users\<用户名>  或  <盘>:/Users/<用户名>  或  D:\work\name（先只认 Users 层，最稳）
USER_PATH_RE = re.compile(r'[A-Za-z]:[\\/]Users[\\/]([^\\/"\'\s:]+)')

SKIP_DIRS = {'sessions', 'logs', 'cache', 'node_modules', '__pycache__', 'output', 'state'}
SKIP_SUFFIX = {'.db', '.db-wal', '.db-shm', '.pyc', '.png', '.jpg', '.jpeg', '.zip', '.gz', '.log'}


def _default_home() -> Path:
    env = os.environ.get('HERMES_HOME')
    if env:
        return Path(env)
    if os.name == 'nt':
        base = os.environ.get('LOCALAPPDATA') or str(Path.home() / 'AppData' / 'Local')
        return Path(base) / 'hermes'
    return Path.home() / '.hermes'


def _whitelist(home: Path):
    """返回要检查的文本文件列表（白名单）。"""
    out = []
    for name in ('config.yaml', 'SOUL.md', '.env'):
        p = home / name
        if p.is_file():
            out.append(p)
    jobs = home / 'cron' / 'jobs.json'
    if jobs.is_file():
        out.append(jobs)
    prof_dir = home / 'profiles'
    if prof_dir.is_dir():
        for prof in sorted(prof_dir.iterdir()):
            if not prof.is_dir():
                continue
            for name in ('config.yaml', 'SOUL.md', '.env'):
                p = prof / name
                if p.is_file():
                    out.append(p)
    scr = home / 'scripts'
    if scr.is_dir():
        for root, dirs, files in os.walk(scr):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for f in files:
                p = Path(root) / f
                if p.suffix.lower() in SKIP_SUFFIX or p.name.endswith('-wal') or p.name.endswith('-shm'):
                    continue
                try:
                    if p.stat().st_size > 4 * 1024 * 1024:
                        continue
                except OSError:
                    continue
                out.append(p)
    return out


def scan(home: Path):
    hits = []          # (path, count, sorted_names)
    prefixes = {}      # '<盘>:\Users\<用户名>' -> 出现次数
    for p in _whitelist(home):
        try:
            text = p.read_text(encoding='utf-8', errors='ignore')
        except OSError:
            continue
        found = USER_PATH_RE.findall(text)
        if not found:
            continue
        # 按 盘符+Users+名 汇总（同时统计两种斜杠）
        for m in re.finditer(r'([A-Za-z])[:\\/]+Users[\\/]+([^\\/"\'\s:]+)', text):
            key = f'{m.group(1)}:\\Users\\{m.group(2)}'
            prefixes[key] = prefixes.get(key, 0) + 1
        hits.append((str(p), len(found), sorted(set(found))))
    return hits, prefixes


def current_user_paths() -> set:
    """本机可能出现的「当前用户目录」写法集合。"""
    home = Path.home()
    s = set()
    for drive in ('C', 'D', 'E'):
        for sep in ('\\', '/'):
            s.add(f'{drive}:{sep}Users{sep}{home.name}')
    la = os.environ.get('LOCALAPPDATA')
    if la:
        s.add(str(Path(la).parent))
    return s


def fix(home: Path, new_home: Path, dry_run: bool):
    cur = current_user_paths()
    old_names = set()
    hits, prefixes = scan(home)
    for key in prefixes:
        old_names.add(key.split('\\')[-1])
    # 只改「明显是旧机器用户名」的那些（不在当前用户名集合里）
    cur_l = {c.lower() for c in cur}
    targets = {k for k in prefixes if k.lower() not in cur_l}
    if not targets:
        print('没有需要改写的旧路径（全部等于本机当前用户目录）。')
        return 0

    new_user_dir = str(Path.home())          # C:\Users\<当前用户>
    replaced_files = 0
    total_repl = 0
    for p_str, _n, _names in hits:
        p = Path(p_str)
        text = p.read_text(encoding='utf-8', errors='ignore')
        new_text = text
        for old in targets:
            name = old.split('\\')[-1]
            drive = old.split(':')[0]
            variants = [f'{drive}:\\Users\\{name}', f'{drive}:/Users/{name}',
                        f'{drive}:\\\\Users\\\\{name}']
            for v in variants:
                if v in new_text:
                    # 保持原有分隔符风格（正/反斜杠），避免写出「反斜杠+正斜杠」混搭路径
                    repl = new_user_dir.replace('\\', '/') if '/' in v else new_user_dir
                    new_text = new_text.replace(v, repl)
        if new_text == text:
            continue
        if dry_run:
            print(f'  [dry-run] 将改写 {p}（{len(targets)} 类前缀）')
            replaced_files += 1
            continue
        bak = p.with_suffix(p.suffix + '.bak-paths-' + time.strftime('%Y%m%d-%H%M%S'))
        bak.write_text(text, encoding='utf-8')
        p.write_text(new_text, encoding='utf-8')
        replaced_files += 1
        total_repl += 1
        print(f'  已改写 {p}（备份 {bak.name}）')

    print(f'\n改写文件 {replaced_files} 个；旧前缀 {sorted(targets)} → {new_user_dir}')
    if dry_run:
        print('（dry-run 未写入任何文件）')
    else:
        print('提醒：config.yaml / cron 的改动需要**重启 Hermes** 才生效。')
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description='检查/修正恢复后残留的旧机器绝对路径')
    ap.add_argument('--home', default=str(_default_home()), help='目标 HERMES_HOME')
    ap.add_argument('--json', action='store_true', help='输出 JSON')
    ap.add_argument('--fix', action='store_true', help='真改写（默认只报告）')
    ap.add_argument('--new-home', default='', help='--fix 时用：新的 HERMES_HOME（默认按当前用户目录）')
    ap.add_argument('--dry-run', action='store_true', help='配合 --fix：只显示将改什么')
    a = ap.parse_args()

    home = Path(a.home)
    if not home.is_dir():
        print(f'目标 HERMES_HOME 不存在：{home}', file=sys.stderr)
        return 3

    if a.fix:
        return fix(home, Path(a.new_home) if a.new_home else home, a.dry_run)

    hits, prefixes = scan(home)
    if a.json:
        print(json.dumps({'home': str(home), 'files': [{'path': p, 'count': n, 'users': u} for p, n, u in hits],
                          'prefixes': prefixes, 'total': sum(prefixes.values())},
                         ensure_ascii=False, indent=2))
    else:
        if not hits:
            print(f'PATHCHECK-OK 未发现旧机器绝对路径（{home}）')
            return 0
        print(f'发现 {len(hits)} 个文件、{sum(prefixes.values())} 处旧绝对路径：')
        for key, n in sorted(prefixes.items(), key=lambda kv: -kv[1]):
            print(f'  {n:5d}  {key}')
        print('\n明细（前 20）：')
        for p, n, users in hits[:20]:
            print(f'  {n:4d}  {p}')
        cur = current_user_paths()
        cur_l = {c.lower() for c in cur}
        bad = [k for k in prefixes if k.lower() not in cur_l]
        if bad:
            print(f'\nPATHCHECK-WARN 其中 {len(bad)} 类前缀不是本机当前用户目录：{sorted(bad)}')
            print('  若这些是旧机器的用户名，恢复后应运行：--fix --new-home <新 HERMES_HOME>')
            return 2
        print('\nPATHCHECK-OK 全部等于本机当前用户目录，无需处理。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
