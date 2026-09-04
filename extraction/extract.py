# -*- coding: utf-8 -*-
"""【第三阶段 · 分层信息抽取（编排入口）】

流水线：规则层(确定性任务) → 深度学习层(复杂模式 NER) → LLM层(整篇文档补充抽取)

- 规则层：确定性抽取，结果始终保留；
- 深度学习层：可用时输出高置信实体的三元组（快、省）；
- LLM 层：整篇正文一次调用交给 LLM 抽取，补足长尾关系；输出经严格格式校验，
  连续格式错误会抛 RuntimeError 由 main() 报错暂停（避免在坏格式上反复消耗费用）；
- 各层结果合并后按 (subject, relation, object, object_type) 去重，保留置信度更高者
  （置信度相同时优先保留规则层结果）。

输入：data/processed/preprocessed_documents.json（第二阶段预处理产物）
输出：data/processed/triples_extracted.json

增量抽取说明：
    - 每次“提取现有数据”默认只抽取“新增的 / 内容哈希发生过变化的”文档，避免
      对已抽取文档重复调用 LLM（documents.db.extract_state 表作为文档级指针）；
    - 首次启用时会把 triples.db 中已有来源文档自动回填为“已抽取”；
    - 想强制全量重抽（例如改了提示词/规则后）加 --force。

输出三元组字段说明（规则层/深度学习层/LLM层统一结构）：
    subject              —— 主语实体名（如“哮喘”）
    subject_type         —— 主语类型规范英文 ID（如 Disease）
    subject_type_label   —— 主语类型中文标签（如“疾病”）
    relation             —— 关系规范英文 ID（如 HAS_SYMPTOM）
    relation_label       —— 关系中文标签（如“常见症状”）
    object               —— 宾语实体名
    object_type          —— 宾语类型规范英文 ID
    object_type_label    —— 宾语类型中文标签
    source_document_id   —— 来源文档编号
    source_text          —— 原文证据句
    confidence           —— 置信度（0~1）
    layer                —— 产出层：rule / deep_learning / llm
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from extraction import deep_learning_layer, llm_layer, rule_layer  # noqa: E402

INPUT = ROOT / "data" / "processed" / "preprocessed_documents.json"
OUTPUT = ROOT / "data" / "processed" / "triples_extracted.json"
DOCUMENTS_DB = ROOT / "data" / "documents.db"
TRIPLES_DB = ROOT / "data" / "triples.db"

_CREATE_STATE_TABLE = """
CREATE TABLE IF NOT EXISTS extract_state (
    document_id    TEXT PRIMARY KEY,
    content_hash   TEXT,
    layer          TEXT,
    extracted_at   TEXT,
    triple_count   INTEGER NOT NULL DEFAULT 0
)
"""

# LLM 调用是网络 IO：默认并发 8，可用环境变量 EXTRACT_WORKERS 覆盖（--workers 参数优先）。
DEFAULT_WORKERS = int(os.environ.get("EXTRACT_WORKERS", "8"))


def _triple_key(triple: dict[str, Any]) -> tuple[Any, ...]:
    """三元组去重键：仅比较事实本身，不比较证据/置信度/来源层。"""
    return (triple["subject"], triple["relation"], triple["object"], triple["object_type"])


# 关系"具体度"：用于跨层语义去重。DL 与 LLM 常对同一对实体给出不同关系
# （如 DL 判 HAS_SYMPTOM、LLM 判 MAY_CAUSE），此时应保留更具体的信息，
# 泛化的 RELATED_TO 优先级最低，避免同一条事实被两层重复表达。
_RELATION_SPECIFICITY: dict[str, int] = {
    "TREATED_BY": 6,
    "DIAGNOSED_BY": 6,
    "HAS_SYMPTOM": 5,
    "HAS_SIDE_EFFECT": 5,
    "HAS_RISK_FACTOR": 4,
    "HIGH_RISK_FOR": 4,
    "MAY_CAUSE": 4,
    "BELONGS_TO": 3,
    "RELATED_TO": 1,
}


def dedupe(triples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """合并跨层重复三元组，分两遍处理：

    1) 完全相同的 (subject, relation, object, object_type)：保留置信度更高者
       （置信度相同时优先规则层结果）；
    2) 跨层语义去重：同一 (subject, object) 对若被多个不同关系表达（DL/LLM 各判一种），
       按 _RELATION_SPECIFICITY 保留最具体的关系；具体度并列时按置信度、其次按层
       （deep_learning > llm）取舍，保证同一对实体只保留一条事实。
    """
    # —— 第一遍：精确去重 ——
    merged: dict[tuple[Any, ...], dict[str, Any]] = {}
    for t in triples:
        key = _triple_key(t)
        existing = merged.get(key)
        if existing is None:
            merged[key] = t
            continue
        new_conf = t.get("confidence", 0.0)
        old_conf = existing.get("confidence", 0.0)
        if new_conf > old_conf or (new_conf == old_conf and t.get("layer") == "rule"):
            merged[key] = t

    # —— 第二遍：按 (subject, object) 跨关系去重 ——
    by_pair: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for t in merged.values():
        by_pair.setdefault((t["subject"], t["object"]), []).append(t)

    result: list[dict[str, Any]] = []
    for group in by_pair.values():
        if len(group) == 1:
            result.extend(group)
            continue
        relations = {t["relation"] for t in group}
        if len(relations) == 1:
            # 同一对实体、同一关系（可能 object_type 不同）：保留置信度最高者
            result.append(max(group, key=lambda t: (t.get("confidence", 0.0), 0 if t.get("layer") == "deep_learning" else 1)))
            continue
        # 不同关系：保留"最具体"的一条，避免重复表达同一事实
        winner = max(
            group,
            key=lambda t: (
                _RELATION_SPECIFICITY.get(t["relation"], 0),
                t.get("confidence", 0.0),
                0 if t.get("layer") == "deep_learning" else 1,
            ),
        )
        result.append(winner)

    return result


def _now_iso() -> str:
    """ISO 时间戳，写入 extract_state.extracted_at。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _open_state() -> sqlite3.Connection:
    """连接 documents.db 并确保 extract_state 表存在（幂等）。"""
    conn = sqlite3.connect(DOCUMENTS_DB)
    conn.execute(_CREATE_STATE_TABLE)
    conn.commit()
    return conn


