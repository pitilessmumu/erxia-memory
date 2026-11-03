# -*- coding: utf-8 -*-
# DECISION: 考卷默认走混检：字面不够硬才让语义上 | when: 2026-11-02 | cause: 现场逼的
"""记忆回归考卷：固定题库 + 死判据 + 可重复跑分。

判据不考「答得像不像」，只考「该想起来的那段，在不在取回来的前 N 条里」。
每条题给一组关键词，取回来的段原文里必须命中至少一个；命不中就判漏。
另有一条反向题：跟记忆无关的问题必须安静（不许硬凑）。

用法：python quiz.py            # 跑全部
      python quiz.py -v         # 带出每条的命中位置
      python quiz.py --限 8     # 只看前 8 段
"""
import io
import json
import time
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import recall  # noqa: E402

CASES = [
    {"q": "为什么进程活着但钩子收不到事件", "kw": ["UIPI", "icacls", "完整性"], "note": "低完整性摘钩"},
    {"q": "会话原文是压缩的，怎么解开的", "kw": ["多帧", "28 b5 2f fd", "zstd"], "note": "zstd 多帧"},
    {"q": "桥跑一阵就不动了", "kw": ["停摆"], "note": "桥停摆"},
    {"q": "任务清单和邮箱里那份是同一个文件吗", "kw": ["硬链接"], "note": "硬链接"},
    {"q": "桌面上偶尔闪一下黑窗口", "kw": ["闪黑窗", "黑窗口"], "note": "闪黑窗"},
    {"q": "智谱的输出里混进了别的东西", "kw": ["stdout", "泄漏"], "note": "stdout 泄漏"},
    {"q": "改完记忆结构怎么知道没搜漏", "kw": ["考卷", "回归", "黑箱"], "note": "回归测试"},
    {"q": "记忆里哪几份是不能忘的", "kw": ["常设", "接力"], "note": "记忆分层"},
    {"q": "换壳完了第一件事干什么", "kw": ["叫他", "叫人"], "note": "换壳先叫人"},
    {"q": "电池坏了怎么办", "kw": ["电池"], "note": "电池坏了报告停手"},
    {"q": "能不能留个本地回退兜底", "kw": ["回退", "兜底"], "note": "不留本地回退"},
    {"q": "记忆索引库用什么存的", "kw": ["SQLite"], "note": "索引库是 SQLite"},
    {"q": "木木说重启的时候我得先问什么", "kw": ["对象"], "note": "重启必须带对象（补语料后转计分）"},
    {"q": "加新功能之前先干什么", "kw": ["轮子"], "note": "先查轮子（补语料后转计分）"},
    {"q": "今天天气怎么样", "kw": [], "note": "反向题：该安静", "quiet": True},
    {"q": "怎么用 Blender 做一集动画", "kw": [], "note": "反向题：该安静（题面不能写进记忆，否则考题泄漏）", "quiet": True},
    {"q": "明天股票会涨吗", "kw": [], "note": "反向题：该安静", "quiet": True},
    {"q": "红烧肉怎么做", "kw": [], "note": "反向题：该安静", "quiet": True},
    {"q": "李白是哪个朝代的", "kw": [], "note": "反向题：该安静", "quiet": True},
]

# 2026-11-02 补语料后，原来的两个「已知洞」已转成计分题：常设/规则/二夏记忆已按 ## 切段
# 写进 origin\corpus（sid ac000001 / ac000003 / ac000004，共 57 段），检索能查到了。
# 这里留空；以后再有库外的题再往进加。
KNOWN_GAPS = []


def run_one(q, limit):
    old_argv, old_out = sys.argv, sys.stdout
    sys.argv = ["recall.py", q, "--json", "--段", str(limit)]
    buf = io.StringIO()
    sys.stdout = buf
    try:
        recall.main()
    except SystemExit:
        pass
    finally:
        sys.argv, sys.stdout = old_argv, old_out
    txt = buf.getvalue().strip()
    if not txt.startswith("{"):
        return None
    return json.loads(txt)


