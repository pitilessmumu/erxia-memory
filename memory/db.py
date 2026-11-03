# -*- coding: utf-8 -*-
"""走 SQLite 索引查记忆：库当索引，正文还在 markdown 里，查到了回原文那几行。

跟 recall.py（每次现算 Python 那版）比，这一版多的是三样：
  · 快——不重读 894 段，库直接给排序结果；
  · 记账——events 表有 weight / last_hit_at / hits / pinned / status，
    查一次就把「谁被用到、什么时候用的」记上（--记 才写，免得跑考卷污染数据）；
  · 口径一样——中文还是自己切 1-gram + 2-gram，查询同一把刀，
    废词表、同义词表、具体词门槛都跟 recall.py 共用，比出来的分才是同一台秤。

用法：
  python db.py "钩子收不到怎么办"
  python db.py "改钩子" --段 4 --宽 300 --记
  python db.py --钻 d1744eaa-036-d1744eaa-050
  python db.py --账本                  # 看看这些天哪些条目真被用到过
"""
import os
import argparse
import math
import re
import sqlite3
from collections import Counter
import sys
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import corpus  # noqa: E402
import config  # noqa: E402

# 原来写死 <工作区>/memory-lab，仓库一搬就错；现在从 config 取（DB_INDEX 环境变量仍然优先）
LAB = config.LAB
# 库路径可用环境变量换：重建时写临时库，写完原子换名，检索那边不会读到半截
DB = config.DB
CORPUS = corpus.CORPUS


def connect():
    if not DB.is_file():
        sys.exit("索引库还没建：先跑 python db-build.py")
    return sqlite3.connect(str(DB))


def vocab(df_max=None):
    db = connect()
    n = db.execute("SELECT COUNT(*) FROM segments").fetchone()[0]
    df = {r[0]: r[1] for r in db.execute("SELECT term, doc FROM seg_vocab")}
    db.close()
    return n, df


def idf_of(df, n):
    return math.log(1.0 + (n - df + 0.5) / (df + 0.5))


def build_query(query, df, n):
    """把问题切成 token，砍废词、挂同义组、要求有具体词——跟 recall.py 同一套判据。"""
    qv = {t: 1.0 for t in corpus.terms(query)}
    qv = {t: w for t, w in qv.items() if df.get(t, 0) <= corpus.DEAD_DF * n}
    for group in corpus.ALIAS:
        if not any(g in query or g in qv for g in group):
            continue
        for g in group:
            for t in corpus.terms(g):
                qv.setdefault(t, 0.5)
    unseen = math.log(1.0 + (n + 0.5) / 0.5)
    total = sum(w * idf_of(df.get(t, 0), n) for t, w in qv.items())
    seen = sum(w * idf_of(df[t], n) for t, w in qv.items() if df.get(t))
    # 认识率闸只在大库上开（2026-11-04）：这个闸是拿 idf 加权算的，库越小 idf 越挤，
    # 十二段的示例库里问「我妈的膝盖怎么回事」认识率只有 31%，永远过不了。
    # 小库时靠下面的 specific 兜着——它要求问题里至少有一个「库里真有的稀有词」，
    # 问天气问闲事照样过不去，不会因为放宽这道闸就答非所问。
    if n >= 100 and total and seen / total < 0.4:
        return None, "这句里大半的词记忆里没有（%.0f%% 认识）。要问的是记忆里的事吗？" % (
            100.0 * seen / total)
    # 小语料保护（2026-11-04，把库发出去时发现的）：idf 的天花板随 n 变。
    # 12 段的示例库里 df=1 的词 idf 只有 2.16，绝对阈值够不着，于是任何问题都被判成
    # 「没有具体词」——第一次接自己那点语料的人一定撞上这个。阈值取「常数」和
    # 「半个天花板」里小的那个：库大的时候天花板远高于常数，行为跟原来一模一样。
    _cap = math.log(1.0 + (n + 0.5) / 0.5)
    _t2 = min(corpus.RARE_IDF2, _cap * 0.5)
    _t3 = min(corpus.RARE_IDF, _cap * 0.5)
    specific = [t for t in qv
                if len(t) >= 2 and (idf_of(df.get(t, 0), n) >=
                                    (_t2 if len(t) == 2 else _t3))
                and df.get(t)]
    if not specific:
        return None, "这句里没有能定住位置的具体词（今天、怎么这类不算）。换个说法。"
    return qv, None


