# -*- coding: utf-8 -*-
"""sectindex —— 常设四册的「认路标记」读成结构，给定位层分层用。

为什么单独一个文件：`route.py` 现在只有「抽屉十一格」这一张地图，常设四册九十五节
还没进定位层（闭嘴那十四句大多在这儿）。分层要的是「先判册、再判节」，
前提是先把 `记忆\\节描述.md` 里那堆「短名 → 问什么话来这节」读成能塞进 prompt 的东西。

不碰检索、不写库、不改记忆文件：只读。
"""
import io
import os
import re
import sys

import config  # 路径统一从 config 取

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

WS = str(config.ROOT)  # 原来写死本机绝对路径，换台机器就全错
DESC = os.path.join(WS, "记忆", "节描述.md")
BOOKS = ["身体与设备", "工具与命令", "流程与做法", "记忆插件"]

RE_ITEM = re.compile(r"^-\s+(.+?)\s+→\s+(.+?)\s*$")
RE_BOOK = re.compile(r"^##\s+(.+?)\s*$")


def load():
    """返回 {册名: [(短名, 认路句), ...]}，册名顺序按 BOOKS。"""
    out = {b: [] for b in BOOKS}
    cur = None
    with io.open(DESC, encoding="utf-8") as f:
        for line in f:
            m = RE_BOOK.match(line)
            if m:
                name = m.group(1).strip()
                cur = name if name in BOOKS else None
                continue
            if cur is None:
                continue
            m = RE_ITEM.match(line.rstrip("\n"))
            if m:
                out[cur].append((m.group(1).strip(), m.group(2).strip()))
    return out


def counts():
    d = load()
    return {b: len(v) for b, v in d.items()}


if __name__ == "__main__":
    d = load()
    total = 0
    for b in BOOKS:
        total += len(d[b])
        print("%s：%d 节" % (b, len(d[b])))
        for name, desc in d[b][:3]:
            print("   %s → %s" % (name, desc))
    print("合计 %d 节" % total)