def _seed_state_from_triples(conn: sqlite3.Connection) -> int:
    """首次启用时回填：把 triples.db 已有来源文档标记为“已抽取”。

    避免启用增量后第一次运行又把历史语料全部重新抽取一遍；
    仅插入不存在的行，不覆盖已有记录。返回本次回填条数。
    """
    try:
        doc_hash = dict(
            conn.execute("SELECT document_id, content_hash FROM documents").fetchall()
        )
    except sqlite3.OperationalError:
        doc_hash = {}
    doc_ids: list[str] = []
    if TRIPLES_DB.is_file():
        try:
            triple_conn = sqlite3.connect(TRIPLES_DB)
            doc_ids = [
                r[0] for r in triple_conn.execute(
                    "SELECT DISTINCT source_document_id FROM triples "
                    "WHERE source_document_id IS NOT NULL"
                )
            ]
            triple_conn.close()
        except sqlite3.Error:
            doc_ids = []
    seeded = 0
    for doc_id in doc_ids:
        content_hash = doc_hash.get(doc_id)
        if not content_hash:
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO extract_state "
            "(document_id, content_hash, layer, extracted_at, triple_count) "
            "VALUES (?, ?, 'seed', ?, 0)",
            (doc_id, content_hash, _now_iso()),
        )
        seeded += cur.rowcount
    if seeded:
        conn.commit()
    return seeded


def _mark_done(conn: sqlite3.Connection, record: dict[str, Any], triple_count: int) -> None:
    """一篇文档成功抽取后记录/更新其内容哈希，供下次增量跳过。"""
    conn.execute(
        "INSERT INTO extract_state "
        "(document_id, content_hash, layer, extracted_at, triple_count) "
        "VALUES (?, ?, 'llm', ?, ?) "
        "ON CONFLICT(document_id) DO UPDATE SET "
        "content_hash = excluded.content_hash, layer = excluded.layer, "
        "extracted_at = excluded.extracted_at, triple_count = excluded.triple_count",
        (
            record.get("document_id"),
            record.get("content_hash"),
            _now_iso(),
            int(triple_count),
        ),
    )
    conn.commit()


