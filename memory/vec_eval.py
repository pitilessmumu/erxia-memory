# -*- coding: utf-8 -*-
"""换说法考卷：题面故意不用语料里的原词（木木那句「当时说球、改天说圆」）。

这才是语义层的考场——原来那十道题都带着原词，字面检索本来就能抓到，
拿它们比不出 embed 的价值。这里每题只说「现象」，正解段里才有专有词。
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import corpus
import quiz

# 题面里刻意不出现的词，正是 kw 里那些
CASES = [
    {"q": "两个地方的文件，为什么改一处另一处也跟着变", "kw": ["硬链接"]},
    {"q": "屏幕上忽然冒出一个黑框，一下就不见了", "kw": ["闪黑窗", "黑窗口"]},
    {"q": "投递那条路走到二十条就不走了", "kw": ["停摆"]},
    {"q": "人还活着，可按键事件一个都收不到", "kw": ["UIPI", "icacls", "完整性"]},
    {"q": "那段文字为什么拆不开，看着像乱码", "kw": ["多帧", "zstd", "28 b5"]},
    {"q": "怎么知道改完东西以后没搜漏", "kw": ["考卷", "回归", "黑箱"]},
    {"q": "让别的模型给我出题判分的那条路", "kw": ["智谱", "zhipu"]},
    {"q": "桌面上的东西和邮箱里的是不是同一份", "kw": ["junction", "硬链接"]},
]


def first_hit(segs, texts, kw):
    for rank, a in enumerate(segs, 1):
        body = ((texts.get(a) or {}).get("text") or "").lower()
        if any(k.lower() in body for k in kw):
            return rank, a
    return None, None


def run_lex(q, limit):
    res = quiz.run_one(q, limit)
    if not res:
        return [], 0.0
    return [s["anchor"] for s in res["segs"]], (res["segs"][0]["score"] if res["segs"] else 0.0)


def run_vec(q, limit, floor=0.0):
    import vec
    r = vec.search(q, k=limit, floor=floor)
    if r.get("err"):
        return [], 0.0
    return [s["anchor"] for s in r["segs"]], (r["segs"][0]["score"] if r["segs"] else 0.0)


def main():
    limit = 8
    av = sys.argv[1:]
    if "--限" in av:
        limit = int(av[av.index("--限") + 1])
    texts = corpus.parse_segments()
    ok_l = ok_v = 0
    print("换说法考卷（前 %d 段内算命中）" % limit)
    for c in CASES:
        al, sl = run_lex(c["q"], limit)
        av_, sv = run_vec(c["q"], limit)
        rl, _ = first_hit(al, texts, c["kw"])
        rv, _ = first_hit(av_, texts, c["kw"])
        ok_l += bool(rl)
        ok_v += bool(rv)
        print("%s 字面 第%s名(%.2f) | 语义 第%s名(%.2f)  %s" % (
            "过" if (rl or rv) else "漏",
            rl or "-", sl, rv or "-", sv, c["q"]))
    print("字面 %d/%d，语义 %d/%d" % (ok_l, len(CASES), ok_v, len(CASES)))


if __name__ == "__main__":
    main()
