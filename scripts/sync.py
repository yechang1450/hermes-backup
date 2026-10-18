# -*- coding: utf-8 -*-
"""hermes-cloud-backup · sync v3.2  (2026-10-18)

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
              "restore_files.py", "run_in_runtime.py", "finish_restore.ps1"]


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


def copy_file(src: Path, dst: Path) -> bool:
    if ignored(src.name):
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return True


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
    reset_dir(dst_root)
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
        skip = {".hub"} if d == "skills" else set()
        n += copy_tree(src, dst_root / d, skip_dirs=skip)
    for f in HOME_EXTRA_FILES:
        src = HERMES_HOME / f
        if src.is_file():
            copy_file(src, MIRROR / "home-overlay" / f)
            n += 1
    report["home_files"] = n


def collect_profiles(report: dict) -> None:
    dst_root = MIRROR / "profiles-overlay"
    reset_dir(dst_root)
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
    log(f"profiles: {len(names)} 档, {sum(report['profiles'].values())} 文件")


def collect_desktop(report: dict) -> None:
    dst_root = MIRROR / "desktop-overlay"
    reset_dir(dst_root)
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
    stats = {"sessions": 0, "files": 0, "bytes_total": 0, "max": 0, "max_file": "", "oversize": []}
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
                path.write_bytes(blob)
                size = len(blob)
                stats["files"] += 1
                stats["bytes_total"] += size
                if size > stats["max"]:
                    stats["max"], stats["max_file"] = size, path.name
                if size > SIZE_GUARD_FILE:
                    stats["oversize"].append(path.name)
    return stats


def export_sessions(report: dict, skip_exports: bool) -> None:
    report["exports"] = {}
    if skip_exports:
        return
    sessions_root = MIRROR / "sessions"
    reset_dir(sessions_root)
    SCRATCH.mkdir(parents=True, exist_ok=True)
    exe = find_hermes_exe()

    def do_one(name: str, profile: str | None) -> None:
        raw = SCRATCH / f"{name}.jsonl"
        if raw.exists():
            raw.unlink()
        cmd = [exe] + (["-p", profile] if profile else []) + \
              ["sessions", "export", "--format", "jsonl", str(raw)]
        t0 = time.time()
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        out = (p.stdout or "") + "\n" + (p.stderr or "")
        skipped = sorted(set(re.findall(r"Session '([0-9A-Za-z_]+)' has more than", out)))
        entry: dict = {"ok": p.returncode == 0, "rc": p.returncode, "sessions": 0,
                       "files": 0, "bytes_total": 0, "max": 0, "max_file": "",
                       "oversize": [], "skipped_gt_20k": skipped,
                       "seconds": round(time.time() - t0, 1)}
        if raw.exists():
            stats = split_to_session_parts(raw, sessions_root / name)
            raw.unlink()
            entry.update(stats)
        if p.returncode != 0 or skipped:
            entry["tail"] = out.strip()[-600:]
        report["exports"][name] = entry
        log(f"export {name}: rc={p.returncode} sessions={entry['sessions']} "
            f"files={entry['files']} raw={entry['bytes_total']/1048576:.1f}MB "
            f"max={entry['max']/1048576:.1f}MB "
            f"skipped={len(skipped)} {entry['seconds']}s")

    do_one("main", None)
    pdir = HERMES_HOME / "profiles"
    for d in sorted(pdir.iterdir()):
        if not (d.is_dir() and not d.name.startswith(".")):
            continue
        if not (d / "profile.yaml").is_file():
            log(f"export {d.name}: 跳过（目录存在但 profile 未注册）")
            continue
        do_one(d.name, d.name)


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
def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    p = subprocess.run(["git", "-C", str(MIRROR), *args],
                       capture_output=True, text=True)
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
    else:
        result["committed"] = False
        log("无变更，跳过 commit")
    if no_push:
        result["pushed"] = None
        return result
    p = git("push", "-u", "origin", "main", check=False)
    if p.returncode != 0:
        log("push 被拒（可能线上有新提交）→ pull --no-rebase 后重试")
        pull = git("pull", "--no-rebase", "origin", "main", check=False)
        if pull.returncode != 0:
            git("merge", "--abort", check=False)
            raise RuntimeError(f"pull/merge 失败：{(pull.stderr or pull.stdout).strip()[:400]}")
        p = git("push", "-u", "origin", "main", check=False)
    if p.returncode != 0:
        raise RuntimeError(f"push 失败：{(p.stderr or p.stdout).strip()[:400]}")
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
    ap.add_argument("--repo-name", default="hermes-backup", help="仓库名（默认 hermes-backup）")
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

    collect_home(report)
    collect_profiles(report)
    collect_desktop(report)
    sync_tools(report)
    export_sessions(report, args.skip_exports)
    over = {n: e["oversize"] for n, e in report.get("exports", {}).items() if e.get("oversize")}
    if over:
        log(f"!! 采集出现超限单文件（>{SIZE_GUARD_FILE // 1048576}MB），中止（不 commit/push）：{over}")
        report["aborted"] = "oversize_session"
        write_meta(report, t0)
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
        return 2
    if total > SIZE_GUARD_TOTAL:
        log(f"!! 镜像总量 {total/1048576:.0f}MB 超上限，中止（不 commit/push）")
        report["aborted"] = "oversize_total"
        write_meta(report, t0)
        return 2

    if args.collect_only:
        log(f"采集完成（collect-only）：总量 {total/1048576:.1f}MB")
        write_meta(report, t0)
        return 0

    if not args.no_push:
        _maybe_create_remote(args.create_remote)
        if not ensure_private(report):
            write_meta(report, t0)
            return 4
    msg = args.message or f"sync {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M %z')} host={report['host']}"
    try:
        git_result = commit_and_push(msg, args.no_push)
        report.update(git_result)
    except RuntimeError as e:
        log(f"!! git 环节失败：{e}")
        report["git_error"] = str(e)
        write_meta(report, t0)
        return 3

    report["finished_at"] = datetime.now().astimezone().isoformat()
    report["duration_seconds"] = round(time.time() - t0, 1)
    write_meta(report, t0)
    log(f"sync 完成：{report['duration_seconds']}s 总量 {total/1048576:.1f}MB "
        f"committed={report.get('committed')} pushed={report.get('pushed')}")
    return 0


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