def extract_one(record: dict[str, Any]) -> list[dict[str, Any]]:
    """对单条文档执行抽取，返回去重后的三元组。

    分层策略：
        1. 深度学习层：可用时输出高置信实体的三元组（快、省）；
        2. LLM 层：整篇正文直接交给 LLM 抽取，补足 DL 覆盖不到的长尾关系
           （如 HAS_SIDE_EFFECT / MAY_CAUSE / RELATED_TO 等）。LLM 输出经严格格式校验，
           格式连续错误会抛 RuntimeError，由 main() 报错暂停（避免在坏格式上反复消耗费用）。

    规则层默认关闭（rule_layer.AVAILABLE=False，模块级开关）：其"标题当疾病主语"与
    模板吞字噪声较大，且与 DL 结果大量重复；需要时把该开关改回 True 即可恢复。
    """
    triples: list[dict[str, Any]] = []
    if rule_layer.AVAILABLE:
        triples.extend(rule_layer.extract(record))

    if deep_learning_layer.AVAILABLE:
        triples.extend(deep_learning_layer.predict_record(record)["triples"])

    # 整篇交给 LLM 抽取（不再做"句子路由聚焦复核"：LLM 看到全文上下文抽取更准，
    # 每篇仅 1 次调用，逻辑更简单）
    triples.extend(llm_layer.extract(record))

    return dedupe(triples)


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="分层信息抽取")
    ap.add_argument("--limit", type=int, default=0,
                    help="增量模式下最多检查前 N 篇文档（按预处理顺序；0=全部新增/变化文档）")
    ap.add_argument("--start", type=int, default=0,
                    help="起始偏移：从第 start 篇开始抽取（0 基；配合 --limit 可跑任意区间）")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                    help=f"并发线程数（LLM 调用是网络 IO，>1 可大幅提速；默认 {DEFAULT_WORKERS}，"
                         "可用环境变量 EXTRACT_WORKERS 调整）")
    ap.add_argument("--force", action="store_true",
                    help="强制全量重抽全部文档（忽略 extract_state 增量指针；会增加 API 消耗）")
    args = ap.parse_args()

    if not INPUT.is_file():
        print(f"ERROR: 预处理产物不存在，请先运行 preprocess/preprocess.py: {INPUT}", file=sys.stderr)
        return 1

    records = json.loads(INPUT.read_text(encoding="utf-8"))
    if not isinstance(records, list) or not records:
        print("ERROR: 预处理产物为空", file=sys.stderr)
        return 2

    start = max(0, args.start)
    end = len(records) if args.limit <= 0 else min(len(records), start + args.limit)
    records = records[start:end]

    state_conn = _open_state()
    skipped = 0
    if args.force:
        candidates = records
    else:
        seeded = _seed_state_from_triples(state_conn)
        if seeded:
            print(f"首次启用增量：已按 triples.db 现有来源回填 {seeded} 篇为“已抽取”。")
        done_hashes = {
            row[0]: row[1]
            for row in state_conn.execute(
                "SELECT document_id, content_hash FROM extract_state"
            ).fetchall()
        }
        candidates = []
        for rec in records:
            rec_id = rec.get("document_id")
            rec_hash = rec.get("content_hash")
            # 只有“内容哈希相同”才跳过；哈希缺失视为待抽取，避免误跳过
            if rec_id and rec_hash and done_hashes.get(rec_id) == rec_hash:
                skipped += 1
            else:
                candidates.append(rec)

    workers = max(1, args.workers)
    print(f"分层抽取 {len(candidates)} 篇（跳过 {skipped} 篇，共 {len(records)} 篇；并发 {workers}）："
          f"规则层 可用；深度学习层 可用={deep_learning_layer.AVAILABLE}；"
          f"LLM层 可用={llm_layer.is_available()}")

    if not candidates:
        print("没有新增或内容变化的文档，跳过抽取。如需全量重抽请加 --force。")
        # 无增量时把输出文件重置为空数组：保证 triples_extracted.json 始终代表
        # “本轮批次的抽取结果”，避免旧批次残留被后续 db/store_triples.py 重复导入。
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text("[]\n", encoding="utf-8")
        state_conn.close()
        return 0

    all_triples: list[dict[str, Any]] = []

    def _run(rec: dict[str, Any]) -> tuple[Any, list[dict[str, Any]]]:
        """对单篇执行抽取并返回 (record, triples)，供并发/串行共用。"""
        return rec.get("document_id"), extract_one(rec)

    try:
        if workers > 1:
            # 并发模式：LLM 调用是网络 IO（约 30s/批），多线程并行可显著缩短总耗时。
            # 线程安全：LLM 无共享可变状态；DL 模型推理在 no_grad 下只读，可多线程调用。
            # 用显式 shutdown(cancel_futures=True)：一旦出错可取消队列中未启动的任务，
            # 实现真正"报错暂停"，而不是等待全部跑完（避免继续消耗 LLM 费用）。
            executor = ThreadPoolExecutor(max_workers=workers)
            futures = {executor.submit(_run, rec): rec for rec in candidates}
            done = 0
            try:
                for future in as_completed(futures):
                    docid, triples = future.result()
                    rec = futures[future]
                    # 只有 LLM 主抽取层可用时才记录进度：避免未配密钥的“半抽取”被跳过
                    if llm_layer.is_available():
                        _mark_done(state_conn, rec, len(triples))
                    all_triples.extend(triples)
                    done += 1
                    # 逐篇打印进度（并发下完成顺序不定，以 done 计数为准）
                    print(f"  [{done}/{len(candidates)}] {docid} 产出 {len(triples)} 条（累计 {len(all_triples)}）", flush=True)
            finally:
                executor.shutdown(wait=False, cancel_futures=True)
        else:
            for i, rec in enumerate(candidates, 1):
                docid, triples = _run(rec)
                # 只有 LLM 主抽取层可用时才记录进度（理由同上）
                if llm_layer.is_available():
                    _mark_done(state_conn, rec, len(triples))
                all_triples.extend(triples)
                # 逐篇打印进度（flush 确保实时可见，便于长任务监控/断点判断）
                print(f"  [{i}/{len(candidates)}] {docid} 产出 {len(triples)} 条（累计 {len(all_triples)}）", flush=True)
    except RuntimeError as exc:
        # LLM 输出连续格式错误 -> 报错暂停：立即停止后续调用，避免继续消耗 token/费用
        print(f"\nERROR: {exc}", file=sys.stderr)
        print("已暂停抽取（LLM 输出格式连续错误）。请检查提示词/模型后重试。", file=sys.stderr)
        # 把已抽到的部分结果落盘，便于断点排查（不中断也不覆盖）
        if all_triples:
            OUTPUT.parent.mkdir(parents=True, exist_ok=True)
            OUTPUT.write_text(json.dumps(dedupe(all_triples), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"已保存已完成部分的 {len(all_triples)} 条三元组 -> {OUTPUT}", file=sys.stderr)
        # 本轮尚未完成入库，撤销已标记的文档状态；否则下次会跳过它们而丢失部分结果。
        if llm_layer.is_available():
            state_conn.executemany(
                "DELETE FROM extract_state WHERE document_id = ?",
                [(rec.get("document_id"),) for rec in candidates if rec.get("document_id")],
            )
            state_conn.commit()
            print("已撤销本轮未入库文档的增量状态，下次将自动重试。", file=sys.stderr)
        state_conn.close()
        return 3

    # 跨文档全局去重一次（不同文档可能抽出相同事实）
    all_triples = dedupe(all_triples)

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(all_triples, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    rule_count = sum(1 for t in all_triples if t["layer"] == "rule")
    dl_count = sum(1 for t in all_triples if t["layer"] == "deep_learning")
    llm_count = sum(1 for t in all_triples if t["layer"] == "llm")

    print(f"抽取完成，共得到 {len(all_triples)} 条三元组")
    print(f"  - 规则层产出：{rule_count}")
    print(f"  - 深度学习层产出：{dl_count}")
    print(f"  - LLM层产出：{llm_count}")
    print(f"  - LLM API 调用次数（含重试）：{llm_layer.call_count()}")
    print(f"  - 输出文件：{OUTPUT}")
    state_conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
