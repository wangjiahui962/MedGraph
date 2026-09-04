# -*- coding: utf-8 -*-
"""【第三阶段 · LLM 层（歧义/冲突/补充抽取）】

规则层覆盖不了的复杂表达、跨句关系、长尾知识与歧义事实，由本层调用大语言模型
做结构化抽取，输出与规则层**完全一致**的三元组结构（字段见 extract.py 说明）。

接入方式（通过环境变量配置，兼容 OpenAI 风格接口）：
    LLM_API_BASE  —— 接口地址，如 https://api.openai.com/v1
    LLM_API_KEY   —— 访问密钥
    LLM_MODEL     —— 模型名，默认 gpt-4o-mini

运行时行为：
    - 未配置 LLM_API_BASE / LLM_API_KEY 时，本层自动降级（is_available() 返回 False，
      extract() 返回空列表），流水线保留规则层结果继续运行；
    - 请求默认关闭模型的思考/推理链（thinking disabled），结构化抽取直接出 JSON，
      避免 deepseek 等模型每篇先烧数秒推理，显著降低单篇耗时（见 THINKING_DISABLED）；
    - 网络类失败（超时/5xx/429 限流）做抖动退避自动重试、结构错误会把错误信息回传重新生成；
      重试耗尽仍失败抛 RuntimeError，由编排层（extract.py）报错暂停并落盘已抽结果；
    - 仅当真正未配置 LLM 时才静默降级为空列表，流水线保留前序层结果继续运行。

结果校验：LLM 返回的 subject_type / relation / object_type 会经 ontology 统一解析，
并做关系 domain/range 校验，非法项被剔除，确保入库质量。
"""

from __future__ import annotations

import json
import os
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ontology import schema  # noqa: E402

DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_CONFIDENCE = 0.80

# 系统提示词只依赖静态本体，构建一次后缓存复用，避免每篇文档重复生成同一份提示词。
_SYSTEM_PROMPT_CACHE: str | None = None

ENV_FILE = ROOT / ".env"


def _load_env_file() -> None:
    """把项目根目录的 .env 加载进 os.environ（已存在的环境变量优先，不覆盖）。

    支持常见 .env 写法：`KEY=value`、`KEY = value`、双引号/单引号取值、`#` 注释、空行。
    加载后即可通过 os.environ.get("LLM_API_BASE") 等方式读取模型接入配置。
    """
    if not ENV_FILE.is_file():
        return
    for raw_line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # 去掉取值两端的成对引号，例如 LLM_API_KEY= "xxx" -> xxx
        if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def _load_config() -> tuple[str, str, str]:
    """读取 LLM 接入配置：返回 (api_base, api_key, model)。

    优先读环境变量，其次从项目根目录 .env 补全（见 _load_env_file）。
    api_base 兼容两种写法：
        - 只填到 /v1（如 https://api.deepseek.cn/v1）  -> 调用时再拼 /chat/completions
        - 直接填到 /chat/completions（本 .env 的写法）-> 原样使用，避免路径重复
    """
    _load_env_file()
    base = os.environ.get("LLM_API_BASE", "").strip().rstrip("/")
    key = os.environ.get("LLM_API_KEY", "").strip()
    model = os.environ.get("LLM_MODEL", "").strip() or DEFAULT_MODEL
    return base, key, model


def is_available() -> bool:
    """LLM 层是否可用（必须同时配置 base 与 key）。"""
    base, key, _ = _load_config()
    return bool(base and key)


