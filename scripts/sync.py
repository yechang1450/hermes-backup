# -*- coding: utf-8 -*-
"""hermes-cloud-backup · sync v3.9  (2026-10-23)

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
  · 观测（v3.9）：每轮一行 runs.jsonl；**健康计数由本脚本在每个出口写**（单一来源，
    哨兵/周期/手工同链路）；连续 ≥3 轮失败写 needs-attention，成功即清零并删标记；
    子进程没跑完时由包装补记（last_exit_code="unfinished"，按 updated_at 判重）。
  · 数据仓换代（v3.7，2026-10-21）：第 1 代 `hermes-backup` 达 4.28GB 后冻结归档；
    当前默认指向第 2 代 `hermes-backup-2026q4`（孤儿快照单提交种子，详见
    references/generations.md）。换代=不停同步、不重写历史、可回退。

安全闸（fail-closed）：
  · 采集完成后若发现单文件 >95MB 或镜像总量 >4GB -> 中止，不 commit 不 push
  · 永不 force-push；push 失败时先 pull --no-rebase 对齐线上，再按 20s/60s 退避重试（最多 3 次；
    真实失败模式=GitHub 对 >6–8 分钟的 SSH 传输直接掐断 / fake-ip 偶发 RST，均属瞬态）；
    出现合并冲突则中止并记录，本地提交保留、下一轮自然补推
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

TOOL_FILES = ["sync.py", "session_layout.py", "concat_sessions.py", "apply_routing.py",
              "restore_sessions.py", "restore_files.py", "run_in_runtime.py", "churn-report.py",
              "finish_restore.ps1", "export_all_profiles.py"]


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
    owner, repo, url = "", (repo_name or _skill_config().get("repo") or "hermes-backup"), ""
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

PUSH_TIMEOUT = 1500                     # 推送预算：一次性大重排（如首次启用尾部稳定化）实测 900s 不够（2026-10-24 23:04 轮）
SIZE_GUARD_FILE = 95 * 1024 * 1024      # 单文件上限
STABILIZE_MIN_BYTES = 5 * 1024 * 1024   # 只对「大会话」做尾部稳定化：小文件整份重写无所谓，
                                        # 但大会话每轮整份重写会拖垮 git 增量（见 session_layout.py）
SIZE_GUARD_TOTAL = 4096 * 1024 * 1024   # 镜像总量上限（v3 会话不压缩，基数更大）
PART_MAX = 12 * 1024 * 1024             # 会话分片阈值（0.4.26 由 45MB 下调）。
                                        # 尾部稳定化后，每轮只有"变动的那个分片"重写 ⇒ 阈值越小、单轮重写越少
                                        # （实测 45MB 时稳态 249MB/轮，其中 208MB 全是 10 个活动会话的末片）。
                                        # 12MB 距 GitHub 单文件 100MB 硬限留 8 倍余量，仍远低于「建议 50MB」。

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
    if src.suffix.lower() == ".db":
        # SQLite（本机多为 WAL）：普通拷贝会丢掉还在 -wal 里、尚未 checkpoint 的已提交事务，
        # 极端情况连建表语句都可能不在 .db 里（见 test-kanban-snapshot.py 的 S1a）。
        # 一律走在线备份 API 取一致快照；内容相同则跳过以保住 mtime。
        try:
            if dst.exists() and dst.stat().st_size == src.stat().st_size and dst.read_bytes() == src.read_bytes():
                return True
        except OSError:
            pass
        return snapshot_sqlite(src, dst)
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


def snapshot_sqlite(src: Path, dst: Path) -> bool:
    """用 SQLite **在线备份 API** 取一致快照（WAL 里已提交的事务也会带上）。

    为什么不能直接 copy：看板库是 `journal_mode=wal`（实测 workbuddy 板），而 `-wal` 文件
    不在备份范围内——只拷 `.db` 会丢掉还在 WAL 里、尚未 checkpoint 的已提交事务
    （restore 后表现为"最近的任务状态没了"）。在线备份 API 会读穿 WAL，产出自包含快照。
    顺带把空闲页压实（`backup()` 写出的是紧凑库），体积通常小于原文件。
    """
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".snapshot-tmp")
        try:
            tmp.unlink()
        except OSError:
            pass
        src_con = sqlite3.connect("file:%s?mode=ro" % src.as_posix(), uri=True, timeout=30)
        try:
            out = sqlite3.connect(str(tmp))
            try:
                src_con.backup(out)
            finally:
                out.close()
        finally:
            src_con.close()
        os.replace(tmp, dst)
        return True
    except Exception as e:
        log(f"!! 快照失败 {src.name}: {e!r}（回退普通拷贝）")
        try:
            return copy_file(src, dst)
        except Exception:
            return False


def copy_kanban_dbs(src: Path, dst: Path) -> int:
    """看板**只**备份每个板的 `boards/<board>/kanban.db`（+ 顶层 `kanban.db` 若有）。

    实测（2026-10-25）：旧实现用 `rglob("*.db")`，把任务工作区/证据/备份目录里的 73 个
    `school80.db` 之类也一起备了（镜像里 84 个 db / 36MB），既违背"工作区属过程记录、不入云"
    的政策，又让每轮多快照 70+ 个无关库。这里按白名单取，工作区里的自然落到 prune。
    """
    n = 0
    targets: list[Path] = []
    boards = src / "boards"
    if boards.is_dir():
        for board in sorted(boards.iterdir()):
            if not board.is_dir():
                continue
            db = board / "kanban.db"
            if db.is_file():
                targets.append(db)
            elif board.name == "_archived":
                # 归档板是 boards/_archived/<board>/kanban.db —— 也是看板数据，照常备份
                for sub in sorted(board.iterdir()):
                    adb = sub / "kanban.db"
                    if sub.is_dir() and adb.is_file():
                        targets.append(adb)
    top = src / "kanban.db"
    if top.is_file():
        targets.append(top)
    for db in targets:
        target = dst / db.relative_to(src)
        # 关键：必须登记为"本轮已访问"，否则 prune_unvisited 会把它当残留删掉
        # （2026-10-25 实案：直接调 snapshot_sqlite 绕过了 copy_file 的登记，镜像里 11 个板库被清空）
        _COPIED.add(str(target))
        try:
            if target.is_file() and target.stat().st_size == db.stat().st_size and target.read_bytes() == db.read_bytes():
                n += 1
                continue
        except OSError:
            pass
        if snapshot_sqlite(db, target):
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


def stabilize_line(raw: bytes) -> bytes:
    """把易变统计字段挪到行尾，使「追加消息」只改动文件尾部。

    见 scripts/session_layout.py 的机理说明：会话是一行超长 JSON，头部只要有一个统计字段
    （message_count / token 计数 / timings …）变化，整行字节全变 ⇒ 所有分片都成新 blob
    （2026-10-24 实测单轮 827MB 重写）。稳定化之后，未变的会话字节完全一致、有追加的只改末尾。

    解析失败（截断/非对象）一律原样返回，绝不丢数据。
    """
    try:
        from session_layout import stabilize       # 同目录模块
    except Exception:
        return raw
    # 内存守卫：解码+重排 243MB 的会话要 ~1.7GB 峰值；可用内存不够就本轮跳过（下轮再试），
    # 不要赌 MemoryError——那会在导出阶段把内存压力传导给整台机器。
    try:
        import psutil as _ps
        if _ps.virtual_memory().available < 2.4 * len(raw):
            return raw
    except Exception:
        pass
    try:
        text = raw.decode("utf-8")
        out = stabilize(text)
    except MemoryError:
        return raw
    except Exception:
        return raw
    return raw if out == text else out.encode("utf-8")


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
            if len(ln) > STABILIZE_MIN_BYTES:
                ln = stabilize_line(ln)
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
            bp = subprocess.Popen([sys.executable, str(runner), str(helper),
                                   "--home", str(HERMES_HOME), "--out", str(rawdir)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                out, err = bp.communicate(timeout=2000)   # 导出阶段预算 33 分钟
            except subprocess.TimeoutExpired:
                # subprocess.run 的超时只杀直接子进程（run_in_runtime 启动器），真正的导出进程
                # 会活下来占住镜像与各档 state.db —— 下一轮再起一个，两个导出同时写同一批文件
                # （2026-10-24 实案：连续两轮 3300s 超时，孤儿持 ceo/state.db、RSS 1.6GB）。
                killed = _kill_tree(bp)
                try:
                    bp.communicate(timeout=15)
                except Exception:
                    pass
                raise RuntimeError(f"export_timeout（批量导出超过 2000s 预算；已杀进程树 {killed} 个）")
            if bp.returncode != 0:
                bulk_ok = False
                log(f"批量导出失败 rc={bp.returncode}：{(out or err or '').strip()[-300:]}")
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
def _git_env() -> dict:
    """git 网络操作的 env：给 SSH 加保活。

    长传输被 GitHub 掐断时，没有保活的 ssh 会静默挂死（进程在、零字节、永不返回）。
    """
    env = os.environ.copy()
    if not env.get("GIT_SSH_COMMAND"):
        env["GIT_SSH_COMMAND"] = ("ssh -o ServerAliveInterval=20 -o ServerAliveCountMax=30 "
                                  "-o TCPKeepAlive=yes")
    return env


def _kill_tree(proc: subprocess.Popen) -> int:
    """杀掉进程树（含孙子）。

    `subprocess` 的超时只杀直接子进程；push 的孙子（git pack-objects / ssh）会留下来占住
    管道，于是**之后每次重试都零字节秒卡**（2026-10-23 实案：一轮推 10 分钟零字节）。
    """
    killed = 0
    try:
        import psutil
    except Exception:
        try:
            proc.kill()
            return 1
        except Exception:
            return 0
    try:
        parent = psutil.Process(proc.pid)
        kids = parent.children(recursive=True)
    except Exception:
        kids = []
    for p in kids:
        try:
            p.kill()
            killed += 1
        except Exception:
            pass
    if proc.poll() is None:
        try:
            proc.kill()
            killed += 1
        except Exception:
            pass
    return killed


def _reap_orphan_exporters() -> list[int]:
    """回收**父进程已消失**的批量导出进程（超时只杀直接子进程留下的孙子）。

    识别条件（全部满足才算，避免误杀"命令行里只是提到文件名"的进程）：
    ① python 解释器；② 命令行含 ``hermes-cloud-backup`` 与 ``export_all_profiles.py``；
    ③ 带 ``--home`` 与 ``--out``（真实调用形态）；④ 父进程已不存在。
    导出会写镜像与读各档 state.db，留着它＝下一轮两个导出进程同时写同一批文件
    （2026-10-24 实案：连续两轮 3300s 超时）。
    """
    killed: list[int] = []
    try:
        import psutil
    except Exception:
        return killed
    me = os.getpid()
    try:
        procs = list(psutil.process_iter(["pid", "ppid", "name", "cmdline"]))
    except Exception:
        return killed
    for p in procs:
        try:
            if (p.info["name"] or "").lower() not in ("python.exe", "python"):
                continue
            argv = p.info["cmdline"] or []
            # 必须是**真实参数**里带脚本路径与 --home/--out，而不是某个 -c 代码串里提到这些字眼
            if not any(str(a).endswith("export_all_profiles.py") for a in argv[1:]):
                continue
            if not ("--home" in argv and "--out" in argv and "hermes-cloud-backup" in " ".join(argv)):
                continue
            if p.info["pid"] == me:
                continue
            ppid = p.info["ppid"]
            if ppid and psutil.pid_exists(ppid):
                continue
            p.kill()
            killed.append(int(p.info["pid"]))
        except Exception:
            continue
    return killed


def _reap_orphan_git() -> list[int]:
    """回收本仓的**孤儿** git/ssh 进程（父进程已消失的那些）。

    只认命令行里带本镜像路径、或带 `ssh.github.com … git-receive-pack` 的进程，且只在
    父进程不存在时动手——不会碰其它 agent 的正常 git 操作。
    """
    killed: list[int] = []
    try:
        import psutil
    except Exception:
        return killed
    mirror_s = str(MIRROR)
    for p in psutil.process_iter(["pid", "name", "cmdline", "ppid"]):
        try:
            name = (p.info["name"] or "").lower()
            if name not in ("git.exe", "git", "ssh.exe", "ssh"):
                continue
            cl = " ".join(p.info["cmdline"] or [])
            if not (mirror_s in cl or "hermes-backup" in cl
                    or ("ssh.github.com" in cl and "git-receive-pack" in cl)):
                continue
            ppid = p.info["ppid"] or 0
            try:
                alive = psutil.Process(ppid).is_running() if ppid > 1 else False
            except Exception:
                alive = False
            if alive:
                continue
            p.kill()
            killed.append(p.info["pid"])
        except Exception:
            continue
    return killed


def git(*args: str, check: bool = True, timeout: float | None = None) -> subprocess.CompletedProcess:
    proc = subprocess.Popen(["git", "-C", str(MIRROR), *args], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env=_git_env())
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        n = _kill_tree(proc)
        try:
            out, err = proc.communicate(timeout=30)
        except Exception:
            out, err = "", ""
        orphans = _reap_orphan_git()
        raise RuntimeError("git %s 超时（预算 %ss；已杀进程树 %d 个，回收孤儿 %d 个）：%s"
                           % (" ".join(args), timeout, n, len(orphans), (err or out or "").strip()[:300]))
    p = subprocess.CompletedProcess(proc.args, proc.returncode, out, err)
    if p.returncode != 0:
        orphans = _reap_orphan_git()          # 失败后清掉残留孙子，避免下一次重试秒卡
        if orphans:
            log("git %s 失败后回收孤儿进程 %d 个" % (" ".join(args[:2]), len(orphans)))
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
    # 推送：最多 3 次（真实失败模式是 GitHub 侧「传输超 ~6–8 分钟即掐断」与 fake-ip 偶发 RST，
    # 两者都是瞬态、重试可过）。每次失败先 pull --no-rebase 对齐线上（非破坏，冲突即中止），
    # 再按 20s / 60s 退避重试；本地提交始终保留，下一轮自然补推。
    attempts = 3
    last_err = ""
    for attempt in range(1, attempts + 1):
        p = git("push", "-u", "origin", "main", check=False, timeout=PUSH_TIMEOUT)
        if p.returncode == 0:
            break
        last_err = (p.stderr or p.stdout).strip()
        log(f"push 失败（第 {attempt}/{attempts} 次）：{last_err[:200]}")
        if attempt == attempts:
            raise RuntimeError(f"push 失败（{attempts} 次均失败）：{last_err[:400]}")
        pull = git("pull", "--no-rebase", "origin", "main", check=False, timeout=600)
        if pull.returncode != 0:
            git("merge", "--abort", check=False)
            raise RuntimeError(f"pull/merge 失败：{(pull.stderr or pull.stdout).strip()[:400]}")
        wait = 20 if attempt == 1 else 60
        log(f"退避 {wait}s 后重试推送")
        time.sleep(wait)
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
    ap.add_argument("--repo-name", default="",
                    help="仓库名（默认取技能目录 local.json 的 repo；都没有则 hermes-backup。"
                         "镜像已有 origin 时一律以 origin 为准——换代只需改 origin/local.json）")
    ap.add_argument("--remote", default="", help="显式指定 remote URL")
    ap.add_argument("--create-remote", action="store_true",
                    help="远端不存在时用 gh 创建私有仓")
    args = ap.parse_args()

    if not acquire_lock():
        return 0

    _orphan_exports = _reap_orphan_exporters()
    if _orphan_exports:
        log(f"回收孤儿导出进程：{_orphan_exports}（父进程已消失，避免与本轮同时写镜像）")

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
HEALTH_FILE = HERMES_HOME / "state" / "hb-sync-health.json"
ATTENTION_MARKER = HERMES_HOME / "state" / "hb-sync-needs-attention"


def _record_health(exit_code: int, line: dict) -> None:
    """连续性记账的**单一来源**：任何入口（cron 包装 / 哨兵 ①②④ / 手工）跑完都会更新。

    失败 = exit_code != 0。连续 >=3 轮失败写"待查看"标记（不推送、不打扰）；
    一旦出现成功轮即清零并删掉标记——标记描述的是"当前是否仍处于连续失败状态"，
    条件消失就不该继续留着（否则旧标记会伪装成新故障）。
    """
    try:
        data = json.loads(HEALTH_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    n = 0 if exit_code == 0 else int(data.get("consecutive_failures") or 0) + 1
    data.update({
        "consecutive_failures": n,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "last_exit_code": exit_code,
        "last_start_ts": line.get("start_ts"),
        "last_end_ts": line.get("end_ts"),
        "last_duration_s": line.get("duration_s"),
    })
    # 成功轮必须清掉上一轮的 last_error：否则历史错误串会一直挂着，被读成"当前仍在失败"
    if exit_code == 0:
        data["last_error"] = None
    try:
        HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = HEALTH_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, HEALTH_FILE)
    except OSError as e:
        log(f"!! 写健康计数失败：{e!r}")
        return
    try:
        if n >= 3:
            ATTENTION_MARKER.write_text(
                "连续 %d 轮同步失败（最后一轮 exit=%s @ %s，用时 %ss）；明细：%s\n"
                % (n, exit_code, line.get("end_ts"), line.get("duration_s"), RUNS_LOG),
                encoding="utf-8")
        elif ATTENTION_MARKER.exists():
            ATTENTION_MARKER.unlink()
    except OSError:
        pass


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
        _record_health(exit_code, line)
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
