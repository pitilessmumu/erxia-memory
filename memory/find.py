# -*- coding: utf-8 -*-
# DECISION: 按关键词反查锚点，用来做 ground truth | when: 2026-11-02 | cause: 现场逼的
"""按关键词在语料里找「段」，给出锚点/类型/时间/命中片段。用来查ground truth、查某句话出自哪一段。

用法：python find.py UIPI "28 B5 2F FD" --宽 80
"""
import re
import sys
from pathlib import Path

import config  # 路径统一从 config 取

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# 原来按 __file__ 往上两级拼 origin/corpus，仓库一搬就错；现在从 config 取
CORPUS = config.CORPUS
# 与 corpus.SEG_RE 同一口径：段头带完整日期，旧的无日期段头也认。
SEG_RE = re.compile(r"^##\s*\[([^\]]+)\]\s*(\S+)\s*((?:\d{4}-\d{2}-\d{2} )?\d{2}:\d{2}:\d{2})\s*$")

kws = [a for a in sys.argv[1:] if not a.startswith("--")]
width = 80
for a in sys.argv[1:]:
    if a.startswith("--宽="):
        width = int(a.split("=", 1)[1])
if not kws:
    sys.exit('给关键词：python find.py UIPI "28 B5 2F FD"')

for f in sorted(CORPUS.glob("*.md")):
    anchor, kind, tm, buf = None, "", "", []
    segs = []
    for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
        m = SEG_RE.match(line)
        if m:
            if anchor:
                segs.append((anchor, kind, tm, "\n".join(buf)))
            anchor, kind, tm, buf = m.group(1), m.group(2), m.group(3), []
        elif anchor is not None:
            buf.append(line)
    if anchor:
        segs.append((anchor, kind, tm, "\n".join(buf)))
    for anchor, kind, tm, text in segs:
        low = text.lower()
        hit = [k for k in kws if k.lower() in low]
        if not hit:
            continue
        print("[%s] %s %s  命中:%s" % (anchor, kind, tm, ",".join(hit)))
        first = low.find(hit[0].lower())
        s = max(0, first - width // 2)
        print("  " + re.sub(r"\s+", " ", text[s:s + width]))
