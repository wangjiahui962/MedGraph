# -*- coding: utf-8 -*-
"""抽取 doc_{start:06d}..doc_{end:06d} 三元组：断点续跑 + 失败重试一轮。

- 每篇完成即写 checkpoint；中断后重跑自动跳过已完成、并重试上次失败文档；
- 输出 data/processed/triples_extracted_{start:06d}_{end:06d}.json（去重后）。

用法：python _extract_range.py --start 151 --end 300 [--workers N]
"""
import argparse
import io
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, ".")

from extraction import llm_layer
from extraction.extract import dedupe, extract_one

PRE = "data/processed/preprocessed_documents.json"


def _load_records(start: int, end: int):
    records = json.load(open(PRE, encoding="utf-8"))
    docs = []
    for r in records:
        did = r.get("document_id", "")
        if did.startswith("doc_"):
            try:
                n = int(did.split("_")[1])
            except ValueError:
                continue
            if start <= n <= end:
                docs.append(r)
    docs.sort(key=lambda r: int(r["document_id"].split("_")[1]))
    if len(docs) != end - start + 1:
        have = {int(r["document_id"].split("_")[1]) for r in docs}
        missing = sorted(set(range(start, end + 1)) - have)
        raise SystemExit(f"范围不完整：应有 {end-start+1} 篇，实际 {len(docs)} 篇，缺失 {missing[:20]}")
    return docs


def _save_state(state, state_path):
    tmp = state_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, state_path)


def _write_output(state, out_path):
    all_triples = dedupe(state["triples"])
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(all_triples, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return len(all_triples)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=int, required=True)
    ap.add_argument("--end", type=int, required=True)
    ap.add_argument("--workers", type=int, default=int(os.environ.get("EXTRACT_WORKERS", "6")))
    args = ap.parse_args()

    docs = _load_records(args.start, args.end)
    tag = f"{args.start:06d}_{args.end:06d}"
    out_path = f"data/processed/triples_extracted_{tag}.json"
    state_path = f"data/processed/.extract_{tag}_state.json"

    state = {"done": {}, "failed": [], "triples": []}
    if os.path.exists(state_path):
        try:
            state = json.load(open(state_path, encoding="utf-8"))
        except Exception:
            state = {"done": {}, "failed": [], "triples": []}

    total = len(docs)
    todo = [r for r in docs if r["document_id"] not in state["done"]]
    print(f"范围 {docs[0]['document_id']}..{docs[-1]['document_id']} 共 {total} 篇；"
          f"已完成 {len(state['done'])}；上次失败 {len(state['failed'])}；本次待跑 {len(todo)}", flush=True)

    llm_layer.reset_call_count()
    t0 = time.time()
    attempts = 0

    def run(rec):
        s = time.time()
        ts = extract_one(rec)
        return ts, time.time() - s

    for _pass in (1, 2):
        if _pass == 1:
            batch = [r for r in docs if r["document_id"] not in state["done"]]
        else:
            batch = [r for r in docs if r["document_id"] in state["failed"]]
            for r in batch:
                state["failed"].remove(r["document_id"])
        if not batch:
            continue
        print(f"---- 第 {_pass} 轮：{len(batch)} 篇 ----", flush=True)
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(run, r): r for r in batch}
            for fut in as_completed(futs):
                docid = futs[fut]["document_id"]
                try:
                    ts, dt = fut.result()
                    state["triples"].extend(ts)
                    state["done"][docid] = len(ts)
                    state["failed"] = [x for x in state["failed"] if x != docid]
                    attempts += 1
                    if attempts % 5 == 0 or attempts == total:
                        print(f"[{attempts}/{total}] {docid} +{len(ts)} 条 "
                              f"(累计 {len(state['triples'])}，{dt:.1f}s，{(time.time()-t0)/60:.1f}min)", flush=True)
                except Exception as exc:  # noqa: BLE001 - 单篇失败不中断整批
                    if docid not in state["failed"]:
                        state["failed"].append(docid)
                    attempts += 1
                    print(f"[{attempts}/{total}] {docid} 失败：{type(exc).__name__}: {exc}", flush=True)
                finally:
                    _save_state(state, state_path)

    n_out = _write_output(state, out_path)
    print(f"完成：成功 {len(state['done'])}/{total}，产出 {n_out} 条（去重后）；"
          f"仍失败 {len(state['failed'])} 篇：{state['failed'] or '无'} -> {out_path}", flush=True)
    return 0 if not state["failed"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("中断：已按当前进度落盘 checkpoint，重跑可续传。", file=sys.stderr)
        raise
