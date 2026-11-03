# -*- coding: utf-8 -*-
# DECISION: 嵌入用 CLS 位池化，跟 bge 官方一致 | when: 2026-11-02 | cause: 现场逼的
"""语义检索（embed 第二路）：bge-small-zh-v1.5 onnx，CPU，不碰显存。

为什么要有这一路：字面对不上就得多来回一轮。木木当时说「球」，改天说「圆」，
现算版按 1/2-gram 命中，一个词都对不上就整条落空；embed 压的就是这个误差。

用法：
  python vec.py --建      # 给全部段落算坐标，写 lab\\index\\emb.npz（约几十秒）
  python vec.py "要办的事"  # 语义取前 N 段（--宽）
  python vec.py --考      # 十条题各打分，用来定安静门槛
"""
import os
import sys, json, time, os
from pathlib import Path

import config  # 路径统一从 config 取

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
# 原来写死 <工作区>/memory-lab，仓库一搬就错；现在从 config 取（VEC_MODEL / VEC_INDEX 仍然优先）
LAB = config.LAB
# 模型和索引都能用环境变量换：VEC_MODEL / VEC_INDEX，方便拿 small 和 base 对照
MODEL = Path(os.environ.get("VEC_MODEL", str(config.MODELS / "bge-small-zh")))
INDEX = Path(os.environ.get("VEC_INDEX", str(LAB / "index" / "emb.npz")))

# bge-zh-v1.5 官方的查询指令，检索时才加，段落不加
QINSTR = "为这个句子生成表示以用于检索相关文章："

CHUNK = int(os.environ.get("VEC_CHUNK", 400))   # 一段太长就切块，切了按最高分算回来
STRIDE = int(os.environ.get("VEC_STRIDE", 320))

_sess = None
_tok = None


def _load():
    global _sess, _tok
    if _sess is not None:
        return _sess, _tok
    import onnxruntime as ort
    from tokenizers import Tokenizer
    model = MODEL / "model_quantized.onnx"
    if not model.exists():
        model = MODEL / "model.onnx"
    so = ort.SessionOptions()
    so.intra_op_num_threads = min(8, (__import__("os").cpu_count() or 4))
    _sess = ort.InferenceSession(str(model), so, providers=["CPUExecutionProvider"])
    _tok = Tokenizer.from_file(str(MODEL / "tokenizer.json"))
    _tok.enable_truncation(max_length=512)
    _tok.enable_padding(pad_id=0, pad_token="[PAD]")
    return _sess, _tok


def encode(texts, is_query=False, batch=16):
    """一批文本 -> L2 归一化后的向量矩阵（float32）。用 CLS 位，跟 bge 官方一致。"""
    import numpy as np
    sess, tok = _load()
    prefix = QINSTR if is_query else ""
    out = []
    for i in range(0, len(texts), batch):
        chunk = [prefix + t for t in texts[i:i + batch]]
        encs = tok.encode_batch(chunk)
        ids = np.array([e.ids for e in encs], dtype=np.int64)
        mask = np.array([e.attention_mask for e in encs], dtype=np.int64)
        feed = {"input_ids": ids, "attention_mask": mask}
        want = {i.name for i in sess.get_inputs()}
        if "token_type_ids" in want:
            feed["token_type_ids"] = np.zeros_like(ids)
        h = sess.run(None, feed)[0]           # last_hidden_state: (b, L, 512)
        v = h[:, 0, :]                        # CLS
        v = v / np.linalg.norm(v, axis=1, keepdims=True).clip(1e-9)
        out.append(v.astype(np.float32))
    return np.vstack(out) if out else np.zeros((0, 512), dtype=np.float32)


def chunks_of(text):
    text = (text or "").strip()
    if len(text) <= CHUNK:
        return [text] if text else []
    return [text[i:i + CHUNK] for i in range(0, len(text), STRIDE) if text[i:i + CHUNK].strip()]


def build(force=False):
    import numpy as np
    if INDEX.exists() and not force:
        print("已存在 %s，加 --强 才重建" % INDEX)
        return
    t0 = time.time()
    segs = _parse_segments()
    anchors, texts = [], []
    for a, s in segs:
        for c in chunks_of(s["text"]):
            anchors.append(a)
            texts.append(c)
    print("段落 %d 条，切成 %d 块，开始算坐标…" % (len(segs), len(texts)))
    vecs = encode(texts)
    INDEX.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(INDEX, anchors=np.array(anchors), vecs=vecs)
    print("写好 %s（%d 块，%.1f 秒，%.1f MB）" % (
        INDEX.name, len(texts), time.time() - t0, INDEX.stat().st_size / 1e6))


def _parse_segments():
    sys.path.insert(0, str(HERE))
    import recall
    d = recall.parse_segments()
    return [(a, s) for a, s in d.items()]


def search(q, k=8, floor=0.0, json_out=False):
    import numpy as np
    if not INDEX.exists():
        return {"err": "还没建索引，先跑 python vec.py --建"}
    z = np.load(INDEX, allow_pickle=True)
    anchors, vecs = z["anchors"], z["vecs"]
    qv = encode([q], is_query=True)[0]
    sims = vecs @ qv
    # 每段取自己最高的那一块（切过块的要还原成段）
    best = {}
    for i, a in enumerate(anchors):
        a = str(a)
        s = float(sims[i])
        if s > best.get(a, -9.0):
            best[a] = s
    ranked = sorted(best.items(), key=lambda kv: -kv[1])
    if floor > 0:
        ranked = [(a, s) for a, s in ranked if s >= floor]
    segs = dict(_parse_segments())
    out = []
    for a, s in ranked[:k]:
        meta = segs.get(a) or {}
        out.append({"anchor": a, "score": round(s, 3), "kind": meta.get("kind"),
                    "time": meta.get("time"), "text": (meta.get("text") or "")[:160]})
    return {"q": q, "segs": out, "floor": floor}


def main():
    av = sys.argv[1:]
    if "--建" in av:
        build(force="--强" in av)
        return
    if "--考" in av:
        import quiz
        for c in quiz.CASES:
            t0 = time.time()
            r = search(c["q"], k=1)
            ms = (time.time() - t0) * 1000
            top = r["segs"][0]["score"] if r.get("segs") else 0.0
            print("%.3f  %5.0fms  %s  <<  %s" % (top, ms, c["q"], c["note"]))
        return
    if not av:
        print(__doc__)
        return
    q = av[0]
    k = int(av[av.index("--宽") + 1]) if "--宽" in av else 8
    floor = float(av[av.index("--最低") + 1]) if "--最低" in av else 0.0
    r = search(q, k=k, floor=floor)
    if "--json" in av:
        print(json.dumps(r, ensure_ascii=False))
        return
    if r.get("err"):
        print(r["err"])
        return
    for s in r["segs"]:
        print("%.3f  %s  %s" % (s["score"], s["anchor"], (s["text"] or "").replace("\n", " ")[:80]))


if __name__ == "__main__":
    main()
