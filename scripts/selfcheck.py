# -*- coding: utf-8 -*-
"""selfcheck.py — 一条命令跑完 hermes-backup 的全部回归与一致性检查。

用法:
    python scripts/selfcheck.py                          # 默认（约 1 分钟）：编译 + 全部 test-*.py + 文档引用
    python scripts/selfcheck.py --repo ~/hermes-backup  # 追加镜像分片体检（+几秒）
    python scripts/selfcheck.py --repo ~/hermes-backup --audit   # 追加会话层覆盖率审计（2–5 分钟）
    python scripts/selfcheck.py --json out.json

退出码: 0 = 全绿；2 = 有失败（明细在正文）。
设计约束: **只读**——不改镜像、不写 HERMES_HOME、不联网；每项都打印自己的 rc 与末行。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
from datetime import datetime
import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PY = sys.executable
# 这些是 HERMES_HOME 里的数据文件名（文档会按名字提到），不是仓内文件——不算悬空引用
EXTERNAL_NAMES = {
    "SOUL.md", "config.yaml", ".env", "auth.json", "profile.yaml",
    "channel_directory.json", "projects.db", "shared-state.db", "kanban.db",
    "sessions.json", "gateway_routing.json", "runs.jsonl",
    # 别的技能里的脚本（本技能文档会指过去，不是本仓文件）
    "session-ctl.py",
}


def run(cmd: list[str], timeout: int = 300) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, cwd=str(ROOT))
        out = (p.stdout or "") + (p.stderr or "")
        last = next((ln.strip() for ln in reversed(out.strip().splitlines()) if ln.strip()), "")
        return p.returncode, last
    except subprocess.TimeoutExpired:
        return 124, f"超时 {timeout}s"
    except Exception as exc:                     # noqa: BLE001
        return 125, f"{type(exc).__name__}: {exc}"


def step(name: str, rc: int, detail: str, results: list) -> None:
    ok = rc == 0
    results.append({"name": name, "ok": ok, "rc": rc, "detail": detail})
    print(("  ✓ " if ok else "  ✗ ") + f"{name:<34} rc={rc:<3} {detail[:80]}", flush=True)



def _warn_if_sync_running() -> None:
    """有同步轮在跑时提醒：测试会写上百 MB、读全部会话库，可能把那一轮拖到超时。
    （2026-10-26 实案：自检与批量导出并发，导出子进程卡在启动阶段 28 分钟。）"""
    import os
    lock = Path(os.environ.get("HERMES_HOME") or (Path(os.environ.get("LOCALAPPDATA", "")) / "hermes")) \
        / "state" / "hermes-backup-sync.lock"
    if not lock.is_file():
        return
    try:
        import json as _json
        pid = int(_json.loads(lock.read_text(encoding="utf-8")).get("pid") or 0)
    except Exception:  # noqa: BLE001
        return
    live = False
    try:
        import psutil
        live = pid > 0 and psutil.pid_exists(pid)
    except Exception:  # noqa: BLE001
        live = False
    if live:
        print(f"!! 有一轮同步正在进行（pid {pid}）：本自检会抢磁盘，可能把它拖到超时——建议等它跑完。",
              flush=True)


def compile_all(results: list) -> None:
    bad = []
    for f in sorted(HERE.glob("*.py")):
        if f.name == "selfcheck.py":
            continue
        p = subprocess.run([PY, "-m", "py_compile", str(f)], capture_output=True, text=True, encoding="utf-8", errors="replace")
        if p.returncode != 0:
            bad.append((f.name, (p.stderr or "").strip().splitlines()[-1:]))
    step("编译全部脚本", 0 if not bad else 2, f"{len(list(HERE.glob('*.py'))) - 1} 个文件" if not bad else str(bad[:2]), results)


def run_tests(results: list) -> None:
    tests = sorted(HERE.glob("test-*.py"))
    if not tests:
        # 0 套测试原来算「没东西可跑」⇒ PASS（2026-10-31 审计发现）
        step("回归测试", 2, "scripts/ 下没有 test-*.py —— 0 套不能算通过", results)
        return
    for t in tests:
        rc, last = run([PY, str(t)])
        step(f"测试 {t.name}", rc, last, results)


def doc_refs(results: list) -> None:
    """文档里反引号引用的仓内文件必须存在。

    判定分两种：带目录的按相对路径核；**只有文件名的**（如 `churn-report.py`）在本仓
    scripts/ references/ templates/ 根目录里任一位置存在即可——文档常按文件名提，不该逼它写全路径。
    外部路径（`D:/…`、`HERMES_HOME/…`、`tools/…`）跳过不判。
    """
    docs = [ROOT / "SKILL.md", ROOT / "README.md", *sorted((ROOT / "references").glob("*.md")),
            *sorted((ROOT / "templates").glob("*.md"))]
    pat = re.compile(r"`([A-Za-z0-9_./\-]+\.(?:py|sh|md|ps1))`")
    known = {f.name for d in (ROOT, ROOT / "scripts", ROOT / "references", ROOT / "templates")
             if d.is_dir() for f in d.iterdir() if f.is_file()}
    missing, external, bare = [], 0, 0
    for d in docs:
        if not d.is_file():
            continue
        for m in pat.finditer(d.read_text(encoding="utf-8")):
            rel = m.group(1)
            if rel.startswith(("D:", "HERMES_HOME/", "tools/")) or ":" in rel:
                external += 1
                continue
            if "/" not in rel:
                bare += 1
                if rel in EXTERNAL_NAMES:
                    external += 1
                    continue
                if rel not in known:
                    missing.append(f"{d.name}:{rel}")
                continue
            if not rel.startswith(("scripts/", "references/", "templates/")):
                external += 1
                continue
            if not (ROOT / rel).is_file():
                missing.append(f"{d.name}:{rel}")
    step("文档引用完整性", 0 if not missing else 2,
         (f"{len(docs)} 份文档，0 处悬空引用（文件名 {bare} / 外部 {external} 已按规则豁免）" if not missing
          else f"{len(missing)} 处: {missing[:3]}"), results)


def privacy_check(results: list) -> None:
    """推前三道（凭据 / 真实私人信息 / 本机绝对路径）——见 scripts/privacy-scan.py。"""
    rc, last = run([PY, str(HERE / "privacy-scan.py")], timeout=180)
    step("公开面卫生（凭据/隐私/路径）", rc, last, results)


def cron_check(results: list) -> None:
    """自动同步的**调度**在不在册、最近有没有真的跳。

    2026-10-31 实案两连：换机后 `cron/jobs.json` 是空的（脚本都在、就是没人调度），
    以及 shrink-guard 静默标记把自动轮压了 5 小时——两次 `cron list` 都显示正常。
    所以这里只认两样：①jobs.json 里有这两个任务；②executions.db 里哨兵最近 15 分钟
    有 completed 记录。缺任一条都判红，并直接给出修复命令。
    """
    home = Path(os.environ.get("HERMES_HOME") or (Path.home() / "AppData" / "Local" / "hermes"))
    jobs_file = home / "cron" / "jobs.json"
    want = ("hermes-backup 哨兵", "hermes-backup 周期同步")
    try:
        raw = json.loads(jobs_file.read_text(encoding="utf-8"))
        jobs = raw if isinstance(raw, list) else raw.get("jobs", [])
    except (OSError, ValueError) as exc:
        step("自动同步调度", 2, f"读不到 {jobs_file}：{exc}", results)
        return
    names = {str(j.get("name", "")) for j in jobs}
    missing = [w for w in want if w not in names]
    if missing:
        step("自动同步调度", 2,
             f"缺任务 {missing}（脚本在但没人调度）—— 用 hermes cron create 按 references/auto-sync.md 重建",
             results)
        return
    # 最近有没有真的跳：读 executions.db 里哨兵任务的行
    db = home / "cron" / "executions.db"
    if not db.exists():
        step("自动同步调度", 2, f"没有 {db}（cron 从未跑过？）", results)
        return
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        row = con.execute(
            "select finished_at, status from executions where job_id in "
            "(select job_id from executions group by job_id) order by rowid desc limit 1").fetchone()
        sent = con.execute(
            "select finished_at, status from executions where job_id=? order by rowid desc limit 1",
            (next((j.get("id") for j in jobs if j.get("name") == want[0]), ""),)).fetchone()
        con.close()
    except sqlite3.Error as exc:
        step("自动同步调度", 2, f"executions.db 读取失败：{exc}", results)
        return
    if not sent:
        step("自动同步调度", 2, "哨兵任务没有任何执行记录（建了但没跳）", results)
        return
    try:
        age_min = (datetime.now().astimezone()
                   - datetime.fromisoformat(sent[0])).total_seconds() / 60
    except Exception:
        age_min = 999
    # ② 两任务必须 enabled（pause 掉也还在册，但不会跑）
    disabled = [j.get("name") for j in jobs if j.get("name") in want and j.get("enabled") is False]
    # ③ 状态标记：这两个存在就说明「上一轮被拦/连续失败」，绿灯会掩盖真问题
    flags = [n for n in ("hb-sync-guard-blocked.json", "hb-sync-needs-attention")
             if (home / "state" / n).exists()]
    if disabled:
        step("自动同步调度", 2, f"任务被停用：{disabled}（cron 里 active=false 不会跑）", results)
        return
    if flags:
        step("自动同步调度", 2,
             f"存在状态标记 {flags} —— 自动轮被拦或连续失败中（成功一轮会自动清除）", results)
        return
    if age_min > 15:
        step("自动同步调度", 2, f"哨兵最近一次执行在 {age_min:.0f} 分钟前（>15）—— 调度可能已停", results)
    else:
        step("自动同步调度", 0, f"两任务在册且启用；哨兵 {age_min:.0f} 分钟前 {sent[1]}", results)


def runtime_copy_check(results: list) -> None:
    """`HERMES_HOME/scripts` 里的运行副本必须与技能源一致（sync.py 例外，它是转调壳）。

    2026-11-01 审计发现：restore_sessions.py / run_in_runtime.py / session_layout.py 三份副本
    比技能源旧，却没人管——cron 与人工都可能调到旧逻辑。
    """
    home = Path(os.environ.get("HERMES_HOME") or (Path.home() / "AppData" / "Local" / "hermes"))
    rt = home / "scripts"
    if not rt.is_dir():
        step("运行副本一致性", 0, "HERMES_HOME/scripts 不存在（未安装自动同步，跳过）", results)
        return
    drift = []
    for src in sorted(HERE.glob("*.py")):
        dst = rt / src.name
        if not dst.exists():
            continue
        if src.name == "sync.py":
            # 必须是壳：小于 4KB 且含 runpy
            body = dst.read_text(encoding="utf-8", errors="replace")
            if dst.stat().st_size > 4096 or "runpy" not in body:
                drift.append(f"{src.name}(应为转调壳，实为 {dst.stat().st_size}B)")
            continue
        if hashlib.sha256(src.read_bytes()).hexdigest() != hashlib.sha256(dst.read_bytes()).hexdigest():
            drift.append(src.name)
    if drift:
        step("运行副本一致性", 2, f"与技能源不一致：{drift[:5]}", results)
    else:
        step("运行副本一致性", 0, "全部同名脚本一致（sync.py 为转调壳）", results)


def psutil_check(results: list) -> None:
    """psutil 不是标准库——缺了内存闸会自动降级，必须让人知道。"""
    try:
        import psutil as _ps
        step("psutil 可用性", 0, f"{_ps.__version__}（内存闸/进程回收可用）", results)
    except Exception as exc:  # noqa: BLE001
        step("psutil 可用性", 0,
             f"缺失（{type(exc).__name__}）——内存闸会降级为逐档 CLI，账本会标 memory_gate_unavailable",
             results)


def preserve_check(repo: str, results: list) -> None:
    """退役档（local.json 的 preserve_sessions）在镜像里必须还在。

    2026-10-31 实案：16 个退役 bot 档的会话文件被「孤儿清理」删掉 1,339 个（1.2GB），
    而这些档在 local.json 里明明是 preserve_sessions——清单只保护了 prune 路径，
    保护不了别的清理工具。这里用**基线条数**（local.json 的 preserve_baseline）卡住：
    任何一档的文件数少于基线 ⇒ 判红，附「从哪个提交取回」的提示。
    """
    cfg_path = ROOT / "local.json"
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        step("退役档完整性", 2, f"读不到 local.json: {exc}", results)
        return
    base = cfg.get("preserve_baseline") or {}
    if not base:
        step("退役档完整性", 0, "未设 preserve_baseline（跳过）", results)
        return
    lost = []
    for name, expect in sorted(base.items()):
        out = subprocess.run(["git", "-C", repo, "ls-files", f"sessions/{name}"],
                             capture_output=True, text=True, encoding="utf-8", errors="replace")
        cur = len([x for x in (out.stdout or "").splitlines() if x.strip()])
        if cur < int(expect):
            lost.append(f"{name} {cur}/{expect}")
    if lost:
        step("退役档完整性", 2,
             f"{len(lost)} 档少于基线: {lost[:4]}（取回：git -C <镜像> checkout <提交> -- sessions/<档>）",
             results)
    else:
        step("退役档完整性", 0, f"{len(base)} 档均不低于基线", results)


def parts_check(repo: str, results: list) -> None:
    rc, last = run([PY, str(HERE / "check-parts.py"), "--repo", repo])
    step("镜像分片体检", rc, last, results)


def coverage_audit(repo: str, home: str, results: list) -> None:
    rc, last = run([PY, str(HERE / "audit-coverage.py"), "--repo", repo, "--home", home], timeout=900)
    step("会话层覆盖率审计", rc, last, results)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="", help="镜像仓目录（给了才跑退役档完整性 + 分片体检；覆盖率审计还要再加 --audit）")
    ap.add_argument("--home", default="", help="活库 HERMES_HOME（默认按环境变量/标准位置推断）")
    ap.add_argument("--audit", action="store_true",
                    help="额外跑会话层覆盖率审计（读全部会话文件，2–5 分钟；需要 --repo）")
    ap.add_argument("--json", default="", help="把结果写成 JSON")
    a = ap.parse_args()

    t0 = time.time()
    print(f"== selfcheck（技能 {ROOT.name}）==", flush=True)
    _warn_if_sync_running()
    results: list = []
    compile_all(results)
    run_tests(results)
    doc_refs(results)
    privacy_check(results)
    cron_check(results)
    runtime_copy_check(results)
    psutil_check(results)
    if a.repo:
        if Path(a.repo).is_dir():
            preserve_check(a.repo, results)
            parts_check(a.repo, results)
            if a.audit:
                home = a.home or str(Path.home() / "AppData" / "Local" / "hermes")
                if Path(home).is_dir():
                    coverage_audit(a.repo, home, results)
                else:
                    step("会话层覆盖率审计", 2, f"活库不存在: {home}", results)
        else:
            step("镜像检查", 2, f"--repo 不存在: {a.repo}", results)

    failed = [r for r in results if not r["ok"]]
    print(f"\n== 合计 {len(results)} 项，失败 {len(failed)}，用时 {time.time() - t0:.1f}s ==")
    for r in failed:
        print(f"  - {r['name']}: rc={r['rc']} {r['detail'][:120]}")
    if a.json:
        Path(a.json).write_text(json.dumps({"results": results, "failed": len(failed)},
                                           ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"明细已写：{a.json}")
    print("SELFCHECK-%s" % ("PASS" if not failed else "FAIL"))
    return 0 if not failed else 2


if __name__ == "__main__":
    sys.exit(main())
