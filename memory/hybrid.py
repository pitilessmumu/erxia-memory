# -*- coding: utf-8 -*-
r"""混着来：字面和语义一起宽召回，交给本机 cross-encoder 重排。

2026-11-02 之前是二选一：字面够硬（≥0.55）只走字面，否则语义先上、字面接后。
那套的毛病在「单门槛」——字面分和语义分不是同一把尺，正题 0.550 和安静题
0.568 能挤在同一个区间，怎么调都有一边要漏。

现在默认（RERANK_ON=True）：两路各取候选、按 anchor 去重交错合并取前 20，
用 bge-reranker-v2-m3（int8 ONNX，CPU）逐条打相关分，按重排分排序、按重排分
定门槛（RERANK_FLOOR）。过了门槛还有一道「对账」，2026-11-02 改成**分段生效**：
重排最高那段 ≥0.75（很确信）时直接放行，只要求问题跟那段有 ≥2 字词面重合；
落在 0.30–0.75 这段弱证据区才走严格对账——字面排第一的那段也得被重排认
（≥0.15）、且跟问题有词面重合，不认就当杂问撞库、安静。
字面一条都没落上时另有一道「安静闸」（详见 search 里的注释）。
2026-11-02 再加两样：重排分乘来源权威系数（只改排序，门槛仍看未加权分）；
判安静前用抽屉的「找我的说法」别名行做一轮纯本地兜底（route=兜底）。
2026-11-02 又加一层「哪条还算数」：返回前按推翻登记表（memory\supersede.py）把旧段
滤掉——只动返回值，门槛和分段逻辑不碰（见 search / _drop_superseded）。
老逻辑原样留着，rerank=False 或重排出错时回退。

用法：python hybrid.py "要办的事" [--宽 8] [--关] [--json]
      --关 走回退的那套老逻辑（字面够硬只走字面）
"""
import os
import sys, json
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import pool  # 候选池在单独一层（见 pool.py）

# 2026-11-02 加题重校：5 道安静题的语义 top1 最高 0.568（PHP 数组），
# 0.59 留 0.022 余量；降到 0.55 会让「PHP 数组」假阳性，所以不再往下压。
VEC_FLOOR = 0.59
# 字面拿到「够硬的证据」就不再问语义。2026-11-02 加题重校：0.45 放行了
# 「重启带对象」这种 0.49 的错段（字面凑出分、但内容不对），所以抬到 0.55。
# 真硬证据（硬链接 0.89、电池 1.01、考卷/回退 0.55）仍直接走字面。
LEX_STRONG = 0.55

# 2026-11-02 加重排：字面/语义两把尺不同，单门槛分不开正题（0.550）和安静题
# （0.568）。改成「先宽召回、再让 cross-encoder 判」，门槛改用重排分（0~1）。
# 开关默认开；关掉就仍走下面原来那套「字面够硬只走字面、否则语义先上」。
RERANK_ON = True
# 否定／比较类问句：不许走「高分免对账」那条快路。
# 它们跟库里内容共享大量词汇，重排容易给高分，可答案是相反的：把 A 的当成 B 的递上来。
RE_NEGATE = __import__("re").compile(
    r"是不是|有没有|为什么[没不]|为啥[没不]|为什么不是|不如|相比|区别|还是|难道|"
    r"是不是该|要不要|该不该|哪个更|凭什么|真的吗|取消了|废了|不选|没选|不装|没装")

# 换说法兜底的门槛：换出来的问法，重排最高分得过这条线才许当答案。
# 没过就闭嘴——「我没有」比「我给你这个」安全。
EXPAND_FLOOR = 0.45
RERANK_FLOOR = 0.30
# 送重排的候选数 CAND 与影子块门槛 SHADOW_SIM 在 pool.py（候选池那层，2026-11-04 搬走）
# 字面一条都没落上时（问题里没有库里的具体词），不能光靠重排一条 0.63 的段就出声：
# 「今天天气怎么样」能撞上转写测试里那句「今天天氣不錯」，重排分 0.79，但它不是记忆。
# 这时要过安静闸：语义 ≥0.59，或者「问题里有记忆认得的词 + 重排 ≥0.75」才开口。
VEC_LINE = 0.59
CONFIDENT = 0.75
# 2026-11-02 压力测试后加的「对账」判据：字面排第一的那段，cross-encoder 认不认。
# 十来条日常杂问（推荐电影、写请假条、手机怎么截图、我会写代码吗）字面都能撞出一两条，
# 但那条段的重排分全 ≤0.03；真问题里最弱的一条也有 0.22，门槛 0.15 两边都不碰。
# 2026-11-02 晚改成**分段生效**：对账只管 0.30–0.75 这段弱证据区，重排 ≥0.75 直接放行。
# 来由是那回考卷掉到 18/19——「记忆索引库用什么存的」正解重排 0.873，被字面头一条
# （重排 0.106）否掉。对账本是防语义低分胡说的，对满分也生效就等于让最弱信号压最强信号。
# 放行不是无条件：还得跟「重排最高那段」有 ≥2 字词面重合。实测杂问最高 0.68、
# 14 道正题全部重合，两边都不碰。别再把 0.75 这条撤掉——语料要是把杂问原话抄进去，
# 0.85 的自撞会回来（见「踩过的坑」里那条）。
GATE_AGREE = 0.15