def _build_system_prompt() -> str:
    """依据本体动态生成精简系统提示词：实体/关系约束 + 关键规则 + 输出格式。

    只输出 JSON 数组，配合 _validate_response / _call_llm_checked 做格式校验，
    结构不合格会自动重试（最多 MAX_LLM_RETRIES 次）。
    """
    global _SYSTEM_PROMPT_CACHE
    cached = _SYSTEM_PROMPT_CACHE
    if cached is not None:
        return cached

    ontology = schema.export()
    lines = [
        "你是医学知识抽取助手。从给定文本抽取事实三元组，严格按下列规则输出。",
        "",
        "一、实体类型（subject_type/object_type 只能填以下英文 ID）：",
    ]
    for eid, info in ontology["entity_types"].items():
        hint = _TYPE_HINTS.get(eid, "")
        lines.append(f"- {eid}：{info['label']}" + (f"，{hint}" if hint else ""))

    lines.append("")
    lines.append("二、关系（relation 只能填以下英文 ID；括号内为允许的主语类型 -> 宾语类型）：")
    for rid, info in ontology["relation_types"].items():
        if rid == "RELATED_TO":
            lines.append("- RELATED_TO：任意两个上述实体之间（兜底，优先用更具体的关系）")
            continue
        src = "/".join(info["domain"])
        dst = "/".join(info["range"])
        lines.append(f"- {rid}（{info['label']}）：{src} -> {dst}")

    lines += [
        "",
        "三、规则：",
        "1. subject 指文本主题的核心医学事物（通常是标题所指疾病），不要抽作者、机构等非医学实体。",
        "2. 只抽原文明确出现的知识，不编造；同一事实重复提到只输出一次。",
        "3. 实体必须是具体医学术语：单独的“治疗”“症状”“患者”“因素”等泛指词不算实体。",
        "4. 实体类型选最贴切的一类；主语或宾语在文中无对应明确实体时丢弃该条，不强补。",
        "5. 仅当没有更具体的关系时才用 RELATED_TO；不确定就不输出。",
        "6. 每个三元组的主语/宾语类型必须落在所选关系的允许范围内。",
        "7. 没有可抽取的知识时输出 []。",
        "",
        "四、输出格式（极其重要）：",
        "只输出一个 JSON 数组，不要任何解释或 Markdown 代码块。每个元素格式固定为：",
        '{"subject": "主语实体名", "subject_type": "实体类型ID", "relation": "关系ID", '
        '"object": "宾语实体名", "object_type": "实体类型ID", "evidence": "原文最短片段"}',
        "evidence 必须逐字来自原文（一字不改），取包含关键实体的最短片段（一般不超过 30 字）。",
        "",
        "示例输出：",
        '[{"subject": "哮喘", "subject_type": "Disease", "relation": "HAS_SYMPTOM", "object": "喘息", "object_type": "Symptom", "evidence": "哮喘的典型症状包括喘息。"}]',
    ]
    prompt_text = "\n".join(lines)
    _SYSTEM_PROMPT_CACHE = prompt_text
    return prompt_text


# 实体类型的简短区分提示（只写归类关键点；泛称/修饰词规则见提示词正文）。
_TYPE_HINTS: dict[str, str] = {
    "Disease": "如哮喘、糖尿病",
    "Symptom": "如喘息、发热、头晕",
    "Drug": "具体药品，不是“抗生素/药物”等泛称",
    "Treatment": "手术、氧疗等措施，不是药品",
    "Examination": "如 X 线、血常规、肺功能",
    "Department": "如呼吸内科、心内科",
    "Population": "如儿童、老年人、孕妇",
    "RiskFactor": "如吸烟、肥胖、高血压",
    "Complication": "如肺炎、呼吸衰竭",
}


def _retry_delay_seconds(attempt: int, retry_after: float | None = None) -> float:
    """计算失败后的重试等待秒数（带全抖动，避免多线程同步重试造成惊群）。

    429 限流时优先使用服务端 Retry-After；其余按 2^(n-1) 秒指数退避并叠加
    0~base 的随机抖动，等待上限为 RETRY_BACKOFF_MAX。
    """
    if retry_after is not None and retry_after > 0:
        return min(RETRY_BACKOFF_MAX, retry_after)
    delay = RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
    return min(RETRY_BACKOFF_MAX, delay + random.uniform(0, delay))


def _retry_after_seconds(exc: urllib.error.HTTPError) -> float | None:
    """从 429 响应头读取 Retry-After（秒），读不到返回 None。"""
    headers = getattr(exc, "headers", None)
    value = headers.get("Retry-After") if headers is not None else None
    if not value:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _warn_retry(kind: str, attempt: int, last_error: str, delay: float) -> None:
    """打印重试提示；最后一次失败只提示、不再等待。"""
    if attempt >= RETRY_MAX_ATTEMPTS:
        print(f"WARN: {kind}（第 {attempt}/{RETRY_MAX_ATTEMPTS} 次）：{last_error}", file=sys.stderr)
        return
    print(f"WARN: {kind}（第 {attempt}/{RETRY_MAX_ATTEMPTS} 次）：{last_error}，{delay:.1f}s 后重试", file=sys.stderr)


