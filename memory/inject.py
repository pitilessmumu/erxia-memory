# -*- coding: utf-8 -*-
"""记忆注入：新壳开工时，按「要接着办的事」取料，不整篇塞。

做法分三段：
  1. 常设 + 接力里跟这件事相关的**小节**（规矩和交接口径，按小节打分，整节不超预算）
  2. 会话原文里相关的**段**（以前怎么做的、当时怎么判的，带锚点）
  3. 拼成一块 1500 字以内的注入块，直接当新壳的第一条消息用

不用把 接力.md（55K）和 常设.md（36K）整篇塞进去——那就是以前掉记忆的根源：
塞满上下文没几天，真正相关的几段又被挤出去。

用法：
  python inject.py --问 "接着做记忆插件的注入部分"
  python inject.py                       # 不给问题，就拿接力里最新的「下一步」
  python inject.py --预算 2000
"""
import re
import sys
import time
from collections import Counter
from pathlib import Path

try:
    # newline="\n" 很关键：Windows 文本模式默认把 \n 翻成 \r\n，插件按行取
    # 「这壳要接着办的事」时会带上行尾 \r，正则 .*$ 匹配不上、话题截不断
    # （2026-11-02 会话标题因此变成半句话）。这里强制 LF。
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import recall  # noqa: E402

MEM = HERE.parent.parent / "记忆"
FILES = [("常设", MEM / "常设.md"), ("接力", MEM / "接力.md"), ("现场", MEM / "现场.md")]
TAIL_ONLY = {"现场": 30}  # 现场是流水账，只认最后三十节

# 给人看的那一块（木木 2026-11-02 提的）：他跟不上技术细节，也想一眼知道到哪了。
# 这份永远是三五行人话，注入块最前面原样带上，换壳时新会话第一条消息里就有它。
# 注入块里「到哪了」读施工进度（基调 + 三条线），不读 现在.md——
# 两个文件都写「到哪了」必然烂一个，2026-11-02 查出来烂的是 现在.md。
PLAIN = MEM / "施工进度.md"


def sections(path, tail=None):
    if not path.is_file():
        return []
    out, cur, buf = [], "", []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("## "):
            if cur:
                out.append((cur, "\n".join(buf).strip()))
            cur, buf = line[3:].strip(), []
        elif cur:
            buf.append(line)
    if cur:
        out.append((cur, "\n".join(buf).strip()))
    if tail:
        out = out[-tail:]
    return out


def hybrid_json(q, n):
    """历史材料一律走混合检索（2026-11-02 改）：字面+语义宽召回 → 重排 → 对账闸。

    以前只在「字面一条都没落上」时才让 hybrid 出手，换壳注入里因此最多只带一段
    历史原文，而且字面凑出来的那段常常不是最相关的。换壳注入一次，多等几秒值得，
    所以改成无条件走 hybrid（它内部本来就有字面这一路，门槛和权重全在 hybrid 里，
    这里一个字都不改）。要求解释器带 onnxruntime——inject 现在用
    `voice\\.venv` 的 python（python-path.txt 指过去），不再用主运行时那个。
    任何异常都吞掉返回 None：检索坏了只少历史段，不能让换壳注入失败。
    """
    try:
        import hybrid
        return hybrid.search(q, k=n)
    except Exception as e:
        # 别装「检索正常」：排障时至少要能分清「真没相关历史」和「onnx 挂了」
        print("历史检索挂了：%s" % type(e).__name__, file=sys.stderr)
        return None


def clip(text, limit):
    """压成一行并在句子边界截断：直接切 200 字常把半句话留在首条消息里。"""
    s = re.sub(r"\s+", " ", text)
    if len(s) <= limit:
        return s
    cut = s[:limit]
    for sep in ("。", "；", "！", "？"):
        i = cut.rfind(sep)
        if i >= limit * 0.5:
            return cut[: i + 1]
    return cut.rstrip() + "…"


