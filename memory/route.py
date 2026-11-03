# -*- coding: utf-8 -*-
"""route.py —— 定位层：先决定「问的是哪一格 / 哪段时间」，再取内容。

设计（2026-11-02，智谱 + WB 定论 + 木木的双轨想法）：
  记亿不是 1623 段平铺的文本，是一批有边界的单位。
  一段记忆的位置 =（实体，时间）这个坐标点：
      实体轴 → 抽屉格（一件东西一行，格里有现行状态、变更史、别名行）
      时间轴 → 片（按时间切，一片一段时间）
  问题就是个带约束的坐标查询：
      只带实体 → 只走实体轴
      只带时间 → 只走时间轴
      两者都带 → 双轨，取交集（「昨天做这个球都出了哪些问题」= 球 × 昨天）
      交集为空 → 降级成单轴（实体轴优先），不硬凑

三条硬约束：
  一、影子块/别名只当「门牌」，不当答案；定位只产出单位，不产出来源。
  二、智谱不可用时**跳过终判**（不是停手，也不是自己编）：走本地粗筛的结果。
  三、空档要显式：那段时间没记录就说没记录，不许让时间轴看起来连续。
"""
import config  # 路径统一从 config 取
import io
import json
import os
import re
import sqlite3
import sys
import urllib.error
import urllib.request

# 原来写死本机绝对路径，换台机器 / 换个目录名就全错；现在从 config 取
WS = str(config.ROOT)
DB = str(config.DB)
KEY_PATH = str(config.SECRETS / "zhipu.key")
API = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
MODEL = "glm-5.3-flash"

_cells = None
_cells_mtime = -1


# ---------------------------------------------------------------- 格

def cells():
    """十一格：锚点、标题、别名行。别名行是定位的门牌，缺了准度就掉。"""
    global _cells, _cells_mtime
    # 库文件动过就重读：重建索引后格子内容/别名可能变了，永久缓存等于「安静地用过期数据」。
    try:
        mt = os.path.getmtime(DB)
    except OSError:
        mt = 0
    if _cells is not None and mt == _cells_mtime:
        return _cells
    out = []
    try:
        con = sqlite3.connect(DB)
        rows = list(con.execute(
            "SELECT anchor, text FROM segments WHERE sid='ac000005' ORDER BY anchor"))
        con.close()
    except sqlite3.Error as e:
        # 库坏了不该炸断整条检索链：返回空表，上层自然走「定位不到 → 闭嘴」。
        print("读抽屉格失败（%s），本次不做抽屉定位" % e, file=sys.stderr)
        _cells, _cells_mtime = [], mt
        return _cells
    for anchor, text in rows:
        head = alias = ""
        for ln in (text or "").splitlines():
            s = ln.strip()
            if not head and s.startswith("#"):
                head = s.lstrip("# ").strip()
            if s.startswith("找我的说法"):
                alias = s.split("：", 1)[-1].strip()
        if head:
            out.append({"anchor": anchor, "head": head, "alias": alias})
    con.close()
    if not out:
        print("抽屉格查到 0 条（sid='ac000005' 可能失效）——实体轴会永远不命中", file=sys.stderr)
    _cells, _cells_mtime = out, mt
    return out


# ---------------------------------------------------------------- 时间轴

TODAY = ("今天", 0)
# 顺序必须按名字长度降序：「前天」是「大前天」的子串，短的先匹配就把长的吃掉
# （2026-11-02 代码审查抓出来的）。
# DECISION: 时间词按名字长度降序匹配 | when: 2026-11-02 | cause: 做歪的
NAMES = [("大前天", 3), ("今天", 0), ("昨天", 1), ("前天", 2)]
RE_NDAY = re.compile(r"(\d+)\s*天前")
RE_CN_DATE = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")
RE_LAST = re.compile(r"最近|上次|上回|前几天|这几天|本周|这个星期|上周|上星期")


