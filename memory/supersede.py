# -*- coding: utf-8 -*-
"""哪条还算数：人工维护的「推翻登记表」+ 把被推翻的旧段从检索结果里滤掉。

病症：库里同时躺着「稳定层七十七段」和「稳定层七十八段」，问「稳定层现在多少段」，
旧的那段照样会被检索捡起来，还可能排在新段前面递出去。

治法不是删段（历史不能删，--钻 还要能翻旧账），而是在检索出口加一层过滤：
登记表说「这条旧结论已经被哪条新结论推翻」，滤的是**返回结果**，不是库；
鉴权闸（阈值、分段逻辑）一个字不动，只让被推翻的那几条不再当答案。

登记表 `supersede.json` 每条记：
  old       旧锚点（当年回答那段）
  new       新锚点（现在算数的那段）
  why       为什么推翻（人话，写给下一个我看）
  date      登记日期 YYYY-MM-DD
  old_sid   旧段所在会话（兜底指纹之一）
  old_head  旧段正文前 12 个字（兜底指纹之一）

锚点会位移：抽取脚本过滤段之后，同一会话里后面的段号减一，锚点跟着变。
所以按锚点查不到（或者锚点还在、正文对不上）时，拿 old_sid + old_head 到库里认人，
认出来的实际锚点就是这轮要滤的锚点。认不出来的，stderr 报一句、跳过，不静默。

用法（检索里已经接好，这里是给手工核对和 run\\supersede.py 用的）：
  import supersede
  kept = supersede.filter_segs(segs, report)   # 段列表进，滤完的段列表出
  supersede.load()                             # 读登记表
  supersede.append(old, new, why)              # 追加一条（先校验两个锚点在库里）
"""
# DECISION: 推翻登记按 old_head 指纹核验，锚点对上但正文对不上就放行并报「疑似误伤」 | when: 2026-11-02 | cause: 外审
# 病根：锚点对上不等于还是那一段——重建重切之后，同一个锚点可能已经指向新内容，
# 照锚点滤会把**新**段误杀，新结论永远进不来，而且不报错（看不见的错最坏）。
# 现在 filter_segs 滤之前核一遍正文指纹（old_head，取旧段前 12 字，归一化后比）：
#   · 指纹对得上 → 照滤；
#   · 锚点在、指纹对不上 → 放行，并在 stderr 报「疑似锚点位移误伤」；
#   · 锚点没了 → 拿 old_sid + old_head 到库里认人，认出就滤认出的那个锚点，认不出就跳过并报锚点。
# 取舍写死：**宁可漏滤，不可错滤**——漏滤看得见（旧答案被递上来，人能去改登记表），
# 错滤看不见（新结论永远进不来，还一句报错都没有）。
# 同一次核对把记账也补上（filter_segs 的 report：n_in / n_out / n_dropped / suspect /
# stale / fp_hit）——不补的话「滤了几条、指纹认回几条」全在暗处，自检只能看它有没有抛异常，
# 而「把一切都滤掉了」这种最坏的 bug 恰恰不抛异常。
# DECISION: 出口记账必须有人对账——进来>0 出去0 当场报红，只记不查等于没记 | when: 2026-11-02 | cause: 木木点的
# 原来 n_dropped / n_in 只写在返回值里，没有任何东西会因为「滤光」报警，账记了没人核。
# 现在 filter_segs 在出口判一次：n_in > 0 且 n_out == 0 → 填 report["alarm"] 并 stderr 报红。
# 为什么这个判据够用：调用方是检索出口，它自己不看这两个数；而「滤光」和「本来没答案」
# 在返回值上完全同形，只有这里知道两者的区别。
# 边界两条：① 进来就是 0 段不报（那是检索没捞到，与登记表无关，报了就是狼来了）；
# ② 同一进程只喊一次（连问十句都滤光时，喊十遍等于没喊；alarm 字段每次照填，机器查得到）。
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import config  # 路径统一从 config 取

HERE = Path(__file__).resolve().parent
REGISTRY = HERE / "supersede.json"
# 原来按 __file__ 往上两级拼 memory-lab，仓库一搬就错；现在从 config 取
DB = config.DB
HEAD_CHARS = 12

# 进程内缓存：登记表/索引库没变就不重查。键里带 mtime + size，改完登记表下一问就生效。
_CACHE = {"key": None, "map": {}, "stale": {}}
# 「全滤」只喊一次：同一个进程里连着问十句都全滤，喊十遍会把 stderr 刷成噪音，
# 人反而看不见第一行。alarm 字段每次照样填，机器那边查得到。
_ALARMED = {"done": False}


