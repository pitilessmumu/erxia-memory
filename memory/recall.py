# -*- coding: utf-8 -*-
# DECISION: 匹配度用 idf 占比，不用求和也不用余弦 | when: 2026-11-02 | cause: 现场逼的
"""记忆检索：一个问题 → 先落「事」→ 再落「段」，带锚点，可回钻原文。

数据来自仓库根下的 origin（位置见 config.py）：
  topics.md          三层里的「事」（157 条，每条一个锚点范围）
  corpus\\<sid>.md    三层里的「段」（894 段，段头 ## [锚点] 类型 时间）

打法：匹配在「段」上算（细节都在段里，事标题是概括过的，只拿标题搜会漏），
呈现按「事」归拢（先给粗的候选，再给细的原文位置）——就是木木说的先粗后细、可回退。

打分：字符 1-gram + 2-gram 的 TF-IDF 余弦。2-gram 保「完整/整性」这种词形，
1-gram 兜「钩子 / 摘钩」这种换字不换意思的。idf 在段一级算。

用法：
  python recall.py "上次那个钩子收不到是怎么回事"
  python recall.py "改钩子" --段 4 --宽 300
  python recall.py --钻 d1744eaa-036-d1744eaa-050 --宽 600   # 回钻整段原文
  python recall.py "桥" --类型 木木                            # 只看木木说的话
"""
import argparse
import math
import re
import sys
from collections import Counter
from pathlib import Path

import config  # 路径统一从 config 取（config.py 不 import 本项目任何模块，不会循环依赖）

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# 原来按 __file__ 往上两级找根，代码一搬出原目录就全错；现在由 config 定根
ROOT = config.ROOT
ORIGIN = config.ORIGIN
TOPICS = config.TOPICS
CORPUS = config.CORPUS

STOP = set("的了是在我你他她它这那和就也都很不到有个一上下不吗呢吧啊么什怎为以与及或而但已还被把让从对向于之其里来说会能可没好又些过所因为然后次东西干那个怎样")

# 聊天里满地都是、当检索词等于没用的字。整串两个字都落在这堆里，这个两字词就不收
# ——不然「今天」「天气」「时候」「怎么样」会把闲话也凑出一堆分来。
FILLER = set("今天明昨现最刚气怎什时感知觉么样候该应当知")

# 段头 ## [锚点] 类型 YYYY-MM-DD HH:MM:SS。日期 2026-11-02 才补上，旧的只有时分秒——
# 两种都认：不认旧的，那一段会被并进上一段的正文里，比时间缺日期更糟。
SEG_RE = re.compile(r"^##\s*\[([^\]]+)\]\s*(\S+)\s*((?:\d{4}-\d{2}-\d{2} )?\d{2}:\d{2}:\d{2})\s*$")
SESS_RE = re.compile(r"^会话\s+([0-9a-f]{6,})")
RANGE_RE = re.compile(r"([0-9a-f]{6,})-(\d{2,3})-([0-9a-f]{6,})-(\d{2,3})")
ITEM_RE = re.compile(r"^\s*(\d+)\.\s*(.+?)\s*[｜|]\s*(.+?)\s*$")


def is_word(ch):
    return ch.isdigit() or ("a" <= ch.lower() <= "z") or ("\u4e00" <= ch <= "\u9fff")


def terms(text):
    out, runs, cur = [], [], []
    for ch in text:
        if is_word(ch):
            cur.append(ch.lower())
        else:
            if cur:
                runs.append("".join(cur))
                cur = []
    if cur:
        runs.append("".join(cur))
    for run in runs:
        n = len(run)
        for i in range(n):
            if run[i] not in STOP:
                out.append(run[i])
            if i + 1 < n and not (run[i] in FILLER and run[i + 1] in FILLER):
                out.append(run[i:i + 2])
        # 整串只留「像标识符的」：带英文数字的命令名、参数名、文件名。
        # 中文长串整串保留以前是个坑——问题原句只会在「转述这个问题」的段里出现一次，
        # idf 高到离谱，把真正的答案段挤出去了。
        if 3 <= n <= 24 and not ("\u4e00" <= run[0] <= "\u9fff"):
            out.append(run)
    return out


