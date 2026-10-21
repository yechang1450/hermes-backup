# -*- coding: utf-8 -*-
"""hermes-cloud-backup · sync v3.7  (2026-10-21)

把本机 Hermes 数据采集到镜像工作区（目录自动解析），然后 commit -> push
到 GitHub 私有仓（永不 force-push）。用法：

    python sync.py                                # 全量：采集 -> commit -> push
    python sync.py --collect-only                 # 只采集（不动 git）
    python sync.py --collect-only --skip-exports  # 采集但跳过会话导出（快速自检）
    python sync.py --no-push                      # 采集 + commit，不 push
    python sync.py --mirror E:\\hermes2           # 指定镜像目录
    python sync.py --owner <gh-user> --create-remote   # 首次：创建私有仓并推送

镜像目录解析顺序：--mirror > 环境变量 HERMES_BACKUP_MIRROR > 技能目录 local.json
的 "mirror" 字段 > D:\\hermes-backup（若存在）> ~/hermes-backup。
仓库解析顺序：镜像的 git remote > --remote > gh 登录账号（默认仓名 hermes-backup）。

存储格式（v3，2026-10-17）：
  · 会话原样存文本：<会话id>.jsonl（>90MB 时切 <会话id>.part-0001.jsonl … 分片）；
    不压缩——预压缩（整包 gzip）会让头部元数据的任何变动把整份文件压缩后的字节
    全部改写，git 的增量(delta)机制失效、每轮把整包当新文件重存（实测 79MB/文件/轮）。
    原样文本让 git delta 直接命中未变区段（实测降到 ~0.01–2MB/文件/轮）。
  · 会话导出（v3.3 提速）：默认单进程批量（export_all_profiles.py 直调官方导出函数，
    与 CLI 输出逐字节一致，实测 ~8x）；失败自动回退逐档 `hermes sessions export`。
  · 写入跳过（v3.4/v3.5）：会话文件与全部覆盖层文件「内容相同就不重写」（时间戳不动→
    git 不重哈希）；过期文件/目录由 prune_stale / prune_unvisited 收敛（不再整目录清空重建）。
    背景：每轮重建覆盖层 4.5 万个小文件会让 ctime 全变，git 被迫每轮重哈希 ~3GB
    （实测 git 提交段 8–10 分钟；干净树上同一操作仅 6 秒）。
  · 备份范围（v3.6）：kanban 只备数据库（boards/*/kanban.db）；任务运行工作区/证据/日志
    属过程记录不入云（实测占该目录 96%：758MB/4.3 万文件 → 30MB/84 库）。
  · 数据仓换代（v3.7，2026-10-21）：第 1 代 `hermes-backup` 达 4.28GB 后冻结归档；
    当前默认指向第 2 代 `hermes-backup-2026q4`（孤儿快照单提交种子，详见
    references/generations.md）。换代=不停同步、不重写历史、可回退。

安全闸（fail-closed）：
  · 采集完成后若发现单文件 >95MB 或镜像总量 >4GB -> 中止，不 commit 不 push
  · 永不 force-push；push 被拒时 pull --no-rebase 后重试一次；出现合并冲突则中止并记录
  · 单实例防撞锁：并发触发时后来者自动让路（锁=HERMES_HOME/state/hermes-backup-sync.lock）——
    锁内记录持有者 pid：持有进程已消失（触发端被掐/崩溃）立即接管，3h 陈旧窗仅为兜底
  · git 索引锁自愈：崩溃遗留的 .git/index.lock（>120s）自动清除并重试一次
"""
from __future__ import annotations

import argparse
import atexit
import fnmatch
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# ---------- 路径与常量 ----------
def _localappdata() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))


def _appdata() -> Path:
    return Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))


HERMES_HOME = _localappdata() / "hermes"
ROAMING = _appdata() / "Hermes"
SKILL_DIR = HERMES_HOME / "skills" / "hermes-cloud-backup"
SCRATCH = HERMES_HOME / "cache" / "scratch" / "hb-exports"

# 下列由 main() 的 resolve_identity() 解析（--mirror / --owner / --remote 可覆盖）
MIRROR = Path(os.environ.get("HERMES_BACKUP_MIRROR") or (Path.home() / "hermes-backup"))
GH_OWNER = ""
GH_REPO = "hermes-backup"
REMOTE_URL = ""
GIT_NAME = ""
GIT_EMAIL = ""

TOOL_FILES = ["sync.py", "concat_sessions.py", "apply_routing.py", "restore_sessions.py",
              "restore_files.py", "run_in_runtime.py", "finish_restore.ps1",
              "export_all_profiles.py"]


def _skill_config() -> dict:
    """技能目录下的 local.json（机器本地配置，不进任何仓库）。"""
    f = SKILL_DIR / "local.json"
    if f.is_file():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def resolve_mirror(cli_value: str) -> Path:
    if cli_value:
        return Path(cli_value)
    env = os.environ.get("HERMES_BACKUP_MIRROR")
    if env:
        return Path(env)
    cfg = _skill_config().get("mirror")
    if cfg:
        return Path(cfg)
    for cand in (Path("D:/hermes-backup"), Path.home() / "hermes-backup"):
        if cand.exists():
            return cand
    return Path.home() / "hermes-backup"