def match_expr(qv, df, n):
    """只有语料里真有的词才能进 MATCH；按 idf 从高到低取前二十四个，别把全是废词的或串撑爆。"""
    toks = [t for t in qv if df.get(t)]
    toks.sort(key=lambda t: -idf_of(df[t], n))
    toks = toks[:24]
    return " OR ".join('"%s"' % t.replace('"', '""') for t in toks)


def literal_ratio(text, qv, df, n):
    """这句问题里，有多大质量真的落在这一段上。

    FTS5 的 bm25 只数「对上了的词」，对不上的词不扣分——所以「PHP 里数组怎么追加」
    能靠一个「追加」凑出高分。回到 recall.py 那套口径：按整句词的 idf 质量算占比，
    落点太少就判没命中，宁可安静也不硬凑。
    """
    low = text.lower()
    total = sum(w * idf_of(df.get(t, 0), n) for t, w in qv.items())
    if not total:
        return 0.0
    hit = sum(w * idf_of(df[t], n) for t, w in qv.items() if df.get(t) and t in low)
    return hit / total


def coverage():
    """库自己的覆盖边界（2026-11-04 加）。

    来由：目前只入库了约三分之一会话。问更早的事时检索安静地返回空，
    使用者分不清「没记过」和「那批会话根本没进来」。读不到边界一律给 None，
    绝不让检索因为这件事失败。
    """
    out = {"built_at": None, "cover_min": None, "cover_max": None,
           "n_segments": None, "n_sources": None, "n_events": None}
    try:
        conn = connect()
        for k in list(out):
            row = conn.execute("SELECT v FROM meta WHERE k = ?", (k,)).fetchone()
            if row and row[0] not in (None, ""):
                out[k] = row[0]
    except Exception:
        pass
    return out


def search(query, nseg=8, ndoc=5, kind=""):
    n, df = vocab()
    qv, err = build_query(query, df, n)
    if err:
        return {"err": err}
    expr = match_expr(qv, df, n)
    db = connect()
    avgdl = float(db.execute("SELECT v FROM meta WHERE k = 'avgdl'").fetchone()[0])
    # 库只干它擅长的：按 FTS5 的排序快速捞出候选（不重读 894 段正文）。
    # 打分仍走 corpus.bm25 同一把秤，比出来的分才说明问题——不然两套分不可比。
    hits = db.execute("SELECT anchor FROM seg_fts WHERE seg_fts MATCH ? "
                      "ORDER BY bm25(seg_fts) LIMIT 400", (expr,)).fetchall()
    need = {t: df.get(t) for t in qv if df.get(t)}
    idf_map = {r[0]: r[1] for r in db.execute(
        "SELECT term, idf FROM terms WHERE term IN (%s)" % ",".join("?" * len(need)),
        tuple(need))} if need else {}
    unseen = math.log(1.0 + (n + 0.5) / 0.5)
    segs = []
    for (anchor,) in hits:
        row = db.execute("SELECT sid, n, kind, time, fname, line, chars, dl, tokens, text "
                         "FROM segments WHERE anchor = ?", (anchor,)).fetchone()
        if not row or (kind and row[2] != kind):
            continue
        tf = Counter(row[8].split())
        score = corpus.bm25(qv, tf, row[7], idf_map, avgdl, unseen)
        if score < 0.3:  # 跟 recall.py 同一条线：低于 0.3 判没命中
            continue
        segs.append({"anchor": anchor, "score": score, "sid": row[0], "n": row[1],
                     "kind": row[2], "time": row[3], "fname": row[4],
                     "line": row[5], "chars": row[6], "text": row[9]})
    segs.sort(key=lambda x: -x["score"])
    events = []
    for sid, seq, title, a, b in db.execute(
            "SELECT sid, seq, title, a, b FROM events WHERE status = 'active'"):
        mine = [x for x in segs if x["sid"] == sid and a <= x["n"] <= b]
        if not mine:
            continue
        top = [x["score"] for x in mine[:2]]
        tscore = sum(1 for t in qv if t in corpus.terms(title) and df.get(t)) * 0.15
        events.append({"sid": sid, "seq": seq, "title": title, "a": a, "b": b,
                       "score": top[0] + 0.3 * (top[1] if len(top) > 1 else 0.0) + tscore,
                       "hits": len(mine), "segs": mine})
    events.sort(key=lambda e: -e["score"])
    db.close()
    if not segs:
        return {"err": "库里没落上合适的段。换个说法；或者去 origin\\topics.md 翻标题。"}
    return {"segs": segs[:nseg], "events": events[:ndoc]}