def parse_segments():
    segs = {}
    if not CORPUS.is_dir():
        sys.exit("缺语料目录：" + str(CORPUS))
    for f in sorted(CORPUS.glob("*.md")):
        sid = f.stem
        anchor, kind, tm, buf = None, "", "", []
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            m = SEG_RE.match(line)
            if m:
                if anchor:
                    segs[anchor] = {"sid": sid, "kind": kind, "time": tm,
                                    "text": "\n".join(buf).strip()}
                anchor, kind, tm, buf = m.group(1), m.group(2), m.group(3), []
            elif anchor is not None:
                buf.append(line)
        if anchor:
            segs[anchor] = {"sid": sid, "kind": kind, "time": tm,
                            "text": "\n".join(buf).strip()}
    return segs


def parse_topics():
    docs, sid = [], ""
    if not TOPICS.is_file():
        sys.exit("缺事表：" + str(TOPICS))
    for line in TOPICS.read_text(encoding="utf-8", errors="replace").splitlines():
        m = SESS_RE.match(line.strip())
        if m:
            sid = m.group(1)
            continue
        m = ITEM_RE.match(line)
        if not m:
            continue
        r = RANGE_RE.search(m.group(3))
        if r:
            docs.append({"sid": r.group(1), "seq": int(m.group(1)),
                         "title": m.group(2).strip(),
                         "a": int(r.group(2)), "b": int(r.group(4))})
    return docs


def aid(sid, n):
    return "%s-%03d" % (sid, n)


def tvec(toks):
    return {t: 1.0 + math.log(c) for t, c in Counter(toks).items()}


K1, B = 1.2, 0.75

# 换个说法就不认得了，是人也不会这样。只放真正会混的几组，别贪多——加宽了会把不相干的段一起捞上来。
ALIAS = [
    ["多帧", "zstd", "解压", "会话原文", "jsonl"],
    ["钩子", "hook", "hookd", "低完整性", "UIPI"],
    ["硬链接", "mklink", "junction", "同步"],
    ["停摆", "卡住", "不动了", "不投了", "断了"],
    ["闪黑窗", "黑窗口", "黑窗", "闪一下"],
    ["报错", "错误", "stderr", "报错原文"],
]

DEAD_DF = 0.3  # 出现在三成以上段里的词，等于没信息量（今天、怎么、时候）
RARE_IDF = 3.0   # 三字以上的词形，够稀就算定得住位置
RARE_IDF2 = 3.6  # 两字的要更稀，「今天」这种两字词才混不进来


def bm25(q, tf, dl, idf, avgdl, miss=0.0):
    """匹配度，落在 0～2.2 之间：命中词的 idf 质量 ÷ 整句词的 idf 质量。

    以前用「求和」+余弦，两种都踩过坑：求和让一堆低信息量的词（今天、怎么）凑出高分，
    余弦把「说得最全的长段」压下去。改成算占比，一句闲话就压不住真正对上的词。
    miss 是「语料里根本没有的词」按多少 idf 记——给高值，问错方向的话自己就沉下去。
    """
    if not q or not dl:
        return 0.0
    num, den = 0.0, 0.0
    for t, w in q.items():
        base = w * idf.get(t, miss)
        den += base
        f = tf.get(t)
        if not f:
            continue
        num += base * f * (K1 + 1.0) / (f + K1 * (1.0 - B + B * dl / avgdl))
    if den <= 0:
        return 0.0
    return min(num / den, K1 + 1.0)