def _parse_slug(url: str) -> str:
    m = re.search(r"github\.com[:/]+([^/]+)/([^/]+?)(?:\.git)?/?$", url.strip())
    return f"{m.group(1)}/{m.group(2)}" if m else ""


def resolve_identity(mirror: Path, owner_arg: str, repo_name: str, remote_arg: str) -> None:
    """解析并写入全局 GH_OWNER / GH_REPO / REMOTE_URL / GIT_NAME / GIT_EMAIL。"""
    global GH_OWNER, GH_REPO, REMOTE_URL, GIT_NAME, GIT_EMAIL
    owner, repo, url = "", (repo_name or "hermes-backup"), ""
    if (mirror / ".git").exists():
        p = subprocess.run(["git", "-C", str(mirror), "remote", "get-url", "origin"],
                           capture_output=True, text=True)
        if p.returncode == 0 and p.stdout.strip():
            url = p.stdout.strip()
            slug = _parse_slug(url)
            if slug:
                owner, repo = slug.split("/", 1)
    if remote_arg:
        url = remote_arg
        slug = _parse_slug(url)
        if slug:
            owner, repo = slug.split("/", 1)
    if not owner and owner_arg:
        owner = owner_arg
    if not owner:
        gh = shutil.which("gh")
        if gh:
            p = subprocess.run([gh, "api", "user", "--jq", ".login"],
                               capture_output=True, text=True)
            if p.returncode == 0 and p.stdout.strip():
                owner = p.stdout.strip()
    if not url and owner:
        url = f"git@github.com:{owner}/{repo}.git"
    GH_OWNER, GH_REPO, REMOTE_URL = owner, repo, url
    GIT_NAME = owner
    GIT_EMAIL = f"{owner}@users.noreply.github.com" if owner else ""

SIZE_GUARD_FILE = 95 * 1024 * 1024      # 单文件上限
SIZE_GUARD_TOTAL = 4096 * 1024 * 1024   # 镜像总量上限（v3 会话不压缩，基数更大）
PART_MAX = 90 * 1024 * 1024             # 会话分片阈值（为 100MB 单文件硬限留余量）

LOCK_FILE = HERMES_HOME / "state" / "hermes-backup-sync.lock"
LOCK_STALE_SECONDS = 3 * 3600           # 兜底：无法核实持有者时的陈旧锁窗口（正常由 pid 存活判定立即接管）

EXCLUDE_PATTERNS = ("*.lock", "*.pid", "*-wal", "*-shm", "*~", "Thumbs.db", "desktop.ini", "*.pyc")
EXCLUDE_DIRNAMES = {"__pycache__", "node_modules"}

HOME_FILES = [
    "config.yaml", ".env", "auth.json", "profile.yaml", "SOUL.md",
    "channel_directory.json", "projects.db", "shared-state.db", "kanban.db",
    "sessions/sessions.json",
]
HOME_DIRS = [
    "memories", "cron", "skills", "kanban", "weixin", "pairing", "platforms",
    "hooks", "vault", "desktop", "bot_relay", "assets", "pending",
    "pending_messages", "state", "scripts", ".curator_backups",
]
HOME_EXTRA_FILES = [  # skills 下跳过 .hub，但保留这两枚小锁文件
    "skills/.hub/lock.json",
    "skills/.hub/index-cache/official_optional_dirs.json",
]

PROFILE_FILES = ["config.yaml", ".env", "auth.json", "profile.yaml", "SOUL.md", "projects.db"]
PROFILE_DIRS = [
    "memories", "cron", "skills", "pairing", "platforms", "plans", "skins",
    "workspace", "hooks", "assets", ".curator_backups",
]

ROAMING_FILES = [
    "active-profile.json", "backend-ownership.json", "connection.json",
    "connections.json", "data-url-read-max.json", "desktop-installation.json",
    "disable-f12.json", "keep-awake.json", "minimize-to-tray.json",
    "native-theme.json", "pool-limits.json", "project-dir.json",
    "secure-token-storage.json", "windows-gpu-stack-cookie-fallback.json",
    "windows-sandbox-fallback.json", "Local State", "Preferences",
]
ROAMING_DIRS = [
    "Local Storage", "WebStorage", "shared_proto_db", "Session Storage",
    "Partitions", "composer-images",
]

LOG = []


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    LOG.append(line)


