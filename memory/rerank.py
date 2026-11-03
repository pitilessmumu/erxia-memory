# -*- coding: utf-8 -*-
# DECISION: 长段落按窗打分取最高，不整段喂 | when: 2026-11-02 | cause: 现场逼的
"""重排：bge-reranker-v2-m3（int8 ONNX）在 CPU 上给候选段打相关分，全程本机。

为什么要这一层：字面分（BM25 口径）和语义分（bge-small 余弦）不是同一把尺，
正题和安静题的最高分能挤在同一个区间（0.550 对 0.568），单门槛怎么调都分不开。
cross-encoder 把「问题 + 段落」一起喂进去，判的是这段到底答不答这个问题，
不是「词碰上了没有」也不是「 embedding 像不像」。

分数口径：模型输出 logits，过 sigmoid 压到 0~1，越大越相关。

用法：
  python rerank.py "加新功能之前先干什么"   # 看候选段各打多少分（候选从 pool.py 取）
  python quiz.py --重排                     # 全部考题各打一遍（正题 / 安静题分界）

这一层是纯库：给「问题 + 候选正文」回分数。候选从哪儿来它不管，命令行演示才去
pool.py 取——这样它不会反过来依赖 hybrid（2026-11-04 拆的环）。
"""
import os
import re
import sys
from pathlib import Path

import config  # 路径统一从 config 取

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
# 原来写死 <工作区>/memory-lab/models，仓库一搬就错；现在从 config 取
MODEL = Path(os.environ.get("RERANK_MODEL", str(config.MODELS / "bge-reranker")))
MAXLEN = int(os.environ.get("RERANK_MAXLEN", 512))
# 段落常比 512 token 长，关键句可能在中间或尾巴。整段截前 512 字会把 zstd 那种
# 一句话证据切掉（实测同一段：截前 512 字 0.12，含证据的窗口 0.55）。
# 所以长段切窗、取最高分那窗的分数；只留前缀窗 + 一个问题词最密的窗，控住开销。
WIN = int(os.environ.get("RERANK_WIN", 480))
STRIDE = max(160, WIN * 2 // 3)
MAXWIN = int(os.environ.get("RERANK_MAXWIN", 2))

_sess = None
_tok = None
_pad_id = 0


def _load():
    """懒加载：没人调 rerank 就不吃这 500MB 内存。"""
    global _sess, _tok, _pad_id
    if _sess is not None:
        return _sess, _tok
    import onnxruntime as ort
    from tokenizers import Tokenizer

    model = None
    for name in ("model_int8.onnx", "model_quantized.onnx", "model.onnx"):
        if (MODEL / name).is_file():
            model = MODEL / name
            break
    if model is None:
        raise FileNotFoundError("重排模型没找到：%s 下没有 model_int8/quantized/onnx" % MODEL)
    if not (MODEL / "tokenizer.json").is_file():
        raise FileNotFoundError("重排模型缺 tokenizer.json：%s" % MODEL)

    so = ort.SessionOptions()
    so.intra_op_num_threads = min(8, os.cpu_count() or 4)
    _sess = ort.InferenceSession(str(model), so, providers=["CPUExecutionProvider"])
    _tok = Tokenizer.from_file(str(MODEL / "tokenizer.json"))
    _tok.enable_truncation(max_length=MAXLEN)
    # XLM-R 的 pad id 是 1（<pad>），BERT 系是 0（[PAD]）：按词表自己认，别写死。
    _pad_id = (_tok.token_to_id("<pad>") or _tok.token_to_id("[PAD]") or 0)
    _tok.enable_padding(pad_id=_pad_id, pad_token=_tok.id_to_token(_pad_id) or "<pad>")
    return _sess, _tok


def _qgrams(q):
    """问题里值得对准的词：英文/数字词 + 中文字符 2-gram。只用来挑窗口，不参与打分。"""
    q = (q or "").lower()
    grams = set(w for w in re.findall(r"[a-z0-9_.]{2,}", q))
    cjk = "".join(re.findall(r"[\u4e00-\u9fff]", q))
    for i in range(len(cjk) - 1):
        grams.add(cjk[i:i + 2])
    return grams


def _windows(text, grams):
    """长段切窗：前缀窗 + 问题词覆盖最多的窗（最多 MAXWIN 个）。"""
    if len(text) <= WIN:
        return [text]
    starts = list(range(0, max(1, len(text) - WIN + 1), STRIDE))
    if starts[-1] + WIN < len(text):
        starts.append(len(text) - WIN)
    low = text.lower()
    ranked = sorted(((sum(1 for g in grams if g in low[i:i + WIN]), i) for i in starts),
                    reverse=True)
    keep = {0} | {i for n, i in ranked[:MAXWIN - 1] if n > 0}
    return [text[i:i + WIN] for i in sorted(keep)]


def rerank(q, texts, batch=8):
    """问题 vs 每个候选 -> 相关分（0~1 的 float 列表，顺序跟 texts 对齐）。

    候选一条都没有就返回空列表；模型没装好会抛异常，让上层决定是回退还是报错。
    长段落按窗打分取最高分（见文件头的 WIN 说明）。
    """
    texts = list(texts or [])
    if not texts:
        return []
    import numpy as np
    sess, tok = _load()
    want = {i.name for i in sess.get_inputs()}
    grams = _qgrams(q)
    groups = [_windows(t or " ", grams) for t in texts]
    pairs = [(q, w if w.strip() else " ") for g in groups for w in g]
    raw = []
    for i in range(0, len(pairs), batch):
        chunk = pairs[i:i + batch]
        encs = tok.encode_batch(chunk)
        ids = np.array([e.ids for e in encs], dtype=np.int64)
        mask = np.array([e.attention_mask for e in encs], dtype=np.int64)
        feed = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in want:
            feed["token_type_ids"] = np.zeros_like(ids)
        logits = sess.run(None, feed)[0]
        logits = np.asarray(logits, dtype=np.float32).reshape(len(chunk), -1)
        # DECISION: 输出维度必须是 (b,1)，不猜 | when: 2026-11-03 | cause: 做歪的
        # 原来按 shape[1] > 1 猜该取哪一列：换成分类型头 (b,2) 时会静默取「label 1」的 logit，
        # 语义完全不同，分数照样 0~1、照样能跑。宁可报错。
        if logits.shape[1] != 1:
            raise RuntimeError("重排模型输出维度是 %s，应为 (b,1)；换模型了要重新确认取列方式"
                               % (logits.shape,))
        z = logits[:, 0]
        raw.extend((1.0 / (1.0 + np.exp(-z))).tolist())
    out, pos = [], 0
    for g in groups:
        out.append(float(max(raw[pos:pos + len(g)])))
        pos += len(g)
    return out


def main():
    av = sys.argv[1:]
    if not av:
        print(__doc__)
        return
    q = av[0]
    import pool
    segs, _, _, _ = pool.candidates(q)
    if not segs:
        print("候选一条都没有")
        return
    scores = rerank(q, [s.get("text") or "" for s in segs])
    for s, sc in sorted(zip(segs, scores), key=lambda p: -p[1]):
        print("%.3f  %s  %s" % (sc, s["anchor"],
                                (s.get("text") or "").replace("\n", " ")[:70]))


if __name__ == "__main__":
    main()