# —— 来源权威加权（2026-11-02 加）——
# 重排分只回答「这段答不答这个问题」，不回答「这段还算不算数」。抽屉是一件事一条、
# 人工维护并带历次变更，权威度最高；常设/规则/二夏记忆是定过的规矩，次之；
# 聊天原文是当时的话，往往已被后来的结论推翻，压最低。系数只乘在重排分上，
# rerank/vec/db 的打分逻辑一律不动。
# 聊天 0.65 是实测定的：用建议的 0.85 时「为什么现在不自动换壳了」聊天 0.970×0.85=0.825
# 仍压着抽屉 0.672，进不了前二；压到 0.65 才让抽屉排到第二（0.970×0.65=0.631），
# 比 0.68 多留一点余量，以后重建索引分数小幅漂移也不会把抽屉挤下去。
# 以后调这里只影响排序，不影响召回；门槛仍看下面未加权的 rerank 分（见 search 里的注释）。
SRC_WEIGHT = {
    "ac000005": 1.00,   # 抽屉：一件事一条，权威度最高
    "ac000001": 0.95,   # 常设
    "ac000003": 0.95,   # 规则
    "ac000004": 0.95,   # 二夏记忆
    # 2026-11-02 实测：换说法时回执霸榜（WB 那二十句里「答偏」的第一名六次是它）。
    # 它是第三方评论，词多覆盖广，不该跟自家事实同权。
    "ac000006": 0.55,   # 智谱回执
    "ac000008": 0.95,   # 壳内容（每壳定下了什么）
    "ac000011": 0.95,   # 规矩表
    "ac000012": 1.00,   # 人物侧写：问他是什么样的人，这个该最权威
    "ac000013": 1.00,   # 施工进度：问现在到哪一步，这个该最权威
    "ac000014": 0.95,
    "ac000015": 0.90,   # 错题集：问「这个坑以前怎么修的」   # 决策索引：问某个决定为什么这么做
    "ac000009": 0.90,   # 壳目录
    "ac000010": 0.80,   # 片（阅读单位，字面是从段里抄的，别让它抢召回）
}
SRC_WEIGHT_CHAT = 0.65  # 其余 sid：历史聊天原文

# —— 安静前的本地兜底（2026-11-02 加）——
# 短问常常「有主体词、但 FTS 被几个虚词带偏」：侧键当初为什么要做、说话的模型叫什么，
# 两条都一条没落上。判安静之前再用主体词去抽屉的「找我的说法」别名行里找一遍，
# 命中就用别名行本身过重排闸（同一个 RERANK_FLOOR），全过程本地、不调云端。
FALLBACK_RARE_IDF = 2.5  # 命中的主体词里至少得有一个够稀，光靠「为什么/什么」不算数


def _check_thresholds():
    """阈值自检：这几个数是模型之外唯一能「一改就悄悄变差」的地方，越界当场抛。

    这是什么东西：把「取值范围」当接口的一部分写进代码，而不只是写在注释里。
    改了不报错、只是分数一起漂——那是 LOCAL-081 那种静默失败。
    第一版只查明显的越界（负值、超过 1 的概率、超过满分 2.2 的分），不追求覆盖全部。
    """
    for name, v, lo, hi in (
            ("VEC_FLOOR", VEC_FLOOR, 0.0, 1.0),
            ("VEC_LINE", VEC_LINE, 0.0, 1.0),
            ("LEX_STRONG", LEX_STRONG, 0.0, 2.2),
            ("RERANK_FLOOR", RERANK_FLOOR, 0.0, 1.0),
            ("CONFIDENT", CONFIDENT, 0.0, 1.0),
            ("GATE_AGREE", GATE_AGREE, 0.0, 1.0),
            ("EXPAND_FLOOR", EXPAND_FLOOR, 0.0, 1.0)):
        if not (lo <= v <= hi):
            raise ValueError("阈值 %s = %r 跑出范围 [%s, %s]：改错了，或者注释过期了"
                             % (name, v, lo, hi))


_check_thresholds()


def lex_overlap(q, text):
    """问题跟这段有没有 ≥2 个字的词面重合（中文取二元字，英文/数字取整词）。

    为什么不用单字：字面这套是 1-gram + 2-gram 混合打的，「我会写代码吗」能靠
    「会」「写」这种单字凑出候选，但那段跟它其实毫无关系。二元词重合才算真沾边。
    """
    import re as _re
    def grams(s):
        s = _re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]+", " ", (s or "").lower())
        out = set()
        for chunk in s.split():
            if chunk.isascii() and len(chunk) >= 2:
                out.add(chunk)
            elif not chunk.isascii():
                for i in range(len(chunk) - 1):
                    out.add(chunk[i:i + 2])
        return out
    t = text or ""
    return any(g in t for g in grams(q))


def src_weight(anchor):
    """按 sid 前缀取来源权重：抽屉 1.0、常设/规则/二夏记忆 0.95、聊天原文 0.68。"""
    sid = str(anchor or "").split("-")[0]
    return SRC_WEIGHT.get(sid, SRC_WEIGHT_CHAT)


_ALIAS_CACHE = None


def _alias_index():
    """抽屉条目的「找我的说法」别名行：[(anchor, 别名行), ...]，进程内只读一次库。"""
    global _ALIAS_CACHE
    if _ALIAS_CACHE is not None:
        return _ALIAS_CACHE
    import re
    import db as dbmod
    db = dbmod.connect()
    rows = db.execute("SELECT anchor, text FROM segments "
                      "WHERE text LIKE '%找我的说法%'").fetchall()
    db.close()
    pat = re.compile(r"找我的说法[：:]\s*(.+)")
    out = []
    for anchor, text in rows:
        for line in (text or "").splitlines():
            m = pat.search(line)
            if m:
                out.append((anchor, m.group(1).strip()))
                break
    _ALIAS_CACHE = out
    return out


