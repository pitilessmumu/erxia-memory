# DECISION: 自述句当第二把尺子 | when: 2026-11-03 | cause: 现场逼的
# -*- coding: utf-8 -*-
"""selfdesc.py —— 给段落配一句「人话自述」，用短句尺度救长段的换说法。

为什么要有它（2026-11-03 实测）：重排模型对「问题 vs 240 字整段」分不开——
查「智谱的输出里混进了别的东西」，正确答案那段只给 0.074，而杂问能给 0.10；
但同样的内容换成一句短自述，同一对给 **0.9952**。差了两个数量级。

智谱和 WB 都指到同一个方向：把「换说法」的判断从检索时挪到写语料时，
用短句这个 rerank 能分的尺度。别名行是格子级的，这个是段落级的。

存哪：lab\\index\\selfdesc.json，形如 {"ac000015-008": "一句自述"}
查哪：hybrid 的核心重排之后，对有自述句的段取 max(原文分, 自述句分)
"""
import io
import json
import os
import sys

import config  # 路径统一从 config 取

WS = str(config.ROOT)  # 原来按 __file__ 往上三级找根，仓库一搬就错
# 原来拼的是 <工作区>/memory-lab/index，现在是 lab/index（config.LAB 就是 lab 目录）
PATH = os.path.join(str(config.LAB), "index", "selfdesc.json")

_cache = {"mtime": None, "map": {}}


def _load():
    """按 mtime 缓存；文件不在就当没有（不是错）。"""
    try:
        mt = os.path.getmtime(PATH)
    except OSError:
        return {}
    if _cache["mtime"] == mt:
        return _cache["map"]
    try:
        m = json.load(io.open(PATH, encoding="utf-8"))
        if not isinstance(m, dict):
            m = {}
    except Exception as e:
        sys.stderr.write("自述句表读坏了（%s）——本轮当没有自述句\\n" % type(e).__name__)
        m = {}
    _cache["mtime"] = mt
    _cache["map"] = m
    return m


def count():
    return len(_load())


def scores(q, anchors):
    """给一批 anchor 返回「跟自述句的相关分」；没有自述句的给 0。

    只对**真有自述句**的那几段调重排——不铺开算，否则每问要多几百次推理。
    """
    m = _load()
    out = [0.0] * len(anchors or [])
    todo = [(i, m[a]) for i, a in enumerate(anchors or []) if a in m and m[a]]
    if not todo:
        return out
    try:
        import rerank as _rk
        sc = _rk.rerank(q, [t for _, t in todo])
        for (i, _), v in zip(todo, sc):
            out[i] = float(v)
    except Exception as e:
        sys.stderr.write("自述句比不了（%s）——按没有处理\\n" % type(e).__name__)
    return out