def parse_time(q):
    """返回 (label, dates) —— dates 是具体日期集合（'YYYY-MM-DD'）；说不出日期就是 None。"""
    import datetime
    now = datetime.date.today()
    for name, back in NAMES:
        if name in q:
            return name, [(now - datetime.timedelta(days=back)).isoformat()]
    m = RE_NDAY.search(q)
    if m:
        back = int(m.group(1))
        if 0 <= back <= 400:
            return "%d天前" % back, [(now - datetime.timedelta(days=back)).isoformat()]
    m = RE_CN_DATE.search(q)
    if m:
        mo, dy = int(m.group(1)), int(m.group(2))
        try:
            d = datetime.date(now.year, mo, dy)
            if d > now:  # 说「3月5日」而现在是 11 月，指的是今年三月已经过去了才对
                d = datetime.date(now.year - 1, mo, dy)
            return m.group(0), [d.isoformat()]
        except ValueError:
            pass
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", q)
    if m:
        try:
            # 必须真校一遍："2026-99-99" 原样返回的话，库里查不到，会被伪装成
            # 「那段时间没有记录」——假空档（2026-11-02 代码审查抓出来的）。
            d = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return m.group(0), [d.isoformat()]
        except ValueError:
            pass
    if "上周" in q or "上星期" in q:
        # 上周一到周日
        start = now - datetime.timedelta(days=now.weekday() + 7)
        return "上周", [(start + datetime.timedelta(days=i)).isoformat() for i in range(7)]
    if RE_LAST.search(q):
        return "最近", None  # 说不出具体哪天，只标记「有时间意图」
    return None, None


# 第三级的触发词：问的是「那段时间我们干了什么」。没有这些词的时间句不归它管
# （「今天天气怎么样」也带时间词，但问的是天，不是我们）。
# 泛问：问的是「那段时间干了些什么」，不是「某一件事怎么解决的」。
# 这类问题没有单一段能回答——它要的是汇总，所以不给答案，给记录（2026-11-02 16:1x）。
RE_VAGUE = re.compile(r"聊了什么|聊了啥|做了什么|做了啥|干了什么|干了啥|弄了什么|弄了啥|"
                      r"发生了哪些|发生什么|都干啥了|忙什么")

RE_ACT = re.compile(r"聊|做|弄|干|说|提|发生|忙|搞|修|改|定|讲|写|试|调|跑|弄过|在做|干了")

HOURS = [("凌晨", 0, 6), ("早上", 5, 10), ("早晨", 5, 10), ("上午", 6, 12),
         ("中午", 11, 14), ("下午", 12, 18), ("傍晚", 17, 20), ("晚上", 18, 24),
         ("夜里", 20, 24), ("深夜", 22, 24)]


def parse_hours(q):
    """问题里说了「上午/下午/晚上」就返回小时范围（左闭右开），没说返回 None。"""
    for name, a, b in HOURS:
        if name in q:
            return a, b
    return None


def segs_in_dates(dates):
    """那几天里有哪些聊天段（按来源分组，方便判断「这一天有没有记录」）。"""
    if not dates:
        return {}
    con = sqlite3.connect(DB)
    out = {}
    for d in dates:
        n = con.execute(
            "SELECT COUNT(*) FROM segments WHERE sid NOT LIKE 'ac%' AND time LIKE ?",
            (d + "%",)).fetchone()[0]
        out[d] = n
    con.close()
    return out


# ---------------------------------------------------------------- 大模型终判

def _key():
    # 先问 config（ZHIPU_KEY 环境变量，或 lab/secrets/zhipu.key），没有再读老的 KEY_PATH
    k = config.key("zhipu")
    if k:
        return k
    try:
        k = io.open(KEY_PATH, encoding="utf-8").read().strip()
    except Exception:
        return None
    return k or None