def _call_llm(messages: list[dict[str, str]]) -> str:
    """调用兼容 OpenAI 的 chat/completions 接口，返回模型回复文本。

    - 抽取是确定性结构化任务，payload 默认带 thinking={"type": "disabled"}
      （开关见 THINKING_DISABLED），关闭模型思考链，避免每篇多烧数秒推理时间；
    - 网络/HTTP 异常（超时、5xx、429 限流、连接错误等）视为可恢复，做抖动退避重试
      （429 优先遵循服务端 Retry-After，见 RETRY 相关常量），避免偶发抖动导致整篇
      数据静默丢失，也避免多线程并发重试相互叠加放大限流；
    - 重试耗尽仍失败则抛出 RuntimeError，由编排层“报错暂停”（不再静默降级为空结果）。
    """
    base, key, model = _load_config()
    # 兼容两种 base 写法：已填到 /chat/completions 则原样使用，否则拼接
    url = base if base.endswith("/chat/completions") else f"{base}/chat/completions"
    payload = json.dumps({
        "model": model,
        "messages": messages,
        "temperature": 0,
        **({"thinking": {"type": "disabled"}} if THINKING_DISABLED else {}),
    }).encode("utf-8")

    last_error: str = "未知网络错误"
    for attempt in range(1, RETRY_MAX_ATTEMPTS + 1):
        try:
            request = urllib.request.Request(
                url,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {key}",
                },
            )
            # 每次真实发出 HTTP 请求都计数（含重试），便于核对 API 调用成本
            global _call_count
            with _call_count_lock:
                _call_count += 1
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:  # noqa: S310 - 用户配置的固定接口
                data = json.loads(response.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as exc:
            status = getattr(exc, "code", None)
            # 4xx 客户端错误（除 429 限流）重试无意义，直接放弃
            if isinstance(status, int) and 400 <= status < 500 and status != 429:
                raise RuntimeError(f"LLM 接口返回客户端错误（HTTP {status}）：{exc}") from exc
            last_error = f"HTTP {status} / {exc}"
            delay = _retry_delay_seconds(attempt, _retry_after_seconds(exc) if status == 429 else None)
            _warn_retry("LLM 调用异常", attempt, last_error, delay)
            if attempt < RETRY_MAX_ATTEMPTS:
                time.sleep(delay)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            # 超时/连接错误等同样可恢复，按抖动退避重试
            last_error = f"{type(exc).__name__}: {exc}"
            delay = _retry_delay_seconds(attempt)
            _warn_retry("LLM 调用异常", attempt, last_error, delay)
            if attempt < RETRY_MAX_ATTEMPTS:
                time.sleep(delay)
        except (KeyError, IndexError, ValueError) as exc:
            # 响应能解析但结构不符预期（无 choices/message 等）：也按可恢复重试
            last_error = f"响应结构异常：{exc}"
            delay = _retry_delay_seconds(attempt)
            _warn_retry("LLM 响应结构异常", attempt, last_error, delay)
            if attempt < RETRY_MAX_ATTEMPTS:
                time.sleep(delay)

    # 重试耗尽仍失败 -> 报错暂停，避免静默丢数据
    raise RuntimeError(f"LLM 连续 {RETRY_MAX_ATTEMPTS} 次调用失败，已暂停。最后错误：{last_error}")


# 网络/调用常量的退避重试与超时设置
RETRY_MAX_ATTEMPTS = 3        # 网络/响应异常的最大总尝试次数（初始 1 次 + 重试 2 次）
RETRY_BACKOFF_BASE = 1.0      # 指数退避基数：第 n 次失败按 2^(n-1) 秒做全抖动退避
RETRY_BACKOFF_MAX = 16.0      # 单次退避等待上限（秒），防止 429 的 Retry-After 过大拖慢整批
REQUEST_TIMEOUT = 60          # 单次 HTTP 请求超时（秒）

# 关闭模型"思考/推理链"模式（deepseek 系列默认开启，单篇会额外烧掉数秒推理）。
# 本项目输出结构化 JSON 三元组、无需长推理，置 True 让每次请求直接出结果（单篇快数倍）。
THINKING_DISABLED = True


# LLM 输出**结构**连续错误的允许次数：超过则报错暂停（避免在纯垃圾输出上反复消耗 token/费用）。
# 注：结构错误（非 JSON 数组）才是值得重试的情况；单条三元组越界/字段缺失已由
# _validate_response 放宽为"整篇合法、逐条丢弃"，不再触发整篇重试，从而避免调用放大。
MAX_LLM_RETRIES = 3

# —— LLM 调用计数 ——
# 统计每次真实发出的 HTTP 请求（含网络重试），用于核对 API 调用成本/次数。
# 线程安全：extract.py 用多线程调用 LLM，故用锁保护计数。
_call_count = 0
_call_count_lock = threading.Lock()


def call_count() -> int:
    """返回累计的 LLM HTTP 调用次数（含重试）。"""
    return _call_count


def reset_call_count() -> None:
    """清零 LLM 调用计数器。"""
    global _call_count
    with _call_count_lock:
        _call_count = 0


def _validate_response(raw: str) -> tuple[bool, str]:
    """宽松校验 LLM 输出**结构**（是否为一个可解析的 JSON 数组）。

    只保证顶层是可解析的数组即视为"合法"，**不**因某条三元组关系越界/字段缺失
    而判定整篇失败——这些逐条问题统一由 _parse_triples 在入库前处理：
    - 合法条目保留；越界（关系不满足 domain/range、字段缺失）条目丢弃；
    - 证据无法定位到原文的条目丢弃。
    这样避免“数组内含一条坏三元组就整篇重取”，从根源降低重试导致的 API 调用放大。
    空数组视为合法（本文无知识可抽）。
    """
    text = (raw or "").strip()
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        return False, "输出中未找到 JSON 数组"
    try:
        items = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        return False, f"JSON 解析失败：{exc}"
    if not isinstance(items, list):
        return False, "顶层不是 JSON 数组"
    return True, ""


def _call_llm_checked(messages: list[dict[str, str]], max_retries: int = MAX_LLM_RETRIES) -> str:
    """调用 LLM 并校验输出**结构**；结构错误（非 JSON 数组）自动重试，最多 max_retries 次。

    网络级退避已内置于 _call_llm，此处只处理"输出不是可解析 JSON 数组"这一结构问题；
    由于 _validate_response 已放宽（越界条目不再判整篇失败），正常输出即使含个别
    越界三元组也**不重试**，从而避免格式重试 × 网络重试的嵌套放大。
    连续 max_retries 次仍非 JSON 数组则抛 RuntimeError，交由编排层"报错暂停"。
    """
    last_error = "未知结构错误"
    for attempt in range(1, max_retries + 1):
        raw = _call_llm(messages)
        ok, last_error = _validate_response(raw)
        if ok:
            return raw
        print(f"WARN: LLM 输出结构错误（第 {attempt}/{max_retries} 次）：{last_error}", file=sys.stderr)
        messages = messages + [
            {"role": "assistant", "content": raw},
            {"role": "user", "content": f"你上次的输出不是合法的 JSON 数组：{last_error}。"
                                        f"请只重新输出一个 JSON 数组，不要任何解释或多余文字。"},
        ]
    raise RuntimeError(f"LLM 输出连续 {max_retries} 次非合法 JSON 数组，已暂停。最后错误：{last_error}")


# 宽松证据定位：把证据/原文统一去除空白、标点与常见全半角差异后匹配。
# LLM 生成的 evidence 常对原句做改写、省略或标点调整，逐字 `in content` 会误杀大量
# 有效三元组；归一化后再判断"证据是否脱胎于原文"，既保留可追溯性又不至于过严。
_REMOVE_CHARS = str.maketrans("", "", " \t\r\n，。、；：？！,.;:?!\"'“”‘’（）()《》<>—…·【】")


def _char_bigrams(s: str) -> set[str]:
    """取字符串的重叠二元字符组（保留中文语义片段），空串返回空集。"""
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) >= 2 else (set() if not s else {s})