def local_fallback(q, k=8):
    """判安静之前的一轮纯本地兜底：主体词 -> 别名行 -> 重排闸。

    只在主路要交白卷时调用。做法：从问句里取库认识的 2 字以上词（不做云端分词），
    到抽屉的「找我的说法」别名行里找；至少命中两个词、且其中一个够稀，才算候选。
    候选的别名行本身送 cross-encoder 打分，仍要过 RERANK_FLOOR 才放行——
    不是「命中就无条件返回」，杂问照样得闭嘴。命中标 route=兜底。
    """
    import db as dbmod
    import rerank as rk

    n, df = dbmod.vocab()
    qv, qerr = dbmod.build_query(q, df, n)
    if qerr:
        return None
    toks = [t for t in qv if len(t) >= 2 and df.get(t)]
    if len(toks) < 2:
        return None

    picks = []  # (anchor, 别名行, 命中词, 命中词 idf 质量)
    for anchor, alias in _alias_index():
        low = alias.lower()
        hit = [t for t in toks if t in low]
        if len(hit) < 2:
            continue
        if not any(dbmod.idf_of(df[t], n) >= FALLBACK_RARE_IDF for t in hit):
            continue
        picks.append((anchor, alias, hit))
    if not picks:
        return None

    scores = rk.rerank(q, [p[1] for p in picks])
    best, best_score = None, 0.0
    for (anchor, alias, hit), sc in zip(picks, scores):
        if float(sc) > best_score:
            best, best_score = (anchor, alias, hit), float(sc)
    if best is None or best_score < RERANK_FLOOR:
        return None

    anchor, alias, hit = best
    db = dbmod.connect()
    row = db.execute("SELECT sid, n, kind, time, fname, line, chars, dl, tokens, text "
                     "FROM segments WHERE anchor = ?", (anchor,)).fetchone()
    db.close()
    if not row:
        return None
    s = {"anchor": anchor, "sid": row[0], "n": row[1], "kind": row[2], "time": row[3],
         "fname": row[4], "line": row[5], "chars": row[6],
         "score0": round(best_score, 4), "rerank": round(best_score, 4),
         "score": round(best_score * src_weight(anchor), 4),
         "text": row[9], "alias_hit": hit, "alias_score": round(best_score, 4)}
    return {"q": q, "route": "兜底", "segs": [s], "fallback": True,
            "fallback_terms": hit}