def chat(prompt, timeout=60):
    k = _key()
    if not k:
        return None
    body = json.dumps({"model": MODEL, "messages": [{"role": "user", "content": prompt}],
                       "temperature": 0.2}).encode("utf-8")
    req = urllib.request.Request(API, data=body, headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + k})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8"))
        return d["choices"][0]["message"]["content"]
    except Exception as e:
        print("定位终判失败（%s），按无终判处理" % type(e).__name__, file=sys.stderr)
        return None


def _grid_text():
    lines = []
    for i, c in enumerate(cells(), 1):
        lines.append("第 %d 格：%s" % (i, c["head"]))
        if c["alias"]:
            lines.append("    （他可能这么问：%s）" % c["alias"][:160])
    return "\n".join(lines)


def judge(qs, timeout=180):
    """一次判一批。返回 [ [格序号...] 或 [] ]，顺序跟 qs 对齐；None 表示终判不可用。"""
    if not qs:
        return []
    body = """下面是一个人的记忆库里「工具抽屉」的十一个格子，每格记一个工具、一个做法或一件事。
格子后面括号里，是他自己以前可能怎么问这件事。

%s

第 12 格：以上都不是（问的事跟上面十一格无关）

下面是 %d 句话。请逐句判断：这句话问的是第几格？最多选两个（确实跨两格时才选两个）。

规矩：
一、只看意思对不对得上，不要被用词不同骗了。他可能完全不说格子的名字。
二、十一个格讲的事跟这句话明显不是一回事，就选第 12 格，不要硬凑。
三、第 12 格是正经答案，不是兜底。

输出格式，每句一行，严格照这个，不要解释：
1. 7
2. 12
3. 3,7

以下：
%s
""" % (_grid_text(), len(qs), "\n".join("%d. %s" % (i + 1, q) for i, q in enumerate(qs)))

    txt = chat(body, timeout=timeout)
    if txt is None:
        return None
    picks = {}
    for m in re.finditer(r"(?m)^\s*(\d+)[.、:：]\s*([\d,，\s]+)$", txt):
        idx = int(m.group(1))
        nums = [int(x) for x in re.findall(r"\d+", m.group(2))]
        picks[idx] = [n for n in nums if 1 <= n <= 12]
    out = []
    for i in range(len(qs)):
        nums = picks.get(i + 1, [])
        out.append([n for n in nums if n != 12])
    if len(picks) < len(qs):
        # 模型输出格式歪了、漏了几行：那些句会被当成「都不是」，跟「模型认真判了 12」不可区分。
        print("终判只解析出 %d/%d 行，其余按「都不是」处理" % (len(picks), len(qs)),
              file=sys.stderr)
    return out


# ---------------------------------------------------------------- 单条定位

def route(q, use_llm=True):
    """返回定位结果：
       cells  —— 命中的格锚点列表（空 = 都不是）
       time   —— 时间轴的标签
       dates  —— 具体日期（None = 有时间意图但说不出哪天）
       hits   —— {日期: 那天的聊天段数}，用来判断空档
       track  —— '实体' / '时间' / '双轨' / '都不是'
       skipped—— 是否跳过了终判（智谱不可用）
    """
    label, dates = parse_time(q)
    hits = segs_in_dates(dates) if dates else {}
    picks = judge([q]) if use_llm else None
    skipped = picks is None
    idxs = (picks or [[]])[0]
    cs = cells()
    got = [cs[i - 1]["anchor"] for i in idxs if 0 < i <= len(cs)]

    if got and dates:
        track = "双轨"
    elif got:
        track = "实体"
    elif (dates or label) and RE_ACT.search(q or ""):
        # DECISION: 时间轨要有动作词才认 | when: 2026-11-02 | cause: 做歪的
        # 「今天天气怎么样」命中「今天」，但没有「聊/做/改」这类词，piece() 本来就不会查；
        # 可 route() 仍标「时间」轨，上层若据此报「今天没有记录」，就是对着天气问句撒谎。
        track = "时间"
    else:
        track = "都不是"
    return {"q": q, "cells": got, "time": label, "dates": dates, "hits": hits,
            "track": track, "skipped": skipped}