def clip_seg(text, limit):
    """历史段的裁剪：压成一行，优先在句子边界断，但**不低于 min(150, limit)**。

    原来的 clip 允许在 limit/2 处断句，历史段会掉到一百字上下；木木要求每段
    150～260 字，边界落得太早宁可硬切（补省略号），也不交半句话。
    预算紧时不能硬抬回 150——那会把总量顶穿，下限跟着 limit 走。
    """
    s = re.sub(r"\s+", " ", text)
    limit = max(20, min(260, limit))
    floor = min(150, limit)
    if len(s) <= limit:
        return s
    cut = s[:limit]
    best = -1
    for sep in ("。", "；", "！", "？"):
        i = cut.rfind(sep)
        if i > best:
            best = i
    if best + 1 >= floor:
        return cut[:best + 1]
    # 硬切时别腰斩 URL/锚点：先退到最后一个空格或 /
    soft = max(cut.rfind(" "), cut.rfind("/"))
    if soft + 1 >= floor:
        return cut[:soft + 1].rstrip() + "…"
    # 退到「://」之前会掉到 floor 以下（比腰斩 URL 更伤预算），这里不特判 scheme，直接硬切补省略号。
    return cut[:limit - 1].rstrip() + "…"


def _int(x):
    """宽容取整：hybrid 的 events 若给出「3条」「12abc」这类非纯数字串，取其中第一个整数，
    一个数都取不到才算 0。别让最后一步整脚本死，也别把「3条」读成 0 命中。"""
    m = re.search(r"-?\d+", str(x if x is not None else ""))
    return int(m.group()) if m else 0


_PRE_LEN = [0]   # 注入块前半段（固定内容）已经打了多少字，进预算账用


def _say(s):
    """打印并记账：注入块里的每一段都要算进预算，不许有漏网的字。"""
    print(s)
    _PRE_LEN[0] += len(s) + 1