# DECISION: 语义候选 3 -> 30 | when: 2026-11-03 | cause: 现场逼的
# 实测：问「球跑一阵就不动了、消息不再投递到会话」，正确答案（讲桥写死投 20 条的）
# 在语义里排 **第 30**（0.654），只取前 3 就把它丢了；可重排给它 **0.8567**。
# 精排判得准、粗排把它丢了——双塔粗排的固有短板。取 30 让重排有机会看到它。
def _search(q, k=8, floor=VEC_FLOOR, vec_k=30, lex_strong=LEX_STRONG, rerank=RERANK_ON):
    if rerank:
        try:
            segs, events, lex, vtop = pool.candidates(q, pool.CAND)
            if not segs:
                fb = local_fallback(q, k)
                if fb:
                    fb["events"] = events
                    return fb
                return {"q": q, "route": "字面", "segs": [], "events": events}
            import rerank as rk
            # 复制一份再改写分数：字面兜底时要拿回原来的 lex（同一批 dict 不能就地改）。
            segs = [dict(s) for s in segs]
            scores = rk.rerank(q, [s.get("text") or "" for s in segs])
            # DECISION: 有自述句的段用两把尺子取高的 | when: 2026-11-03 | cause: 现场逼的
            # 重排对「问题 vs 长段」分不开（实测 0.074），对「问题 vs 一句短自述」能给 0.9952。
            # 所以给配了自述句的段再比一次，取 max——没配自述句的段行为不变。
            # DECISION: 自述句只抬排序分，不动闸门 | when: 2026-11-03 | cause: 做歪的
            # 原来把 max(原文分, 自述句分) 直接写回 scores，等于自述句既能排序又能过闸门。
            # 实测副作用：11 个「沾边但不该出声」的问题里 8 个被抬过 0.30——
            # 「桥牌怎么玩」0.545、「智谱好用吗」0.924。根因是自述句含关键词，
            # 而「故障自述」跟「故障求助」天然同构（用户遇到泄漏，问的就是「输出混进东西」）。
            # 现在：排序用 rank（取高的），闸门仍只看 rerank（原文分）。
            rank_scores = list(scores)
            try:
                import selfdesc as _sd
                _bonus = _sd.scores(q, [s.get("anchor") for s in segs])
                rank_scores = [max(a, b) for a, b in zip(scores, _bonus)]
            except Exception:
                pass
            for s, sc in zip(segs, scores):
                s["score0"] = s.get("score")
                s["rerank"] = round(float(sc), 4)   # 未加权的重排分，门槛只认它
                s["score"] = round(s["rerank"] * src_weight(s.get("anchor")), 4)
            # 排序分：有自述句的段用抬过的那个，没有的就是原文分。
            # 只影响先后，不影响谁能过闸门（闸门读的是 rerank）。
            for s, rs in zip(segs, rank_scores):
                s["rank"] = round(float(rs) * src_weight(s.get("anchor")), 4)
            segs.sort(key=lambda s: -float(s.get("rank") or s.get("score") or 0))
            # 门槛仍看未加权的 rerank：RERANK_FLOOR 是对 cross-encoder 校准出来的证据线，
            # 乘上来源系数后它就不再是那个意思了。实测直接拿加权分过门槛，会把 0.30 的
            # stdout 段压到 0.204、0.32 的考卷段压到 0.218，考卷当场掉两题。
            keep = [s for s in segs if float(s.get("rerank") or 0) >= RERANK_FLOOR]
            # 对账：重排最高的那段，字面那头认不认。字面头一条的重排分太低，说明这个「撞上」
            # 只是词面巧合（推荐电影撞上「先打主我给你看一个东西」），cross-encoder 都说
            # 没关系，那就是杂问撞库，不是记忆。
            by = {s.get("anchor"): float(s.get("rerank") or 0) for s in segs}
            top_seg = max(segs, key=lambda s: float(s.get("rerank") or 0))
            top = float(top_seg.get("rerank") or 0) if keep else 0.0
            confident = top >= CONFIDENT
            agree = by.get(lex[0].get("anchor"), 0.0) if lex else 0.0
            specific = None
            negate = bool(RE_NEGATE.search(q or ""))
            if keep and lex and confident and not negate:
                # 分段生效（2026-11-02 改）：重排 ≥0.75 是「很强」的证据，别再拿字面头一条
                # 这种弱信号去否它。原来的严格要求对所有分数生效，「记忆索引库用什么存的」
                # 正解重排 0.873 被字面头一条 0.106 否掉，考卷掉到 18/19。
                # 保留一条便宜的下限：问题跟「重排最高的那段」得有 ≥2 字词面重合。
                # 看的是同一段、另一路信号，不是拿别的段来否决它。
                # DECISION: 词面校验也要算自述句 | when: 2026-11-03 | cause: 现场逼的
                # 自述句把重排推到 0.99 之后，这一关还拿「原文」比词面：问「桥跑一阵就不动了」，
                # 原文里只有「桥」这个单字、凑不出两字词面，于是又被判安静——
                # 自述句救回来的分，被按原文算的词面关否掉了。所以这里把自述句并进去。
                _t = top_seg.get("text") or ""
                try:
                    import selfdesc as _sd2
                    _d = (_sd2._load() or {}).get(top_seg.get("anchor"))
                    if _d:
                        _t = _t + " " + _d
                except Exception:
                    pass
                quiet = not lex_overlap(q, _t)
            elif keep and lex:
                # 0.30–0.75 是弱证据区，这时才要交叉验证：字面既然指了一条段，那条得同时
                # 过两关——① 重排认它（分 ≥0.15）；② 问题跟它有 ≥2 字的词面重合。
                # 这两关是给「词面凑出来的假命中」备的（我会写代码吗、附近哪有好吃的），
                # 它们撞上的段里一个字面二元词都没有，是单字凑出来的。
                quiet = (agree < GATE_AGREE) or not lex_overlap(q, lex[0].get("text") or "")
            elif keep:
                # 字面一条都没落上时，先问一句：这问题里有没有记忆里认得的「具体词」？
                # 有（只是 FTS 没落上），重排很确信（≥0.75）就认；
                # 没有（今天/怎么/天气这种），只有语义确实很像（≥0.59）才当它在问记忆——
                # 不然「今天天气怎么样」会撞上转写测试里那句「今天天氣不錯」，重排 0.79。
                import db as dbmod
                n, df = dbmod.vocab()
                _, qerr = dbmod.build_query(q, df, n)
                specific = qerr is None
                quiet = (vtop < VEC_LINE and not (specific and confident)) or negate
            else:
                quiet = False
            if quiet:
                # 判安静之前先做一轮纯本地兜底：短问常常有主体词、只是被虚词带偏了。
                fb = local_fallback(q, k)
                if fb:
                    fb.update({"events": events, "cand": len(segs), "vtop": vtop,
                               "agree": round(agree, 2)})
                    return fb
                # 最后一道：定位层（先判「问的是哪一格」，命中就整条取回那一格）。
                # 为什么放在这里而不是检索入口：它要调外部模型，最贵；而且入口处它会
                # 抢在所有人前面接管——考卷那些「库里有、但不在格子里」的题会被硬塞，
                # 实测把 19/19 打到 13/19。放在兜底位就互不打扰：三层能答就根本轮不到它。
                # 兜底位三道门：抽屉格 → 时间片 → 常设册节（见 _locate）
                _lo = _locate(q)
                if _lo:
                    return _lo
                # 换说法兜底：闭嘴之前，把问句换成库里自己的说法再搜一次
                # （别人独立出的四十句里六成闭嘴，缝就在这儿）。
                try:
                    import qexpand
                    for alt in qexpand.alts(q, limit=2):
                        r2 = _search(alt, k=k, floor=floor, vec_k=vec_k,
                                     lex_strong=lex_strong, rerank=rerank)
                        segs2 = (r2 or {}).get("segs") or []
                        top2 = max([float(x.get("rerank") or 0) for x in segs2] or [0])
                        if segs2 and top2 >= EXPAND_FLOOR:
                            return {"q": q, "route": "换说法兜底", "segs": segs2[:k],
                                    "via": alt, "events": events, "cand": len(segs),
                                    "vtop": vtop, "top2": round(top2, 3),
                                    "bottom": RERANK_FLOOR}
                except Exception as e:
                    print("换说法兜底跳过（%s: %s）" % (type(e).__name__, e), file=sys.stderr)
                return {"q": q, "route": "安静", "segs": [], "events": events,
                        "cand": len(segs), "vtop": vtop, "specific": specific,
                        "agree": round(agree, 2), "bottom": RERANK_FLOOR}
            if keep:
                return {"q": q, "route": "重排", "segs": keep[:k], "events": events,
                "negate": negate,
                        "cand": len(segs), "vtop": vtop, "agree": round(agree, 2),
                        "bottom": RERANK_FLOOR}
            if lex:
                # 重排一条都觉得不够格：只有字面排第一的那段本身也被重排认（≥0.15）才算数，
                # 退回去用字面的排序，别把「重排不认」当成「记忆里没有」。
                # 连它都不认，就是杂问撞库，安静。
                if agree >= GATE_AGREE:
                    return {"q": q, "route": "字面兜底", "segs": lex[:k], "events": events,
                            "cand": len(segs), "agree": round(agree, 2), "bottom": RERANK_FLOOR}
                fb = local_fallback(q, k)
                if fb:
                    fb.update({"events": events, "cand": len(segs), "vtop": vtop,
                               "agree": round(agree, 2)})
                    return fb
                # 最后一道：定位层（先判「问的是哪一格」，命中就整条取回那一格）。
                # 为什么放在这里而不是检索入口：它要调外部模型，最贵；而且入口处它会
                # 抢在所有人前面接管——考卷那些「库里有、但不在格子里」的题会被硬塞，
                # 实测把 19/19 打到 13/19。放在兜底位就互不打扰：三层能答就根本轮不到它。
                # 兜底位三道门：抽屉格 → 时间片 → 常设册节（见 _locate）
                _lo = _locate(q)
                if _lo:
                    return _lo
                # 换说法兜底：闭嘴之前，把问句换成库里自己的说法再搜一次
                # （别人独立出的四十句里六成闭嘴，缝就在这儿）。
                try:
                    import qexpand
                    for alt in qexpand.alts(q, limit=2):
                        r2 = _search(alt, k=k, floor=floor, vec_k=vec_k,
                                     lex_strong=lex_strong, rerank=rerank)
                        segs2 = (r2 or {}).get("segs") or []
                        top2 = max([float(x.get("rerank") or 0) for x in segs2] or [0])
                        if segs2 and top2 >= EXPAND_FLOOR:
                            return {"q": q, "route": "换说法兜底", "segs": segs2[:k],
                                    "via": alt, "events": events, "cand": len(segs),
                                    "vtop": vtop, "top2": round(top2, 3),
                                    "bottom": RERANK_FLOOR}
                except Exception as e:
                    print("换说法兜底跳过（%s: %s）" % (type(e).__name__, e), file=sys.stderr)
                return {"q": q, "route": "安静", "segs": [], "events": events,
                        "cand": len(segs), "vtop": vtop, "specific": specific,
                        "agree": round(agree, 2), "bottom": RERANK_FLOOR}
            fb = local_fallback(q, k)
            if fb:
                fb.update({"events": events, "cand": len(segs)})
                return fb
            return {"q": q, "route": "重排", "segs": [], "events": events,
                    "cand": len(segs), "bottom": RERANK_FLOOR}
        except Exception as e:
            # 模型没装好/跑不动就老实回退到老逻辑，别让检索整条瘫掉。
            err = "%s: %s" % (type(e).__name__, e)
    else:
        err = None

    # —— 回退路径：rerank=False 或重排自己出错时，仍走原来的二选一 ——
    import db as dbmod
    res = dbmod.search(q, nseg=k, ndoc=3)
    segs = res.get("segs") or []
    top = max([float(s.get("score") or 0) for s in segs], default=0.0)
    if segs and top >= lex_strong:
        out = {"q": q, "route": "字面", "segs": segs, "events": res.get("events") or []}
    else:
        import vec
        r = vec.search(q, k=vec_k, floor=floor)
        if r.get("err"):
            out = {"q": q, "route": "字面", "segs": segs, "events": res.get("events") or [],
                   "vec_err": r["err"]}
        else:
            vsegs = r.get("segs") or []
            if vsegs:
                # 语义证在先：字面证据不够硬的时候，先给语义捞上来的，字面的接在后面当备选。
                # 不去丢字面的结果——它可能正好是对的，只是分数低。
                merged = vsegs + [s for s in segs if s["anchor"] not in {v["anchor"] for v in vsegs}]
                out = {"q": q, "route": "语义+字面", "segs": merged,
                       "events": res.get("events") or []}
            else:
                out = {"q": q, "route": "字面", "segs": segs,
                       "events": res.get("events") or []}
    if err:
        out["rerank_err"] = err
    return out


