# -*- coding: utf-8 -*-
"""看语义打分的分布：正题有没有「峰」，安静题是不是一片水。"""
import sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import numpy as np
import vec
import quiz

z = np.load(vec.INDEX, allow_pickle=True)
anchors, mat = z["anchors"], z["vecs"]

print("%-28s %6s %6s %6s %6s  %s" % ("题", "top1", "top2", "top8", "均", "峰差"))
for c in quiz.CASES:
    qv = vec.encode([c["q"]], is_query=True)[0]
    sims = mat @ qv
    best = {}
    for i, a in enumerate(anchors):
        a = str(a)
        s = float(sims[i])
        if s > best.get(a, -9):
            best[a] = s
    v = sorted(best.values(), reverse=True)
    top8 = v[:8]
    print("%-28s %.3f %.3f %.3f %.3f  %.3f  %s" % (
        c["q"], v[0], v[1], top8[-1], float(np.mean(top8)), v[0] - float(np.mean(top8)),
        "安静" if c.get("quiet") else ""))