def _warn(msg):
    """报一句到 stderr：登记表出问题时不许静默。自己也不许把检索搞挂。"""
    try:
        print(msg, file=sys.stderr)
        sys.stderr.flush()
    except Exception:
        pass


def _norm(text):
    """指纹比对口径：去掉所有空白、转小写。"""
    return "".join((text or "").split()).lower()


def _head(text):
    """正文前 12 个字（原样存，比对时再归一化）。"""
    return (text or "")[:HEAD_CHARS]


def _stat_key(p):
    try:
        st = p.stat()
        return (str(p), st.st_mtime_ns, st.st_size)
    except Exception:
        return (str(p), 0, 0)


def load(path=None):
    """读登记表，返回条目列表。

    文件不在 -> 空列表（还没登记过，是正常状态）；文件坏了 -> 报一句、返回空列表，
    绝不让检索因为一张表而瘫掉。
    """
    p = Path(path) if path else REGISTRY
    try:
        raw = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except Exception as e:
        _warn("推翻登记表读不了（%s: %s），这轮按没有推翻处理" % (type(e).__name__, e))
        return []
    try:
        doc = json.loads(raw)
    except Exception as e:
        _warn("推翻登记表不是合法 JSON（%s），这轮按没有推翻处理" % e)
        return []
    items = doc.get("entries") if isinstance(doc, dict) else doc
    if not isinstance(items, list):
        _warn("推翻登记表的 entries 不是列表，这轮按没有推翻处理")
        return []
    out = []
    for i, it in enumerate(items, 1):
        if not isinstance(it, dict):
            _warn("推翻登记表第 %d 条不是对象，跳过" % i)
            continue
        old = str(it.get("old") or "").strip()
        if not old:
            _warn("推翻登记表第 %d 条没写 old 锚点，跳过" % i)
            continue
        out.append({"old": old,
                    "new": str(it.get("new") or "").strip(),
                    "why": str(it.get("why") or "").strip(),
                    "date": str(it.get("date") or "").strip(),
                    "old_sid": str(it.get("old_sid") or "").strip(),
                    "old_head": str(it.get("old_head") or "").strip()})
    return out


def _db(path_db=None):
    p = Path(path_db) if path_db else DB
    if not p.is_file():
        raise FileNotFoundError("索引库不在：%s" % p)
    return sqlite3.connect(str(p))


def _resolve_now(entries, db):
    """把登记表落到库里真实的锚点。

    返回 (要滤的锚点->条目, 失效条目列表, 兜底也认不出的条目列表)。
    """
    mapping, stale, lost = {}, [], []
    for e in entries:
        old = e["old"]
        head = _norm(e["old_head"])
        row = db.execute("SELECT anchor, sid, text FROM segments WHERE anchor = ?",
                         (old,)).fetchone()
        # DECISION: 缺 old_head 的登记要告警 | when: 2026-11-02 | cause: 做歪的
        # 这条登记是手写的，JSON 可以人直接编辑。old_head 空掉之后锚点一对上就直接滤，
        # 绕过了全部指纹核验——违反文件头自己定的「宁可漏滤不可错滤」（智谱挑出来的）。
        if not head:
            _warn("登记 %s 缺 old_head（只按锚点滤，不可靠）" % old)
        # DECISION: 指纹口径统一成「包含」 | when: 2026-11-03 | cause: 做歪的
        # 原来 resolve 用 startswith（头必须从段首开始）、filter 用 in（正文中间含也算），
        # 两种口径不一致：in 会让「旧段正文中间碰巧含这 12 字」的其他段也通过核验被滤。
        # 统一成 in——宁可漏滤不可错滤，而 startswith 更严、会让该滤的漏掉。
        if row and (not head or head in _norm(row[2])):
            mapping[old] = e
            continue
        # 锚点失效，或锚点还在但正文对不上（段号挪位后撞到别的段）：按指纹认人。
        stale.append(e)
        sid = e.get("old_sid") or (row[1] if row else "")
        hit = None
        if sid and head:
            for anchor, text in db.execute("SELECT anchor, text FROM segments WHERE sid = ?",
                                           (sid,)):
                if _norm(text).startswith(head):
                    hit = anchor
                    break
        if hit:
            mapping[hit] = dict(e, resolved=hit, drifted=True)
        else:
            lost.append(e)
    return mapping, stale, lost