def _pid_alive(pid: int) -> bool:
    """持锁进程是否仍在世（Windows 用 OpenProcess/STILL_ACTIVE，其余平台 os.kill(0)）。"""
    if not pid:
        return False
    try:
        if os.name == "nt":
            import ctypes
            k32 = ctypes.windll.kernel32
            h = k32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return False
            try:
                code = ctypes.c_ulong()
                if k32.GetExitCodeProcess(h, ctypes.byref(code)):
                    return code.value == 259  # STILL_ACTIVE
                return False
            finally:
                k32.CloseHandle(h)
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def acquire_lock() -> bool:
    """单实例防撞锁：多触发源（手动＋自动）并发时后来者让路；
    锁记录持有者 pid——持有进程已消失（如触发端被掐、进程崩溃）时立即接管。"""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    if LOCK_FILE.exists():
        try:
            age = time.time() - LOCK_FILE.stat().st_mtime
        except OSError:
            age = 0
        holder = 0
        try:
            holder = int(json.loads(LOCK_FILE.read_text(encoding="utf-8")).get("pid") or 0)
        except Exception:
            holder = 0
        if holder and not _pid_alive(holder):
            log(f"锁持有者进程 {holder} 已不存在——立即接管（免等陈旧窗）")
        elif age < LOCK_STALE_SECONDS:
            log(f"已有另一轮同步在进行（锁年龄 {int(age)}s）——本轮退出，让路")
            return False
        else:
            log(f"发现陈旧锁（{int(age)}s > {LOCK_STALE_SECONDS}s），接管")
    LOCK_FILE.write_text(
        json.dumps({"pid": os.getpid(),
                    "started": datetime.now().astimezone().isoformat()}),
        encoding="utf-8")
    atexit.register(release_lock)
    return True


def release_lock() -> None:
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def resolved(p: Path) -> str:
    return str(p.as_posix())


def ignored(name: str) -> bool:
    return any(fnmatch.fnmatch(name, pat) for pat in EXCLUDE_PATTERNS)


_COPIED: set[str] = set()    # 本轮实际保留的镜像侧路径（供 prune_unvisited 收敛）


def copy_file(src: Path, dst: Path) -> bool:
    if ignored(src.name):
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    _COPIED.add(str(dst))
    try:
        st = src.stat()
        if dst.exists():
            dt = dst.stat()
            if dt.st_size == st.st_size and int(dt.st_mtime) == int(st.st_mtime):
                return True                     # 大小+mtime 一致＝未变：完全不碰（保镜像侧 ctime/mtime→git 不重哈希）
            if dt.st_size == st.st_size and dst.read_bytes() == src.read_bytes():
                return True                     # 内容一致：不重写（时间戳不动）
    except OSError:
        pass
    shutil.copy2(src, dst)
    return True


def prune_unvisited(root: Path) -> int:
    """删掉本轮采集未保留的镜像侧文件与空目录（源侧已删除的残留）。"""
    removed = 0
    for r, dirs, files in os.walk(root, topdown=False):
        for f in files:
            p = Path(r) / f
            if str(p) not in _COPIED:
                try:
                    p.unlink()
                    removed += 1
                except OSError:
                    pass
        for d in dirs:
            dp = Path(r) / d
            try:
                if not any(dp.iterdir()):
                    dp.rmdir()
            except OSError:
                pass
    return removed


def copy_kanban_dbs(src: Path, dst: Path) -> int:
    """看板只备份数据库（boards/*/kanban.db）；run 工作区/证据/日志属过程记录，不入云备份。
    实测：kanban 目录 758MB/4.3 万文件中 96% 是任务运行工作区（含整份 git 克隆）。"""
    n = 0
    for db in src.rglob("*.db"):
        if db.is_file() and copy_file(db, dst / db.relative_to(src)):
            n += 1
    return n


def copy_tree(src: Path, dst: Path, skip_dirs: set[str] | None = None) -> int:
    n = 0
    skip = (skip_dirs or set()) | EXCLUDE_DIRNAMES
    for root, dirs, files in os.walk(src):
        rel = Path(root).relative_to(src)
        dirs[:] = [d for d in dirs if d not in skip and not ignored(d)]
        for f in files:
            if copy_file(Path(root) / f, dst / rel / f):
                n += 1
    return n


def copy_checked(src: Path, dst: Path) -> bool:
    """文件或目录：存在则拷。"""
    if src.is_file():
        return copy_file(src, dst)
    if src.is_dir():
        copy_tree(src, dst)
        return True
    return False


def reset_dir(d: Path) -> None:
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True, exist_ok=True)


# ---------- 采集 ----------
def collect_home(report: dict) -> None:
    dst_root = MIRROR / "home-overlay"
    dst_root.mkdir(parents=True, exist_ok=True)      # 不清空：未变文件原样保留（保 ctime→git 不重哈希）
    n = 0
    for f in HOME_FILES:
        src = HERMES_HOME / f
        if src.is_file():
            copy_file(src, dst_root / f)
            n += 1
    for d in HOME_DIRS:
        src = HERMES_HOME / d
        if not src.is_dir():
            continue
        if d == "kanban":
            n += copy_kanban_dbs(src, dst_root / d)
            continue
        skip = {".hub"} if d == "skills" else set()
        n += copy_tree(src, dst_root / d, skip_dirs=skip)
    for f in HOME_EXTRA_FILES:
        src = HERMES_HOME / f
        if src.is_file():
            copy_file(src, MIRROR / "home-overlay" / f)
            n += 1
    gone = prune_unvisited(dst_root)
    if gone:
        log(f"home-overlay 清理：移除 {gone} 个残留文件")
    report["home_files"] = n


