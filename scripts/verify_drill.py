# -*- coding: utf-8 -*-
"""verify_drill.py — 恢复演练对账器：镜像会话文件（真值） vs 沙箱恢复库 逐个会话比对。

用法：python verify_drill.py <沙箱根目录>
  沙箱根目录下应有：repo/（克隆的备份仓） 与 home/（恢复出的 HERMES_HOME）

口径（与 references/restore-drill.md 第 6 步一致）：
  · 会话组 = sessions/<档>/ 下 <sid>.jsonl 与 <sid>.part-NNNN.jsonl（按名序拼接）
  · 真值 = json.loads(拼接文本)['messages'] 的长度（不是行数——导出文件是单行巨 JSON）
  · 沙箱 = 该档 state.db 的 messages 按 session_id 分组计数
  · 0 消息会话：sessions 表有该 id 即算已导入（否则报假阳性）
验收线：差异 0 项；末行打印 VERIFY-DONE 供脚本/人判读。
"""
import json
import os
import sqlite3
import sys
import time
from pathlib import Path


def session_groups(d: Path) -> dict:
    g: dict[str, list] = {}
    for f in sorted(os.listdir(d)):
        if f.endswith('.jsonl') and '.part-' not in f:
            g[f[:-6]] = [d / f]
        elif '.part-' in f and f.endswith('.jsonl'):
            base = f.split('.part-')[0]
            g.setdefault(base, []).append(d / f)
    return g


USAGE = """用法: python verify_drill.py <演练基目录>
  基目录下需有 repo/（备份仓克隆）与 home/（恢复目标）。
  只读：不写镜像、不改活库；输出每个档的「镜像条数 vs 库内条数」对账，末行 VERIFY-DRILL-OK / FAILED。"""


def main() -> int:
    if "-h" in sys.argv[1:] or "--help" in sys.argv[1:]:
        print(USAGE)
        return 0
    pos = [a for a in sys.argv[1:] if not a.startswith("-")]
    if not pos:
        print(USAGE)
        return 2
    base = Path(pos[0])
    repo, home = base / 'repo', base / 'home'
    profs = [p for p in sorted(os.listdir(repo / 'sessions')) if (repo / 'sessions' / p).is_dir()]
    print('== 档位：%s ==' % profs, flush=True)

    t_total = time.time()
    total_truth_s = total_truth_m = total_box_s = total_box_m = 0
    mismatch = []

    for prof in profs:
        t0 = time.time()
        db = home / 'state.db' if prof == 'main' else home / 'profiles' / prof / 'state.db'
        box_s = box_m = -1
        sess_counts: dict = {}
        sess_ids: set = set()
        if db.is_file():
            con = sqlite3.connect('file:%s?mode=ro' % db.as_posix(), uri=True)
            box_s = con.execute('select count(*) from sessions').fetchone()[0]
            box_m = con.execute('select count(*) from messages').fetchone()[0]
            for sid, n in con.execute('select session_id, count(*) from messages group by session_id'):
                sess_counts[sid] = n
            sess_ids = {r[0] for r in con.execute('select id from sessions')}
            con.close()
        gd = session_groups(repo / 'sessions' / prof)
        truth_s = len(gd)
        truth_m = 0
        parts_n = 0
        prof_mism = 0
        for sid, files in sorted(gd.items()):
            try:
                text = ''.join(f.read_text(encoding='utf-8', errors='replace') for f in files)
                n = len((json.loads(text)).get('messages') or [])
            except Exception as e:
                mismatch.append((prof, sid, '解析失败: %s' % e))
                continue
            truth_m += n
            got = sess_counts.get(sid)
            if got is None and sid in sess_ids:
                got = 0  # 0 消息会话：sessions 表里有即已导入
            if got is None:
                mismatch.append((prof, sid, '沙箱未导入（镜像 %d 条）' % n))
                prof_mism += 1
            elif got != n:
                mismatch.append((prof, sid, '消息计数差：镜像 %d vs 沙箱 %d' % (n, got)))
                prof_mism += 1
            if len(files) > 1:
                parts_n += 1
                print('   [分片] %s → %d 条 | %s' % (sid[:44], n, 'OK' if got == n else 'MISMATCH got=%s' % got), flush=True)
        extra = [sid for sid in sess_counts if sid not in gd]
        if extra:
            mismatch.append((prof, '—', '沙箱多出未在镜像的会话 %d 个：%s' % (len(extra), extra[:5])))
        total_truth_s += truth_s
        total_truth_m += truth_m
        if box_s >= 0:
            total_box_s += box_s
            total_box_m += box_m
        print('[%s] 镜像 %d 会话/%d 消息（分片组 %d） ⇒ 沙箱 %s 会话/%s 消息 | 差异 %d | %.0fs' % (
            prof, truth_s, truth_m, parts_n, box_s, box_m, prof_mism, time.time() - t0), flush=True)

    print()
    print('== 总计（%.0fs）==' % (time.time() - t_total))
    print('镜像: %d 会话 / %d 消息' % (total_truth_s, total_truth_m))
    print('沙箱: %d 会话 / %d 消息' % (total_box_s, total_box_m))
    print('== 差异清单：%d 项 ==' % len(mismatch))
    for x in mismatch[:30]:
        print('  ', x)
    print('VERIFY-DONE')
    return 0 if not mismatch else 1


if __name__ == '__main__':
    sys.exit(main())
