# -*- coding: utf-8 -*-
"""正题在语义路里排第几、拿多少分——决定「第二路」该不该带门槛、门槛放哪。"""
import sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import numpy as np
import vec, quiz, corpus

segs = corpus.parse_segments()
z = np.load(vec.INDEX, allow_pickle=True)
anchors, mat = z["anchors"], z["vecs"]

for c in quiz.CASES:
    if c.get("quiet"):
        continue
    qv = vec.encode([c["q"]], is_query=True)[0]
    sims = mat @ qv
    best = {}
    for i, a in enumerate(anchors):
        a = str(a)
        s = float(sims[i])
        if s > best.get(a, -9):
            best[a] = s
    ranked = sorted(best.items(), key=lambda kv: -kv[1])
    rank = score = None
    for r, (a, s) in enumerate(ranked, 1):
        body = ((segs.get(a) or {}).get("text") or "").lower()
        if any(k.lower() in body for k in c["kw"]):
            rank, score = r, s
            break
    print("%-28s 正解第%d名 分%.3f  (第一名%.3f)" % (
        c["q"], rank or -1, score or 0, ranked[0][1]))