def book(events, segs):
    """记账：谁被查到就用一条 UPDATE 记住，不重写文件。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db = connect()
    for e in events:
        db.execute("UPDATE events SET hits = hits + 1, last_hit_at = ? WHERE sid = ? AND seq = ?",
                   (now, e["sid"], e["seq"]))
    db.commit()
    db.close()


def drill(spec):
    m = corpus.RANGE_RE.search(spec)
    if not m:
        sys.exit("回钻要一个范围，像 d1744eaa-036-d1744eaa-050")
    sid, a, b = m.group(1), int(m.group(2)), int(m.group(4))
    db = connect()
    ev = db.execute("SELECT seq, title FROM events WHERE sid = ? AND a = ? AND b = ?",
                    (sid, a, b)).fetchone()
    print("# %s %s %s" % (sid, "第%s事" % ev[0] if ev else "", ev[1] if ev else "(未登记)"))
    for anchor, fname, line, text in db.execute(
            "SELECT anchor, fname, line, text FROM segments WHERE sid = ? AND n BETWEEN ? AND ? "
            "ORDER BY n", (sid, a, b)):
        print("\n[%s] %s 第 %d 行" % (anchor, fname, line))
        print(text)
    db.close()


def outline(sid=""):
    """「我今天干了什么」那个视角：先按会话列出干过的每一件事，再点进去看细节。

    人回忆就是这么走的——先想起今天做了哪几件事，再定是哪一件，最后才是当时的细节。
    所以这张清单排在检索前面：不知道要问什么的时候，先翻它。
    """
    db = connect()
    where = "WHERE status = 'active'" + (" AND sid = ?" if sid else "")
    args = (sid,) if sid else ()
    rows = db.execute("SELECT sid, seq, title, a, b, hits FROM events %s "
                      "ORDER BY sid, seq" % where, args).fetchall()
    if not rows:
        print("没有登记过的事。")
        db.close()
        return
    cur = ""
    for s, seq, title, a, b, hits in rows:
        if s != cur:
            rng = db.execute("SELECT MIN(time), MAX(time), COUNT(*) FROM segments WHERE sid = ?",
                             (s,)).fetchone()
            print("\n会话 %s（%s–%s，%d 段）" % (s, rng[0] or "-", rng[1] or "-", rng[2]))
            cur = s
        print("%s%2d. %s  [%s-%03d-%s-%03d]" % ("★" if hits else " ", seq, title, s, a, s, b))
    print("\n★ 是被查到过的。点进某件事看原文：python db.py --钻 <会话>-001-<会话>-0NN")
    db.close()


def ledger():
    db = connect()
    rows = db.execute("SELECT sid, seq, title, hits, last_hit_at, weight FROM events "
                      "WHERE hits > 0 ORDER BY hits DESC, last_hit_at DESC LIMIT 25").fetchall()
    if not rows:
        print("账本还是空的：还没用 --记 查过东西。")
        return
    print("用得最多的条目（hits / 最后用到 / 权重）")
    for sid, seq, title, hits, last, w in rows:
        print("%3d 次  %-19s  %.1f  %s 第%s事 %s"
              % (hits, last or "-", w, sid, seq, title))
    db.close()


def main():
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("query", nargs="*")
    ap.add_argument("--段", dest="nseg", type=int, default=8)
    ap.add_argument("--事", dest="ndoc", type=int, default=5)
    ap.add_argument("--宽", dest="width", type=int, default=320)
    ap.add_argument("--类型", dest="kind", default="")
    ap.add_argument("--钻", dest="drill", default="")
    ap.add_argument("--记", dest="book", action="store_true")
    ap.add_argument("--账本", dest="ledger", action="store_true")
    ap.add_argument("--清单", dest="outline", nargs="?", const="", default=None)
    args, unknown = ap.parse_known_args()
    if unknown:
        args.query += unknown

    if args.outline is not None:
        outline(args.outline)
        return
    if args.ledger:
        ledger()
        return
    if args.drill:
        drill(args.drill)
        return

    query = " ".join(args.query).strip()
    if not query:
        sys.exit('给个问题。像：python db.py "钩子收不到那块怎么查的"')

    res = search(query, args.nseg, args.ndoc, args.kind)
    if res.get("err"):
        print(res["err"])
        return
    segs, events = res["segs"], res["events"]
    if not segs:
        print("库里没落上合适的段。换个说法；或者去 origin\\topics.md 翻标题。")
        return
    if args.book:
        book(events, segs)

    print("候选事（问题：%s）" % query)
    for i, e in enumerate(events, 1):
        print("%d. %.2f %d段  %s 第%s事 %s  [%s-%03d-%s-%03d]"
              % (i, e["score"], e["hits"], e["sid"], e["seq"], e["title"],
                 e["sid"], e["a"], e["sid"], e["b"]))

    print("\n命中段")
    qlong = [t for t in corpus.terms(query) if len(t) >= 2]
    for s in segs:
        owner = next((e for e in events
                      if e["sid"] == s["sid"] and e["a"] <= s["n"] <= e["b"]), None)
        tag = "第%s事 %s" % (owner["seq"], owner["title"]) if owner else "未归事"
        print("\n[%s] %.2f %s %s  %s" % (s["anchor"], s["score"], s["kind"], s["time"], tag))
        print(corpus.snippet(s["text"], qlong, args.width))
    if args.book:
        print("\n（这次查到的事已经记账）")


# DECISION: 引用可以写 anchor 也可以写 uid，两个都认 | when: 2026-11-02 | cause: 现场逼的
# DECISION: 引用按 uid 与 anchor 双认 | when: 2026-11-02 | cause: 现场逼的
def by_ref(ref):
    """按引用取一段：anchor 号或 uid 都认。

    为什么要两个都认（2026-11-02）：anchor 是「全册第几段」，位置依赖本身就是脆的——
    标题改名、重切、跨文件合并，位置都会跟着变，旧引用就静默指错。
    uid 挂在标题上，不随这些变。新写的引用一律写 uid；旧的 anchor 号继续有效，不用一次性改完。

    **更正一句曾经写错的话**：这里原先写着「被门槛滤掉一段、或补回一段，后面的号会全体后移」。
    实测不是这样——滤掉的段在编号时照样占号、只是不入库，库里 anchor 本来就带洞，
    补回原位不动。所以 uid 解决的不是漂移，是「引用别挂在位置上」。
    """
    db = connect()
    try:
        return db.execute("SELECT anchor, sid, uid, text FROM segments "
                          "WHERE anchor = ? OR uid = ? LIMIT 1", (ref, ref)).fetchone()
    finally:
        db.close()


if __name__ == "__main__":
    main()