# ---------------------------------------------------------------- 第三级：杂谈

# DECISION: 按半小时切窗口（不是整小时、不是按天） | when: 2026-11-02 | cause: 现场逼的
def _windows(segs, minutes=30):
    """把一片聊天按半小时分组。窗口是这台机器上最自然的"小单位"：
    一次对话通常连续十几分钟就换话题，半小时窗口里的事往往是一件事。"""
    import datetime
    win = {}
    bad = 0
    for s in segs:
        tm = (s.get("time") or "")[:19].replace("T", " ")
        try:
            dt = datetime.datetime.fromisoformat(tm)
        except (ValueError, TypeError):
            # 格式不认识就跳过并计数，不许静默丢段——静默丢段会表现成「那几段神秘消失」。
            bad += 1
            continue
        key = "%s:%02d%s" % (dt.date().isoformat(), dt.hour, "00" if dt.minute < 30 else "30")
        win.setdefault(key, []).append(s)
    if bad:
        print("半小时分组跳过 %d 段（时间格式不认识）" % bad, file=sys.stderr)
    return win


def piece(q, k=5):
    """第三级降级：先按时间圈范围，再对到「哪半小时」，最后在那一小段里找。

    为什么要分两级（2026-11-02 实测）：我们的聊天只有两天，11-01 就 864 段，
    按"天"圈范围等于没圈。按半小时切出来大约四五十个窗口，问题先对窗口、再对段，
    池子就落到几十段——正是 bge-small 够用的量级（208 段 top10 72%）。

    说不出具体日期（「最近」「上次」）就不在这里硬找，返回 None 交给原来的三层：
    没有范围地搜一千三百段，等于把上午的老问题再犯一遍。

    空档显式化（木木 2026-11-02 定）：圈出来是空的，要明说「那段时间没有记录」，
    不许装作查到了、也不许沉默——「真的没聊」和「聊了但我没找到」是两回事。
    """
    label, dates = parse_time(q)
    if not dates:
        return None
    if not RE_ACT.search(q):
        return None  # 带了时间词，但问的不是「那段时间我们做了什么」
    con = sqlite3.connect(DB)
    segs = []
    for d in dates:
        for anchor, text, tm in con.execute(
                "SELECT anchor, text, time FROM segments "
                "WHERE sid NOT LIKE 'ac%' AND time LIKE ? ORDER BY time", (d + "%",)):
            segs.append({"anchor": anchor, "text": text or "", "time": tm})
    con.close()
    hr = parse_hours(q)
    if hr and segs:
        # 说了「下午」就只留十二点到十八点。说了时段却没有记录，也算空档，别拿别的时段凑。
        # 但时间格式不合法的段不能当「0 点」处理——那会把「有记录」清成「没记录」，
        # 正是假空档的一种（2026-11-02 代码审查抓出来的）。
        bad_time = 0
        keep_hr = []
        for s in segs:
            tm = (s.get("time") or "")
            try:
                h = int(tm[11:13])
            except (ValueError, IndexError):
                bad_time += 1
                continue
            if hr[0] <= h < hr[1]:
                keep_hr.append(s)
        if bad_time:
            print("跳过 %d 段（时间格式不合形）" % bad_time, file=sys.stderr)
        if not keep_hr and bad_time:
            # DECISION: 全是坏时间戳时不算空档 | when: 2026-11-02 | cause: 做歪的
            # 段被丢弃、集合变空，若照样报「那个时段没有记录」，就是自己定义的假空档——
            # 「确实没记录」和「有记录但时间全坏」是两件事，后者该降级不是装空。
            return {"q": q, "route": "降级", "segs": [], "cand": bad_time, "time": label,
                    "dates": dates, "degraded": True,
                    "note": "%s 有 %d 段记录，但时间戳全不合形，我没法按时段筛——不敢说「没有记录」"
                            % (label, bad_time)}
        segs = keep_hr
        if not segs:
            return {"q": q, "route": "时间空档", "segs": [], "cand": 0, "time": label,
                    "dates": dates,
                    "note": "%s那个时段没有记录（%d–%d 点之间一条都没有）"
                            % (label, hr[0], hr[1])}
    if not segs:
        return {"q": q, "route": "时间空档", "segs": [], "cand": 0, "time": label,
                "dates": dates, "note": "%s没有记录（那段时间一条聊天都没有）" % "、".join(dates)}

    # DECISION: 泛问走列记录，不做检索 | when: 2026-11-02 | cause: 现场逼的
    if RE_VAGUE.search(q):
        # 泛问走「列记录」：不做检索、不打分，直接把那段时间的段按时间交上去，
        # 由上层（我）去读、去汇总。这不是偷懒——单段检索对这种问题天然无效，
        # 硬要它给一段，就是拿一段当整段时间的答案，等于编。
        # DECISION: 泛问均匀采样，不只给最早几段 | when: 2026-11-02 | cause: 现场逼的
        # 原来是 segs[:k]——864 段的一天，给的是凌晨那几段，「昨天聊了什么」全看运气。
        step = max(1, len(segs) // max(1, k))
        head = segs[::step][:k]
        return {"q": q, "route": "时间列表", "segs": head, "cand": len(segs),
                "time": label, "dates": dates,
                "note": "那段时间共 %d 段，先给最早的 %d 段（要看别的时段说清楚）"
                        % (len(segs), len(head))}

    win = _windows(segs)
    keys = sorted(win)
    reps = [" ".join(x["text"][:120] for x in win[kk][:2]) for kk in keys]

    try:
        import numpy as np
        import vec as vecmod
        qv = vecmod.encode([q], is_query=True)[0]
        rv = vecmod.encode(reps)
        sims = rv @ qv
        order = list(np.argsort(-sims))[:3]
        picked = [keys[i] for i in order]
    except Exception as e:
        # 语义选窗失败就闭嘴，不许拿「最早三个窗口」顶上——那三个跟问题可能毫无关系，
        # 而三级闸门只有 0.20，不相关的段很容易混过去（WB 代码审查 BUG-7）。
        # DECISION: 选窗失败即闭嘴，不拿最早三个窗口顶上 | when: 2026-11-02 | cause: 现场逼的
        print("窗口语义不可用（%s），本问不做时间定位" % type(e).__name__, file=sys.stderr)
        return {"q": q, "route": "降级", "segs": [], "cand": len(segs), "time": label,
                "dates": dates, "degraded": True,
                "note": "%s 圈到 %d 段，但选窗用的语义模型不可用——不敢瞎挑，先闭嘴"
                        % (label, len(segs))}

    pool = [s for kk in picked for s in win[kk]]
    try:
        import numpy as np
        import vec as vecmod
        qv = vecmod.encode([q], is_query=True)[0]
        tv = vecmod.encode([s["text"][:400] for s in pool])
        sims = tv @ qv
        for i, s in enumerate(pool):
            s["score"] = round(float(sims[i]), 4)
        pool.sort(key=lambda s: -s["score"])
    except Exception as e:
        print("片内语义跳过（%s）" % type(e).__name__, file=sys.stderr)

    # 闸门：语义挑出来的段，还得被重排认过才许当答案。
    # 不设这道，「今天天气怎么样」会因为「今天」两个字被圈进来当场出声。
    try:
        import rerank as rk
        top = pool[:max(k, 6)]
        scores = rk.rerank(q, [s["text"] for s in top])
        for s, sc in zip(top, scores):
            s["rerank"] = round(float(sc), 4)
        # 0.10 等于没闸（一级是 0.30）。三级候选质量本来就更差，闸门该更严不是更松。
        # 取 0.20 是实测出来的：0.30 会把该答的全拦住，0.20 只拦最差那句。
        # DECISION: 三级闸门取 0.20（不是 0.10 也不是 0.30） | when: 2026-11-02 | cause: 现场逼的
        keep = [s for s in top if s.get("rerank", 0) >= 0.20]
        keep.sort(key=lambda s: -s["rerank"])
    except Exception as e:
        # 重排器不可用 ≠ 没有答案。早先这两件事报同一句话，等于把「工具坏了」
        # 伪装成「闸门在正常闭嘴」（2026-11-02 代码审查抓出来的）。
        print("时间定位重排不可用（%s）" % type(e).__name__, file=sys.stderr)
        return {"q": q, "route": "降级", "segs": [], "cand": len(segs), "time": label,
                "dates": dates, "windows": picked, "degraded": True,
                "note": "%s 圈到 %d 段，但重排器不可用，我没有能力判断哪段答得上——先闭嘴"
                        % (label, len(segs))}
    if not keep:
        return {"q": q, "route": "安静", "segs": [], "cand": len(segs), "time": label,
                "dates": dates, "windows": picked,
                "note": "圈到 %s 有 %d 段，但没有一段真正答得上这个问题" % (label, len(segs))}

    return {"q": q, "route": "时间定位", "segs": keep[:k], "cand": len(segs),
            "time": label, "dates": dates, "windows": picked,
            "note": "圈到 %s，%d 段，落到 %d 个半小时窗口" % (label, len(segs), len(picked))}


# ------------------------------------------------- 第四级：常设四册（先判册，再判节）

# DECISION: 段身份 uid = 标题短哈希，anchor 只当展示序号（两套并存） | when: 2026-11-02 | cause: 现场逼的
# DECISION: 常设按「册 → 节」两级判，不把 88 节一次塞进 prompt | when: 2026-11-02 | cause: 现场逼的
_SECTS = None


def sects():
    """四册的认路标记：{册名: [(节短名, 问什么话来这节), ...]}。"""
    global _SECTS
    if _SECTS is None:
        try:
            import sectindex
            _SECTS = sectindex.load()
        except Exception as e:
            print("节描述读不到（%s: %s），这一级不做" % (type(e).__name__, e), file=sys.stderr)
            _SECTS = {}
    return _SECTS


def _book_grid():
    """册级 prompt 的正文：每册一行，后头跟着它里面的节短名（只当线索，不写认路句）。"""
    lines = []
    for i, (book, items) in enumerate(sects().items(), 1):
        names = "、".join(n for n, _ in items)
        lines.append("第 %d 册：%s（%d 节：%s）" % (i, book, len(items), names))
    return "\n".join(lines)


def _pick_nums(txt, hi):
    """从模型回话里抠出节/册序号。

    实测它有时候只回一个「4」——不照格式来。先按格式那一行抠，抠不到就取全文第一个数：
    宁可少认（判不出就当没命中），也不许把无关的数字硬当序号。
    """
    if not txt:
        return []
    m = re.search(r"(?m)^\s*\d+[.、:：]\s*([\d,，\s]+)$", txt)
    raw = m.group(1) if m else txt
    out = []
    for x in re.findall(r"\d+", raw):
        n = int(x)
        if 1 <= n <= hi and n not in out:
            out.append(n)
    return out[:2] if not m else out


def judge_book(q, timeout=90):
    """问的是哪一册。返回册名（或 None = 都不是 / 判不了）。"""
    books = list(sects().keys())
    if not books:
        return None
    body = """下面是一个人的记忆库，分成四册，每册管一类事。每册后面括号里，是它里面各节的短名。

%s

第 %d 册：以上都不是

下面一句话，问的是第几册？最多选两册（确实跨两册时才选两个）。

规矩：
一、只看意思对不对得上，不要被用词不同骗了。他可能完全不提册名，说的事却在某一册里。
二、四册讲的事跟这句话明显不是一回事，就选第 %d 册；那是正经答案，不是兜底。
三、这是找路，不是答题，别猜他要什么答案。

输出只有一行，照这个格式，不要解释：
1. 3

以下是这句话：
1. %s
""" % (_book_grid(), len(books) + 1, len(books) + 1, q)

    txt = chat(body, timeout=timeout)
    if txt is None:
        return None
    nums = _pick_nums(txt, len(books))
    if not nums:
        return None
    # 只认第一个：两册命中时先取最像的那册，避免一次拉两册回来塞满上下文
    return books[nums[0] - 1]


def judge_sect(q, book, timeout=90):
    """在这一册里，问的是哪几节。返回节短名列表（空 = 都不是）。"""
    items = sects().get(book) or []
    if not items:
        return []
    lines = []
    for i, (name, desc) in enumerate(items, 1):
        lines.append("第 %d 节：%s —— %s" % (i, name, desc))
    body = """下面是「%s」这一册里的 %d 节。每节后面那句，是「问什么话来这节」——它写的是人嘴里
真会说的话，不是内容摘要。

%s

第 %d 节：以上都不是

下面一句话，问的是第几节？最多选两节（这件事确实跨两节时才选两个）。

规矩：
一、拿这句话去比「问什么话来这节」那一句，哪一节的话跟他问的最像就是它。
二、不要因为两句话有几个词一样就选；要问的**是同一件事**。
三、这一册里没有对得上的，就选第 %d 节；那是正经答案，不是兜底。

输出只有一行，照这个格式，不要解释：
1. 7

以下是这句话：
1. %s
""" % (book, len(items), "\n".join(lines), len(items) + 1, len(items) + 1, q)

    txt = chat(body, timeout=timeout)
    if txt is None:
        return []
    nums = _pick_nums(txt, len(items))
    out = []
    for n in nums:
        name = items[n - 1][0]
        if name not in out:
            out.append(name)
    return out[:2]


def _norm(s):
    """只留中文和字母数字，用来比节名。

    为什么要有这一步：节描述里写的是短名（「犯错和任务清单两条硬规矩」），
    册子里那条标题带冒号（「犯错和任务清单：两条硬规矩」）——不归一化就永远对不上，
    而且错得很安静（锚点为空，上层当「这节没命中」）。2026-11-02 实测踩到。
    """
    return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]+", "", s or "")