def collect_profiles(report: dict) -> None:
    dst_root = MIRROR / "profiles-overlay"
    dst_root.mkdir(parents=True, exist_ok=True)
    pdir = HERMES_HOME / "profiles"
    names = (sorted(d.name for d in pdir.iterdir()
                    if d.is_dir() and not d.name.startswith(".")
                    and (d / "profile.yaml").is_file())
             if pdir.is_dir() else [])
    report["profiles"] = {}
    for name in names:
        base = pdir / name
        n = 0
        for f in PROFILE_FILES:
            src = base / f
            if src.is_file():
                copy_file(src, dst_root / name / f)
                n += 1
        for d in PROFILE_DIRS:
            src = base / d
            if src.is_dir():
                n += copy_tree(src, dst_root / name / d, skip_dirs={".hub"} if d == "skills" else None)
        report["profiles"][name] = n
    gone = prune_unvisited(dst_root)
    log(f"profiles: {len(names)} 档, {sum(report['profiles'].values())} 文件"
        + (f"（清理 {gone} 个残留）" if gone else ""))


def collect_desktop(report: dict) -> None:
    dst_root = MIRROR / "desktop-overlay"
    dst_root.mkdir(parents=True, exist_ok=True)
    n = 0
    for f in ROAMING_FILES:
        src = ROAMING / f
        if src.is_file():
            copy_file(src, dst_root / f)
            n += 1
    for d in ROAMING_DIRS:
        src = ROAMING / d
        if src.is_dir():
            n += copy_tree(src, dst_root / d)
    gone = prune_unvisited(dst_root)
    if gone:
        log(f"desktop-overlay 清理：移除 {gone} 个残留文件")
    report["desktop_files"] = n


def find_hermes_exe() -> str:
    w = shutil.which("hermes")
    if w:
        return w
    cand = HERMES_HOME / "bin" / "hermes.exe"
    if cand.is_file():
        return str(cand)
    raise RuntimeError("hermes 可执行文件未找到")


def sync_tools(report: dict) -> None:
    """把技能内的脚本与手册镜像进仓库（tools/ + 仓库根 SKILL.md / RESTORE.md）。"""
    dst = MIRROR / "tools"
    dst.mkdir(parents=True, exist_ok=True)
    n = 0
    src_dir = SKILL_DIR / "scripts"
    if src_dir.is_dir():
        for f in TOOL_FILES:
            s = src_dir / f
            if s.is_file():
                shutil.copy2(s, dst / f)
                n += 1
    sk = SKILL_DIR / "SKILL.md"
    if sk.is_file():
        shutil.copy2(sk, MIRROR / "SKILL.md")
        n += 1
    tpl = SKILL_DIR / "templates" / "RESTORE.md"
    if tpl.is_file():
        shutil.copy2(tpl, MIRROR / "RESTORE.md")
        n += 1
    report["tools_mirrored"] = n
    log(f"tools 镜像: {n} 件（tools/ + SKILL.md + RESTORE.md）")


def ensure_private(report: dict) -> bool:
    """推送前私有性检查：gh 可用时验证；非私有则尝试改回；改不回来则拒绝推送。"""
    gh = shutil.which("gh")
    slug = f"{GH_OWNER}/{GH_REPO}"
    if not GH_OWNER:
        report["private_check"] = "未解析到仓库属主，跳过私有检查（务必人工确保仓库私有！）"
        log("!! " + report["private_check"])
        return True
    if not gh:
        report["private_check"] = "gh 不可用，跳过检查（务必人工确保仓库私有！）"
        log("!! " + report["private_check"])
        return True
    p = subprocess.run([gh, "api", f"repos/{slug}", "--jq", ".private"],
                       capture_output=True, text=True)
    if p.returncode != 0:
        report["private_check"] = f"检查失败，跳过：{(p.stderr or '').strip()[:120]}"
        log("!! " + report["private_check"])
        return True
    if p.stdout.strip().lower() == "true":
        report["private_check"] = "已确认私有"
        return True
    log("!! 仓库不是私有！尝试改回私有…")
    fix = subprocess.run([gh, "repo", "edit", slug, "--visibility", "private",
                          "--accept-visibility-change-consequences"],
                         capture_output=True, text=True)
    if fix.returncode == 0:
        report["private_check"] = "非私有→已改回私有"
        log("已改回私有，继续推送")
        return True
    report["private_check"] = f"非私有且改回失败：{(fix.stderr or '').strip()[:200]}"
    log("!! " + report["private_check"] + " —— 拒绝推送")
    return False


SESSION_ID_RE = re.compile(rb'"id"\s*:\s*"([0-9A-Za-z_.-]{4,128})"')


