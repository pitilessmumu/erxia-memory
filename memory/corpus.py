# -*- coding: utf-8 -*-
# DECISION: 切段、切词、打分单独一层，不跟召回流程捆一起 | when: 2026-11-04 | cause: 结构理出来的
"""语料解析与字面打分：把 origin 下的东西认成「段」和「事」，再给它们算分。

这一层只管三件事——认出段头、把话切成词、算 BM25 口径的分。它不关心「召回怎么走」：
先给事还是先给段、要不要回钻、空档怎么解释，都是 recall.py 的事。

为什么要单独一层（2026-11-04 改的）：这些函数原来住在 recall.py 里，名字叫「召回」，
实际干的是切段和打分，db、db_build、hybrid、inject、quiz、vec、vec_eval、vec_rank
八个模块都伸手进来拿工具。名字和内容对不上，读的人以为在调召回流程，其实是在借扳手。

数据来源（路径见 config.py）：
  topics.md          三层里的「事」（每条一个锚点范围）
  corpus\\<sid>.md    三层里的「段」（段头 ## [锚点] 类型 时间）

段头格式：## [锚点] 类型 YYYY-MM-DD HH:MM:SS
日期 2026-11-02 才补上，旧的只有时分秒——两种都认，不认旧的会把整段并进上一段。
"""
import math
import re
import sys
from collections import Counter

import config  # 路径统一从 config 取（config.py 不 import 本项目任何模块，不会循环依赖）

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = config.ROOT
ORIGIN = config.ORIGIN
TOPICS = config.TOPICS
CORPUS = config.CORPUS

STOP = set("的了是在我你他她它这那和就也都很不到有个一上下不吗呢吧啊么什怎为以与及或而但已还被把让从对向于之其里来说会能可没好又些过所因为然后次东西干那个怎样")

# 聊天里满地都是、当检索词等于没用的字。整串两个字都落在这堆里，这个两字词就不收
# ——不然「今天」「天气」「时候」「怎么样」会把闲话也凑出一堆分来。
FILLER = set("今天明昨现最刚气怎什时感知觉么样候该应当知")

# 段头 ## [锚点] 类型 YYYY-MM-DD HH:MM:SS。日期 2026-11-02 才补上，旧的只有时分秒——
# 两种都认：不认旧的，那一段会被并进上一段的正文里，比时间缺日期更糟。
SEG_RE = re.compile(r"^##\s*\[([^\]]+)\]\s*(\S+)\s*((?:\d{4}-\d{2}-\d{2} )?\d{2}:\d{2}:\d{2})\s*$")
SESS_RE = re.compile(r"^会话\s+([0-9a-f]{6,})")
RANGE_RE = re.compile(r"([0-9a-f]{6,})-(\d{2,3})-([0-9a-f]{6,})-(\d{2,3})")
ITEM_RE = re.compile(r"^\s*(\d+)\.\s*(.+?)\s*[｜|]\s*(.+?)\s*$")


def is_word(ch):
    return ch.isdigit() or ("a" <= ch.lower() <= "z") or ("\u4e00" <= ch <= "\u9fff")


def terms(text):
    out, runs, cur = [], [], []
    for ch in text:
        if is_word(ch):
            cur.append(ch.lower())
        else:
            if cur:
                runs.append("".join(cur))
                cur = []
    if cur:
        runs.append("".join(cur))
    for run in runs:
        n = len(run)
        for i in range(n):
            if run[i] not in STOP:
                out.append(run[i])
            if i + 1 < n and not (run[i] in FILLER and run[i + 1] in FILLER):
                out.append(run[i:i + 2])
        # 整串只留「像标识符的」：带英文数字的命令名、参数名、文件名。
        # 中文长串整串保留以前是个坑——问题原句只会在「转述这个问题」的段里出现一次，
        # idf 高到离谱，把真正的答案段挤出去了。
        if 3 <= n <= 24 and not ("\u4e00" <= run[0] <= "\u9fff"):
            out.append(run)
    return out


