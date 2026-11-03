# DECISION: 混检的早期对照脚本，留作比较基线 | when: 2026-11-02 | cause: 做歪的
import os, sys, time
# 原来写死本机绝对路径；改成脚本自己所在目录，放在哪儿都能跑
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hybrid, quiz, corpus, vec_eval, db
texts = corpus.parse_segments()

def show(title, cases):
    ok=0; ms=0
    print("=== %s ==="%title)
    for c in cases:
        t=time.time(); r=hybrid.search(c["q"], k=8); ms+=time.time()-t
        segs=r["segs"]
        if c.get("quiet"):
            passed=not segs; ok+=passed
            print(("过" if passed else "漏"), "%-8s"%r["route"], "安静  ", c["q"])
            continue
        hit=None
        for rank,s in enumerate(segs[:8],1):
            body=((texts.get(s["anchor"]) or {}).get("text") or s.get("text") or "").lower()
            if any(k.lower() in body for k in c["kw"]): hit=rank; break
        ok+=bool(hit)
        print(("过" if hit else "漏"), "%-8s"%r["route"], "第%s名"%hit if hit else "  -  ", c["q"])
    print("得分 %d/%d，平均 %.0f ms"%(ok,len(cases),ms*1000/len(cases)))
    return ok

show("十道原题", quiz.CASES)
show("换说法", vec_eval.CASES)
print("=== 字面自己给的最高分（诊断用）===")
for c in quiz.CASES + vec_eval.CASES:
    r=db.search(c["q"], nseg=8, ndoc=3)
    print("%.2f  %s"%(max([float(s.get("score") or 0) for s in (r.get("segs") or [])], default=0), c["q"]))