def split_to_session_parts(raw: Path, outdir: Path) -> dict:
    """把「一行=一个会话」的 jsonl 原样拆成每会话 .jsonl（>90MB 切 .part-NNNN.jsonl 分片）。

    刻意不压缩：整包 gzip 会让头部元数据的任何变动把整份文件的压缩字节全部改写，
    git 的增量(delta)机制由此失效——每轮同步都把整包当新文件重存（实测 79MB/文件/轮）。
    原样存文本后，git delta 直接命中未变区段（实测降到 ~0.01–2MB/文件/轮）。"""
    outdir.mkdir(parents=True, exist_ok=True)
    stats = {"sessions": 0, "files": 0, "bytes_total": 0, "max": 0, "max_file": "",
             "oversize": [], "_files": []}
    seen: dict[str, int] = {}
    with open(raw, "rb") as fin:
        for idx, ln in enumerate(fin):
            m = SESSION_ID_RE.search(ln[:16384])
            sid = m.group(1).decode("ascii", "replace") if m else f"line{idx:04d}"
            dup = seen.get(sid, 0)
            seen[sid] = dup + 1
            if dup:
                sid = f"{sid}-{dup}"
            stats["sessions"] += 1
            if len(ln) <= PART_MAX:
                outs = [(outdir / f"{sid}.jsonl", ln)]
            else:
                outs = [(outdir / f"{sid}.part-{k // PART_MAX + 1:04d}.jsonl", ln[k:k + PART_MAX])
                        for k in range(0, len(ln), PART_MAX)]
            for path, blob in outs:
                size = len(blob)
                same = False
                if path.exists() and path.stat().st_size == size:
                    try:
                        same = path.read_bytes() == blob     # 内容相同就不重写：保住 mtime→git 跳过整树重哈希
                    except OSError:
                        same = False
                if not same:
                    path.write_bytes(blob)
                stats["_files"].append(path.name)
                stats["files"] += 1
                stats["bytes_total"] += size
                if size > stats["max"]:
                    stats["max"], stats["max_file"] = size, path.name
                if size > SIZE_GUARD_FILE:
                    stats["oversize"].append(path.name)
    return stats


def prune_stale(sessions_root: Path, keep: dict[str, set[str]]) -> None:
    """删掉本轮导出集合之外的旧文件（会话已删除 / 档已不存在），其余原样保留、mtime 不动。
    「不重写未变文件」的前提是目录不再整清空——收敛任务由这里承担。"""
    removed = 0
    for d in sorted(sessions_root.iterdir()):
        if not d.is_dir():
            continue
        names = keep.get(d.name)
        if names is None:
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
            continue
        for f in d.iterdir():
            if f.is_file() and f.name not in names:
                f.unlink()
                removed += 1
    if removed:
        log(f"sessions 清理：移除 {removed} 个过期文件/目录")