def run_one_db(q, limit):
    """同一条题，换 SQLite 索引那版查（库当索引、正文不进来，只回锚点和行号）。"""
    import db as dbmod
    res = dbmod.search(q, nseg=limit, ndoc=5)
    if res.get("err"):
        return None
    # DECISION: 空 segs 回 None，别回空壳 | when: 2026-11-03 | cause: 做歪的
    # 原来只要没 err 就回 dict，segs 空也回——安静题在 --引擎 db 下必判漏、在 混 下判过，
    # 同一批题在两个引擎上的跑分不可比（智谱挑出来的）。
    if not res.get("segs"):
        return None
    dbmod.connect().close()
    return {"segs": [{"anchor": s["anchor"], "score": round(s["score"], 2),
                      "kind": s["kind"], "time": s["time"], "text": s["text"]}
                     for s in res["segs"]],
            "docs": [{"seq": e["seq"], "title": e["title"], "a": e["a"], "b": e["b"],
                      "sid": e["sid"]} for e in res["events"]]}


def run_one_hybrid(q, limit):
    """混着来：字面证据不够硬时才让语义上（hybrid.py 定的规矩）。"""
    import hybrid
    res = hybrid.search(q, k=limit)
    if not res.get("segs"):
        return None
    return {"segs": [{"anchor": s["anchor"], "score": round(float(s.get("score") or 0), 2),
                      "kind": s.get("kind"), "time": s.get("time"), "text": s.get("text")}
                     for s in res["segs"][:limit]],
            "docs": [{"seq": e["seq"], "title": e["title"], "a": e["a"], "b": e["b"],
                      "sid": e["sid"]} for e in (res.get("events") or [])],
            "route": res.get("route")}


def main():
    limit = 8
    verbose = False
    av = sys.argv[1:]
    if "-v" in av:
        verbose = True
    if "--限" in av:
        limit = int(av[av.index("--限") + 1])

    segs = recall.parse_segments()
    engine = "py"
    if "--引擎" in av:
        engine = av[av.index("--引擎") + 1]
    ok, total_ms = 0, 0
    print("记忆考卷（每条第 %d 段以内算命中，引擎 %s）" % (limit, engine))
    for c in CASES:
        t0 = time.time()
        if engine == "混":
            res = run_one_hybrid(c["q"], limit)
        elif engine == "db":
            res = run_one_db(c["q"], limit)
        else:
            res = run_one(c["q"], limit)
        total_ms += (time.time() - t0) * 1000
        top = res["segs"][0]["score"] if res and res.get("segs") else 0.0
        if c.get("quiet"):
            passed = res is None
            print("%s 安静  %.2f  %s" % ("过" if passed else "漏", top, c["q"]))
            ok += passed
            continue
        if res is None:
            print("漏  %s —— 一条都没落上（%s）" % (c["q"], c["note"]))
            continue
        hit, where = None, None
        for rank, s in enumerate(res["segs"], 1):
            body = ((segs.get(s["anchor"]) or {}).get("text") or s.get("text") or "").lower()
            for k in c["kw"]:
                if k.lower() in body:
                    hit, where, hit_rank = k, s["anchor"], rank
                    break
            if hit:
                break
        if hit:
            ok += 1
            # DECISION: 分数要打在命中段上，不是第 1 段 | when: 2026-11-03 | cause: 做歪的
            # 原来打印 top（第 1 段的分），但「过」看的是命中段——命中可能在第 8 段，
            # 人一眼读成「这个分就过了」，跑分趋势被骗（双方都点过）。
            # 现在两个数并列：第 1 段分、命中段分，各自带未加权的重排分。
            _s0 = res["segs"][0]
            _sh = res["segs"][hit_rank - 1]
            print("过  第1段 %.2f(重排%s) / 命中第%d段 %.2f(重排%s)  %s  提到[%s]  %s"
                  % (top, _s0.get("rerank", "-"), hit_rank, _sh.get("score", 0),
                     _sh.get("rerank", "-"), where, hit, c["q"]))
            if verbose:
                for d in res["docs"][:3]:
                    print("     事: %s" % d["title"])
        else:
            print("漏  %s —— 前 %d 段里没有[%s]（%s）" % (c["q"], limit, "/".join(c["kw"]), c["note"]))
            if verbose:
                print("     取回: %s" % ", ".join(s["anchor"] for s in res["segs"]))
    print("得分 %d/%d，平均每问 %.0f ms" % (ok, len(CASES), total_ms / len(CASES)))
    if KNOWN_GAPS:
        print("已知洞 %d 条（不计分：问的是 记忆\\*.md 里的规矩，检索语料只收会话段）：" % len(KNOWN_GAPS))
        for g in KNOWN_GAPS:
            print("  · %s —— %s" % (g["q"], g["note"]))


if __name__ == "__main__":
    main()