def _resolve(path=None, db_path=None):
    """带缓存的 resolve：返回 (锚点->条目, 失效统计)。库或表变了自动重算。"""
    p = Path(path) if path else REGISTRY
    d = Path(db_path) if db_path else DB
    key = (_stat_key(p), _stat_key(d))
    if key == _CACHE["key"]:
        return _CACHE["map"], _CACHE["stale"]
    entries = load(path)
    info = {"entries": len(entries), "stale": [], "lost": [], "fp_hit": 0}
    mapping = {}
    if entries:
        db = _db(db_path)
        try:
            mapping, stale, lost = _resolve_now(entries, db)
        finally:
            db.close()
        info["stale"], info["lost"] = stale, lost
        info["fp_hit"] = sum(1 for e in mapping.values() if e.get("drifted"))
        if stale:
            msg = "登记表里有 %d 条锚点已失效，用指纹兜底" % len(stale)
            if info["fp_hit"]:
                msg += "（认回 %d 条）" % info["fp_hit"]
            if lost:
                msg += "；还有 %d 条指纹也没认出来，先跳过：%s" % (
                    len(lost), "、".join(x["old"] for x in lost))
            _warn(msg)
    _CACHE.update({"key": key, "map": mapping, "stale": info})
    return mapping, info


def filter_segs(segs, report=None, path=None):
    """把一批段里的旧段滤掉，返回过滤后的段列表（幂等、不抛异常）。

    report 传入一个 dict 时会填上：n_in / n_out / n_dropped / n_old（涉及几条登记）
    和 dropped（逐段的 anchor / old / new / why）。登记表坏了就当没登记过，原样返回。
    """
    out = list(segs or [])
    info = {"dropped": [], "n_dropped": 0, "n_old": 0, "n_in": len(out), "n_out": len(out),
            "n_entries": 0, "stale": 0, "lost": 0, "fp_hit": 0}
    try:
        mapping, st = _resolve(path)
        info["n_entries"] = len(mapping)
        info["stale"] = len(st.get("stale") or [])
        info["lost"] = len(st.get("lost") or [])
        info["fp_hit"] = st.get("fp_hit", 0)
        if mapping:
            kept = []
            for s in out:
                a = (s or {}).get("anchor")
                e = mapping.get(a)
                if e is None:
                    kept.append(s)
                    continue
                # 锚点对上不等于还是那一段：重建重切之后，同一个锚点可能已经指向新内容。
                # 有指纹就核一遍；对不上就放行，只记一笔「疑似误伤」——宁可漏滤，不可错滤
                # （错滤会让新结论永远进不来，而且不报错）。
                hd = _norm(e.get("old_head") or "")
                if hd and hd not in _norm((s or {}).get("text") or ""):
                    kept.append(s)
                    info.setdefault("suspect", []).append(
                        {"anchor": a, "old_head": e.get("old_head")})
                    continue
                info["dropped"].append({"anchor": a, "old": e.get("old"),
                                        "new": e.get("new"), "why": e.get("why"),
                                        "resolved": e.get("resolved", a)})
            out = kept
    except Exception as e:
        _warn("推翻登记表用不上（%s: %s），这轮按没推翻处理" % (type(e).__name__, e))
    if info.get("suspect"):
        _warn("有 %d 条登记锚点对上了但正文对不上，已放行（疑似锚点位移误伤）：%s"
              % (len(info["suspect"]), ", ".join(x["anchor"] for x in info["suspect"][:4])))
    info["n_out"] = len(out)
    info["n_dropped"] = len(info["dropped"])
    info["n_old"] = len({d["old"] for d in info["dropped"]})
    # 全滤报警（2026-11-02 木木点的）：记账不难，难的是有人对账。
    # 光是「滤了几条」写在返回值里，等于没人看——真出了「滤光」那种事，调用方只会安静地
    # 拿到空结果，看上去跟「这题本来就没答案」一模一样。所以这条要当场报红：
    # 进来 > 0 段、出去 0 段，只有两种可能——登记表登记宽了（把不该滤的也登记了），
    # 或者库坏了（锚点全指到登记过的段）。两种都不许安静。
    # 进来就是 0 段的不报：那是检索本来就没捞到东西，跟推翻登记无关（这一条是防误报）。
    if info["n_in"] > 0 and info["n_out"] == 0:
        info["alarm"] = "全滤 %d>0->0" % info["n_in"]
        if not _ALARMED["done"]:
            _ALARMED["done"] = True
            _warn("红：推翻登记把检索结果全滤光了（进来 %d 段、出去 0 段，涉及 %d 条登记）。"
                  "这跟「没找到答案」长得一模一样，但它是滤出来的。去核两件事："
                  "登记表是不是登记宽了；锚点是不是全指到登记过的段了。"
                  % (info["n_in"], info["n_old"]))
    if report is not None:
        try:
            report.update(info)
        except Exception:
            pass
    return out


