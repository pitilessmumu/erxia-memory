# -*- coding: utf-8 -*-
"""路径和配置：一个地方说了算，别在十个文件里各写一遍。

这是什么东西：全项目唯一一处「路径从哪儿来」的定义。它不 import 本项目任何模块，
依赖方向是单向的——所有人都来拿，它谁也不找。

2026-11-04 改硬了一处：**找不到根就报错，不再猜**。原来最后一句是「什么都找不到就
当代码在根下面一层」，猜错了也不喊；十六个模块拿着错路径往下跑，建库报成功、检索全空，
错题本 LOCAL-075 就是这么来的。宁可 import 就崩，也不要带着错路径活着。

根目录怎么找：优先看环境变量 `ERXIA_HOME`；没设就从本文件往上走，找到同时能看见
`origin` 或 `lab` 的那一层当根。这样你放在 D 盘、放在别的目录名下面都行。

    erxia-memory/          <- 根（ROOT）
      memory/              <- 代码（HERE），本文件就在这儿
      origin/              <- 语料：corpus/*.md + topics.md
      lab/                 <- 索引库、模型、答案簿，都是生成物，不进仓库
        index/mem.db
        models/bge-small-zh/
        models/bge-reranker/

环境变量（都有默认值，不设也能跑）：
    ERXIA_HOME     根目录
    DB_INDEX       索引库位置，重建时指到临时库
    ERXIA_MODELS   模型目录
    VEC_MODEL      语义模型目录
    RERANK_MODEL   重排模型目录
    ZHIPU_KEY      定位层用的云端 key（不设就读 lab/secrets/zhipu.key，也没有就跳过定位）
    SRC_MAP        来源登记表（群聊拒收那张，默认 lab/来源.json）
    ALLOW_GROUP    设为 1 才收群聊内容
"""
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent


class ConfigError(RuntimeError):
    """路径或配置不对。抛这个而不是 sys.exit：让调用方决定是退出还是换一条路。"""


def _find_root():
    """定根目录。找不到就抛 ConfigError——这里不猜（见文件头）。"""
    env = os.environ.get("ERXIA_HOME")
    if env:
        p = Path(env).resolve()
        if not p.is_dir():
            raise ConfigError("ERXIA_HOME 指的不是一个目录：%s" % p)
        return p
    p = HERE
    for _ in range(6):          # 往上最多走六层，够用了，别无限跑
        if (p / "origin").is_dir() or (p / "lab").is_dir():
            return p
        if p.parent == p:
            break
        p = p.parent
    raise ConfigError(
        "找不到根目录：从 %s 往上六层都没看见 origin/ 或 lab/。\n"
        "  要么设 ERXIA_HOME 指到仓库根，要么把代码放在含 origin/ 的那层下面。\n"
        "  这里故意不猜一个默认值——猜错了不会报错，只会让十六个模块一起走错路。" % HERE)


ROOT = _find_root()
ORIGIN = ROOT / "origin"
CORPUS = ORIGIN / "corpus"
TOPICS = ORIGIN / "topics.md"
LAB = ROOT / "lab"
DB = Path(os.environ.get("DB_INDEX", str(LAB / "index" / "mem.db")))
MODELS = Path(os.environ.get("ERXIA_MODELS", str(LAB / "models")))
SECRETS = LAB / "secrets"
SRC_MAP = Path(os.environ.get("SRC_MAP", str(LAB / "来源.json")))

__all__ = ["HERE", "ROOT", "ORIGIN", "CORPUS", "TOPICS", "LAB", "DB",
           "MODELS", "SECRETS", "SRC_MAP", "ConfigError", "check"]


def check(need_db=False):
    """开工前自检：该在的都在吗？缺东西当场抛 ConfigError，不返回半成品。

    这是什么东西：把「路径错了」这件事提前到入口处炸，而不是等某个模块拿着空路径
    去 connect、让 sqlite 静默建一个空库（LOCAL-075 / LOCAL-080）。
    调用时机：建库、注入、检索的命令行入口，第一行就调。
    need_db=True 时连索引库一起验（读库的场合用；建库时传 False）。
    """
    bad = []
    if not ORIGIN.is_dir():
        bad.append("缺语料根目录：%s" % ORIGIN)
    if not CORPUS.is_dir():
        bad.append("缺语料目录：%s" % CORPUS)
    elif not list(CORPUS.glob("*.md")):
        bad.append("语料目录里一个 .md 都没有：%s" % CORPUS)
    if not TOPICS.is_file():
        bad.append("缺事表：%s" % TOPICS)
    if bad:
        raise ConfigError("配置自检没过：\n  " + "\n  ".join(bad))
    try:
        LAB.mkdir(parents=True, exist_ok=True)
        DB.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise ConfigError("建不出 lab 目录（%s）：%s" % (LAB, e))
    if need_db:
        if not DB.is_file():
            raise ConfigError("索引库还没建：%s\n  先跑 python db-build.py" % DB)
        if DB.stat().st_size == 0:
            raise ConfigError("索引库是 0 字节的空文件：%s\n"
                              "  这是 sqlite 建库失败留下的壳，删掉再跑 python db-build.py" % DB)
    return []


def key(name="zhipu"):
    """云端 key：先看环境变量，再看 lab/secrets/<name>.key。都没有就返回空串。"""
    env = os.environ.get(name.upper() + "_KEY") or os.environ.get("ERXIA_KEY")
    if env:
        return env.strip()
    p = SECRETS / (name + ".key")
    try:
        return p.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


if __name__ == "__main__":
    print("ROOT     %s  %s" % (ROOT, "有" if ROOT.is_dir() else "缺"))
    for k, v in [("CORPUS", CORPUS), ("TOPICS", TOPICS), ("LAB", LAB),
                 ("DB", DB), ("MODELS", MODELS), ("SRC_MAP", SRC_MAP)]:
        print("%-8s %s  %s" % (k, v, "有" if Path(v).exists() else "缺"))
    try:
        check()
        print("自检：过")
    except ConfigError as e:
        print("自检：没过\n%s" % e)