def _evidence_matches(evidence: str, content: str) -> bool:
    """放宽判断：证据与原文去空白/标点后**整段包含**或**字符重复率足够高**。

    容忍 LLM 对原句的轻改写（删连接词、换标点、微调措辞），同时避免把明显自编的句子
    当成证据。两次判据任一成立即通过：
      1) 证据整段（归一化后）出现在原文归一化文本中；
      2) 证据的字符二元组大多能在原文中找到（重叠率 >= 0.5）。
    """
    ev = evidence.translate(_REMOVE_CHARS)
    ct = content.translate(_REMOVE_CHARS)
    if not ev:
        return False
    if ev in ct:
        return True
    ev_grams = _char_bigrams(ev)
    if not ev_grams:
        return False
    ct_grams = _char_bigrams(ct)
    hit = sum(1 for g in ev_grams if g in ct_grams)
    return hit / len(ev_grams) >= 0.5


def _parse_triples(raw: str, record: dict[str, Any]) -> list[dict[str, Any]]:
    """解析 LLM 输出的 JSON 数组，校验并归一化为标准三元组结构。"""
    text = (raw or "").strip()
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        return []

    try:
        items = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    if not isinstance(items, list):
        return []

    document_id = record.get("document_id")
    content = record.get("content", "")
    triples: list[dict[str, Any]] = []

    for item in items:
        if not isinstance(item, dict):
            continue
        rel = schema.resolve_relation(item.get("relation"))
        sub = schema.resolve_entity_type(item.get("subject_type"))
        obj = schema.resolve_entity_type(item.get("object_type"))
        subject = str(item.get("subject", "")).strip()
        object_ = str(item.get("object", "")).strip()
        evidence = str(item.get("evidence") or item.get("source_text") or "").strip()

        # 必要字段缺失或违反本体约束时丢弃
        if not (rel and sub and obj and subject and object_):
            continue
        if not schema.is_valid_relation(rel, sub, obj):
            continue

        # evidence 需能定位到原文（放宽匹配：去空白/标点后再判断，容忍 LLM 轻改写）。
        # 无法追溯来源的证据对应三元组直接丢弃，保证每条入库事实有可靠原文依据。
        if not evidence or not _evidence_matches(evidence, content):
            continue

        # LLM 已不再输出 confidence，统一使用系统默认值（DEFAULT_CONFIDENCE）。
        # 若个别模型仍带了该字段，忽略之，保证同一管道内 LLM 层置信度口径一致。
        confidence = DEFAULT_CONFIDENCE

        triples.append({
            "subject": subject,
            "subject_type": sub,
            "subject_type_label": schema.entity_type_label(sub),
            "relation": rel,
            "relation_label": schema.relation_label(rel),
            "object": object_,
            "object_type": obj,
            "object_type_label": schema.entity_type_label(obj),
            "source_document_id": document_id,
            "source_text": evidence,
            "confidence": round(confidence, 4),
            "layer": "llm",
        })
    return triples


def extract(record: dict[str, Any]) -> list[dict[str, Any]]:
    """LLM 层抽取（整篇文档一次调用）：未配置时返回空列表（降级）。

    整篇正文直接交给 LLM，配合 _build_system_prompt 让其返回结构化 JSON 数组；
    - 网络/HTTP 异常已内置于 _call_llm 做指数退避重试，重试耗尽仍失败会抛 RuntimeError；
    - 输出经 _validate_response 严格校验，连续格式错误重试 MAX_LLM_RETRIES 次仍失败
      同样抛 RuntimeError；
    - 无论网络还是格式错误连续失败，都交由编排层（extract.py）"报错暂停"，而不是静默
      吞掉整篇数据。只有真正"未配置 LLM"时才返回空列表降级。
    """
    if not is_available():
        return []

    title = record.get("title") or ""
    content = record.get("content") or ""
    messages = [
        {"role": "system", "content": _build_system_prompt()},
        {"role": "user", "content": f"标题：{title}\n正文：{content}"},
    ]
    raw = _call_llm_checked(messages)
    return _parse_triples(raw, record)