def export_sessions(report: dict, skip_exports: bool) -> None:
    report["exports"] = {}
    if skip_exports:
        return
    sessions_root = MIRROR / "sessions"
    sessions_root.mkdir(parents=True, exist_ok=True)   # 不清空：内容未变的会话文件保持原样（mtime 不动→git 不重哈希）
    SCRATCH.mkdir(parents=True, exist_ok=True)
    exe = find_hermes_exe()
    pdir = HERMES_HOME / "profiles"
    keep: dict[str, set[str]] = {}

    def add_entry(name: str, raw: Path, extra: dict) -> None:
        entry: dict = {"ok": True, "rc": 0, "sessions": 0, "files": 0, "bytes_total": 0,
                       "max": 0, "max_file": "", "oversize": [], "skipped_gt_20k": []}
        entry.update(extra)
        if raw.exists():
            stats = split_to_session_parts(raw, sessions_root / name)
            raw.unlink()
            entry.update(stats)
            keep[name] = set(entry.pop("_files", []))
        report["exports"][name] = entry
        log(f"export {name}: sessions={entry['sessions']} files={entry['files']} "
            f"raw={entry['bytes_total']/1048576:.1f}MB max={entry['max']/1048576:.1f}MB")

    # ── 路径 A（默认，v3.3）：单进程批量导出＝官方导出函数直调（实测 ~8x；输出与 CLI 逐字节一致）──
    wanted = ["main"]
    if pdir.is_dir():
        for d in sorted(pdir.iterdir()):
            if d.is_dir() and not d.name.startswith(".") and (d / "profile.yaml").is_file():
                wanted.append(d.name)
    scripts_dir = Path(__file__).resolve().parent
    runner, helper = scripts_dir / "run_in_runtime.py", scripts_dir / "export_all_profiles.py"
    rawdir = SCRATCH / "bulk-export"
    bulk_ok = runner.is_file() and helper.is_file()
    if bulk_ok:
        shutil.rmtree(rawdir, ignore_errors=True)
        t0 = time.time()
        try:
            bp = subprocess.run([sys.executable, str(runner), str(helper),
                                 "--home", str(HERMES_HOME), "--out", str(rawdir)],
                                capture_output=True, text=True, timeout=2000)  # 导出阶段预算 33 分钟
            if bp.returncode != 0:
                bulk_ok = False
                log(f"批量导出失败 rc={bp.returncode}：{(bp.stdout or bp.stderr or '').strip()[-300:]}")
        except subprocess.TimeoutExpired:
            raise RuntimeError("export_timeout（批量导出超过 2000s 预算）")
        except Exception as e:
            bulk_ok = False
            log(f"批量导出异常 {type(e).__name__}：回退逐档 CLI")
        if bulk_ok:
            missing = [n for n in wanted if not (rawdir / f"{n}.jsonl").exists()]
            if missing:
                bulk_ok = False
                log(f"批量导出缺件 {missing}：回退逐档 CLI")
            else:
                log(f"批量导出完成：{len(wanted)} 档 / {time.time() - t0:.0f}s（单进程）")
                for name in wanted:
                    add_entry(name, rawdir / f"{name}.jsonl", {"via": "bulk"})
                shutil.rmtree(rawdir, ignore_errors=True)
                prune_stale(sessions_root, keep)
                return

    # ── 路径 B（回退）：逐档 hermes CLI（v3.2 行为，原样保留）──
    def do_one(name: str, profile: str | None) -> None:
        raw = SCRATCH / f"{name}.jsonl"
        if raw.exists():
            raw.unlink()
        cmd = [exe] + (["-p", profile] if profile else []) + \
              ["sessions", "export", "--format", "jsonl", str(raw)]
        t0 = time.time()
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        out = (p.stdout or "") + "\n" + (p.stderr or "")
        skipped = sorted(set(re.findall(r"Session '([0-9A-Za-z_]+)' has more than", out)))
        entry: dict = {"ok": p.returncode == 0, "rc": p.returncode, "sessions": 0,
                       "files": 0, "bytes_total": 0, "max": 0, "max_file": "",
                       "oversize": [], "skipped_gt_20k": skipped,
                       "seconds": round(time.time() - t0, 1), "via": "cli"}
        if raw.exists():
            stats = split_to_session_parts(raw, sessions_root / name)
            raw.unlink()
            entry.update(stats)
            keep[name] = set(entry.pop("_files", []))
        if p.returncode != 0 or skipped:
            entry["tail"] = out.strip()[-600:]
        report["exports"][name] = entry
        log(f"export {name}: rc={p.returncode} sessions={entry['sessions']} "
            f"files={entry['files']} raw={entry['bytes_total']/1048576:.1f}MB "
            f"max={entry['max']/1048576:.1f}MB "
            f"skipped={len(skipped)} {entry['seconds']}s")

    do_one("main", None)
    for d in sorted(pdir.iterdir()):
        if not (d.is_dir() and not d.name.startswith(".")):
            continue
        if not (d / "profile.yaml").is_file():
            log(f"export {d.name}: 跳过（目录存在但 profile 未注册）")
            continue
        do_one(d.name, d.name)
    prune_stale(sessions_root, keep)


def dump_routing(report: dict) -> None:
    out_dir = MIRROR / "supplement"
    out_dir.mkdir(parents=True, exist_ok=True)
    db = HERMES_HOME / "state.db"
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        rows = list(con.execute(
            "SELECT scope, session_key, entry_json, updated_at FROM gateway_routing"))
    finally:
        con.close()
    data = {
        "note": "state.db 的 gateway_routing 表原样导出（微信/QQ 等平台线程 -> 会话映射）。"
                "恢复：用 tools/apply_routing.py 写回（需 Hermes 停止）。",
        "exported_at": datetime.now().astimezone().isoformat(),
        "rows": [
            {"scope": r[0], "session_key": r[1], "entry_json": r[2], "updated_at": r[3]}
            for r in rows
        ],
    }
    (out_dir / "gateway_routing.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    report["routing_rows"] = len(rows)


# ---------- 安全闸 ----------
def size_audit() -> tuple[list[tuple[str, int]], int]:
    big: list[tuple[str, int]] = []
    total = 0
    for root, dirs, files in os.walk(MIRROR):
        dirs[:] = [d for d in dirs if d != ".git"]
        for f in files:
            p = Path(root) / f
            try:
                s = p.stat().st_size
            except OSError:
                continue
            total += s
            if s > SIZE_GUARD_FILE:
                big.append((str(p), s))
    return big, total


# ---------- git ----------
def git(*args: str, check: bool = True, timeout: float | None = None) -> subprocess.CompletedProcess:
    p = subprocess.run(["git", "-C", str(MIRROR), *args],
                       capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} -> rc={p.returncode}: "
                           f"{(p.stderr or p.stdout).strip()[:500]}")
    return p


def purge_index_lock(min_age: int) -> bool:
    """git 索引锁自愈：镜像仓只由本工具写入，残留的 .git/index.lock 视为崩溃遗留。
    min_age 秒数内的锁不动（避免误删正在进行的操作）；返回是否清除了锁。"""
    lock = MIRROR / ".git" / "index.lock"
    if not lock.exists():
        return False
    try:
        age = time.time() - lock.stat().st_mtime
    except OSError:
        age = 0
    if age < min_age:
        return False
    try:
        lock.unlink()
        log(f"清除陈旧 git index.lock（年龄 {int(age)}s）")
        return True
    except OSError as e:
        log(f"!! index.lock 清除失败：{e}")
        return False


