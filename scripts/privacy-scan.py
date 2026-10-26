# -*- coding: utf-8 -*-
"""privacy-scan.py — 推送前的公开面自检（三类：凭据 / 真实私人信息 / 本机绝对路径）。

约定（owner 2026-10-26）：私有仓日常 push 不必请示；**公开 / Release / npm / 加协作者或部署密钥**
才是需要授权的动作。但无论公开还是私有，推前都要过这三道——私有仓迟早可能公开，历史删不干净。

用法:
    python scripts/privacy-scan.py                     # 扫技能目录本身（发布前最常用）
    python scripts/privacy-scan.py --repo D:/path/repo # 扫某个 git 仓的**被跟踪文件**
    python scripts/privacy-scan.py --repo . --history   # 连全历史一起扫（git log -p）
    python scripts/privacy-scan.py --terms-file local.json --allow-data-repo
    python scripts/privacy-scan.py --json out.json

判定：命中任何一条 → 退出码 2，并打印 file:line 与命中片段（片段截断，避免把秘密打进日志）。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

# ① 凭据类文件名（被跟踪就算命中）
CRED_NAME = re.compile(r"(^|/)(\.env(\..*)?|.*\.(pem|p12|pfx|key)|.*token.*|.*secret.*|.*credential.*)$", re.I)
# ② 通用真实私人信息模式（真实姓名靠 --terms 或 local.json 的 private_terms，绝不硬编码进仓）
PII_PATTERNS = [
    ("身份证号", re.compile(r"(?<![0-9A-Za-z])[1-9][0-9]{16}[0-9Xx](?![0-9A-Za-z])")),
    ("手机号", re.compile(r"(?<![0-9])1[3-9][0-9]{9}(?![0-9])")),
    ("邮箱", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
]
# ③ 本机绝对路径（占位符 %LOCALAPPDATA% / $HOME / ~ 不算）
ABS_PATH = re.compile(r"(?:[A-Za-z]:[\\/]Users[\\/][A-Za-z0-9._-]+|/home/[a-z][a-z0-9_-]{1,32}/)")
SKIP_DIRS = {".git", "__pycache__", ".venv", "node_modules", ".pytest_cache"}


def _iter_files(root: Path):
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.relative_to(root).parts):
            continue
        if p.name == "local.json":          # 机器本地配置：不入库，也不扫（含本机路径是正常的）
            continue
        yield p


def _tracked(repo: Path) -> list[Path]:
    out = subprocess.run(["git", "-C", str(repo), "ls-files", "-z"], capture_output=True, check=True).stdout
    return [repo / p for p in out.decode("utf-8", "replace").split("\0") if p]


def _lines_of(path: Path) -> list[str]:
    try:
        if path.stat().st_size > 4 * 1024 * 1024:      # 大二进制/大文件不逐行扫
            return []
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


# 这些长得像邮箱但不是"个人信息"：git SSH 地址、noreply、文档占位域名
EMAIL_NOT_PII = re.compile(r"^(git@|ssh@|.*@users\.noreply\.github\.com$|.*@example\.(com|org|net)$|.*@localhost$)", re.I)


def _email_is_pii(addr: str) -> bool:
    return not EMAIL_NOT_PII.match(addr)


def scan_files(files: list[Path], base: Path, terms: list[str]) -> list[dict]:
    hits: list[dict] = []
    for f in files:
        rel = str(f.relative_to(base)) if f.is_relative_to(base) else str(f)
        if CRED_NAME.search(rel.replace("\\", "/")) and not rel.endswith(".md"):
            hits.append({"class": "① 凭据文件", "file": rel, "line": 0, "snippet": rel})
            continue
        for i, line in enumerate(_lines_of(f), 1):
            low = line.lower()
            if ABS_PATH.search(line):
                hits.append({"class": "③ 本机绝对路径", "file": rel, "line": i,
                             "snippet": ABS_PATH.search(line).group(0)[:80]})
            for name, pat in PII_PATTERNS:
                m = pat.search(line)
                if m and (name != "邮箱" or _email_is_pii(m.group(0))):
                    hits.append({"class": f"② {name}", "file": rel, "line": i,
                                 "snippet": m.group(0)[:40]})
            for t in terms:
                if t and t.lower() in low:
                    hits.append({"class": "② 禁用词", "file": rel, "line": i, "snippet": t[:40]})
    return hits


def scan_history(repo: Path, terms: list[str]) -> list[dict]:
    """扫全历史的 diff 文本（只看命中行，不落盘）。"""
    hits: list[dict] = []
    try:
        out = subprocess.run(["git", "-C", str(repo), "log", "--all", "-p", "--unified=0"],
                             capture_output=True, timeout=600).stdout.decode("utf-8", "replace")
    except Exception as exc:                      # noqa: BLE001
        return [{"class": "历史扫描失败", "file": str(repo), "line": 0, "snippet": repr(exc)[:80]}]
    for i, line in enumerate(out.splitlines(), 1):
        if not line.startswith("+") or line.startswith("+++"):
            continue
        body = line[1:]
        m = ABS_PATH.search(body)
        if m:
            hits.append({"class": "③ 历史·本机路径", "file": f"diff:{i}", "line": i, "snippet": m.group(0)[:80]})
        for name, pat in PII_PATTERNS:
            mm = pat.search(body)
            if mm and (name != "邮箱" or _email_is_pii(mm.group(0))):
                hits.append({"class": f"② 历史·{name}", "file": f"diff:{i}", "line": i, "snippet": mm.group(0)[:40]})
        for t in terms:
            if t and t.lower() in body.lower():
                hits.append({"class": "② 历史·禁用词", "file": f"diff:{i}", "line": i, "snippet": t[:40]})
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="", help="要扫的 git 仓（默认扫技能目录本身）")
    ap.add_argument("--history", action="store_true", help="连全历史一起扫（慢，发布前跑一次）")
    ap.add_argument("--terms-file", default="", help="含真实姓名等禁用词的文件（JSON：{private_terms:[...]} 或纯文本一行一词）")
    ap.add_argument("--allow-data-repo", action="store_true",
                    help="数据备份仓：.env / 密钥**就是**备份对象，①类不判（其余两类照判）")
    ap.add_argument("--json", default="")
    a = ap.parse_args()

    terms: list[str] = []
    if a.terms_file:
        tf = Path(a.terms_file)
        if tf.is_file():
            raw = tf.read_text(encoding="utf-8")
            try:
                data = json.loads(raw)
                terms = [str(x) for x in (data.get("private_terms") or [])]
            except Exception:                     # noqa: BLE001
                terms = [ln.strip() for ln in raw.splitlines() if ln.strip()]

    if a.repo:
        repo = Path(a.repo).resolve()
        files = _tracked(repo)
        base = repo
    else:
        repo, base = ROOT, ROOT
        files = list(_iter_files(ROOT))

    hits = scan_files(files, base, terms)
    if a.allow_data_repo:
        hits = [h for h in hits if not h["class"].startswith("①")]
    if a.history:
        hits += scan_history(repo, terms)

    print(f"== privacy-scan：{base} ==")
    print(f"  扫描 {len(files)} 个文件" + ("（含全历史）" if a.history else "") +
          (f"；禁用词 {len(terms)} 个" if terms else ""))
    if not hits:
        print("PRIVACY-OK 三类零命中")
        if a.json:
            Path(a.json).write_text(json.dumps({"hits": [], "files": len(files)}, ensure_ascii=False, indent=1),
                                    encoding="utf-8")
        return 0
    for h in hits[:80]:
        print(f"  ✗ [{h['class']}] {h['file']}:{h['line']}  {h['snippet']}")
    if len(hits) > 80:
        print(f"  … 另有 {len(hits) - 80} 条")
    print(f"PRIVACY-FAIL 命中 {len(hits)} 条")
    if a.json:
        Path(a.json).write_text(json.dumps({"hits": hits, "files": len(files)}, ensure_ascii=False, indent=1),
                                encoding="utf-8")
    return 2


if __name__ == "__main__":
    sys.exit(main())