_FRESH_CHECKED = False


def _check_index_fresh():
    """每次进程里只查一次：索引戳比记忆文件旧，就在后台起重建，这次仍用旧索引。

    木木 2026-11-02 要的「改完记忆自动重建」落在这儿——挂在检索入口而不是养常驻进程，
    因为常驻进程会默默死掉没人发现，而检索每次都会被叫醒。检查失败的代价是跳过，
    绝不因为新鲜度把检索搞挂。
    """
    global _FRESH_CHECKED
    if _FRESH_CHECKED:
        return
    _FRESH_CHECKED = True
    # 跑考卷/杂问闸时不许在背后重建：一边写库一边读，评出来的分数不可信
    if os.environ.get("DSH_NO_AUTOREBUILD"):
        return
    try:
        import freshness
        is_old, newest, ts = freshness.stale()
        if not is_old:
            return
        state = freshness.rebuild_in_background()
        print("索引过期了（记忆文件最新 %.0f > 索引戳 %.0f），%s；这次先用旧索引回答。"
              % (newest, ts, "后台已开始重建" if state == "started" else ("有一个重建正在跑" if state == "already-running" else "重建起不来")),
              file=sys.stderr)
    except Exception as e:
        print("新鲜度检查跳过（%s: %s）" % (type(e).__name__, e), file=sys.stderr)


