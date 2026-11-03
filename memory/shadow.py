# -*- coding: utf-8 -*-
# DECISION: 影子块只进向量层、自己永不当答案 | when: 2026-11-02 | cause: 现场逼的
"""shadow.py —— 影子块检索：拿真人问法当桥，把「该命中的事实段」拉进候选。

三条硬约束（WB 挑刺报告，2026-11-02）：
  一、影子块只进向量层，**不进字面索引**。否则字面第一名变成影子块，对账退化成
     「字面认影子块、重排也认影子块」，安全机制被架空。
  二、影子块**永远不作为答案**：命中只返回它指回的事实段（parent）。
  三、质量不过关的宁可不要：相似度不到线的不进候选。

数据来自 run\\build-shadow.py（变体 A：从聊天记录借真人问法）。
"""
import io
import sys
import json
import os

import numpy as np

import config  # 路径统一从 config 取

# 原来按 __file__ 往上三级拼 memory-lab/index，仓库一搬就错；现在从 config 取
LAB = os.path.join(str(config.LAB), "index")
NPZ = os.path.join(LAB, "shadow.npz")
JSN = os.path.join(LAB, "shadow.json")

_cache = {}


def _load():
    if _cache:
        return _cache
    try:
        d = np.load(NPZ, allow_pickle=True)
        vecs = d["vecs"]
        items = json.load(io.open(JSN, encoding="utf-8"))["items"]
    except FileNotFoundError:
        # 文件不存在是正常的（还没建影子块），不吵
        _cache.update({"vecs": None, "items": []})
        return _cache
    except Exception as e:
        # DECISION: 影子块读挂了要出声 | when: 2026-11-02 | cause: 做歪的
        # 原来所有异常一律静默返回空——npz 损坏、json 格式变了、文件写一半，
        # 全都表现为「影子块永远不命中」，一句日志没有（智谱挑出来的）。
        sys.stderr.write("影子块读取失败（%s）——本轮跳过，不是「没有命中」\n" % type(e).__name__)
        _cache.update({"vecs": None, "items": []})
        return _cache
    _cache.update({"vecs": vecs, "items": items})
    return _cache


def count():
    return len(_load()["items"])


def search(q, k=3, floor=0.62):
    """返回 [{parent, sim, q}]：影子块命中后指向的事实段，按相似度降序。"""
    c = _load()
    if c["vecs"] is None or not c["items"]:
        return []
    import vec as vecmod
    qv = vecmod.encode([q], is_query=True)[0]
    n = np.linalg.norm(qv)
    if not n:
        return []
    sims = (c["vecs"] @ qv) / (np.linalg.norm(c["vecs"], axis=1) * n)
    # DECISION: 零向量出的 nan 不许混进候选 | when: 2026-11-02 | cause: 做歪的
    # 某行向量全零时 sims 出 nan，而 `s < floor` 对 nan 判 False——nan 一路通过质量线。
    # vec.py 用 clip(1e-9) 防了，这里没有（智谱挑出来的）。nan 一律当 -1。
    sims = np.nan_to_num(sims, nan=-1.0, posinf=-1.0, neginf=-1.0)
    order = np.argsort(-sims)[:k]
    out = []
    for i in order:
        s = float(sims[i])
        if s < floor:
            continue
        it = c["items"][int(i)]
        out.append({"parent": it["parent"], "sim": round(s, 3), "q": it["q"][:40]})
    return out
