# -*- coding: utf-8 -*-
# DECISION: 计数问整条返回那一格，不做片段化 | when: 2026-11-02 | cause: 现场逼的
"""countq.py —— 计数类问题的答案要「整条取回」，不许片段化。

来由（智谱 评审3-20261102.md:17，木木 2026-11-02 让照做）：
「改了几次」是计数问题，分诊只解决「找到哪条」，不解决「数」。抽屉的变更行如果在检索库
被打散成片段取回来，模型拿到的是几行残片，数出来可能是 2 也可能是 4，而且它不会觉得有问题。
触发条件：任何带「几次 / 哪几次 / 最近一次是什么时候」的问句。

做法：识别到计数问法，就不走字面/语义/重排那套片段召回，直接按条目把整条取出来，
连变更行一起交给调用方；同时把「变更行几条」明明白白报出来，答案里只许用这个数。
"""
import re
import sqlite3

ANN = r"\d{4}-\d{2}-\d{2}"          # 变更行的日期开头
RE_ROW = re.compile(r"(?m)^-\s*(" + ANN + r")([^\n]*)")
# 行内写着「改回／作废／推翻／不做了」的，是撤销记录：它自己不产生新状态，
# 只是把前一条废掉。计数时单列出来，不许混进「改了几次」里。
# DECISION: 撤销词前面不许是否定 | when: 2026-11-02 | cause: 做歪的
# 原来「推翻」「放弃」匹配行内任意位置，「决定不放弃侧键方案」也被算成撤销记录，
# count 偏小；而注记要求「回答次数用现存」——数错比闭嘴更违背设计底线。
VOID_WORDS = ("改回", "作废", "推翻", "不做了", "回退", "放弃")


def is_void(line):
    """行里有没有撤销动作。否定式不算——「决定不放弃」「没有推翻」都不该当撤销。

    只往前看两个字：是「不」「没」就跳过这一处继续找。
    （原来用一条正则加负向断言，只看紧挨着一个字，「没有推翻」漏掉了，2026-11-02 修。）
    """
    s = line or ""
    for w in VOID_WORDS:
        start = 0
        while True:
            i = s.find(w, start)
            if i < 0:
                break
            pre = s[max(0, i - 2):i]
            if not any(x in pre for x in ("不", "没")):
                return True
            start = i + len(w)
    return False

# 计数问法：命中就走整条取回
COUNT_PAT = re.compile(
    r"几次|哪几次|几次了|第几次|最近一次|上一次是什么时候|多久一次|改过几|定过几|变过几|几回")

DRAWER_SID = "ac000005"


def _db():
    import config  # 原来按 __file__ 往上三级拼 memory-lab，仓库一搬就错
    return str(config.DB)
# 计数问句里那些跟「哪一格」无关的通用词，先摘掉，剩下的才是实体
# （「侧键最近一次是什么时候改的」摘完只剩「侧键」，直接去格子里找人）
RE_GENERIC = re.compile(
    r"最近一次|上一次|这一?次|几次了?|哪几次|第几次|多久一次|改过|定过|变过|改的|改回|是什么时候|什么时候|吗|呢")



def is_count(q):
    return bool(COUNT_PAT.search(q or ""))


def _bigrams(s):
    s = re.sub(r"[\s，。、；：？！「」（）()\-—…/]", "", s or "")
    return set(s[i:i + 2] for i in range(max(0, len(s) - 1))) or set(s)


def _cells(con):
    rows = list(con.execute(
        "SELECT anchor, text FROM segments WHERE sid = ? ORDER BY anchor", (DRAWER_SID,)))
    out = []
    for anchor, text in rows:
        head = (text or "").strip().splitlines()
        title = ""
        for ln in head[:3]:
            if ln.strip().startswith("##"):
                title = ln.strip().lstrip("#").strip()
                break
        alias = ""
        for ln in head:
            # 只认「行首就是找我的说法」那一行：正文里顺口提到这四个字的句子
            # 会把别名抓成一句无关的话（第一版就抓成了「查不到的理由是没归并」）
            if ln.strip().startswith("找我的说法"):
                alias = ln.split("：", 1)[-1] if "：" in ln else ln
        out.append({"anchor": anchor, "text": text or "", "title": title, "alias": alias,
                    "key": _bigrams(title + alias + title)})
    return out


def pick(q, con_path=None):
    """挑最像的那一格。返回 (cell, score) 或 (None, 0)。"""
    qb = _bigrams(q)
    ent = RE_GENERIC.sub("", q or "").strip()
    con = sqlite3.connect(con_path or _db())
    try:
        cells = _cells(con)
        # 第一优先：摘掉通用词后的实体，在格的标题或「找我的说法」里出现
        if 2 <= len(ent) <= 8:
            for c in cells:
                if ent in (c["title"] + c["alias"]):
                    return c, 1.0
        best, bs = None, 0.0
        for c in cells:
            if not c["key"] or not qb:
                continue
            inter = len(qb & c["key"])
            score = inter / float(len(qb) or 1)          # 问句覆盖率，不是并集比
            if score > bs:
                best, bs = c, score
        return best, bs
    finally:
        con.close()


def answer(q, con_path=None, floor=0.60):
    """计数问：整条返回那一格 + 变更行清单 + 条数。"""
    cell, score = pick(q, con_path)
    if not cell or score < floor:
        return {"route": "计数·没找准", "segs": [], "count": 0, "score": score,
                "note": "没找到对应的抽屉格，不许猜数"}
    rows = [m.group(1) + m.group(2) for m in RE_ROW.finditer(cell["text"])]
    void = [r for r in rows if is_void(r)]
    live = [r for r in rows if not is_void(r)]
    # DECISION: 计数也要过推翻登记 | when: 2026-11-02 | cause: 做歪的
    # 白天第一轮就点过这个洞：整条取回只按行内「改回/作废」过滤，认不出跨格的推翻，
    # 被推翻的旧结论照样算进次数——数错比不给更糟。这里逐行过 supersede。
    _dropped = 0
    try:
        import supersede as _sp
        _rep = {}
        _kept = _sp.filter_segs([{"anchor": cell["anchor"], "text": r} for r in live], report=_rep)
        if _rep.get("n_in"):
            _dropped = _rep.get("n_dropped", 0)
            live = [x["text"] for x in _kept]
        if _rep.get("n_in") and not live:
            return {"route": "计数·被推翻滤光", "segs": [], "count": 0,
                    "count_all": len(rows), "void": len(void), "score": score,
                    "cell": cell["title"], "alarm": True,
                    "note": "这一格的变更行全被推翻登记滤掉了（%d 条），不敢报数" % _rep.get("n_in")}
    except Exception:
        pass

    return {
        "route": "计数·整条取回",
        "segs": [{"anchor": cell["anchor"], "text": cell["text"], "score": score}],
        "cell": cell["title"],
        "score": score,
        "count": len(live),
        "dropped_by_supersede": _dropped,
        "count_all": len(rows),
        "void": len(void),
        "rows": rows,
        "note": ("整条取回：变更行共 %d 条，其中 %d 条是撤销记录（改回／作废／推翻），"
                 "现存 %d 条。回答次数用「现存」，并说明历史共几条；不许只报总数。"
                 % (len(rows), len(void), len(live))),
    }
