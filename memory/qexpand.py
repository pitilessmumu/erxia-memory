# -*- coding: utf-8 -*-
# DECISION: 换说法兜底只用库里自己的说法，不编新句子 | when: 2026-11-02 | cause: 现场逼的
"""qexpand.py —— 换说法兜底：判安静之前，把问题换成库里自己的说法再搜一次。

来由（2026-11-02 实测）：别人独立出的四十句问法，六成直接闭嘴。查下来缝在重排——
那些句子与库里的词面几乎不重合，重排分不到 0.30。可库里其实存着「将来你可能怎么问」
这一行（抽屉每格的「找我的说法」）。所以闭嘴之前先做一件事：拿问句去这一行里找最像的
说法，用库里自己的说法再搜一遍。

只换词面、不动语义：换出来的句子是**库里原话**，不是我编的。找不到像的就不换。
"""
import io
import os
import re

import config  # 路径统一从 config 取

WS = str(config.ROOT)  # 原来按 __file__ 往上三级找根，仓库一搬就错
DRAWER = os.path.join(WS, "记忆", "抽屉.md")

# 固定同义词：这几对是口语和库里术语之间最常见的落差
SYN = [
    ("圆的东西", "悬浮球"), ("圆球", "悬浮球"), ("球的", "悬浮球"),
    ("变字", "转写"), ("说的变成字", "转写"), ("语音转文字", "转写"),
    ("总结", "摘要"), ("压缩文章", "摘要"),
    ("新窗口", "换壳"), ("开新窗", "换壳"), ("换个聊天", "换壳"),
    ("声音", "变声"), ("音色", "变声"),
    ("找东西", "检索"), ("翻记忆", "检索"), ("查记忆", "检索"),
]

_cache = None


def _aliases():
    """抽屉每格「找我的说法」那一行，按顿号切开，就是库里自己的问法。"""
    global _cache
    if _cache is not None:
        return _cache
    out = []
    try:
        text = io.open(DRAWER, encoding="utf-8").read()
    except Exception:
        _cache = out
        return out
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith("找我的说法"):
            body = s.split("：", 1)[-1]
            for a in re.split(r"[、，,]", body):
                a = a.strip(" 。")
                if 4 <= len(a) <= 24:
                    out.append(a)
    _cache = out
    return out


def _bg(s):
    s = re.sub(r"[\s，。、；：？！「」（）()\-—…/]", "", s or "")
    return set(s[i:i + 2] for i in range(max(0, len(s) - 1))) or set(s)


def alts(q, limit=2, floor=0.34):
    """返回最多 limit 个「库里自己的说法」，按相似度降序。找不到就空列表。"""
    if not q:
        return []
    cands = []
    qb = _bg(q)
    if not qb:
        return []
    # 固定同义词先做一遍替换，得到更容易命中的形式
    for k, v in SYN:
        if k in q:
            cands.append(q.replace(k, v))
    for a in _aliases():
        ab = _bg(a)
        if not ab:
            continue
        score = len(qb & ab) / float(len(qb))
        if score >= floor:
            cands.append(a)
    seen, out = set(), []
    for c in cands:
        c = c.strip()
        if not c or c == q or c in seen:
            continue
        seen.add(c)
        out.append(c)
        if len(out) >= limit:
            break
    return out