def snippet(text, qlong, width):
    t = re.sub(r"\s+", " ", text).strip()
    if len(t) <= width:
        return t
    pos = -1
    for term in sorted(qlong, key=len, reverse=True):
        p = t.find(term)
        if p >= 0:
            pos = p
            break
    start = 0 if pos < 0 else max(0, pos - width // 3)
    if start + width > len(t):
        start = max(0, len(t) - width)
    return ("…" if start else "") + t[start:start + width] + \
           ("…" if start + width < len(t) else "")


def main():
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("query", nargs="*")
    ap.add_argument("--事", dest="nd", type=int, default=5)
    ap.add_argument("--段", dest="ns", type=int, default=8)
    ap.add_argument("--宽", dest="width", type=int, default=320)
    ap.add_argument("--钻", dest="drill", default="")
    ap.add_argument("--类型", dest="kind", default="")
    ap.add_argument("--最低", dest="floor", type=float, default=0.3)
    ap.add_argument("--json", dest="json", action="store_true")
    args, unknown = ap.parse_known_args()
    if unknown:
        args.query += unknown

    segs = parse_segments()
    docs = parse_topics()

    if args.drill:
        r = RANGE_RE.search(args.drill)
        if not r:
            sys.exit("回钻要一个范围，像 d1744eaa-036-d1744eaa-050")
        sid, a, b = r.group(1), int(r.group(2)), int(r.group(4))
        hit = [d for d in docs if d["sid"] == sid and d["a"] == a and d["b"] == b]
        print("# %s %s %s" % (sid, "第%s事" % hit[0]["seq"] if hit else "", hit[0]["title"] if hit else "(未登记)"))
        for n in range(a, b + 1):
            s = segs.get(aid(sid, n))
            if not s or (args.kind and s["kind"] != args.kind):
                continue
            print("\n[%s] %s %s" % (aid(sid, n), s["kind"], s["time"]))
            print(re.sub(r"\n{3,}", "\n\n", s["text"])[:args.width])
        return

    query = " ".join(args.query).strip()
    if not query:
        sys.exit('给个问题。像：python recall.py "钩子收不到那块怎么查的"')

    # 段一级：tf + 长度
    total = 0
    for a, s in segs.items():
        toks = terms(s["text"])
        s["tf"] = Counter(toks)
        s["dl"] = len(toks)
        total += s["dl"]
    avgdl = total / max(1, len(segs))

    df = Counter()
    for s in segs.values():
        for t in s["tf"]:
            df[t] += 1
    n = max(1, len(segs))
    idf = {t: math.log(1.0 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}

    qlist = terms(query)
    qv = {t: 1.0 for t in qlist}
    # 一句话里绝大多数词都是废话，先按出现广度砍掉（今天、怎么、时候这类）
    qv = {t: w for t, w in qv.items() if df.get(t, 0) <= DEAD_DF * n}
    if not qv:
        print("这句里没有能当检索词的成分（要问的是记忆里的事，不是天气）。")
        return
    # 换个说法也得认：命中同义组就把它那组的词一起挂上，权重给一半
    for group in ALIAS:
        if not any(g in query or g in qv for g in group):
            continue
        for g in group:
            for t in terms(g):
                if t not in qv:
                    qv[t] = 0.5
    qlong = [t for t in qv if len(t) >= 2]

    # 底噪：这句话里大半的词在语料里根本不存在，就不是在问记忆里的事
    unseen_idf = math.log(1.0 + (n + 0.5) / 0.5)

    # 长问题别截词：试过只留最「值钱」的十二个词，结果反而把同义组挤掉、把闲话抬上来，
    # 考卷从 10/10 掉到 8/10。凑热闹的长句交给调用方先收窄（inject.py 就是这么干的）。

    total_mass = sum(w * idf.get(t, unseen_idf) for t, w in qv.items())
    seen_mass = sum(w * idf.get(t, unseen_idf) for t, w in qv.items() if df.get(t))
    if total_mass and seen_mass / total_mass < 0.4:
        print("这句里大半的词记忆里没有（%.0f%% 认识）。要问的是记忆里的事吗？"
              % (100.0 * seen_mass / total_mass))
        return

    # 一句真问题里，总得有一两个「具体的词形」（UIPI、多帧、硬链接这种）。
    # 只对上今天、怎么这类到处都是的词，说明问的不是记忆里的事——单字一律不算。
    def specific(t):
        if len(t) < 2:
            return False
        return idf.get(t, 0.0) >= (RARE_IDF2 if len(t) == 2 else RARE_IDF)

    rare = {t for t in qv if specific(t)}
    if not rare:
        print("这句里没有能定住位置的具体词（今天、怎么这类不算）。换个说法。")
        return

    scored = []
    for a, s in segs.items():
        if args.kind and s["kind"] != args.kind:
            continue
        if not any(t in s["tf"] for t in rare):
            continue
        sc = bm25(qv, s["tf"], s["dl"], idf, avgdl, unseen_idf)
        if sc > 0:
            scored.append((sc, a, s))
    scored.sort(key=lambda x: -x[0])

    # 被推翻的旧段在这里滤掉（登记表见 memory\supersede.py）：只滤「命中候选」这一层，
    # --钻 回钻历史原文不滤——那是在翻旧账，旧的得看得见。登记表坏了就当没推翻。
    try:
        import supersede as _supersede
        _rep = {}
        _kept = _supersede.filter_segs([{"anchor": a} for _sc, a, _s in scored], _rep)
        if _rep.get("n_dropped"):
            _alive = {x["anchor"] for x in _kept}
            scored = [x for x in scored if x[1] in _alive]
            print("（滤掉 %d 段被推翻的旧结论）" % _rep["n_dropped"], file=sys.stderr)
    except Exception as _e:
        print("（推翻登记表用不上：%s: %s）" % (type(_e).__name__, _e), file=sys.stderr)

    if not scored or scored[0][0] < args.floor:
        best = scored[0][0] if scored else 0.0
        print("没落上合适的段（最高分 %.3f）。换个说法；或者去 origin\\topics.md 翻标题。" % best)
        return

    # 段 → 事：每件事取它名下最强的两段，标题再单独加一点
    titles = [terms(d["title"]) for d in docs]
    tavg = sum(len(t) for t in titles) / max(1, len(titles))
    for d, tt in zip(docs, titles):
        inrange = [(sc, a, s) for sc, a, s in scored[:400]
                   if s["sid"] == d["sid"] and d["a"] <= int(a[-3:]) <= d["b"]]
        d["segs"] = inrange
        top = [x[0] for x in inrange[:2]]
        tsc = bm25(qv, Counter(tt), len(tt), idf, max(tavg, 1.0), unseen_idf)
        d["score"] = (top[0] if top else 0.0) + 0.3 * (top[1] if len(top) > 1 else 0.0) + 0.5 * tsc
        d["hits"] = len(inrange)

    docs.sort(key=lambda d: -d["score"])
    cand = [d for d in docs if d["score"] >= args.floor][:args.nd]

    if args.json:
        import json
        out = {"query": query,
               "docs": [{"sid": d["sid"], "seq": d["seq"], "title": d["title"],
                         "a": d["a"], "b": d["b"], "score": round(d["score"], 4),
                         "hits": d["hits"]} for d in cand],
               "segs": [{"anchor": a, "score": round(sc, 4), "kind": s["kind"],
                         "time": s["time"]} for sc, a, s in scored[:args.ns]]}
        print(json.dumps(out, ensure_ascii=False))
        return

    print("候选事（问题：%s）" % query)
    for i, d in enumerate(cand, 1):
        print("%d. %.3f %d段  %s 第%s事 %s  [%s-%03d-%s-%03d]"
              % (i, d["score"], d["hits"], d["sid"], d["seq"], d["title"],
                 d["sid"], d["a"], d["sid"], d["b"]))
    if not cand:
        print("（段有分，但没归到登记过的事上）")

    seen, shown = set(), 0
    print("\n命中段")
    for sc, a, s in scored:
        if shown >= args.ns:
            break
        if a in seen:
            continue
        seen.add(a)
        shown += 1
        owner = next((d for d in docs if d["sid"] == s["sid"] and d["a"] <= int(a[-3:]) <= d["b"]), None)
        tag = "第%s事 %s" % (owner["seq"], owner["title"]) if owner else "未归事"
        print("\n[%s] %.3f %s %s  %s" % (a, sc, s["kind"], s["time"], tag))
        print(snippet(s["text"], qlong, args.width))


if __name__ == "__main__":
    main()
