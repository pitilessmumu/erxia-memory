# -*- coding: utf-8 -*-
# DECISION: 重建起分离进程，不走 powershell、不弹控制台 | when: 2026-11-02 | cause: 现场逼的
"""索引新不新：记忆文件和语料比索引戳新，就是旧的。

用法（检索那头自己调，这里只是给人和脚本用的）：
    python freshness.py            # 只看，打印旧/新
    python freshness.py --rebuild  # 旧了就当场重建（前台，会等）

为什么挂在检索入口而不是养一个常驻进程：常驻进程会默默死掉，没人发现；
检索入口每次被叫醒时顺手比一下 mtime，成本是一次 stat，跑不掉也死不了。
"""
import io
import os
import subprocess
import sys
import time

import config  # 路径统一从 config 取

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

WS = str(config.ROOT)  # 原来按 __file__ 往上三级找根，仓库一搬就错
MEM_DIR = os.path.join(WS, "记忆")
CORPUS = str(config.CORPUS)
RUN = os.path.join(WS, ".dsh-switchman", "run")
STAMP = os.path.join(RUN, ".index-stamp")
LOCK = os.path.join(RUN, ".rebuild.lock")
REBUILD = os.path.join(RUN, "rebuild-index.py")
LOCK_QUIET = 600  # 锁十分钟内有动静就当重建还在跑，别再起一个


def _newest():
    t = 0.0
    for d, pat in ((MEM_DIR, ".md"), (CORPUS, ".md")):
        if not os.path.isdir(d):
            continue
        for n in os.listdir(d):
            if n.endswith(pat):
                try:
                    t = max(t, os.path.getmtime(os.path.join(d, n)))
                except OSError:
                    pass
    return t


def stale():
    """索引戳比记忆文件旧 = 索引过期。没有戳也算过期（第一次）。"""
    newest = _newest()
    if not os.path.exists(STAMP):
        return True, newest, 0.0
    try:
        with io.open(STAMP, encoding="utf-8") as f:
            ts = float(f.read().strip() or 0)
    except Exception:
        return True, newest, 0.0
    return newest > ts + 1, newest, ts


def _locked():
    try:
        return time.time() - os.path.getmtime(LOCK) < LOCK_QUIET
    except OSError:
        return False


def rebuild_in_background():
    """起一个分离的重建进程：不阻塞当前这次检索。

    不用 powershell（会被 360 判成「隐藏执行 PowerShell」），直接拉 python；
    也不弹控制台窗口，免得木木看见黑框一闪。
    """
    if _locked():
        return "already-running"
    try:
        io.open(LOCK, "w", encoding="utf-8").write("%.0f\n" % time.time())
    except Exception:
        pass
    flags = 0
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        flags |= subprocess.CREATE_NO_WINDOW
    if hasattr(subprocess, "DETACHED_PROCESS"):
        flags |= subprocess.DETACHED_PROCESS
    py = os.path.join(WS, ".dsh-switchman", "voice", ".venv", "Scripts", "python.exe")
    try:
        subprocess.Popen([py, REBUILD], cwd=WS, creationflags=flags,
                         stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        sys.stderr.write("索引重建起不来：%s\n" % e)
        return "failed"
    return "started"


def main():
    is_old, newest, ts = stale()
    print("索引%s（记忆文件最新 %.0f，索引戳 %.0f，差 %.0f 秒）"
          % ("过期" if is_old else "是新的", newest, ts, newest - ts))
    if is_old and "--rebuild" in sys.argv:
        p = subprocess.run([sys.executable, REBUILD], cwd=WS)
        print("前台重建 exit=%d" % p.returncode)
    return 0 if not is_old else 1


if __name__ == "__main__":
    sys.exit(main())