def _fresh_note(seg):
    """从格子的「现行状态（日期）」里读出它最后一次更新是几天前。"""
    import datetime
    import re as _re
    m = _re.search(r"现行状态[（(]\s*(\d{4}-\d{2}-\d{2})", seg.get("text") or "")
    if not m:
        return None
    try:
        d = datetime.date.fromisoformat(m.group(1))
    except ValueError:
        return None
    days = (datetime.date.today() - d).days
    if days <= 0:
        return None
    if days >= 7:
        return "注意：这一格最后一次更新是 %d 天前（%s），结论可能已经过时" % (days, m.group(1))
    return "这一格更新于 %s（%d 天前）" % (m.group(1), days)


# DECISION: 常设定位接在兜底位第三道门（抽屉→时间片→常设节） | when: 2026-11-02 | cause: 现场逼的
_route_cache = {}


def _route_answer(q):
    """定位命中抽屉格 → 整条取回那一格（含现行状态与变更行）；不命中返回 None。

    空档显式化（木木 2026-11-02 定）：问题里带了日期、但那天一条记录都没有时，
    不许装作查到了，也不许沉默——要在 note 里明说「那段时间没有记录」，
    因为「真的没聊」和「聊了但我没找到」是两回事，得给不同答案。
    """
    if q in _route_cache:
        return _route_cache[q]
    import route as routemod
    rq = routemod.route(q)
    if not rq.get("cells"):
        return None

    import db as dbmod
    con = dbmod.connect()
    segs = []
    for a in rq["cells"]:
        row = con.execute("SELECT text FROM segments WHERE anchor=?", (a,)).fetchone()
        if row and row[0]:
            segs.append({"anchor": a, "text": row[0], "rerank": 1.0, "score": 1.0})
    # DECISION: 抽屉格闸门只记分不拦 | when: 2026-11-02 | cause: 现场逼的
    # 白天的结论是「整格直给，但出口加一道轻量校验」，WB 建议拿 rerank 0.20 拦。
    # 实测两条路都不通：rerank 对「问题 vs 240 字整段」给 0.001~0.015（三级里同样的
    # rerank 能给 0.97，尺度完全不同），bge-small 余弦又把相关与不相关全挤在 0.44~0.53。
    # 本地两个模型都判不了「答非所问」，硬拦会把对的也拦掉——所以只留分，不拦。
    if segs:
        try:
            import rerank as _rk
            _sc = _rk.rerank(q, [(s.get("text") or "")[:240] for s in segs])
            # gate_score 已删（2026-11-03）：记了没有任何代码路径读它。
            # 「为了以后分析」不是用途是借口——真要分析时加一行就是零成本。
            _ = _sc   # 保留这次调用：它至少能证明重排跑通了
        except Exception:
            pass
    if not segs:
        return None

    notes = []
    # 格子的新鲜度（WB 2026-11-02 挑出来的：定位 38/38 测的是「选对格」，
    # 不是「格里装的东西对不对」。格子里的现行状态要是旧的、被推翻了没登记，
    # 定位照样全对、闸门照样放行，答案是错的，而回归集一点都抓不到。
    # 这里不拦，只把它多旧写在明面上——让上层知道这是「多久以前的结论」。
    _fn = next((_fresh_note(x) for x in segs if _fresh_note(x)), None)
    if _fn:
        notes.append(_fn)
    if rq.get("dates"):
        # 先工具类、找不到再杂谈（木木 2026-11-02 定的分诊顺序）：
        # 问题带了具体日期，但格子里一条那天的记录都没有，就说明这一格答不了，
        # 让给第三级去聊天流水里翻，别拿格子的现行状态糊弄过去。
        blob = "".join(s.get("text") or "" for s in segs)
        if not any((d in blob) or (d[5:] in blob) for d in rq["dates"]):
            return None
        empty = [d for d, n in (rq.get("hits") or {}).items() if n == 0]
        if empty:
            notes.append("那几天里没有记录：%s" % "、".join(sorted(empty)))
    out = {"q": q, "route": "格定位", "segs": segs, "cand": len(segs), "events": [],
           "track": rq.get("track"), "time": rq.get("time"), "note": "；".join(notes)}
    _route_cache[q] = out
    return out


