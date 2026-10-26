"""回归：_reap_orphan_exporters 只回收"孤儿导出"、不碰活着的父进程。

背景（2026-10-24 实案）：批量导出超时只杀了直接子进程，真正的导出进程活下来占住
镜像与各档 state.db；下一轮再起一个 → 两个导出同时写同一批文件，连续两轮 3300s 超时。

判据：
  S1 孤儿（父进程已死、命令行含 hermes-cloud-backup + export_all_profiles.py + --home/--out）⇒ 被杀
  S2 父进程健在的同形态进程 ⇒ 不动
  S3 命令行只提到文件名（不带 --home/--out）的孤儿 ⇒ 不动（防误杀）
"""
from __future__ import annotations

import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sync  # noqa: E402

import psutil  # noqa: E402

# 只要求「以 export_all_profiles.py 结尾」的 argv 尾；用脚本自身目录拼，避免把用户名/绝对路径写进公开仓
FAKE = str(Path(__file__).resolve().parent / "export_all_profiles.py")
SLEEPER = "import time; time.sleep(120)"


def _spawn(args: list[str]) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", SLEEPER, *args],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _spawn_orphan_maker(argv_tail: list[str]) -> None:
    """派生一个"中间人"，它再派生目标进程后自己退出 → 目标成为孤儿。"""
    code = (
        "import subprocess,sys,time;"
        f"subprocess.Popen([sys.executable,'-c',{SLEEPER!r},*{argv_tail!r}]);"
        "time.sleep(2)"
    )
    subprocess.run([sys.executable, "-c", code], check=False)


def _find(pred) -> list[int]:
    out = []
    for p in psutil.process_iter(["pid", "ppid", "name", "cmdline"]):
        try:
            cl = " ".join(p.info["cmdline"] or [])
            if SLEEPER in cl and pred(p, cl):
                out.append(p.info["pid"])
        except Exception:
            continue
    return out


def _alive(pid: int) -> bool:
    try:
        return psutil.Process(pid).is_running()
    except Exception:
        return False


def _wait_find(pred, timeout: float = 6.0):
    """轮询等进程可见：固定 sleep(1) 在负载高时会假红（实测 8 次里 1 次 find=[]）。"""
    end = time.time() + timeout
    while time.time() < end:
        got = _find(pred)
        if got:
            return got
        time.sleep(0.2)
    return []


def main() -> int:
    fails = 0
    # 每次跑用唯一 token：否则本机/上一轮遗留的同类进程会被算进本轮的 find 里（实测会假红）
    token = "hcb" + uuid.uuid4().hex[:10]

    # S1：孤儿
    # 并发保护：常驻哨兵/其它测试也可能在跑 _reap_orphan_exporters()，可能抢在我们观察前
    # 就把刚生成的孤儿收掉（实测同一台机上会偶发 find=[]）。重试三次，任一次观察到即算数。
    orphans, killed = [], []
    for attempt in range(3):
        _spawn_orphan_maker([FAKE, "--home", token + str(attempt), "--out", token + str(attempt)])
        # 判据用「argv 带本 run 的 token」而不是目录名：克隆目录未必叫 hermes-cloud-backup
        orphans = _wait_find(lambda p, cl: token in cl and "--home" in cl and "--out" in cl
                             and not psutil.pid_exists(p.info["ppid"]))
        if orphans:
            break
        time.sleep(1.0)
    killed = sync._reap_orphan_exporters()
    # 刚 kill 的进程可能还没落气：给最多 5 秒
    deadline = time.time() + 5
    while time.time() < deadline and any(_alive(pid) for pid in orphans):
        time.sleep(0.2)
    ok1 = bool(orphans) and all(not _alive(pid) for pid in orphans)
    print(("PASS" if ok1 else "FAIL") + f"  S1 孤儿被回收：find={orphans} killed={killed}")
    fails += 0 if ok1 else 1

    # S2：父进程健在
    live = _spawn([FAKE, "--home", token + "live", "--out", token + "live"])
    _wait_find(lambda p, cl: p.pid == live.pid)
    sync._reap_orphan_exporters()
    ok2 = _alive(live.pid)
    print(("PASS" if ok2 else "FAIL") + f"  S2 父进程健在者不动：pid={live.pid} alive={ok2}")
    fails += 0 if ok2 else 1
    try:
        live.kill()
    except Exception:
        pass

    # S3：孤儿但命令行不含 --home/--out（只是提到文件名）
    _spawn_orphan_maker([FAKE, "--token", token + "na"])
    fake = _wait_find(lambda p, cl: (token + "na") in cl and "--home" not in cl
                      and not psutil.pid_exists(p.info["ppid"]))
    sync._reap_orphan_exporters()
    ok3 = bool(fake) and all(_alive(pid) for pid in fake)
    print(("PASS" if ok3 else "FAIL") + f"  S3 只提文件名者不杀：find={fake}")
    fails += 0 if ok3 else 1
    for pid in fake:
        try:
            psutil.Process(pid).kill()
        except Exception:
            pass

    print(f"\n== 3 场景，失败 {fails} ==")
    return 0 if fails == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
