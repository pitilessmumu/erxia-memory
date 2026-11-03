# -*- coding: utf-8 -*-
"""路径和配置：一个地方说了算，别在十个文件里各写一遍。

原来的作者是把它装在自己机器上的，路径直接写死（`C:\\Users\\<某人>\\...`）。
发出来之前改掉了：所有路径都从这里取，仓库放哪儿都能跑。

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


def _find_root():
    env = os.environ.get("ERXIA_HOME")
    if env:
        return Path(env).resolve()
    p = HERE
    for _ in range(6):          # 往上最多走六层，够用了，别无限跑
        if (p / "origin").is_dir() or (p / "lab").is_dir():
            return p
        if p.parent == p:
            break
        p = p.parent
    return HERE.parent           # 什么都没有就当成「代码在根下面一层」


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
           "MODELS", "SECRETS", "SRC_MAP"]


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
    for k, v in [("ROOT", ROOT), ("CORPUS", CORPUS), ("TOPICS", TOPICS),
                 ("LAB", LAB), ("DB", DB), ("MODELS", MODELS), ("SRC_MAP", SRC_MAP)]:
        print("%-8s %s  %s" % (k, v, "有" if Path(v).exists() else "缺"))
