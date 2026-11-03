# -*- coding: utf-8 -*-
"""别人的话进不进库：来源登记 + 群聊默认拒收。

边界（2026-11-04 木木定，写在最前面免得后来人以为是漏做）：
群聊里的话是**别人的**，不是本人的记忆。它会三头都出问题——
污染库（别人的事当成本人的事）、侵犯群友（他们没同意被记）、淹没检索
（一群人的闲聊量远大于两个人）。所以默认一律拒收，不给「配一下就收」的捷径。

要收，必须显式开三道：单独一个库、单独命名、不与私人记忆混查。
我们只给这个默认值和开关，不替使用者判断该不该收——那不是我们能决定的，
也不是我们的义务。仓库里附了一份给使用者的告知（见 NOTICE-群聊.md），
让接手的那个「大肥鱼」自己去问它的用户要不要做。

来源登记存在 lab\\来源.json，按会话 id（语料的文件名主干）记：
    {"aef2fa70": {"platform": "bili", "chat": "group", "session": "家人群"}}
没登记的会话一律当私聊——本库现有的语料全是本人的会话，这个默认是对的；
但**新接入别人的数据时必须先登记**，不登记就等于是默认它私聊，那是漏。
"""
import json
import os
import sys
from pathlib import Path

import config  # 路径统一从 config 取

HERE = Path(__file__).resolve().parent
# 原来写死 <工作区>/memory-lab，仓库一搬就错；SRC_MAP 环境变量仍然优先（config 里已处理）
MAP = config.SRC_MAP

GROUP = "group"
PRIVATE = "private"

_cache = {"mtime": None, "map": {}}


def load():
    """读登记表。文件不在、读坏、格式不对，一律当空表——绝不让检索因为它瘫。"""
    p = Path(MAP)
    try:
        mt = p.stat().st_mtime
    except OSError:
        return {}
    if _cache["mtime"] == mt:
        return _cache["map"]
    out = {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            for k, v in raw.items():
                if isinstance(v, dict):
                    out[str(k)] = v
    except Exception as e:
        print("来源登记表读不了（%s: %s），按全私聊处理" % (type(e).__name__, e),
              file=sys.stderr)
        out = {}
    _cache["mtime"], _cache["map"] = mt, out
    return out


def _sid_of(seg):
    """段上的会话 id：优先 sid，退到文件名主干。"""
    s = str(seg.get("sid") or "")
    if s:
        return s
    f = str(seg.get("fname") or seg.get("file") or "")
    return Path(f).stem if f else ""


def chat_of(sid_or_seg):
    """这条是群聊还是私聊。没登记 = 私聊（见文件头的界限说明）。"""
    if isinstance(sid_or_seg, dict):
        sid = _sid_of(sid_or_seg)
    else:
        sid = str(sid_or_seg or "")
    rec = load().get(sid) or {}
    c = str(rec.get("chat") or "").lower()
    return GROUP if c == GROUP else PRIVATE


def is_group(sid_or_seg):
    return chat_of(sid_or_seg) == GROUP


def allow_group():
    """开还是不开：环境变量 ALLOW_GROUP=1 才算开。
    注意——开了也建议只对单独的库开，别跟私人记忆混在一起查。"""
    return os.environ.get("ALLOW_GROUP", "0") == "1"


def filter_segs(segs, allow=None):
    """按来源过滤。返回 (留下的, 拿掉的)。默认拿掉群聊。

    过滤是纯机械的、只看登记表，不调模型、不看语义——因为这里判错代价太大：
    把别人的话当成本人的记忆递出去，比漏一条严重得多。
    """
    if allow is None:
        allow = allow_group()
    if allow or not segs:
        return segs, []
    kept, dropped = [], []
    for s in segs:
        (dropped if is_group(s) else kept).append(s)
    return kept, dropped


def gate(platform="", chat="", session="", sid=""):
    """入库前的闸：给接入方一句话，收还是不收，以及为什么。

    返回 (ok, reason, message)。ok=False 时调用方应当**不落库**，
    并把 message 原样交给使用者（它说的是人话，可以直接转）。
    """
    chat = str(chat or "").lower()
    if chat not in (GROUP, PRIVATE):
        chat = PRIVATE if not chat else chat
    if chat == GROUP and not allow_group():
        msg = ("这个会话是群聊（%s%s），里面不只是你一个人的话。默认不收："
               "别人的话混进来，检索的时候会当成你的记忆讲出去。"
               "真要收，得单开一个库、单独起名，别和私人记忆混着查。"
               % (session or sid or "未命名", ("·" + platform) if platform else ""))
        return (False, "group_refused", msg)
    return (True, "ok", "")


def sweep(segs):
    """给建库脚本用：把要收的留下，顺带报一下丢了多少、都是谁。"""
    kept, dropped = filter_segs(segs)
    if dropped:
        sids = sorted(set(_sid_of(s) for s in dropped))
        print("群聊拒收：拿掉 %d 段（会话 %s）。要收请见 sources.py 文件头。"
              % (len(dropped), "、".join(sids[:6]) + ("…" if len(sids) > 6 else "")),
              file=sys.stderr)
    return kept


def main():
    ap = __import__("argparse").ArgumentParser(description="看来源登记和默认闸")
    ap.add_argument("--sid", default="", help="查某个会话被当成什么")
    a = ap.parse_args()
    m = load()
    print("登记表：%s（%d 条）" % (MAP, len(m)))
    print("ALLOW_GROUP：%s" % ("开" if allow_group() else "关（群聊一律拒收）"))
    if a.sid:
        print("%s → %s" % (a.sid, chat_of(a.sid)))
    for k, v in sorted(m.items()):
        print("  %s  %s  %s" % (k, v.get("chat") or "private", v.get("session") or ""))


if __name__ == "__main__":
    main()
