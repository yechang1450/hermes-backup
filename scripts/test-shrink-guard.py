# -*- coding: utf-8 -*-
"""新机/未恢复保护（shrink guard）回归。

2026-10-31 实案：新机装好 Hermes（活库几十个会话）后，哨兵按 overdue 自动跑了一轮，
把镜像的 home-overlay 1,569 个、profiles 8,284 个、sessions 2,311 个文件当「残留」清掉；
只因 `gh` 不可用拒绝推送，备份才没被覆盖。零会话闸（guard_zero_sessions）只看「0 个」，
拦不住「有一点但极少」，所以在采集前加了这道闸。

判据：拒绝＝返回 (False, 原因)；放行＝(True, "")。
"""
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import sync  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, ok: bool) -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"PASS  {name}")
    else:
        FAIL += 1
        print(f"FAIL  {name}")


def make_live(home: Path, n: int) -> None:
    """造一个只有 sessions 表的假 state.db（shrink_guard 只查 count(*））。"""
    home.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(home / "state.db"))
    con.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY)")
    con.executemany("INSERT INTO sessions (id) VALUES (?)", [(f"s{i}",) for i in range(n)])
    con.commit()
    con.close()


def make_mirror(root: Path, per_profile: dict[str, int]) -> Path:
    m = root / "mirror"
    for name, n in per_profile.items():
        d = m / "sessions" / name
        d.mkdir(parents=True, exist_ok=True)
        for i in range(n):
            (d / f"{name}-sess{i}.jsonl").write_text("{}\n", encoding="utf-8")
    return m


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        # 隔离：默认没有 preserve_sessions，避免读到本机真实的 local.json
        old_cfg = sync._skill_config
        sync._skill_config = lambda: {}

        # S1：备份 100 / 活库 5 ⇒ 拒绝（这就是 10-31 的形态）
        home1 = root / "home1"
        make_live(home1, 5)
        m1 = make_mirror(root / "h1", {"main": 100})
        old_home = sync.HERMES_HOME
        sync.HERMES_HOME = home1
        ok1, why1 = sync.shrink_guard(m1)
        check("S1 备份100/活库5 ⇒ 拒绝", ok1 is False)
        check("S1 原因带两侧数字", ("活库 5" in why1) and ("备份 100" in why1))

        # S2：备份 100 / 活库 80 ⇒ 放行（正常增量）
        home2 = root / "home2"
        make_live(home2, 80)
        sync.HERMES_HOME = home2
        ok2, _ = sync.shrink_guard(m1)
        check("S2 备份100/活库80 ⇒ 放行", ok2 is True)

        # S3：备份很小（< 闸门下限）⇒ 不拦，哪怕活库是 0
        home3 = root / "home3"
        make_live(home3, 0)
        sync.HERMES_HOME = home3
        m3 = make_mirror(root / "h3", {"main": sync.SHRINK_GUARD_MIN_BASE - 10})
        ok3, _ = sync.shrink_guard(m3)
        check("S3 小档不误伤", ok3 is True)

        # S4：多分片只按会话 id 计一次（3 会话 / 7 文件）
        m4 = make_mirror(root / "h4", {"main": 3})
        d4 = m4 / "sessions" / "main"
        for i in range(3):
            for part in (1, 2):
                (d4 / f"main-sess{i}.part-{part:04d}.jsonl").write_text("{}\n", encoding="utf-8")
        cnt = sync._count_mirror_sessions(m4)
        check("S4 分片只算一次（3 而不是 10）", cnt.get("main") == 3)

        # S5：活库读不到 ⇒ 该档不参与判定（宁可放过，不误拦）
        home5 = root / "home5"
        home5.mkdir(parents=True)
        (home5 / "state.db").write_text("not a database", encoding="utf-8")
        sync.HERMES_HOME = home5
        ok5, _ = sync.shrink_guard(m1)
        check("S5 活库读不到 ⇒ 放行（不误拦）", ok5 is True)

        # S6：工作树被误 prune 过，仍按 git HEAD 计数（否则闸门永远不响）
        m6 = make_mirror(root / "h6", {"main": 60})
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@t"}
        import os
        e6 = dict(os.environ, **env)
        for cmd in (["git", "init", "-q"], ["git", "add", "-A"],
                    ["git", "commit", "-qm", "seed"]):
            subprocess.run(cmd, cwd=str(m6), env=e6, check=True, capture_output=True)
        for f in (m6 / "sessions" / "main").glob("*.jsonl"):
            f.unlink()
        home6 = root / "home6"
        make_live(home6, 10)
        sync.HERMES_HOME = home6
        cnt6 = sync._count_mirror_sessions(m6)
        check("S6 工作树清空后仍按 HEAD 计（60）", cnt6.get("main") == 60)
        ok6, _ = sync.shrink_guard(m6)
        check("S6 工作树被清空仍拦得住", ok6 is False)

        # S7：退役档（preserve_sessions）既是「不计入 shrink 判据」也是「不 prune」
        #     形态＝10-31 真实场景：备份 main 100 + ceo 400，活库只剩 main 80（bot 档不恢复）
        home7 = root / "home7"
        make_live(home7, 80)
        m7 = make_mirror(root / "h7", {"main": 100, "ceo": 400})
        sync.HERMES_HOME = home7
        ok7a, why7 = sync.shrink_guard(m7)
        check("S7 未声明退役档 ⇒ 拒绝（ceo 400 计入分母）", ok7a is False)
        check("S7 拒绝原因点名 ceo", "ceo" in why7)

        sync._skill_config = lambda: {"preserve_sessions": ["ceo"]}
        check("S7 preserve 读到 ceo", sync._preserved_profiles() == {"ceo"})
        ok7b, _ = sync.shrink_guard(m7)
        check("S7 声明退役档后 ⇒ 放行（只比 main 100/80）", ok7b is True)
        check("S7 ceo 不被列进可删档", sync._deletable_profiles({"ceo"}) == set())
        check("S7 未声明的档仍按原逻辑判可删", sync._deletable_profiles({"zzz"}) == {"zzz"})

        sync._skill_config = old_cfg
        sync.HERMES_HOME = old_home

    print(f"\n== {PASS + FAIL} 项，失败 {FAIL} ==")
    return 0 if FAIL == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
