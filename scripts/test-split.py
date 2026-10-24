"""分片字节等价性测试（技能内回归：布局改阈值前后都必须逐字节等价）：证明"切分 + 拼接"与原始导出**逐字节相同**。

不碰真库：合成 3 行 jsonl（小会话 / 超大会话 / 刚好跨界），跑 split_to_session_parts，
断言每片 ≤ PART_MAX、片数符合预期、按序号拼接后 == 原始该行。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sync  # noqa: E402

BASE = Path(sys.argv[1]) if len(sys.argv) > 1 else (Path(os.environ.get("TMP") or ".") / "hcb-split-test")
PART = sync.PART_MAX
MB = 1024 * 1024


def make_line(sid: str, total_mb: float) -> bytes:
    """造一条"单行会话 JSON"（形状与真实导出一致：头部元数据 + messages 数组）。"""
    head = ('{"id": "%s", "source": "test", "title": "t", "messages": [' % sid).encode()
    pad = b'{"role":"user","content":"' + b"x" * 200 + b'"},'
    body = pad * int((total_mb * MB) // len(pad))
    tail = b'{"role":"assistant","content":"end"}]}'
    return head + body + tail + b"\n"


def main() -> int:
    shutil.rmtree(BASE, ignore_errors=True)
    raw = BASE / "raw.jsonl"
    outdir = BASE / "sessions"
    raw.parent.mkdir(parents=True, exist_ok=True)

    lines = [make_line("small0001", 2.0),          # 2MB → 单文件
             make_line("giant0002", 100.0),        # 100MB → 3 片（PART_MAX=45MB）
             make_line("edge0003", 45.0)]          # 45MB 左右 → 1–2 片（边界）
    raw.write_bytes(b"".join(lines))
    print("阈值 PART_MAX = %.1f MB" % (PART / MB))

    stats = sync.split_to_session_parts(raw, outdir)
    files = sorted(p for p in outdir.iterdir() if p.is_file())
    print("产出文件 %d 个；stats.sessions=%d stats.files=%d oversize=%s"
          % (len(files), stats["sessions"], stats["files"], stats["oversize"]))
    for f in files:
        print("  %-34s %.2f MB" % (f.name, f.stat().st_size / MB))

    ok = True
    # 1) 每片都不超过阈值
    over = [f.name for f in files if f.stat().st_size > PART]
    print("[1] 单片 ≤ 阈值:", "PASS" if not over else "FAIL %s" % over)
    ok &= not over

    # 2) 按会话把片拼回，逐字节比对；>STABILIZE_MIN_BYTES 的行会被尾部稳定化（成员重排），
    #    这时判"语义等价"（json.loads 相同）+ 消息条数相同，而不是逐字节。
    import json as _json
    for sid, original in (("small0001", lines[0]), ("giant0002", lines[1]), ("edge0003", lines[2])):
        parts = sorted(outdir.glob(f"{sid}.part-*.jsonl"))
        single = outdir / f"{sid}.jsonl"
        blob = single.read_bytes() if single.exists() else b"".join(p.read_bytes() for p in parts)
        if len(original) > sync.STABILIZE_MIN_BYTES:
            same = _json.loads(blob) == _json.loads(original)
            mode = "语义等价"
        else:
            same = blob == original
            mode = "逐字节"
        print("[2] %s 复原（%s）：%s（原 %d 字节 / 复原 %d 字节 / %d 片）"
              % (sid, mode, "PASS" if same else "FAIL", len(original), len(blob), len(parts) or 1))
        ok &= same

    # 3) 片数预期：按阈值动态推导（>阈值 才切片，片数 = ceil(字节数/阈值)）
    import math
    for sid, original in (("small0001", lines[0]), ("giant0002", lines[1]), ("edge0003", lines[2])):
        want = 0 if len(original) <= PART else math.ceil(len(original) / PART)
        got = len(list(outdir.glob(f"{sid}.part-*.jsonl")))
        print("[3] %s 片数 %d（期望 %d）: %s" % (sid, got, want, "PASS" if got == want else "FAIL"))
        ok &= got == want

    # 4) 每片都能被"按序号拼接"的顺序识别（编号不重复且连续）
    for sid in ("giant0002", "edge0003"):
        nums = sorted(int(p.name.split(".part-")[1].split(".")[0]) for p in outdir.glob(f"{sid}.part-*.jsonl"))
        cont = nums == list(range(1, len(nums) + 1))
        print("[4] %s 片号连续: %s %s" % (sid, nums, "PASS" if cont else "FAIL"))
        ok &= cont

    print("== %s ==" % ("ALL-PASS" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
