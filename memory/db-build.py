# -*- coding: utf-8 -*-
"""建 SQLite 索引库：库当索引，正文还是 origin\\corpus 里的 markdown。

按 11-01 定下的形态做——
  · 库只当索引：每段一条记录，存锚点、文件名、行号，正文留在 markdown 里；
  · 记账字段一起上：events 表带 weight / last_hit_at / hits / pinned / status，
    以后降温、条目加权、最后用到这些规矩直接查表就能跑，不用我手工翻；
  · 中文分词自己切：SQLite 自带的分词器不认中文，先把 1-gram + 2-gram 切好存进
    FTS5 的 token 列，查询也用同一把刀，两边口径一致。

用法：python db-build.py            # 全量重建（先删后建）
      python db-build.py --看家底    # 只报数，不建
"""
import sqlite3
import os
import sys
from collections import Counter
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import recall  # noqa: E402
import config  # noqa: E402

# 原来写死 <工作区>/memory-lab，仓库一搬就错；现在从 config 取（DB_INDEX 环境变量仍然优先）
LAB = config.LAB
DB = config.DB
SEG_RE = recall.SEG_RE


import hashlib
import re


# DECISION: 段身份用 uid（标题派生），anchor 只当展示序号 | when: 2026-11-02 | cause: 现场逼的
# 病根：anchor 是「全册顺编的第几段」，被门槛滤掉一段只少一行、不重排，
# 所以补回一段（或滤掉一段）会让它后面所有锚点全体后移，文档里手写的引用静默指错。
# 现在 uid = sid + 标题归一化后的短哈希，**挂在标题上**：
#   · 改了标题 = 改了身份，那一节自己的引用会断 —— 这是刻意接受的代价。
#     从「一次全局错位」降成「一节可登记的事故」；重建会自动重算 uid，旧引用要跟着改。
#   · 所以：改标题的那一次，必须同步改引用它的地方（或干脆写回旧标题）。
# 两套并存：anchor 继续给人看、给旧引用用；uid 给新引用用，不随增删漂移。
def _norm(s):
    """只留中文和字母数字——与 route.py 的 _norm 同口径，别各写一份。"""
    return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]+", "", s or "")


def uid_of(sid, text):
    head = (text or "").split("\n", 1)[0]
    head = re.sub(r"^#+\s*", "", head)
    return "%s#%s" % (sid, hashlib.sha1(_norm(head).encode("utf-8")).hexdigest()[:8])


def collect():
    """把语料和事表读成待入库的行，顺带记下每段在文件里的起始行。"""
    segs, events = [], []
    for f in sorted(recall.CORPUS.glob("*.md")):
        sid, anchor, kind, tm, start = f.stem, None, "", "", 0
        buf = []
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        for i, line in enumerate(lines):
            m = SEG_RE.match(line)
            if m:
                if anchor:
                    segs.append({"anchor": anchor, "sid": sid, "kind": kind, "time": tm,
                                 "fname": f.name, "line": start, "text": "\n".join(buf).strip()})
                anchor, kind, tm, start, buf = m.group(1), m.group(2), m.group(3), i + 2, []
            elif anchor is not None:
                buf.append(line)
        if anchor:
            segs.append({"anchor": anchor, "sid": sid, "kind": kind, "time": tm,
                         "fname": f.name, "line": start, "text": "\n".join(buf).strip()})
    for d in recall.parse_topics():
        events.append({"sid": d["sid"], "seq": d["seq"], "title": d["title"],
                       "a": d["a"], "b": d["b"]})
    return segs, events