def _sect_anchor(book, name):
    """节在库里的锚点。

    实情（2026-11-02 查证，别再猜）：四册合成一个 sid='ac000001'，段标题只有节名，
    **不带册名**（`## 悬浮球（二夏的嘴和耳朵）`），fname 全是 ac000001.md。
    所以这里只按节名归一化匹配；册名只用来缩小「这一节属于哪一册」，不参与匹配。
    """
    try:
        con = sqlite3.connect(DB)
        rows = list(con.execute(
            "SELECT anchor, text FROM segments WHERE sid='ac000001' ORDER BY anchor"))
        con.close()
    except sqlite3.Error as e:
        print("读常设段失败（%s）" % e, file=sys.stderr)
        return None
    want = _norm(name)
    if not want:
        return None
    for anchor, text in rows:
        head = (text or "").split("\n", 1)[0]
        if want in _norm(head):
            return anchor
    return None


def route_sect(q):
    """分层定位入口：先判册，再判节。

    返回 {"book", "sects": [短名...], "anchors": [...], "skipped"}；
    册判不出来（模型不可用 / 都不是）时 book=None，调用方自己退回去。
    """
    book = judge_book(q)
    if not book:
        return {"book": None, "sects": [], "anchors": [], "skipped": True}
    names = judge_sect(q, book)
    anchors = []
    for n in names:
        a = _sect_anchor(book, n)
        if a:
            anchors.append(a)
    return {"book": book, "sects": names, "anchors": anchors, "skipped": False}

