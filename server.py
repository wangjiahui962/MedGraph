# -*- coding: utf-8 -*-
"""MedGraph 本地轻量后端服务（仅用 Python 标准库）。

用途：给前端 web 界面提供两个能力——
    1. POST /api/collect  —— 增加新数据：从中文维基百科搜索 N 篇新文章，繁体转简体后存入 documents.db，默认继续抽取并更新图谱（auto_extract=false 可关闭）。
    2. POST /api/extract  —— 提取现有数据：预处理 → LLM 抽取 → 关键词过滤 → 入库 → 导出前端。
    3. GET  /api/jobs     —— 查询当前/最近任务状态（前端轮询用，便于提示“可以刷新了”）。

设计说明：
    - 采集/抽取都是耗时任务，因此在后台线程里跑子进程，接口立即返回“已启动”；
    - 任务状态保存在内存 dict 中（本机单用户教学工具，无需持久化）；
    - 服务仅监听 127.0.0.1，仅本机可访问；跨域头 CORS 允许由 Vite 代理访问；
    - 配置在项目根目录 .env，运行前请确认 LLM_API_BASE / LLM_API_KEY / LLM_MODEL 已就绪。

启动：
    python server.py            # 默认 127.0.0.1:8756
    python server.py --port 9000
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

# 项目根目录（server.py 所在处），所有子进程都在此目录下运行
ROOT = Path(__file__).resolve().parent

# 后端运行目录内需生成的产物（前端通过 Vite 代理访问 /api）
PROCESSED = ROOT / "data" / "processed"
TRIPLES_JSON = PROCESSED / "triples.json"
DOCUMENTS_JSON = PROCESSED / "documents.json"
EXTRACTED_JSON = PROCESSED / "triples_extracted.json"
FRONTEND_PUBLIC_DIR = ROOT / "frontend" / "public" / "data"
FRONTEND_PUBLIC_DATA = FRONTEND_PUBLIC_DIR / "triples.json"

# 任务状态存放（内存）：job_id -> {"id","kind","status","started_at","finished_at","message","detail"}
_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()


def _now() -> str:
    """返回本地时间的 ISO 字符串，用于任务状态时间戳。"""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def _try_register_job(kind: str) -> str | None:
    """已有 running 任务时返回 None；否则注册新任务并返回 job_id。

    注册与“检查是否繁忙”在同一个锁内完成，避免两个并发请求同时通过检查。
    """
    job_id = f"{kind}_{uuid.uuid4().hex[:8]}"
    with _JOBS_LOCK:
        if any(j["status"] == "running" for j in _JOBS.values()):
            return None
        _JOBS[job_id] = {
            "id": job_id,
            "kind": kind,
            "status": "running",
            "started_at": _now(),
            "finished_at": None,
            "message": "任务进行中…",
            "detail": None,
        }
    return job_id


def _update_job(job_id: str, **fields) -> None:
    """更新一条任务记录。"""
    with _JOBS_LOCK:
        if job_id in _JOBS:
            _JOBS[job_id].update(fields)


def _run_in_project(*cmd: str, capture: int = 80000) -> tuple[int, str]:
    """在项目根目录运行命令，返回 (returncode, 截断后的输出)。"""
    try:
        proc = subprocess.run(
            list(cmd),
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except Exception as exc:  # 进程启动失败（如找不到 python）
        return 1, f"无法启动子进程: {exc}"
    out = (proc.stdout or "") + (proc.stderr or "")
    if len(out) > capture:
        out = out[:capture] + "\n…（输出过长已截断）"
    return proc.returncode, out


def _export_to_frontend() -> tuple[bool, str]:
    """把三元组与文档原文 JSON 同步到前端 public/data/，供页面刷新后读取。"""
    if not TRIPLES_JSON.is_file():
        return False, f"未找到 {TRIPLES_JSON.name}，先执行抽取导出。"
    FRONTEND_PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(TRIPLES_JSON, FRONTEND_PUBLIC_DIR / TRIPLES_JSON.name)
    msgs = [f"已同步 {TRIPLES_JSON.name} → frontend/public/data/"]
    if DOCUMENTS_JSON.is_file():
        shutil.copyfile(DOCUMENTS_JSON, FRONTEND_PUBLIC_DIR / DOCUMENTS_JSON.name)
        msgs.append(f"已同步 {DOCUMENTS_JSON.name} → frontend/public/data/")
    else:
        msgs.append(
            "未找到 documents.json，文档『展开原文』需先运行 "
            "python db/store_documents.py --export-frontend 生成"
        )
    return True, "；".join(msgs)


def _run_extract(job_id: str, limit: int = 0, finalize: bool = True) -> None:
    """提取现有数据：预处理 → 抽取 → 入库 → 导出前端。"""
    steps = [
        ("数据预处理", (sys.executable, "preprocess/preprocess.py")),
        ("增量分层信息抽取", (sys.executable, "extraction/extract.py") + (("--limit", str(limit)) if limit > 0 else ())),
    ]
    for label, cmd in steps:
        _update_job(job_id, message=f"正在执行：{label}…")
        code, out = _run_in_project(*cmd)
        if code != 0:
            _update_job(
                job_id, status="failed", finished_at=_now(),
                message=f"{label}失败", detail=out,
            )
            return
    # 入库前先按关键词规则剔除坏三元组（泛称/学科名/标题误用/顿号并列/中医证候等）
    _update_job(job_id, message="正在按关键词规则过滤坏三元组…")
    code, out = _run_in_project(sys.executable, "db/filter_extracted.py")
    if code != 0:
        _update_job(job_id, status="failed", finished_at=_now(),
                    message="关键词过滤失败", detail=out)
        return
    # 本轮抽取结果入库（triples_extracted.json → triples.db，按唯一索引增量合并、不清库）
    # 先将已有前端图谱导入数据库，避免数据库为空时导出覆盖历史知识。
    _update_job(job_id, message="正在校准历史图谱数据库…")
    if TRIPLES_JSON.is_file():
        code, out = _run_in_project(sys.executable, "db/store_triples.py", str(TRIPLES_JSON))
        if code != 0:
            _update_job(job_id, status="failed", finished_at=_now(),
                        message="历史图谱初始化失败", detail=out)
            return
    _update_job(job_id, message="正在把本轮抽取结果入库…")
    code, out = _run_in_project(sys.executable, "db/store_triples.py")
    if code != 0:
        _update_job(job_id, status="failed", finished_at=_now(),
                    message="三元组入库失败", detail=out)
        return
    # 从 DB 导出前端图谱数据（triples.db → data/processed/triples.json）
    _update_job(job_id, message="正在导出前端图谱数据…")
    code, out = _run_in_project(sys.executable, "db/store_triples.py", "--export")
    if code != 0:
        _update_job(job_id, status="failed", finished_at=_now(),
                    message="三元组导出失败", detail=out)
        return
    # 为前端补充置信度、来源统计和质量标记，并生成冲突报告
    _update_job(job_id, message="正在评估三元组质量并检测冲突…")
    code, out = _run_in_project(sys.executable, "quality/assess.py")
    if code != 0:
        _update_job(job_id, status="failed", finished_at=_now(),
                    message="三元组质量评估失败", detail=out)
        return
    # 导出文档原文索引（前端“展开原文”按需读取；opencc 缺失时保留原文不中断）
    _update_job(job_id, message="正在导出文档原文索引…")
    code, out = _run_in_project(sys.executable, "db/store_documents.py", "--export-frontend")
    if code != 0:
        _update_job(job_id, status="failed", finished_at=_now(),
                    message="文档原文索引导出失败", detail=out)
        return
    ok, msg = _export_to_frontend()
    if not ok:
        _update_job(job_id, status="failed", finished_at=_now(), message=msg)
        return
    if finalize:
        _update_job(job_id, status="succeeded", finished_at=_now(),
                    message="提取完成，图谱数据与文档原文已更新。", detail=msg)


def _run_collect(job_id: str, count: int = 5, auto_extract: bool = True) -> None:
    """采集维基文章；默认继续执行抽取并发布最新图谱。"""
    import re

    _update_job(job_id, message=f"正在从维基百科搜索新增最多 {count} 篇文章…")
    code, out = _run_in_project(
        sys.executable, "-m", "collector.wiki_add", "--count", str(count)
    )
    if code != 0:
        _update_job(job_id, status="failed", finished_at=_now(),
                    message="维基文章采集失败（请检查网络）", detail=out)
        return
    m = re.search(r"ADDED:(\d+)", out)
    added = int(m.group(1)) if m else 0
    # 新文档已入库，同步生成并发布文档原文索引，保证前端“展开原文”可读新文档
    _update_job(job_id, message="正在导出文档原文索引…")
    code, out2 = _run_in_project(sys.executable, "db/store_documents.py", "--export-frontend")
    if code != 0:
        _update_job(job_id, status="failed", finished_at=_now(),
                    message="文档原文索引导出失败", detail=out2)
        return
    ok, msg = _export_to_frontend()
    if not ok:
        _update_job(job_id, status="failed", finished_at=_now(), message=msg)
        return
    if auto_extract:
        _update_job(job_id, message="采集完成，正在自动抽取并更新图谱…")
        _run_extract(job_id, finalize=False)
        with _JOBS_LOCK:
            failed = _JOBS.get(job_id, {}).get("status") == "failed"
        if failed:
            return
        message = (
            f"新增 {added} 篇文章，已完成信息抽取并更新图谱。"
            if added else "没有新增文章，已重新抽取并更新图谱。"
        )
    else:
        message = (
            f"维基新增完成：已导入 {added} 篇文章（繁体已转简体）。"
            if added else "维基没有找到新的可添加文章（或检索词都命中已有文章）。"
        )
    _update_job(job_id, status="succeeded", finished_at=_now(),
                message=message, detail=out.strip())


def _spawn(kind: str, **kwargs) -> str | None:
    """在后台线程启动一个任务，返回 job_id；已有任务在跑时返回 None。"""
    job_id = _try_register_job(kind)
    if job_id is None:
        return None
    targets = {
        "extract": _run_extract,
        "collect": _run_collect,
    }
    target = targets[kind]
    threading.Thread(target=target, args=(job_id,), kwargs=kwargs, daemon=True).start()
    return job_id


def _json_response(handler: BaseHTTPRequestHandler, code: int, payload: dict) -> None:
    """写 JSON 响应并加上跨域头。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class MedGraphHandler(BaseHTTPRequestHandler):
    """HTTP 请求处理：路由 /api/collect、/api/extract、/api/jobs。"""

    def log_message(self, fmt: str, *args) -> None:  # 精简日志
        sys.stderr.write(f"[medgraph-server] {self.address_string()} {fmt % args}\n")

    def do_OPTIONS(self):  # 预检请求
        _json_response(self, 204, {})

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/jobs":
            with _JOBS_LOCK:
                jobs = sorted(_JOBS.values(), key=lambda j: j["started_at"], reverse=True)[:10]
            _json_response(self, 200, {"jobs": jobs})
        elif path == "/api/health":
            _json_response(self, 200, {"ok": True, "jobs_running": sum(1 for j in _JOBS.values() if j["status"] == "running")})
        else:
            _json_response(self, 404, {"error": "Not Found", "path": path})

    def do_POST(self):
        path = urlparse(self.path).path
        # 读取请求体（前端可选传参数，如 limit）
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except json.JSONDecodeError:
            body = {}
        if path not in ("/api/extract", "/api/collect"):
            _json_response(self, 404, {"error": "Not Found", "path": path})
            return
        if path == "/api/extract":
            try:
                limit = max(0, int(body.get("limit") or 0))
            except (TypeError, ValueError):
                _json_response(self, 400, {"error": "参数 limit 必须是整数。"})
                return
            job_id = _spawn("extract", limit=limit)
        else:
            # /api/collect：采集后默认自动抽取（可传 auto_extract=false 仅采集）
            try:
                count = max(1, min(100, int(body.get("count") or 5)))
            except (TypeError, ValueError):
                _json_response(self, 400, {"error": "参数 count 必须是整数。"})
                return
            auto_extract = body.get("auto_extract", True)
            if not isinstance(auto_extract, bool):
                _json_response(self, 400, {"error": "参数 auto_extract 必须是布尔值。"})
                return
            job_id = _spawn("collect", count=count, auto_extract=auto_extract)
        if job_id is None:
            _json_response(
                self, 409,
                {"error": "已有任务正在运行，请等待其完成后再试。"},
            )
            return
        _json_response(self, 202, {
            "job_id": job_id,
            "kind": "extract" if path == "/api/extract" else "collect",
            "status": "started",
        })


def main() -> int:
    parser = argparse.ArgumentParser(description="MedGraph 本地后端服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8756)
    args = parser.parse_args()
    print(f"MedGraph 后端服务已启动: http://{args.host}:{args.port}")
    print("前端请保持 Vite 开发服务器运行，/api 请求会通过代理转发到本服务。")
    try:
        ThreadingHTTPServer((args.host, args.port), MedGraphHandler).serve_forever()
    except KeyboardInterrupt:
        print("\n后端服务已停止。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