def main():
    segs, events = collect()
    # 群聊默认拒收（2026-11-04）：入库端就拦，别等到检索才发现别人的话混在里面。
    # 出口还有一道（hybrid.search），两道都拦是故意的——一道管新接入，一道管旧库。
    try:
        import sources
        _n0 = len(segs)
        segs = sources.sweep(segs)
        if len(segs) != _n0:
            print("群聊拒收：入库 %d 段，拿掉 %d 段" % (len(segs), _n0 - len(segs)))
    except Exception as e:
        print("来源过滤跳过（%s: %s）" % (type(e).__name__, e), file=sys.stderr)
    print("语料 %d 段、事 %d 条" % (len(segs), len(events)))
    # 0 段不许往下建（2026-11-04 踩出来的）：段头格式写错时一段都解析不出来，
    # 而建库照样成功、退出码照样 0，表现是「建好了但什么都查不到」。
    # 宁可在这儿硬停，把格式摊出来给人对。
    if not segs:
        sys.exit("语料 0 段，不建空库。段头格式应该是：\n"
                 "  ## [会话id-序号] 谁说的 2026-11-02 09:20:18\n"
                 "（方括号里不能多东西；谁说的和日期之间只隔一个空格；日期可以不带）\n"
                 "现在看的目录：%s" % recall.CORPUS)
    if "--看家底" in sys.argv:
        chars = sum(len(s["text"]) for s in segs)
        print("正文合计 %d 字，库路径 %s" % (chars, DB))
        return

    DB.parent.mkdir(parents=True, exist_ok=True)
    for p in (LAB / "data", LAB / "quiz"):
        p.mkdir(parents=True, exist_ok=True)
    if DB.exists():
        DB.unlink()

    db = sqlite3.connect(str(DB))
    db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE segments(
            anchor TEXT PRIMARY KEY, sid TEXT, n INTEGER, kind TEXT, time TEXT,
            fname TEXT, line INTEGER, chars INTEGER, dl INTEGER, tokens TEXT, text TEXT,
            uid TEXT);
        CREATE TABLE events(
            sid TEXT, seq INTEGER, title TEXT, a INTEGER, b INTEGER,
            weight REAL DEFAULT 1.0, last_hit_at TEXT, hits INTEGER DEFAULT 0,
            pinned INTEGER DEFAULT 0, status TEXT DEFAULT 'active',
            PRIMARY KEY(sid, seq));
        CREATE TABLE terms(term TEXT PRIMARY KEY, df INTEGER, idf REAL);
        CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);
        CREATE VIRTUAL TABLE seg_fts USING fts5(anchor UNINDEXED, tokens);
        CREATE VIRTUAL TABLE seg_vocab USING fts5vocab(seg_fts, 'row');
    """)
    rows, fts = [], []
    total_dl = 0
    seen_uid = {}
    for s in segs:
        toks = recall.terms(s["text"])
        total_dl += len(toks)
        uid = uid_of(s["sid"], s["text"])
        # 同一册里两节标题一样时 uid 会撞：给后来的一节加后缀，别让两段共用一个身份
        seen_uid[uid] = seen_uid.get(uid, 0) + 1
        if seen_uid[uid] > 1:
            uid = "%s-%d" % (uid, seen_uid[uid])
        rows.append((s["anchor"], s["sid"], int(s["anchor"][-3:]), s["kind"], s["time"],
                     s["fname"], s["line"], len(s["text"]), len(toks),
                     " ".join(toks), s["text"], uid))
        fts.append((s["anchor"], " ".join(toks)))
    db.executemany(
        "INSERT INTO segments(anchor, sid, n, kind, time, fname, line, chars, dl, tokens, text, uid) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    db.executemany("INSERT INTO seg_fts(anchor, tokens) VALUES (?,?)", fts)
    db.execute("INSERT INTO meta(k, v) VALUES ('avgdl', ?)",
               (total_dl / max(1, len(segs)),))
    # 覆盖范围（2026-11-04 加）：库必须知道自己从哪天开始有记忆。
    # 来由：目前只入库了约三分之一会话，问更早的事时检索安静地返回空，
    # 使用者分不清「没记过」和「那批会话根本没进来」。这几个键是给检索侧报边界用的。
    _times = sorted(s["time"] for s in segs if s.get("time"))
    _srcs = set(s["fname"] for s in segs if s.get("fname"))
    db.execute("INSERT INTO meta(k, v) VALUES ('built_at', datetime('now','localtime'))")
    db.execute("INSERT INTO meta(k, v) VALUES ('cover_min', ?)", (_times[0] if _times else "",))
    db.execute("INSERT INTO meta(k, v) VALUES ('cover_max', ?)", (_times[-1] if _times else "",))
    db.execute("INSERT INTO meta(k, v) VALUES ('n_segments', ?)", (str(len(segs)),))
    db.execute("INSERT INTO meta(k, v) VALUES ('n_sources', ?)", (str(len(_srcs)),))
    # n_events 等 events 表插完再写：解析出来的条数里有重复（topics.md 有会话被切两遍），
    # 拿 len(events) 记会虚高（2026-11-04 实测 157 vs 实际 145）。
    # 每个词的 df 和 idf 一次算好存表，查询时只按需取，不用每次扫全库
    db.execute("INSERT INTO terms(term, df, idf) SELECT term, doc, 0 FROM seg_vocab")
    db.execute("UPDATE terms SET idf = ln(1.0 + (? - df + 0.5) / (df + 0.5))", (len(segs),))
    rows, fts = [], []
    seen = set()
    dup = 0
    for e in events:
        if (e["sid"], e["seq"]) in seen:  # topics.md 里个别会话被切过两遍，去掉重复
            dup += 1
            continue
        seen.add((e["sid"], e["seq"]))
        rows.append((e["sid"], e["seq"], e["title"], e["a"], e["b"]))
    db.executemany("INSERT INTO events(sid, seq, title, a, b) VALUES (?,?,?,?,?)", rows)
    db.execute("INSERT INTO meta(k, v) VALUES ('n_events', ?)",
               (str(db.execute("SELECT COUNT(*) FROM events").fetchone()[0]),))
    db.commit()
    n = db.execute("SELECT COUNT(*) FROM segments").fetchone()[0]
    m = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    size = DB.stat().st_size / 1024 / 1024
    # 抽一段验一下能不能按 token 找回去
    probe = db.execute(
        "SELECT anchor FROM seg_fts WHERE seg_fts MATCH ? ORDER BY bm25(seg_fts) LIMIT 3",
        ("钩子 OR 钩",)).fetchall()
    print("入库：段 %d、事 %d、库 %.1f MB。试查『钩子』前三条：%s"
          % (n, m, size, ", ".join(r[0] for r in probe)))
    db.close()


if __name__ == "__main__":
    main()
