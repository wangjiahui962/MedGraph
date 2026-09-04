# -*- coding: utf-8 -*-
"""下载 CMeEE（中文医学实体抽取）公开语料，用于深度学习层 NER 训练。

数据来源：https://github.com/Z-MU-Z/cmeee
（CBLUE 评测基准中的中文医学实体识别子任务，CC 协议公开数据）

下载文件（train/dev/test 均为 JSON，含 text 与 entities 字段）：
    CMeEE_train.json / CMeEE_dev.json / CMeEE_test.json
输出目录：data/external/cmeee/

运行：python data/download_cmeee.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from urllib.request import urlretrieve

# 通过 jsDelivr CDN 拉取（raw.githubusercontent 在国内直连大文件容易卡住）
BASE_URL = "https://cdn.jsdelivr.net/gh/Z-MU-Z/cmeee@master/data/CBLUEDatasets/CMeEE/"
FILES = ["CMeEE_train.json", "CMeEE_dev.json", "CMeEE_test.json"]

OUT_DIR = Path(__file__).resolve().parent / "external" / "cmeee"


def _download(url: str, dest: Path, retries: int = 3) -> None:
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            urlretrieve(url, str(dest))
            size = os.path.getsize(str(dest))
            print(f"OK  {dest.name}  {size / 1024 / 1024:.2f} MB", flush=True)
            return
        except Exception as exc:  # 网络失败重试后明确报错
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"下载失败 {url}: {last_error}")


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        dest = OUT_DIR / name
        if dest.is_file() and dest.stat().st_size > 0:
            print(f"SKIP {name}（已存在）")
            continue
        try:
            _download(BASE_URL + name, dest)
        except RuntimeError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

    # 校验：抽样打印一条样例，确认字段结构
    sample = OUT_DIR / FILES[0]
    if sample.is_file():
        items = json.loads(sample.read_text(encoding="utf-8"))
        print(f"样例条数：{len(items)}")
        if items:
            first = items[0]
            print("字段：", list(first.keys()))
            print("text 截断：", (first.get("text") or "")[:60])
            print("entities 样例：", (first.get("entities") or [])[:2])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())