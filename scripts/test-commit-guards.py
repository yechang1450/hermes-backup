# -*- coding: utf-8 -*-
"""提交期两道闸的回归：批量删除闸（MassDeleteError）与「档目录还在就不整档删」。

2026-10-31 审计发现：
  · 只按本轮 wanted 判「档已删除」，`profile.yaml` 暂时缺失/改名就会 rmtree 整档历史；
  · 采集期任何一层被整批清掉时，原来照样提交并推送（2,311 会话 + 8,284 profile 文件的实案）。
用法：python scripts/test-commit-guards.py（纯标准库；用假 git，不碰真仓、不联网）
"""
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import sync  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(("PASS  " + name + (("  | " + detail) if detail else "")).rstrip())
    else:
        FAIL += 1
        print(("FAIL  " + name + (("  | " + detail) if detail else "")).rstrip())


class _P:
    def __init__(self, out="", rc=0):
        self.stdout, self.stderr, self.returncode = out, "", rc


def fake_git(deleted, tracked):
    """按参数返回假的 git 输出；只模拟 commit_and_push 用到的四个子命令。"""
    def _git(*args, **kw):
        if args[:1] == ("add",):
            return _P()
        if args[:2] == ("diff", "--cached"):
            return _P("\n".join(deleted) + ("\n" if deleted else ""))
        if args[:1] == ("ls-files",):
            return _P("\n".join(tracked) + ("\n" if tracked else ""))
        if args[:2] == ("status", "--porcelain"):
            return _P("M x\n" if deleted else "")
        if args[:1] == ("rev-parse",):
            return _P("main\n")
        return _P()
    return _git


def run_commit_case(deleted, tracked):
    orig = (sync.MIRROR, sync.git, sync.ensure_repo, sync.purge_index_lock, sync.REMOTE_URL)
    try:
        sync.git = fake_git(deleted, tracked)
        sync.ensure_repo = lambda: None
        sync.purge_index_lock = lambda *a, **k: False
        sync.REMOTE_URL = "https://example.invalid/x.git"
        sync.commit_and_push("t", no_push=True)
        return None
    except sync.MassDeleteError as e:
        return e
    finally:
        sync.MIRROR, sync.git, sync.ensure_repo, sync.purge_index_lock, sync.REMOTE_URL = orig


def main() -> int:
    # A. 批量删除闸
    e = run_commit_case([f"f{i}" for i in range(150)], [f"f{i}" for i in range(400)])
    check("A1 删 150/400（37%）⇒ 拦下", isinstance(e, sync.MassDeleteError), str(e)[:60])
    e = run_commit_case([f"f{i}" for i in range(50)], [f"f{i}" for i in range(400)])
    check("A2 删 50/400（12.5%）⇒ 放行", e is None)
    e = run_commit_case([f"f{i}" for i in range(150)], [f"f{i}" for i in range(2000)])
    check("A3 删 150/2000（7.5%）⇒ 放行", e is None)
    e = run_commit_case([], [f"f{i}" for i in range(400)])
    check("A4 无删除 ⇒ 放行", e is None)

    # B. 整档删除授权：活库里目录还在就不许删
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        (home / "profiles" / "proj-80").mkdir(parents=True)
        (home / "profiles" / "gone").parent.mkdir(parents=True, exist_ok=True)
        orig_home = sync.HERMES_HOME
        sync.HERMES_HOME = home
        try:
            got = sync._deletable_profiles({"main", "proj-80", "gone"})
        finally:
            sync.HERMES_HOME = orig_home
        check("B1 目录还在的档不删", "proj-80" not in got, str(sorted(got)))
        check("B2 目录已消失的档才删", "gone" in got, str(sorted(got)))
        check("B3 main 永远不整档删", "main" not in got, str(sorted(got)))

    print(f"\n== {PASS + FAIL} 项，失败 {FAIL} ==")
    return 0 if FAIL == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