def _read_doc(path=None):
    """读整份 JSON（追加/删除用）。文件坏了直接抛——这时候宁可报错也不覆盖人的表。"""
    p = Path(path) if path else REGISTRY
    if not p.is_file():
        return {"version": 1, "entries": []}
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        raise RuntimeError("推翻登记表读不了，先手工修：%s（%s）" % (p, e))
    if not isinstance(doc, dict):
        doc = {"version": 1, "entries": doc}
    if not isinstance(doc.get("entries"), list):
        raise RuntimeError("推翻登记表的 entries 不是列表，先手工修：%s" % p)
    return doc


def _write_doc(doc, path=None):
    """整份写盘：先写 .tmp 再替换，写一半断了也不会留下半张表。"""
    p = Path(path) if path else REGISTRY
    doc["version"] = doc.get("version") or 1
    doc.setdefault("note", "被推翻的旧结论：检索时按 old 锚点把旧段滤掉。"
                           "old_head 是旧段正文前 12 字，紧跟在 old_sid 后当锚点位移的兜底指纹。")
    text = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(p)


def append(old, new, why, date=None, path=None):
    """追一条：先校验两个锚点在库里都存在，不存在就抛错、不写盘。"""
    old = (old or "").strip()
    new = (new or "").strip()
    if not old or not new:
        raise ValueError("旧锚点、新锚点都得给")
    if old == new:
        raise ValueError("旧锚点和新锚点不能是同一个：%s" % old)
    db = _db()
    try:
        ro = db.execute("SELECT sid, text FROM segments WHERE anchor = ?", (old,)).fetchone()
        rn = db.execute("SELECT anchor FROM segments WHERE anchor = ?", (new,)).fetchone()
    finally:
        db.close()
    if ro is None:
        raise LookupError("旧锚点在库里找不到：%s（不写盘）" % old)
    if rn is None:
        raise LookupError("新锚点在库里找不到：%s（不写盘）" % new)
    doc = _read_doc(path)
    entries = doc["entries"]
    for e in entries:
        if isinstance(e, dict) and str(e.get("old") or "").strip() == old:
            raise ValueError("这条旧锚点已经登记过了：%s（要改先 --drop 再重登）" % old)
    entry = {"old": old, "new": new, "why": (why or "").strip(),
             "date": (date or datetime.now().strftime("%Y-%m-%d")).strip(),
             "old_sid": ro[0], "old_head": _head(ro[1])}
    entries.append(entry)
    _write_doc(doc, path)
    return entry


def remove(old, path=None):
    """撤掉一条（登记错了、或测试条目）：按 old 锚点删，返回删掉的那条。"""
    old = (old or "").strip()
    doc = _read_doc(path)
    entries = doc["entries"]
    kept = [e for e in entries if not (isinstance(e, dict) and str(e.get("old") or "").strip() == old)]
    if len(kept) == len(entries):
        raise LookupError("登记表里没有这条旧锚点：%s" % old)
    doc["entries"] = kept
    _write_doc(doc, path)
    return [e for e in entries if e not in kept]


def status(path=None):
    """逐条报状态（给 --list）：锚点还在不在、指纹认到哪。每次现查，不吃缓存。"""
    entries = load(path)
    out = []
    if not entries:
        return out
    db = _db()
    try:
        mapping, stale, lost = _resolve_now(entries, db)
    finally:
        db.close()
    for e in entries:
        hit = None
        for anchor, item in mapping.items():
            if item.get("old") == e["old"]:
                hit = anchor
                break
        if hit == e["old"]:
            state = "锚点在库"
        elif hit:
            state = "锚点已失效，指纹认出 → %s" % hit
        else:
            state = "锚点已失效，指纹也没认出（这轮跳过）"
        out.append(dict(e, state=state, resolved=hit,
                        drifted=any(x.get("old") == e["old"] for x in stale),
                        lost=any(x.get("old") == e["old"] for x in lost)))
    return out
