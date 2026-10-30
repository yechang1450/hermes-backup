# -*- coding: utf-8 -*-
"""健康记账单元测试：sync.py 单一来源 + 包装"未跑完"补偿去重。

用法：`python scripts/test-hb-health.py`（纯标准库；只写临时目录）
要点：
  · sync.py 的 home 只按 LOCALAPPDATA 解析、**不读 HERMES_HOME**，必须注入模块属性，
    否则会写真实 state 目录；
  · 只清 BASE/home，不动 BASE 本身。
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
S = SCRIPT_DIR
BASE = Path(os.environ.get("HB_TEST_BASE") or tempfile.mkdtemp(prefix="hb-health-test-")) / "health"
HOME_DIR = BASE / "home"
shutil.rmtree(HOME_DIR, ignore_errors=True)
(HOME_DIR / "state").mkdir(parents=True, exist_ok=True)
os.environ["HERMES_HOME"] = str(HOME_DIR)

sys.path.insert(0, str(S))
import sync  # noqa: E402

sync.HERMES_HOME = HOME_DIR
sync.HEALTH_FILE = HOME_DIR / "state" / "hb-sync-health.json"
sync.ATTENTION_MARKER = HOME_DIR / "state" / "hb-sync-needs-attention"
sync.RUNS_LOG = HOME_DIR / "state" / "hb-sync-runs.jsonl"

HEALTH = sync.HEALTH_FILE
MARKER = sync.ATTENTION_MARKER
assert str(HEALTH).startswith(str(HOME_DIR)), "拒绝写真实 home：%s" % HEALTH
ok_all = True


def check(name, cond, detail=""):
    global ok_all
    print(("  ✓ " if cond else "  ✗ ") + name + (" | " + detail if detail else ""))
    ok_all &= bool(cond)


def main() -> int:
    print("工作目录：%s" % HOME_DIR)
    print("== A. sync._record_health：连续失败 3 次 → 标记；成功 → 清零并删标记 ==")
    for i in range(1, 4):
        sync._record_health(3, {"start_ts": "x", "end_ts": "2026-10-23T00:00:0%d" % i, "duration_s": 10 + i})
    h = json.loads(HEALTH.read_text(encoding="utf-8"))
    check("consecutive_failures == 3", h.get("consecutive_failures") == 3, str(h.get("consecutive_failures")))
    check("标记已写", MARKER.exists())
    check("标记含轮次与用时", MARKER.exists() and "exit=3" in MARKER.read_text(encoding="utf-8"),
          MARKER.read_text(encoding="utf-8").strip()[:60] if MARKER.exists() else "")
    sync._record_health(0, {"start_ts": "y", "end_ts": "2026-10-23T00:10:00", "duration_s": 42})
    h = json.loads(HEALTH.read_text(encoding="utf-8"))
    check("成功后清零", h.get("consecutive_failures") == 0, str(h.get("consecutive_failures")))
    check("标记已删", not MARKER.exists())
    check("last_exit_code 已更新", h.get("last_exit_code") == 0 and h.get("last_duration_s") == 42,
          str(h.get("last_exit_code")))

    print("== B. 包装 bump_unfinished：只在 sync.py 没记账时补记（判重用 updated_at） ==")
    spec = importlib.util.spec_from_file_location("hb_autosync", S / "hb-autosync.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.HEALTH = HEALTH
    mod.MARKER = MARKER
    assert str(mod.HEALTH).startswith(str(HOME_DIR))

    now = datetime.now().astimezone()
    h = json.loads(HEALTH.read_text(encoding="utf-8"))
    h["updated_at"] = now.isoformat(timespec="seconds")
    h["consecutive_failures"] = 1
    HEALTH.write_text(json.dumps(h), encoding="utf-8")
    n = mod.bump_unfinished(time.time() - 60, "timeout 3300s")
    check("未被重复计数", n == 1 and json.loads(HEALTH.read_text(encoding="utf-8"))["consecutive_failures"] == 1, str(n))

    h["updated_at"] = (now - timedelta(hours=2)).isoformat(timespec="seconds")
    HEALTH.write_text(json.dumps(h), encoding="utf-8")
    n = mod.bump_unfinished(time.time() - 60, "timeout 3300s")
    h2 = json.loads(HEALTH.read_text(encoding="utf-8"))
    check("已补记 +1", n == 2 and h2.get("consecutive_failures") == 2, str(n))
    check("记了 unfinished 与原因", h2.get("last_exit_code") == "unfinished" and "timeout" in (h2.get("last_error") or ""))

    h2["updated_at"] = (now - timedelta(hours=1)).isoformat(timespec="seconds")
    HEALTH.write_text(json.dumps(h2), encoding="utf-8")
    n = mod.bump_unfinished(time.time() - 60, "exited before recording")
    check("第 3 次写标记", n == 3 and MARKER.exists(), str(n))

    print("== C. shrink guard（exit 6）不算失败：计数不增不减、不写标记 ==")
    h3 = {"consecutive_failures": 2, "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
          "last_exit_code": 3, "last_error": "x"}
    HEALTH.write_text(json.dumps(h3), encoding="utf-8")
    if MARKER.exists():
        MARKER.unlink()
    sync._record_health(6, {"start_ts": "z", "end_ts": "2026-10-31T02:09:08", "duration_s": 0})
    h4 = json.loads(HEALTH.read_text(encoding="utf-8"))
    check("计数不增不减（仍 2）", h4.get("consecutive_failures") == 2, str(h4.get("consecutive_failures")))
    check("记下了 exit 6", h4.get("last_exit_code") == 6, str(h4.get("last_exit_code")))
    check("不写 needs-attention", not MARKER.exists())
    sync._record_health(6, {"start_ts": "z2", "end_ts": "2026-10-31T02:29:08", "duration_s": 0})
    h5 = json.loads(HEALTH.read_text(encoding="utf-8"))
    check("连续两次 exit 6 也不涨", h5.get("consecutive_failures") == 2, str(h5.get("consecutive_failures")))

    print()
    print("== 总判定：%s ==" % ("PASS" if ok_all else "FAIL"))
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