def _sect_answer(q):
    """第四级：常设四册（先判册、再判节），命中就整条取回那几节。

    DECISION: 接在兜底位、排在抽屉和时间片之后 | when: 2026-11-02 | cause: 现场逼的
    来由：闭嘴那十四句大多是「规矩类」问题（问该怎么做、问当初怎么定），
    抽屉十一格装不下它们，常设四册才是。放在兜底位是因为它最贵（两次模型调用）。

    DECISION: 判中只是提议，取回的段要过重排才算命中 | when: 2026-11-02 | cause: 现场逼的
    原来这里「小模型判中就整条取回、score 硬填 1.0」，零校验。20:3x 回归时杂问
    「我会写代码吗」被拽到「命令行里不许塞代码」上出过一次声（26 次复跑没再出，是偶发——
    偶发更要堵，因为它平时安静，出事那次会被当成真答案）。判册判节都是本地小模型给的，
    它错的时候跟对的时候一样自信。
    实测分布：真问题（换壳名字 / 重启先问什么 / 加新功能前先干什么）0.952~0.992；
    同批杂问（下雨、量子力学、煮鸡蛋、写诗、一年几个月、我会写代码吗）0.000~0.012。
    门槛复用 RERANK_FLOOR（0.30），不另造一个数——同一件事一条线。
    """
    import route as routemod
    s = routemod.route_sect(q)
    anchors = s.get("anchors") or []
    if not anchors:
        return None
    import db as dbmod
    con = dbmod.connect()
    segs = []
    for a in anchors:
        row = con.execute("SELECT text FROM segments WHERE anchor=?", (a,)).fetchone()
        if row and row[0]:
            segs.append({"anchor": a, "text": row[0]})
    if not segs:
        return None
    import rerank as rrmod
    scores = rrmod.rerank(q, [s2["text"] for s2 in segs])
    keep = []
    for s2, sc in zip(segs, scores):
        if sc >= RERANK_FLOOR:
            s2["rerank"] = float(sc)
            s2["score"] = float(sc)
            keep.append(s2)
    if not keep:
        print("册节定位判到「%s / %s」但重排最高只有 %.2f，当没命中、让给下一层"
              % (s["book"], "、".join(s["sects"]), max(scores or [0.0])), file=sys.stderr)
        return None
    return {"q": q, "route": "册节定位", "segs": keep, "cand": len(keep),
            "book": s["book"], "sects": s["sects"],
            "note": "按「%s / %s」取回" % (s["book"], "、".join(s["sects"]))}


def _locate(q):
    """兜底位上的三道门，按便宜到贵排：抽屉格 → 时间片 → 常设册节。

    原来这两段胶水在 _search 里抄了两遍（两个出口各一份），改一处忘一处是迟早的；
    合成一个函数，出口只有一个答案：「命中就返回结果，都不中返回 None」。
    """
    if os.environ.get("ROUTE_ON", "1") == "0":
        return None
    try:
        r = _route_answer(q)
        if r:
            return r
    except Exception as e:
        print("抽屉定位跳过（%s: %s）" % (type(e).__name__, e), file=sys.stderr)
    try:
        import route as _rm
        p = _rm.piece(q)
        if p:
            return p
    except Exception as e:
        print("时间片定位跳过（%s: %s）" % (type(e).__name__, e), file=sys.stderr)
    if os.environ.get("SECT_ON", "1") != "0":
        try:
            r = _sect_answer(q)
            if r:
                return r
        except Exception as e:
            print("册节定位跳过（%s: %s）" % (type(e).__name__, e), file=sys.stderr)
    return None


def search(q, k=8, floor=VEC_FLOOR, vec_k=30, lex_strong=LEX_STRONG, rerank=RERANK_ON):
    """对外入口：跑 _search，再把登记表里被推翻的旧段从返回结果里滤掉。

    来由：库里同时躺着「稳定层七十七段」和「七十八段」，旧的那段照样会被检索捡起来、
    当答案递出去。过滤只动返回的 segs（外加一条 superseded 记账），门槛、排序、分段
    逻辑一概不碰；登记表坏了就当没推翻，绝不让检索因为一张表瘫掉（幂等，滤两次一样）。
    """
    _check_index_fresh()
    # 定位层（2026-11-02）：先判「问的是哪一格」，命中就整条取回那一格的内容。
    # 为什么放在最前面：把一千六百段缩到一格，比从全库捞段准得多——实测定位 38/38、
    # 杂问 20/20 全拒。命中才接管；不命中（或关了 ROUTE_ON）就走原来那条三层。
    # 计数类问题（「几次 / 哪几次 / 最近一次」）不许片段化：整条取回那一格，
    # 并把变更行条数报出来，回答只能用这个数（智谱 评审3:17，木木 2026-11-02 让照做）。
    try:
        import countq
        if countq.is_count(q):
            r = countq.answer(q)
            if r.get('segs'):
                return r
            print('计数问句没找准格子，退回普通检索（数要另核）', file=sys.stderr)
    except Exception as e:
        print('计数分诊跳过（%s: %s）' % (type(e).__name__, e), file=sys.stderr)
    res = _search(q, k=k, floor=floor, vec_k=vec_k, lex_strong=lex_strong, rerank=rerank)
    res = _drop_superseded(res)
    # 群聊默认拒收（2026-11-04）。入库端已经有闸（sources.gate），这里出口再拦一道：
    # 别人接进来的库、历史库可能没走过那道闸，而出库这一下判错更贵——
    # 把别人的话当成本人的记忆讲出去，比少一条严重得多。
    if isinstance(res, dict) and res.get("segs"):
        try:
            import sources
            if not sources.allow_group():
                kept, dropped = sources.filter_segs(res["segs"])
                if dropped:
                    res["segs"] = kept
                    res["group_dropped"] = [str(s.get("sid") or "") for s in dropped][:10]
        except Exception as e:
            print("来源过滤跳过（%s: %s）" % (type(e).__name__, e), file=sys.stderr)
    # 空结果带原因码（2026-11-04）：静默返回空，等于让「没入库」冒充「没记过」。
    if isinstance(res, dict) and not res.get("segs"):
        res["empty"] = _explain_empty(q, res)
    return res