def ensure_repo() -> None:
    if not (MIRROR / ".git").exists():
        log("git init")
        git("init", "-b", "main")
        git("config", "user.name", GIT_NAME or "hermes-backup")
        if GIT_EMAIL:
            git("config", "user.email", GIT_EMAIL)
        git("config", "core.autocrlf", "false")
        git("config", "core.safecrlf", "false")
    if not REMOTE_URL:
        raise RuntimeError("无法解析备份仓地址：请先 `gh auth login`，"
                           "或用 --owner <你的GitHub用户名> / --remote <URL> 指定")
    remotes = git("remote", check=False).stdout.split()
    if "origin" not in remotes:
        git("remote", "add", "origin", REMOTE_URL)
    elif git("remote", "get-url", "origin").stdout.strip() != REMOTE_URL:
        git("remote", "set-url", "origin", REMOTE_URL)


def commit_and_push(message: str, no_push: bool) -> dict:
    result: dict = {}
    ensure_repo()
    # 冲突残留防御：merge 状态未清理则中止
    if (MIRROR / ".git" / "MERGE_HEAD").exists():
        raise RuntimeError("仓库处于未完成的 merge 状态，请人工处理后重跑")
    purge_index_lock(120)
    t0 = time.time()
    try:
        git("add", "-A")
    except RuntimeError as e:
        if "index.lock" not in str(e):
            raise
        log("git add 撞上 index.lock —— 清锁后重试一次")
        purge_index_lock(0)
        git("add", "-A")
    status = git("status", "--porcelain").stdout.strip()
    if status:
        git("commit", "-m", message)
        result["committed"] = True
        log(f"git 提交完成：{time.time() - t0:.0f}s（含 add 与自动 gc）")
    else:
        result["committed"] = False
        log("无变更，跳过 commit")
    if no_push:
        result["pushed"] = None
        return result
    t1 = time.time()
    p = git("push", "-u", "origin", "main", check=False, timeout=600)  # push 阶段预算 10 分钟
    if p.returncode != 0:
        log("push 被拒（可能线上有新提交）→ pull --no-rebase 后重试")
        pull = git("pull", "--no-rebase", "origin", "main", check=False, timeout=600)
        if pull.returncode != 0:
            git("merge", "--abort", check=False)
            raise RuntimeError(f"pull/merge 失败：{(pull.stderr or pull.stdout).strip()[:400]}")
        p = git("push", "-u", "origin", "main", check=False, timeout=600)
    if p.returncode != 0:
        raise RuntimeError(f"push 失败：{(p.stderr or p.stdout).strip()[:400]}")
    log(f"git 推送完成：{time.time() - t1:.0f}s")
    result["pushed"] = True
    return result


def _maybe_create_remote(enabled: bool) -> None:
    if not enabled or not GH_OWNER:
        return
    gh = shutil.which("gh")
    if not gh:
        log("!! --create-remote 需要 gh，跳过")
        return
    slug = f"{GH_OWNER}/{GH_REPO}"
    p = subprocess.run([gh, "repo", "view", slug], capture_output=True, text=True)
    if p.returncode == 0:
        return
    log(f"远端不存在，创建私有仓 {slug} …")
    cr = subprocess.run([gh, "repo", "create", slug, "--private"],
                        capture_output=True, text=True)
    if cr.returncode == 0:
        log(f"已创建私有仓 {slug}")
    else:
        log(f"!! 建仓失败（rc={cr.returncode}）：{(cr.stderr or cr.stdout).strip()[:200]}"
            " —— 远端若不存在，后续 push 会失败")