def main():
    q = ""
    budget = 1500
    av = sys.argv[1:]

    def arg_of(flag):
        """取 --xx 后面那个参数；没有、或者跟上来的又是一个 --xx，都算没给。"""
        try:
            i = av.index(flag)
        except ValueError:
            return None
        if i + 1 >= len(av) or av[i + 1].startswith("--"):
            return None
        return av[i + 1]

    if "--问" in av:
        q = arg_of("--问")
        if not (q or "").strip():
            sys.exit('--问 后面要跟一句话，比如 --问 "接着做注入"。这次没给。')
    if "--预算" in av:
        bv = arg_of("--预算")
        try:
            budget = max(200, int(bv))
        except (TypeError, ValueError):
            sys.exit("--预算 后面要跟一个数字，比如 --预算 1500。")

    relay = sections(MEM / "接力.md")
    # DECISION: 注入块的任务优先取施工进度的「下一步」 | when: 2026-11-02 | cause: 现场逼的
    # 以前直接取现场最后一条流水，可流水记的是「刚干完的事」。拿它当任务，新壳会去补
    # 一段早做完的账——2026-11-02 实际发生过：它读到的任务是「杂问只跑五句被木木点到」，
    # 于是又去把杂问跑了一遍。
    if not q:
        try:
            prog = (MEM / "施工进度.md").read_text(encoding="utf-8", errors="replace")
            m = re.search(r"\*\*下一步\*\*[:：]\s*(.+)", prog)
            if m:
                q = re.sub(r"^[-*\s]+", "", m.group(1)).replace("**", "").strip()[:80]
        except Exception:
            pass
    if not q:
        # 没有「下一步」，才退回旧办法：先看现场最后一行，再退回接力的「下一步」
        live = MEM / "现场.md"
        if live.is_file():
            lines = [x.strip() for x in live.read_text(encoding="utf-8", errors="replace").splitlines()]
            lines = [x for x in lines if x and not x.startswith("#")]
            if lines:
                # 只认「- 开头」的流水条目，跳过「还没做/下一步」这类尾巴；
                # 再取最后一条的六十个字——整段流水账当问题，匹配就散
                bullets = [x for x in lines if x.startswith("- ")
                           and "还没做" not in x and "下一步" not in x]
                pick = bullets[-1] if bullets else lines[-1]
                # 掐掉时间戳、括号里的注解、markdown 星号，留正句
                pick = re.sub(r"^[-*\s]+", "", pick)
                pick = re.sub(r"^\d{1,2}[:：]\d{2}[0-9A-Za-z]*\s*", "", pick)
                pick = re.sub(r"（[^）]{0,40}）", "", pick)
                pick = pick.replace("**", "")
                q = pick[:50]
        if not q:
            for title, body in reversed(relay):
                if "下一步" in title or "下一壳" in title:
                    q = title + " " + " ".join(body.splitlines()[:3])
                    break
        if not q:
            q = relay[-1][0] if relay else ""
    if not q:
        sys.exit("既没给 --问，接力里也没找到「下一步」。")

    _say("【这壳要接着办的事】" + re.sub(r"\s+", " ", q)[:300])

    # 注入块放「规则」，不放「信息」（2026-11-02 智谱定的，用户那句「我很累」逼出来的）：
    # 信息按需去取，规则常驻。这样闲聊时不会被工程内容压着，闲聊里被顺嘴问到工程问题时
    # 又不会愣住——因为「什么时候必须去读」是写死的，不靠临场想起。
    # DECISION: 注入块带一行基调指针 | when: 2026-11-03 | cause: 现场逼的
    # 白天基调管理讨论定的六步闭环第 3 步——「换壳注入块里加一行基调指针，
    # 不是把整个图纸塞进注入块，而是一行：当前基准是什么、什么时候定的」。
    # 代码里一直没落地，所以新壳不知道「有基调该查」（WB 挑出来的）。
    try:
        _prog = (MEM / "施工进度.md").read_text(encoding="utf-8", errors="replace")
        import re as _re
        _m = _re.search(r"\*\*基调\*\*[:：]\s*(.+)", _prog)
        if _m:
            _say("\n【当前基调】" + _re.sub(r"\s+", " ", _m.group(1)).strip()[:160])
    except Exception:
        pass

    _say("\n【开工前必须先读】")
    _say("动手之前先读 记忆\\施工进度.md —— 那是「在盖什么、到第几层」的唯一依据。")
    _say("下面两种情况必须先读：①他提到任何在做的线、任务或决定；②你要改任何生产文件。")
    _say("纯闲聊（他说累、想聊会儿）不用读，也不用主动提工程的事。")

    # 零、先给一段人话：木木看不懂技术细节，但得一眼知道到哪一步了
    if PLAIN.is_file():
        raw = PLAIN.read_text(encoding="utf-8", errors="replace")
        # 优先只带「注入摘要」那一节：全文太长（两千多字），而且开头是文件自己的说明，
        # 截前七百字会截到元信息、截不到进度（2026-11-02 实际截错过一次）。
        m = re.search(r"(?ms)^##\s*注入摘要.*?(?=^##\s|\Z)", raw)
        plain = m.group(0) if m else raw
        plain = "\n".join(x for x in plain.splitlines()
                           if not x.startswith("#") and not x.startswith("---")).strip()
        if plain:
            _say("\n【现在到哪儿了】")
            _say(plain[:700])

    # 一、规矩和交接口径：按小节打分，只取相关的那几节
    pool = []
    for tag, path in FILES:
        for title, body in sections(path, TAIL_ONLY.get(tag)):
            if len(body) < 30:
                continue
            toks = recall.terms(title + " " + body[:1500])
            pool.append({"tag": tag, "title": title, "body": body,
                         "tf": Counter(toks), "dl": len(toks)})
    df = Counter()
    for d in pool:
        for t in d["tf"]:
            df[t] += 1
    n = max(1, len(pool))
    sidf = {t: __import__("math").log(1.0 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}
    avgdl = sum(d["dl"] for d in pool) / n
    qv = {t: 1.0 for t in recall.terms(q)}
    miss = __import__("math").log(1.0 + (n + 0.5) / 0.5)
    for d in pool:
        d["score"] = recall.bm25(qv, d["tf"], d["dl"], sidf, avgdl, miss)
    pool.sort(key=lambda d: -d["score"])

    # DECISION: 前面已打印的固定段也要进预算账 | when: 2026-11-03 | cause: 做歪的
    # 原来 used 从 0 起算，可注入块最前面已经打了四段固定内容——
    # 【这壳要接着办的事】最多 300 字、【当前基调】约 100、【开工前必须先读】约 120、
    # 【现在到哪儿了】最多 700。加起来一千二，全没进账，「预算 1500」实际输出近两千，
    # 只打一行「超预算了」照旧输出（智谱挑出来的）。
    # 前面已打印的固定段（办的事 / 基调 / 必读 / 到哪了）按实际长度记账。
    # 不能写死一个数——2026-11-03 写过 1220，实际只用了 820，白扣四百预算，
    # 结果「以前在哪儿做过」那段一条都给不出来。
    used = _PRE_LEN[0]
    printed_titles = set()  # 第一路打过的小节标题，兜底不许再打一遍
    for tag in ("常设", "接力"):
        # 每个来源最多一节、正文 200 字（2026-11-02 木木说首条消息太长了：
        # 原来常设两节 817 字 + 以前三段 875 字，加起来比真正要办的活还长）
        got = [d for d in pool if d["tag"] == tag and d["score"] > 0.4][:1]
        if not got:
            continue
        _hdr = "\n【%s里对得上的小节】" % tag
        print(_hdr)
        used += len(_hdr)
        for d in got:
            body = clip(d["body"], 200)
            line = "· " + d["title"] + "：" + body
            print(line)
            used += len(line) + 1
            printed_titles.add(d["title"])

    # 二、以前怎么做的：会话原文里的段（2026-11-02 改：一律走混合检索，取 3～4 段）
    res = hybrid_json(q, 8)
    events = (res or {}).get("events") or []
    # 段 → 事：hybrid 的 events 里自带每件事的段表，比按区间反推稳（vec 那路没有 docs）
    doc_of = {}
    for e in events:
        for s2 in e.get("segs") or []:
            # hybrid 万一哪天把段表换成 anchor 字符串列表，别在这里 AttributeError
            if isinstance(s2, dict) and s2.get("anchor"):
                doc_of[s2["anchor"]] = e

    try:
        segs = recall.parse_segments() if (res and res.get("segs")) else {}
    except Exception as e:
        print("段索引读不出来：%s" % type(e).__name__, file=sys.stderr)
        segs = {}
    kind_w = {"压缩摘要": 1.25, "事实": 1.2, "二夏": 1.0, "报错": 0.9, "木木": 0.85, "子代理": 0.95}
    # 标准记忆文件的 sid：常设四册合起来是 ac000001、规则 ac000003、二夏记忆 ac000004、抽屉 ac000005
    MEM_SIDS = {"ac000001": "常设", "ac000003": "规则", "ac000004": "二夏记忆", "ac000005": "抽屉"}
    today = time.strftime("%Y-%m-%d")

    def tier_of(sid, tm):
        """优先级（木木 2026-11-02 定）：今天的会话原文 > 更早的会话原文 > 标准记忆文件。

        判据只用段自带的时间字段和 anchor 前缀（sid），不另编。标准记忆文件的时间
        一律是入库那天 00:00:00，不能拿日期当判据——所以先认 sid 再认日期。
        时间字段是 0000-00-00 或压根不成日期时，也按标准记忆处理（日期显示不明）。
        """
        tm = str(tm or "")
        if sid in MEM_SIDS:
            return 2
        if tm.startswith("0000") or len(tm) < 10:
            return 2
        return 0 if tm.startswith(today) else 1

    ranked = []
    for s in (res or {}).get("segs") or []:
        anchor = str(s.get("anchor") or "")
        if not anchor:
            continue
        meta = segs.get(anchor) or {}
        body = re.sub(r"\s+", " ", meta.get("text") or s.get("text") or "")
        # 问句、太短的段、只有一句话的段，不适合当「以前怎么做的」放进注入块
        if len(body) < 150 or ("？" in body[:120] and len(body) < 600):
            continue
        sid = meta.get("sid") or s.get("sid") or anchor.split("-")[0]
        tm = meta.get("time") or s.get("time") or ""
        e = doc_of.get(anchor) or {}
        sc = float(s.get("score") or 0) * kind_w.get(s.get("kind") or meta.get("kind"), 1.0)
        ranked.append((tier_of(sid, tm), -sc, anchor, s, body, sid, tm,
                       e.get("seq"), e.get("title")))
    ranked.sort(key=lambda x: (x[0], x[1]))

    # 同一份文件最多两段、同一件事最多两段：别四段全来自同一节
    picked, sid_n, doc_n = [], Counter(), Counter()
    for item in ranked:
        if len(picked) >= 4:
            break
        sid, seq = item[5], item[7]
        if sid_n[sid] >= 2 or doc_n[(sid, seq)] >= 2:
            continue
        sid_n[sid] += 1
        doc_n[(sid, seq)] += 1
        picked.append(item)

    # 三、今天这一层：接力的最新一节不进语料（每轮都变，过期交接会误导），索引里没有它；
    # 分段里一段「今天」都没取到时，把接力头一节原样带上——那是新壳最该先知道的事。
    if (relay and relay[0][0].startswith(today) and relay[0][0] not in printed_titles
            # 注意：picked 为空时 not any(...) 为真，于是会补上接力头一节——
            # 这正是想要的（今天一段都没取到，就让新壳先看到最要紧的那件事）。
            # 这处「空集合为真」是设计，不是 bug（2026-11-02 排查确认）。
            and not any(x[0] == 0 for x in picked)):
        head_len = min(600, max(0, budget - used - 450))
        head_body = re.sub(r"\s+", " ", relay[0][1])
        if head_len >= 150 and head_body:
            shown = head_body[:head_len] + ("…" if len(head_body) > head_len else "")
            print("\n【本壳刚做完的（接力最新一节，%s）】" % today)
            line = "· " + relay[0][0] + "：" + shown
            print(line)
            used += len(line) + 1
            printed_titles.add(relay[0][0])

    # 四、历史段按预算分配：3～4 段，每段 150～260 字，宁可多给一条短的，
    # 也不让一条吃掉全部预算。
    left = max(0, budget - used)
    n_seg = min(4, len(picked))
    while n_seg > 0 and left < 150 * n_seg:
        n_seg -= 1
    if n_seg == 0 and picked:
        # 预算真不够时，宁可给一条短的，也别一条都不给——
        # 注释里写的是「宁可多给一条短的」，代码原来是「一条都不给」。
        n_seg = 1
    if n_seg:
        per = min(260, left // n_seg)
        _hdr2 = "\n【以前在哪儿做过、怎么判的】"
        print(_hdr2)
        used += len(_hdr2)
        for tier, _negs, anchor, s, body, sid, tm, seq, title in picked[:n_seg]:
            src = ("标准记忆 " + MEM_SIDS[sid]) if sid in MEM_SIDS else ("壳 " + sid)
            tm_s = str(tm or "")
            if tier == 0:
                day = "今天 " + tm_s[11:16]
            elif tm_s.startswith("0000") or len(tm_s) < 10:
                day = "日期不明"
            else:
                day = tm_s[:16] or "日期不明"
            head = ("第%s事 %s" % (seq, title)) if seq else "未归事"
            prefix = "· [%s] %s · %s · %s：" % (anchor, src, day, head)
            # 头部（锚点/来源/日期/事）也要吃预算，不然四行下来隐性超两三百字
            room = max(20, min(per, budget - used - len(prefix) - 1))
            piece = clip_seg(body, room)
            line = prefix + piece
            print(line)
            used += len(line) + 1

    # 这条线上的事（只报标题，不占正文预算）
    strong = [e for e in events if _int(e.get("hits")) >= 2][:3]
    if strong:
        _hdr3 = "\n【这条线上的事，按相关度】"
        print(_hdr3)
        used += len(_hdr3)
        for e in strong:
            print("· %s 第%s事 %s [%s-%03d-%s-%03d]"
                  % (e.get("sid"), e.get("seq"), e.get("title"),
                     e.get("sid"), _int(e.get("a")), e.get("sid"), _int(e.get("b"))))

    # 兜底：自动取料（不给 --问）时问题是从流水里掐出来的半句，常年匹配不上，
    # 结果注入块里只剩一行标题——那等于没注入。这时候退回「接力最新的两节 +
    # 相关度最高的那几段」，宁可粗一点，也得让新壳开口有东西。
    if used < 300:
        print("\n【兜底：问题没匹配上，以下是最新交接原文】")
        for title, body in relay[:2]:
            if title in printed_titles:
                continue
            left = budget - used
            cap = min(420, left - len(title) - 2)
            if cap < 40:
                continue
            body = re.sub(r"\s+", " ", body)[:cap]
            if body:
                line = "· " + title + "：" + body
                print(line)
                used += len(line) + 1
                printed_titles.add(title)
        if not n_seg and res and res.get("segs"):
            print("\n【这条线上最相关的几段（兜底）】")
            for s in res["segs"][:2]:
                a2 = s.get("anchor")
                left = budget - used
                cap = min(260, left - len(str(a2)) - 4)
                if cap < 40:
                    break
                body = re.sub(r"\s+", " ", (segs.get(a2) or {}).get("text") or s.get("text") or "")[:cap]
                if not body:
                    continue
                line = "· [%s] %s" % (a2, body)
                print(line)
                used += len(line) + 1

    if used > budget:
        print("\n（超预算了：实取 %d 字，目标 %d。下次收窄问题。）" % (used, budget))


if __name__ == "__main__":
    main()
