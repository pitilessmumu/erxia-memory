# -*- coding: utf-8 -*-
# DECISION: 候选池单独一层，重排的命令行也来这儿取候选 | when: 2026-11-04 | cause: 结构理出来的
"""候选池：字面（SQLite/FTS5）和语义（bge-small）各拿一批，按 anchor 去重交错合并。

这一层只回答「哪些段落值得看」，不回答「哪一段答得上这个问题」——后者是 rerank.py 的事，
最后的取舍、出口闸、空结果解释在 hybrid.py。

为什么要独立一层（2026-11-04 改的）：它的实现原来住在 hybrid.py 里，而 rerank.py 的
命令行又要 import hybrid 去拿候选，两边就互相依赖了——改任何一边的签名，另一边都跟着震。
搬出来以后依赖是单向的：hybrid -> pool、rerank -> pool，池子自己不认任何人。
"""
import sys

# 影子块的门槛：真人问法跟事实段的语义相似度到这个数，才把 parent 拉进候选。
SHADOW_SIM = 0.62

# DECISION: 送重排的候选 20 -> 60 | when: 2026-11-03 | cause: 现场逼的
# 实测：问「球跑一阵就不动了、消息不再投递到会话」，正确答案（讲桥写死投 20 条的）
# 在语义里排第 **30**——被 CAND=20 挡在门外，可重排给它 **0.8567**。
# 精排判得准、粗排把它丢了，这是双塔粗排的固有短板；只能让重排多看一眼。
# 代价：每次多比 ~40 对，实测每问多 1-2 秒。杂问会不会因此更容易撞，跑完看。
CAND = 35          # 合并后送重排的候选数


def candidates(q, k=CAND):
    """宽召回：字面（SQLite/FTS5）和语义（bge-small）各拿一批，按 anchor 去重交错合并。

    交错而不是「字面全要、语义补位」：字面 top20 很可能全被同一个词的变体占满，
    语义那条正题就挤不进候选了。交错合并保证两路各自的头部都进得来。
    返回 (合并候选, events, 字面候选, 语义最高分)。
    """
    import db as dbmod
    import vec

    segs, events = [], []
    r = dbmod.search(q, nseg=k, ndoc=3)
    if not r.get("err"):
        segs = list(r.get("segs") or [])
        events = r.get("events") or []
    v = vec.search(q, k=k, floor=0.0)
    vsegs = [] if v.get("err") else (v.get("segs") or [])
    # 影子块（真人问法）：命中只把 parent 那段拉进候选，影子块自己不作为答案。
    # 三条硬约束见 memory\shadow.py 头注释（不进字面索引、不当答案、不到线不进）。
    try:
        import shadow
        have = {s.get("anchor") for s in vsegs}
        for sp in shadow.search(q, k=3, floor=SHADOW_SIM):
            if sp["parent"] not in have:
                vsegs.append({"anchor": sp["parent"], "score": sp["sim"], "via": "shadow"})
                have.add(sp["parent"])
    except Exception as e:
        print("影子块跳过（%s: %s）" % (type(e).__name__, e), file=sys.stderr)
    vtop = max([float(s.get("score") or 0) for s in vsegs], default=0.0)
    out, seen = [], set()
    for i in range(max(len(segs), len(vsegs))):
        for src in (segs, vsegs):
            if i >= len(src):
                continue
            s = src[i]
            a = s.get("anchor")
            if not a or a in seen:
                continue
            seen.add(a)
            out.append(s)
    out = out[:k]
    # 语义那路只带 160 字截断，重排要看全段：缺的从正文补回来。
    if any(len(s.get("text") or "") <= 160 for s in out):
        import corpus
        full = corpus.parse_segments()
        for s in out:
            body = (full.get(s.get("anchor")) or {}).get("text")
            if body:
                s["text"] = body
    return out, events, segs, vtop