# ---------- 主流程 ----------
def main() -> int:
    global MIRROR
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # 中文日志在任意终端一致
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--collect-only", action="store_true")
    ap.add_argument("--skip-exports", action="store_true")
    ap.add_argument("--no-push", action="store_true")
    ap.add_argument("--message", default="")
    ap.add_argument("--mirror", default="", help="镜像工作区目录（默认自动探测）")
    ap.add_argument("--owner", default="", help="GitHub 属主（默认从镜像 remote 或 gh 登录解析）")
    ap.add_argument("--repo-name", default="hermes-backup-2026q4", help="仓库名（默认 hermes-backup-2026q4＝第 2 代仓；第 1 代 hermes-backup 已归档）")
    ap.add_argument("--remote", default="", help="显式指定 remote URL")
    ap.add_argument("--create-remote", action="store_true",
                    help="远端不存在时用 gh 创建私有仓")
    args = ap.parse_args()

    if not acquire_lock():
        return 0

    t0 = time.time()
    MIRROR = resolve_mirror(args.mirror)
    resolve_identity(MIRROR, args.owner, args.repo_name, args.remote)
    log(f"解析：mirror={MIRROR} owner={GH_OWNER or '(未解析)'} "
        f"remote={REMOTE_URL or '(未解析)'}")
    MIRROR.mkdir(parents=True, exist_ok=True)
    (MIRROR / "logs").mkdir(parents=True, exist_ok=True)
    report: dict = {
        "version": 1,
        "host": socket.gethostname(),
        "hermes_home": resolved(HERMES_HOME),
        "started_at": datetime.now().astimezone().isoformat(),
    }
    log(f"sync 开始 host={report['host']} home={report['hermes_home']}")

    t_ph = time.time()
    collect_home(report)
    collect_profiles(report)
    collect_desktop(report)
    sync_tools(report)
    report["timings"] = {"collect": round(time.time() - t_ph, 1)}
    t_ph = time.time()
    try:
        export_sessions(report, args.skip_exports)
    except RuntimeError as e:
        log(f"!! 导出阶段失败：{e}")
        report["aborted"] = str(e)
        write_meta(report, t0)
        append_run(report, t0, 2)
        return 2
    report["timings"]["export"] = round(time.time() - t_ph, 1)
    over = {n: e["oversize"] for n, e in report.get("exports", {}).items() if e.get("oversize")}
    if over:
        log(f"!! 采集出现超限单文件（>{SIZE_GUARD_FILE // 1048576}MB），中止（不 commit/push）：{over}")
        report["aborted"] = "oversize_session"
        write_meta(report, t0)
        append_run(report, t0, 2)
        return 2
    dump_routing(report)

    big, total = size_audit()
    report["size_total_bytes"] = total
    if big:
        log("!! 发现超限单文件，中止（不 commit/push）：")
        for path, s in big[:10]:
            log(f"   {path} = {s/1048576:.1f}MB")
        report["aborted"] = "oversize_file"
        write_meta(report, t0)
        append_run(report, t0, 2)
        return 2
    if total > SIZE_GUARD_TOTAL:
        log(f"!! 镜像总量 {total/1048576:.0f}MB 超上限，中止（不 commit/push）")
        report["aborted"] = "oversize_total"
        write_meta(report, t0)
        append_run(report, t0, 2)
        return 2

    if args.collect_only:
        log(f"采集完成（collect-only）：总量 {total/1048576:.1f}MB")
        write_meta(report, t0)
        append_run(report, t0, 0)
        return 0

    if not args.no_push:
        _maybe_create_remote(args.create_remote)
        if not ensure_private(report):
            write_meta(report, t0)
            append_run(report, t0, 4)
            return 4
    msg = args.message or f"sync {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M %z')} host={report['host']}"
    t_ph = time.time()
    try:
        git_result = commit_and_push(msg, args.no_push)
        report.update(git_result)
    except (RuntimeError, subprocess.TimeoutExpired) as e:
        log(f"!! git 环节失败：{e}")
        report["git_error"] = str(e)
        write_meta(report, t0)
        append_run(report, t0, 3)
        return 3
    report["timings"]["git"] = round(time.time() - t_ph, 1)

    report["finished_at"] = datetime.now().astimezone().isoformat()
    report["duration_seconds"] = round(time.time() - t0, 1)
    write_meta(report, t0)
    log(f"sync 完成：{report['duration_seconds']}s 总量 {total/1048576:.1f}MB "
        f"committed={report.get('committed')} pushed={report.get('pushed')}")
    append_run(report, t0, 0)
    return 0


RUNS_LOG = HERMES_HOME / "state" / "hb-sync-runs.jsonl"


def append_run(report: dict, t0: float, exit_code: int) -> None:
    """每轮一行结构化记录（runs.jsonl）——验收/排障的唯一客观证据。"""
    try:
        RUNS_LOG.parent.mkdir(parents=True, exist_ok=True)
        exports = report.get("exports", {}) or {}
        line = {
            "start_ts": report.get("started_at"),
            "end_ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "duration_s": round(time.time() - t0, 1),
            "exit_code": exit_code,
            "timings": report.get("timings", {}),
            "sessions": sum(e.get("sessions", 0) for e in exports.values()),
            "files": sum(e.get("files", 0) for e in exports.values()),
            "size_total_mb": round((report.get("size_total_bytes") or 0) / 1048576, 1),
            "committed": report.get("committed"),
            "pushed": report.get("pushed"),
            "aborted": report.get("aborted"),
            "git_error": (report.get("git_error") or "")[:200],
            "via": ("bulk" if any(e.get("via") == "bulk" for e in exports.values()) else "cli"),
        }
        with open(RUNS_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    except Exception as e:
        log(f"!! 写 runs.jsonl 失败：{e!r}")


def write_meta(report: dict, t0: float) -> None:
    report.setdefault("finished_at", datetime.now().astimezone().isoformat())
    report.setdefault("duration_seconds", round(time.time() - t0, 1))
    meta = MIRROR / "meta" / "sync-state.json"
    meta.parent.mkdir(parents=True, exist_ok=True)
    meta.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    logfile = MIRROR / "logs" / "last-run.log"
    logfile.write_text("\n".join(LOG) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
