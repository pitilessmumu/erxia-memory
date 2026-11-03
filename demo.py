# -*- coding: utf-8 -*-
"""跑一个例子：给一句话，把库里最像的几段原文捞出来。

    python demo.py 我妈的膝盖怎么回事

只走字面检索（不需要模型，不需要联网）。装了模型的话会把三层都用上，
没装就退化成纯字面——会自己说一声，不会假装跑了全部。
"""
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "memory"))

import db  # noqa: E402


def main():
    q = " ".join(sys.argv[1:]).strip() or "骨关节炎要紧吗"

    if not db.DB.is_file():
        sys.exit("索引库还没建。先跑：python memory/db-build.py")

    res = None
    try:
        # 三层都上（要模型）。vec / rerank 各自会在缺件时自己退，不抛。
        import hybrid
        res = hybrid.search(q, k=3)
        # 三层各自缺件时会自己退（并往 stderr 说一声），所以这里不敢保证真跑了三层
        print("（先试三层：字面 + 语义 + 重排；有哪层没跑起来会在下面说）\n")
    except Exception as e:
        print("（只有字面检索：%s: %s——语义和重排没跑起来，通常是模型还没放）\n"
              % (type(e).__name__, e))
        res = db.search(q, nseg=3, ndoc=3)

    if res.get("err"):
        print(res["err"])
        return

    empty = res.get("empty") or {}
    segs = res.get("segs") or []
    if not segs:
        # 空结果必须说清是哪一种（没入库 / 没记过 / 被推翻滤掉），不静默。
        print("没找到。原因：%s" % (empty.get("reason") or "未分类"))
        print(empty.get("message") or "")
        cov = empty.get("coverage") or {}
        if cov.get("cover_min"):
            print("\n（库覆盖 %s 至 %s，共 %s 段）"
                  % (cov.get("cover_min"), cov.get("cover_max"), cov.get("n_segments")))
        return

    print("问：%s\n" % q)
    for i, s in enumerate(segs, 1):
        print("[%d] %s  %.3f  %s" % (i, s.get("anchor"), s.get("score") or 0, s.get("time") or ""))
        body = (s.get("text") or "").strip().splitlines()
        print("    " + (body[0][:80] if body else ""))
        print()


if __name__ == "__main__":
    main()
