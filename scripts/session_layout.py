"""会话文件"尾部稳定化"：把易变的统计字段挪到对象末尾，使**追加消息只改动文件尾部**。

## 为什么需要
会话导出是「一行=一个会话」的超长 JSON。头部连着几个**每轮都会变的统计字段**
（`message_count` / token 计数 / `timings` / `last_activity_at` / `transcript_revision` …）。
头部一变，整行字节全变 ⇒ 固定字节偏移切出来的**每一个分片都成为新 blob**
（2026-10-24 实测：单轮 827MB 被重写，其中 10 个巨会话各 3–6 个 45MB 分片全改）。
git 的 delta 因此失效，仓体积按 ~150MB/小时增长，三天两头要换代。

把这些字段移到对象尾部后：未变的会话字节完全一致（写入跳过）；有追加的会话只在
**末尾**产生差异 ⇒ 只有最后一个分片变化。

## 做法（零解析，纯文本扫描）
不 `json.loads` 整行（243MB 的会话要 ~2GB 峰值内存，本机常常不够），改用字符串感知的
顶层扫描：识别顶层 `"key": value` 成员的边界（跳过字符串里的逗号/括号），把指定 key
的成员原文整段剪下、按原样追加到右花括号之前。**不重排、不改写任何字节内容**。

## 契约
- `stabilize(text)` 返回新文本；对同一输入幂等；`json.loads(输出) == json.loads(输入)`。
- 输入不是单个 JSON 对象（解析失败/被截断）时抛 `ValueError`，调用方回退原样切片。
"""
from __future__ import annotations

# 每轮都可能变、且不属于会话正文的顶层字段（顺序即移至尾部的顺序）
VOLATILE_KEYS = (
    "message_count",
    "api_call_count",
    "tool_call_count",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "estimated_cost_usd",
    "timings",
    "tool_names",
    "model_config",
    "last_active",
    "last_activity_at",
    "transcript_revision",
    "updated_at",
    "exported_at",
    "_usage_anchor",
    "gateway_runtime",
)


def _skip_string(s: str, i: int) -> int:
    """i 指向开引号，返回闭合引号之后的位置。"""
    i += 1
    n = len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == '"':
            return i + 1
        i += 1
    raise ValueError("字符串未闭合")


def _skip_value(s: str, i: int) -> int:
    """i 指向值的首字符，返回值结束后的位置（顶层标量/对象/数组都支持）。"""
    n = len(s)
    if i >= n:
        raise ValueError("值缺失")
    c = s[i]
    if c == '"':
        return _skip_string(s, i)
    if c in "{[":
        depth = 0
        while i < n:
            ch = s[i]
            if ch == '"':
                i = _skip_string(s, i)
                continue
            if ch in "{[":
                depth += 1
            elif ch in "}]":
                depth -= 1
                if depth == 0:
                    return i + 1
            i += 1
        raise ValueError("括号未闭合")
    # 数字 / true / false / null
    while i < n and s[i] not in ",}]":
        i += 1
    return i


def top_level_members(text: str) -> list[tuple[str, int, int]]:
    """返回 [(key, start, end)]：每个顶层成员（含末尾逗号）在 text 中的区间。

    `start` 指向 key 的引号前，`end` 指向该成员（及其后逗号）之后。
    """
    i = 0
    n = len(text)
    while i < n and text[i] != "{":
        i += 1
    if i >= n:
        raise ValueError("不是 JSON 对象")
    i += 1
    out: list[tuple[str, int, int]] = []
    while True:
        while i < n and text[i] in " \t\r\n":
            i += 1
        if i < n and text[i] == "}":
            return out
        if i >= n or text[i] != '"':
            raise ValueError("顶层成员应以字符串键开头（偏移 %d）" % i)
        key_start = i
        key_end = _skip_string(text, i)
        key = text[i + 1:key_end - 1]
        i = key_end
        while i < n and text[i] in " \t\r\n":
            i += 1
        if i >= n or text[i] != ":":
            raise ValueError("键后应为冒号（偏移 %d）" % i)
        i += 1
        while i < n and text[i] in " \t\r\n":
            i += 1
        i = _skip_value(text, i)
        while i < n and text[i] in " \t\r\n":
            i += 1
        if i < n and text[i] == ",":
            i += 1
        out.append((key, key_start, i))
        if i >= n:
            raise ValueError("对象未闭合")


def stabilize(text: str, keys: tuple[str, ...] = VOLATILE_KEYS) -> str:
    """把 keys 里的顶层成员移到对象末尾，其余成员保持原顺序与原字节。"""
    members = top_level_members(text)
    keyset = set(keys)
    moved = [m for m in members if m[0] in keyset]
    if not moved:
        return text
    keep = [m for m in members if m[0] not in keyset]

    def _chunk(m: tuple[str, int, int]) -> str:
        raw = text[m[1]:m[2]]
        return raw.rstrip().rstrip(",").rstrip()

    parts = [_chunk(m) for m in keep + moved]
    return "{" + ", ".join(parts) + "}" + text[text.rindex("}") + 1:]