def parse_segments():
    segs = {}
    if not CORPUS.is_dir():
        sys.exit("缺语料目录：" + str(CORPUS))
    for f in sorted(CORPUS.glob("*.md")):
        sid = f.stem
        anchor, kind, tm, buf = None, "", "", []
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            m = SEG_RE.match(line)
            if m:
                if anchor:
                    segs[anchor] = {"sid": sid, "kind": kind, "time": tm,
                                    "text": "\n".join(buf).strip()}
                anchor, kind, tm, buf = m.group(1), m.group(2), m.group(3), []
            elif anchor is not None:
                buf.append(line)
        if anchor:
            segs[anchor] = {"sid": sid, "kind": kind, "time": tm,
                            "text": "\n".join(buf).strip()}
    return segs


def parse_topics():
    docs, sid = [], ""
    if not TOPICS.is_file():
        sys.exit("缺事表：" + str(TOPICS))
    for line in TOPICS.read_text(encoding="utf-8", errors="replace").splitlines():
        m = SESS_RE.match(line.strip())
        if m:
            sid = m.group(1)
            continue
        m = ITEM_RE.match(line)
        if not m:
            continue
        r = RANGE_RE.search(m.group(3))
        if r:
            docs.append({"sid": r.group(1), "seq": int(m.group(1)),
                         "title": m.group(2).strip(),
                         "a": int(r.group(2)), "b": int(r.group(4))})
    return docs


def aid(sid, n):
    return "%s-%03d" % (sid, n)


def tvec(toks):
    return {t: 1.0 + math.log(c) for t, c in Counter(toks).items()}


K1, B = 1.2, 0.75

# 换个说法就不认得了，是人也不会这样。只放真正会混的几组，别贪多——加宽了会把不相干的段一起捞上来。
ALIAS = [
    ["多帧", "zstd", "解压", "会话原文", "jsonl"],
    ["钩子", "hook", "hookd", "低完整性", "UIPI"],
    ["硬链接", "mklink", "junction", "同步"],
    ["停摆", "卡住", "不动了", "不投了", "断了"],
    ["闪黑窗", "黑窗口", "黑窗", "闪一下"],
    ["报错", "错误", "stderr", "报错原文"],
]

DEAD_DF = 0.3  # 出现在三成以上段里的词，等于没信息量（今天、怎么、时候）
RARE_IDF = 3.0   # 三字以上的词形，够稀就算定得住位置
RARE_IDF2 = 3.6  # 两字的要更稀，「今天」这种两字词才混不进来


def bm25(q, tf, dl, idf, avgdl, miss=0.0):
    """匹配度，落在 0～2.2 之间：命中词的 idf 质量 ÷ 整句词的 idf 质量。

    以前用「求和」+余弦，两种都踩过坑：求和让一堆低信息量的词（今天、怎么）凑出高分，
    余弦把「说得最全的长段」压下去。改成算占比，一句闲话就压不住真正对上的词。
    miss 是「语料里根本没有的词」按多少 idf 记——给高值，问错方向的话自己就沉下去。
    """
    if not q or not dl or not avgdl or avgdl <= 0:
        # avgdl <= 0 说明语料里一段都没有：拿它当除数会 ZeroDivisionError，
        # 而在调用方看来只是「这一问没分」——又是一个静默失败。直接给 0。
        return 0.0
    num, den = 0.0, 0.0
    for t, w in q.items():
        base = w * idf.get(t, miss)
        den += base
        f = tf.get(t)
        if not f:
            continue
        num += base * f * (K1 + 1.0) / (f + K1 * (1.0 - B + B * dl / avgdl))
    if den <= 0:
        return 0.0
    return min(num / den, K1 + 1.0)


def snippet(text, qlong, width):
    t = re.sub(r"\s+", " ", text).strip()
    if len(t) <= width:
        return t
    pos = -1
    for term in sorted(qlong, key=len, reverse=True):
        p = t.find(term)
        if p >= 0:
            pos = p
            break
    start = 0 if pos < 0 else max(0, pos - width // 3)
    if start + width > len(t):
        start = max(0, len(t) - width)
    return ("…" if start else "") + t[start:start + width] + \
           ("…" if start + width < len(t) else "")