_BACK_WORDS = ("之前", "以前", "那时候", "当时", "最开始", "当初", "上次",
               "早先", "原来", "最初", "过去", "去年", "前年", "上个月",
               "上周", "前几天", "早年", "小时候", "从前", "那会儿")


def _human_date(s):
    """2026-10-30 → 2026年10月30日。认不出就原样返回，不抛。"""
    p = str(s or "?").split("-")
    if len(p) == 3 and all(x.isdigit() for x in p):
        return "%s年%s月%s日" % (p[0], int(p[1]), int(p[2]))
    return str(s or "?")


def _explain_empty(q, res):
    """空结果必须说清是哪一种（2026-11-04 木木要求）。

    以前检索没东西就安静返回，使用者分不清三种情况：那段时间没入库、
    真的没记过、有相关但都被推翻登记滤掉了。这三种在界面上长得一模一样，
    于是人会把「没进来」当成「没发生」，或者当成插件坏了。
    """
    try:
        import db
        cov = db.coverage()
    except Exception:
        cov = {}
    sup = res.get("superseded") if isinstance(res, dict) else None
    if sup and sup.get("n_dropped"):
        return {"reason": "filtered_superseded",
                "message": "有几段相关，但都被推翻登记滤掉了（旧的已被新的取代）",
                "coverage": cov}
    grp = res.get("group_dropped") if isinstance(res, dict) else None
    if grp:
        # 群聊内容被拦下时，别让使用者以为「没记过」——是有一批，但它不该在这儿。
        return {"reason": "filtered_group",
                "message": "相关的几段来自群聊，按默认规矩没有收进来（别人的话不进本人的记忆）。"
                           "真要收，得单开一个库、单独起名，别和私人记忆混着查。",
                "coverage": cov, "group_dropped": grp}
    lo = str(cov.get("cover_min") or "?")[:10]
    hi = str(cov.get("cover_max") or "?")[:10]
    # 给人看的日期换成「2026年10月30日」这种，别甩 ISO 格式
    lo_h = _human_date(lo)
    hi_h = _human_date(hi)
    back = [w for w in _BACK_WORDS if w in q]
    if back and cov.get("cover_min"):
        # 只声明边界，不断言这一问的答案在不在边界外——后者我们不知道。
        # 文案要像人说话，并把球抛回给用户：是删了还是没加，只有他知道。
        # 称呼不写死（不同用户喜欢的称呼不一样），只说事实和自己的困惑。
        # 机器可读的边界仍在 coverage 字段里，不靠这句话承载。
        return {"reason": "out_of_coverage",
                "message": "%s之前的会话没写进我的记忆库。是删掉了，还是根本没加进来呀？"
                           "我只知道自己从那天起才有记忆，再早的就想不起来了——"
                           "你帮我想想？" % lo_h,
                "coverage": cov}
    return {"reason": "not_found",
            "message": "库里没有落上的段（覆盖 %s 至 %s，共 %s 段）。" % (lo_h, hi_h, cov.get("n_segments") or "?"),
            "coverage": cov}


def _drop_superseded(res):
    """把 res["segs"] 里被推翻的旧段拿掉。登记表出任何事都原样返回，不抛。"""
    segs = res.get("segs") if isinstance(res, dict) else None
    if not segs:
        return res
    try:
        import supersede
        rep = {}
        kept = supersede.filter_segs(segs, rep)
    except Exception as e:
        print("推翻登记表用不上（%s: %s），这轮按没推翻处理" % (type(e).__name__, e),
              file=sys.stderr)
        return res
    if rep.get("n_dropped") or rep.get("alarm"):
        res["segs"] = kept
        res["superseded"] = {"n": rep.get("n_dropped"),
                             "in": rep.get("n_in"), "out": rep.get("n_out"),
                             "drop": [d.get("anchor") for d in (rep.get("dropped") or [])],
                             "new": [d.get("new") for d in (rep.get("dropped") or [])]}
        # 全滤要跟着结果走一程：stderr 是给人看的，这个字段是给自检和上层看的。
        # 少了它，「结果被滤空」和「本来没答案」在返回值里完全同形（2026-11-02 木木点的）。
        if rep.get("alarm"):
            res["superseded"]["alarm"] = rep["alarm"]
            print("红：检索结果被推翻登记滤空了（%s），这一问等于没答案" % rep["alarm"],
                  file=sys.stderr)
    return res


def main():
    av = sys.argv[1:]
    if not av:
        print(__doc__)
        return
    q = av[0]
    k = int(av[av.index("--宽") + 1]) if "--宽" in av else 8
    floor = float(av[av.index("--最低") + 1]) if "--最低" in av else VEC_FLOOR
    r = search(q, k=k, floor=floor, rerank="--关" not in av)
    if "--json" in av:
        print(json.dumps(r, ensure_ascii=False))
        return
    print("走的%s路" % r["route"])
    if r.get("fallback"):
        print("（兜底命中：%s）" % "、".join(r.get("fallback_terms") or []))
    if r.get("rerank_err"):
        print("（重排没用上，回退了：%s）" % r["rerank_err"])
    for s in r["segs"]:
        print("%.2f  %s  %s" % (float(s.get("score") or 0), s["anchor"],
                                (s.get("text") or "").replace("\n", " ")[:80]))
    if not r["segs"]:
        print("（没有落上的，安静的）")


if __name__ == "__main__":
    main()
