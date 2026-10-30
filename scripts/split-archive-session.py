#!/usr/bin/env python3
"""把一条（或多条）巨会话里 active=0 的归档行拆到独立「归档会话」。

用途：桌面端打开一条上万消息的会话时，WS `session.resume` 会把整份显示投影
（含 compacted 行）一次性推过去，几十 MB 起步。把归档行挪走后主线会话只剩
active 的几百条，载荷降一到两个数量级；旧历史一条不删，只是换了归属。

幂等 / 可逆：归档会话已存在则跳过；回滚＝把 messages.session_id 改回去。

用法：
    python split-archive-session.py --db <state.db> --session <sid> [--session <sid> ...]
    python split-archive-session.py --db <state.db> --session <sid> --apply

判据：--apply 后打印的两组行数应等于搬前 dry-run 的数字，且 `pragma quick_check` 为 ok。

先读这条再动手：
  * `messages` 上的触发器只盯 `active/compacted` 与 role/content/tool_*/timestamp，
    **不盯 `session_id`** —— 所以这里的 UPDATE 不会触发 display 重排（否则是 O(n^2)）。
  * 归档会话的 `session_key` 必须留 NULL：平台靠它找「这条微信/QQ 对话属于哪条线程」，
    写成原会话的 key 会让下一条消息落到归档里。
  * 别改 `active`：那会让模型上下文跟着变；这里只改归属。
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import time


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--db', required=True, help='state.db 路径')
    p.add_argument('--session', action='append', required=True, help='要瘦身的会话 id（可重复）')
    p.add_argument('--apply', action='store_true', help='真的写库；不给就是 dry-run')
    p.add_argument('--no-backup', action='store_true', help='--apply 时不先备份 state.db（不推荐）')
    return p.parse_args()


def main() -> int:
    a = parse_args()
    if not os.path.isfile(a.db):
        print('找不到库：%s' % a.db)
        return 2

    if a.apply and not a.no_backup:
        stamp = time.strftime('%Y%m%d-%H%M%S')
        bak = '%s.bak-split-%s' % (a.db, stamp)
        shutil.copy2(a.db, bak)
        print('已备份 -> %s (%.1f MB)' % (os.path.basename(bak), os.path.getsize(bak) / 1024 / 1024))

    c = sqlite3.connect(a.db, timeout=30)
    print('模式: %s\n' % ('【写入】' if a.apply else '【dry-run 只读】'))
    bad = 0

    for live in a.session:
        row = c.execute('select source, title, model, model_config, cwd, user_id from sessions where id=?',
                        (live,)).fetchone()
        if row is None:
            print('=== %s : 库里没这条会话，跳过' % live)
            bad += 1
            continue
        source, title, model, model_cfg, cwd, user_id = row

        n_arch = c.execute('select count(*) from messages where session_id=? and active=0', (live,)).fetchone()[0]
        n_keep = c.execute('select count(*) from messages where session_id=? and active=1', (live,)).fetchone()[0]
        span = c.execute('select min(timestamp), max(timestamp) from messages where session_id=? and active=0',
                         (live,)).fetchone()
        print('=== %s  (%s)' % (live, title or ''))
        print('  要搬走的归档行 : %d' % n_arch)
        print('  留下的 active 行: %d' % n_keep)
        if n_arch == 0:
            print('  没有归档行，无需处理\n')
            continue

        arch = live + '_hist'
        exists = c.execute('select 1 from sessions where id=?', (arch,)).fetchone()
        if exists:
            print('  目标会话 %s 已存在 -> 跳过\n' % arch)
            continue
        if not a.apply:
            print('  → [dry-run] 将新建 %s 并搬走 %d 行，主会话 message_count 改为 %d\n'
                  % (arch, n_arch, n_keep))
            continue

        c.execute('''insert into sessions (id, source, started_at, ended_at, end_reason, message_count,
                                          title, title_source, model, model_config, cwd, user_id,
                                          session_key, chat_id, chat_type, last_activity_at)
                     values (?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,?)''',
                  (arch, source, span[0] or 0, span[1], 'archived', n_arch,
                   '归档 · %s 早期历史' % (title or live), 'manual',
                   model, model_cfg, cwd, user_id, span[1]))
        cur = c.execute('update messages set session_id=? where session_id=? and active=0', (arch, live))
        c.execute('update sessions set message_count=? where id=?', (n_keep, live))
        print('  → 已新建 %s 并搬走 %d 行\n' % (arch, cur.rowcount))

    if a.apply:
        c.commit()
        print('=== 校验 ===')
        for live in a.session:
            for sid in (live, live + '_hist'):
                r = c.execute('select count(*) from messages where session_id=?', (sid,)).fetchone()
                if r:
                    act = c.execute('select count(*) from messages where session_id=? and active=1', (sid,)).fetchone()[0]
                    print('  %-46s 共 %6d 行 (active=%d)' % (sid, r[0], act))
        print('\nquick_check =', c.execute('pragma quick_check').fetchone()[0])

    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
