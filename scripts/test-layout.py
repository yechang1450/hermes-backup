"""session_layout 的三项判据：等价性 / 幂等 / 追加后的前缀稳定性。"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from session_layout import KEEP_KEYS, stabilize, top_level_members  # noqa: E402

fails = 0


def check(name: str, ok: bool, extra: str = "") -> None:
    global fails
    print(("PASS" if ok else "FAIL") + f"  {name}" + (f"  [{extra}]" if extra else ""))
    if not ok:
        fails += 1


def sample(path: Path) -> dict:
    return json.loads(path.read_bytes())


def synthetic_sample() -> tuple[str, str]:
    """没有镜像时的自足样本：造一个"一行=一个会话"的 JSON，含易变统计字段与 2 万条消息。

    2026-10-26 改：原来硬编码 D:/hermes-backup，换台机器就 IndexError、整套回归跑不了。
    """
    msgs = [{"role": "user" if i % 2 else "assistant",
             "content": f"第 {i} 条消息，用于撑大样本，内容重复但 id 不同。",
             "id": f"m{i}"} for i in range(20000)]
    obj = {
        "id": "synthetic0001", "source": "cli", "user_id": "u", "session_key": "k",
        "message_count": len(msgs), "input_tokens": 123, "output_tokens": 456,
        "timings": {"total": 1.5}, "transcript_revision": 7,
        "last_activity_description": "synthetic",
        "messages": msgs,
    }
    return json.dumps(obj, ensure_ascii=False), "synthetic0001(合成)"


def main() -> int:
    # 可用 HB_TEST_MIRROR 指向别处；拿不到真实样本就退回合成样本（保持自足）
    src = Path(os.environ.get("HB_TEST_MIRROR") or r"D:/hermes-backup/sessions/main")
    cand = None
    if src.is_dir():
        pool = sorted((p for p in src.glob("*.jsonl") if p.stat().st_size < 2 * 1024 * 1024),
                      key=lambda p: p.stat().st_size)
        if pool:
            cand = pool[-1]
    if cand is not None:
        raw = cand.read_bytes().decode("utf-8").rstrip("\n")
        name = cand.name
    else:
        raw, name = synthetic_sample()
        print("（未找到 D:/hermes-backup 镜像，使用合成样本——本测试自足，不依赖本机备份）")
    obj = json.loads(raw)
    print(f"样本 {name}  {len(raw)/1048576:.2f}MB  顶层键 {len(obj)} 个")

    out = stabilize(raw)
    check("S1 语义等价 json.loads(out) == json.loads(in)", json.loads(out) == obj)
    check("S2 幂等 stabilize(out) == out", stabilize(out) == out)
    moved = [m[0] for m in top_level_members(out)][-6:]
    check("S3 易变字段已落在尾部", any("token" in k or "timing" in k or k == "message_count" for k in moved),
          str(moved))

    # S4 追加一条消息后：messages 值（到原右括号为止）应逐字节不变 ⇒ git 只看到尾部变化
    def value_text(t: str) -> tuple[int, int]:
        for k, s, e in top_level_members(t):
            if k == "messages":
                start = t.index(":", s) + 1
                while t[start] in " \t\r\n":
                    start += 1
                end = e
                while t[end - 1] in " \t\r\n,":
                    end -= 1
                return start, end
        raise AssertionError("no messages")

    s0, e0 = value_text(raw)
    old_val = raw[s0:e0]
    close = raw.rindex("]", s0, e0)
    raw2 = raw[:close] + ', {"id": "999999", "role": "user", "content": "x"}' + raw[close:]
    s1, e1 = value_text(raw2)
    new_val = raw2[s1:e1]
    ok = new_val.startswith(old_val[:-1]) and new_val != old_val
    check("S4 追加后 messages 值前缀不变（尾部追加）", ok,
          f"前缀 {len(old_val)-1} 字节 / 新值 {len(new_val)} 字节")
    # 稳定化输出里该值也应原样保留
    out2 = stabilize(raw2)
    check("S4b 稳定化未改动 messages 值", new_val in out2)

    # S5 性能：合成 40MB 会话
    big = {"id": "synth", "title": "t", "message_count": 1,
           "messages": [{"id": str(i), "role": "user", "content": "x" * 400} for i in range(100000)]}
    t0 = time.time()
    big_raw = json.dumps(big, ensure_ascii=False)
    t1 = time.time()
    out3 = stabilize(big_raw)
    t2 = time.time()
    check("S5 40MB 级扫描可用", json.loads(out3) == big, f"序列化 {t1-t0:.1f}s / 稳定化 {t2-t1:.1f}s / {len(big_raw)/1048576:.0f}MB")

    # S6 前半段必须严格是 KEEP_KEYS（身份 + messages），其余全在尾部
    order = [k for k, _s, _e in top_level_members(out)]
    kept = [k for k in order if k in KEEP_KEYS]
    expect = [k for k in KEEP_KEYS if k in order]
    check("S6 前半段＝KEEP_KEYS 且 messages 收尾", kept == expect and kept[-1] == "messages",
          f"{len(kept)} 个")

    print(f"\n== 6 项，失败 {fails} ==")
    return 0 if fails == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
