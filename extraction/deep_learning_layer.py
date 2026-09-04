# -*- coding: utf-8 -*-
"""【第三阶段 · 深度学习层（NER 实体边界识别）】

设计目标：修复规则层"后缀模板"造成的实体边界切分错误（例如把
“前尚无特定药物能直接治疗”整段误判成一个宾语）。做法是把客体实体识别建模为
**中文序列标注（BIO 命名实体识别）**，用预训练模型学习“正确的实体跨度”。

本体关系不变，本层只负责“识别出边界正确的实体”，再按实体类型映射回本体关系
（与规则层相同的映射），因此输出三元组结构与规则层完全一致（layer=deep_learning）。

标签体系（同时标注疾病主语与 5 类客体实体）：
    O / B·I-Disease / B·I-Symptom / B·I-Drug / B·I-Treatment / B·I-Examination / B·I-RiskFactor

模型：bert-base-chinese + TokenClassification（首选）；缺 GPU 可换 BiLSTM-CRF，
仅需替换 _get_model() 中加载的模型。

接入说明：
    - 模型权重默认不存在 -> AVAILABLE=False、extract() 返回 []，编排层自动降级到 LLM；
    - 运行 extraction/dl_train.py 训练并把权重保存到 models/ner 后，本层自动变为可用。
    - 置信度取模型对每个实体跨度的 token 概率均值，落在 0~1 之间。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from extraction import rule_layer  # noqa: E402 复用医学词典做词面校验
from ontology import schema  # noqa: E402

# 深度学习层开关：默认关闭。
# 关闭原因：当前阶段改用 LLM 完成剩余的信息抽取，不再使用深度学习层产出三元组；
# 关闭后 AVAILABLE=False，predict_record() 返回空，编排层 extract.py 只跑 LLM 层。
# 注意：dl_train.py / dl_eval.py 仍可独立运行（训练/评估用），不受本开关影响。
DEEP_LEARNING_ENABLED = False

MODEL_DIR = ROOT / "models" / "ner"

# 本层需要标注的实体类型：Disease（主语）+ 5 类客体（与规则层客体一致）。
# Disease 纳入 NER 后，可由句子内识别出的疾病直接作为主语，而非固定用文档标题。
ENTITY_TYPES_FOR_NER: list[str] = [
    "Disease", "Symptom", "Drug", "Treatment", "Examination", "RiskFactor",
]

# BIO 标签与索引映射（训练脚本 dl_train.py 使用同一套）
LABELS: list[str] = ["O"] + [
    f"{prefix}-{entity}"
    for entity in ENTITY_TYPES_FOR_NER
    for prefix in ("B", "I")
]
LABEL2ID: dict[str, int] = {label: i for i, label in enumerate(LABELS)}
ID2LABEL: dict[int, str] = {i: label for i, label in enumerate(LABELS)}

# 客体实体类型 -> 本体关系 ID（与规则层一致；Disease 是主语，不在此表中）
TYPE_TO_RELATION: dict[str, str] = {
    "Symptom": "HAS_SYMPTOM",
    "Drug": "TREATED_BY",
    "Treatment": "TREATED_BY",
    "Examination": "DIAGNOSED_BY",
    "RiskFactor": "HAS_RISK_FACTOR",
}

# 实体最小长度：单字实体（如"状/病/瘤/龋"）几乎都是多字实体被拍碎的片段，
# 直接过滤掉，避免产出错误三元组，同时作为"DL 边界不可靠"的路由信号。
MIN_ENTITY_LEN = 2

# 高置信阈值：实体置信度 >= 该值视为"深度学习层足够自信"，直接产出；
# 低于该值（以及任何单字碎片）视为"DL 不确定"，整句转交 LLM 层复核。
# 经验值来自 extraction/dl_eval.py 在 documents.db 抽样上的网格搜索（F1 最优 ≈ 0.7）。
CONFIDENT_THRESHOLD = 0.7

# 实体首尾需剥离的"边界噪声"字符：标点 + 常见虚词/助词。
# NER 常把"的流感疫苗"整段判成一个实体，把两端这些字剥掉即可得到正确边界"流感疫苗"。
_BOUNDARY_STRIP = set(
    "，。；：、！？!?\"'“”‘’（）()《》〈〉【】[]{}…—,.;: "
) | set("的地得之了着过在与和及或等这那该其为已被而")

# NER 高频误判的动词/泛指/人群词：作为独立实体出现时基本是噪声（如"服用""治疗""成年人"），
# 不直接产出，而是作为"不确定"信号路由给 LLM 复核
_NOISE_TERMS = {
    "服用", "使用", "应用", "治疗", "预防", "诊断", "检测", "检查",
    "感染", "出现", "发生", "导致", "引起", "造成", "表现", "患者",
    "病人", "症状", "体征", "药物", "药品", "用药", "护理", "维生素",
    # 人群词常被 NER 误判为 Disease 作主语，加入黑名单
    "成年人", "成人", "儿童", "小孩", "老年人", "老人", "青少年", "婴儿",
    "女性", "男性", "人群", "患者群",
}

# NER 常把句子尾部的连接词/语气词吞进实体（如"電解質失衡甚至""咳嗽等"），
# 从尾部剥掉这些填充成分，得到干净边界（"電解質失衡甚至" -> "電解質失衡"）。
_FILLER_TAILS = ("甚至", "以及", "并且", "或者", "等等", "等的", "等",
                 "加之", "此外", "隨後", "继而", "還有", "还有")


def _strip_filler_tail(term: str) -> str:
    """剥掉实体尾部的连接词/语气词，返回干净术语（可能为空）。"""
    for f in _FILLER_TAILS:
        if term.endswith(f):
            return term[: -len(f)].strip()
    return term


# 高频被 NER 误判为 Symptom 的"非症状名词"（事件/统计/器械等），
# 作为独立实体出现时基本是噪声，直接丢弃（整篇 LLM 会补全真实实体）。
_SYMPTOM_BLACKLIST = {
    "死亡", "死亡率", "發病", "發病率", "病死", "病死率", "疫情", "傳染",
    "爆發", "大流行", "患病人數", "病例", "確診", "治癒", "康復",
}

# 客体"半封闭类"的词面校验：DL 判为 Drug / Treatment / Examination 时，
# 必须命中医学词典或匹配类型后缀才算可信；否则多为非实体误判，例如
#   "外科口罩/布口罩/检疫站" -> Examination、"疫情調查/數據蒐集/保持社交距離" -> Treatment、
#   "李氏人工肝系统" -> Drug。
# 这些高置信但错判的实体直接丢弃（LLM 层整篇抽取会补全真实实体），
# 避免"高置信度也救不了错判类型"的问题。
_LEXICON_CHECK: dict[str, set[str]] = {
    "Drug": rule_layer.DRUG_TERMS,
    "Treatment": rule_layer.TREATMENT_TERMS,
    "Examination": rule_layer.EXAMINATION_TERMS,
}
_TYPE_SUFFIX: dict[str, tuple[str, ...]] = {
    "Drug": ("药", "素", "剂", "林", "霉素", "肽", "醇", "酚", "酸", "酮", "胺",
             "苷", "单抗", "疫苗", "注射液", "片剂", "胶囊", "糖皮质激素"),
    "Treatment": ("治疗", "疗法", "手术", "康复", "接种", "化疗", "放疗", "吸氧",
                  "移植", "注射", "护理", "隔离", "隔離", "透析", "理疗", "牵引"),
    "Examination": ("检查", "测定法", "检测", "筛查", "试验", "扫描", "镜检", "超声",
                    "造影", "活检", "监测", "测定", "拍片", "阅片"),
}


def _plausible_entity(term: str, etype: str) -> bool:
    """实体词面是否与该类型自洽（过滤高置信但仍错判的非实体）。

    开放类（Disease/Symptom/RiskFactor/Complication/Department/Population）不校验；
    Drug/Treatment/Examination 属半封闭类，命中词典或类型后缀才可信。
    """
    if etype not in _LEXICON_CHECK:
        return True
    if term in _LEXICON_CHECK[etype]:
        return True
    return term.endswith(_TYPE_SUFFIX.get(etype, ()))

# 文档标题判定为"疾病名"的常见后缀：只有当标题以这些后缀结尾时，
# 才允许在句子中没有 Disease 主语时兜底用标题作主语。
# 这样"慢性胰腺炎/肺炎"这类疾病条目可用标题，而"带原者/塔里木兔/马来西亚皇家警察"
# 这类非疾病条目不会被硬凑成主语（交由 LLM 判断）。
_DISEASE_SUFFIXES = (
    "病", "症", "炎", "癌", "瘤", "肿", "痛", "毒", "热", "风", "伤",
    "障碍", "功能不全", "综合症", "综合征", "感染", "出血", "中毒",
    "硬化", "衰竭", "梗死", "栓塞", "痉挛", "麻痹", "昏迷", "水肿",
    "坏死", "增生", "结核", "紊乱", "功能紊乱", "缺陷", "畸形", "溃疡",
)


def _is_disease_title(title: str) -> bool:
    """判断文档标题是否像疾病名（长度 2~20 且以医学后缀结尾）。"""
    t = (title or "").strip()
    if not 2 <= len(t) <= 20:
        return False
    return t.endswith(_DISEASE_SUFFIXES)


def _trim_boundary(term: str) -> str:
    """剥掉实体两端的标点/虚词，返回修剪后的实体（可能为空）。"""
    i, j = 0, len(term)
    while i < j and term[i] in _BOUNDARY_STRIP:
        i += 1
    while j > i and term[j - 1] in _BOUNDARY_STRIP:
        j -= 1
    return term[i:j]


def _deps_importable() -> bool:
    """检查 torch / transformers 是否可导入。"""
    try:
        import torch  # noqa: F401
        import transformers  # noqa: F401
        return True
    except ImportError:
        return False


def _model_exists() -> bool:
    return MODEL_DIR.exists()


AVAILABLE = DEEP_LEARNING_ENABLED and _model_exists() and _deps_importable()

# 模型缓存（懒加载）：避免在未训练/未装依赖时产生开销
_tokenizer = None
_model = None


def _sentences_of(record: dict[str, Any]) -> list[str]:
    """取预处理后的句子；缺失时回退简单分句。"""
    sentences = record.get("sentences")
    if isinstance(sentences, list) and sentences:
        return [s for s in sentences if s]
    import re
    content = record.get("content") or ""
    return [p.strip() for p in re.split(r"(?<=[。！？!?；;])", content) if p.strip()]


def _disable_incompatible_torchvision():
    """绕开与 torch 版本不匹配的 torchvision（纯文本任务用不到它）。

    当前环境 torch(2.6) 与 torchvision(0.24) 版本不匹配，transformers 加载任意模型时
    都会 import image_utils -> import torchvision，进而触发 `torchvision::nms` 报错。
    把 is_torchvision_available 置假即可让 transformers 跳过 torchvision 导入。
    """
    try:
        import transformers.utils as _tu
        import transformers.utils.import_utils as _iu

        _tu.is_torchvision_available = lambda: False
        _iu.is_torchvision_available = lambda: False
    except Exception:
        pass


def _get_model():
    """懒加载本地 NER 模型（tokenizer + model），模型优先放到 GPU 上推理。"""
    global _tokenizer, _model
    if _model is None:
        import torch
        from transformers import AutoModelForTokenClassification, AutoTokenizer

        _disable_incompatible_torchvision()
        _tokenizer = AutoTokenizer.from_pretrained(str(MODEL_DIR))
        _model = AutoModelForTokenClassification.from_pretrained(str(MODEL_DIR))
        # 训练脚本已在 GPU 上训练，推理也放到 GPU：CPU 上逐句跑 199 层 BERT 太慢
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        _model.to(device)
        _model.eval()
    return _tokenizer, _model


def _predict_sentence(
    tokenizer, model, sentence: str, with_position: bool = False
) -> list[tuple[str, str, float]] | list[tuple[str, str, float, int, int]]:
    """对单个句子做 NER 预测。

    返回列表：
        - with_position=False：[(实体文本, 实体类型, 置信度)]（兼容外部调用）
        - with_position=True： [(实体文本, 实体类型, 置信度, 起始, 结束)]（predict_record 内部用）
    """
    import torch

    encoding = tokenizer(
        sentence, return_offsets_mapping=True, return_tensors="pt", truncation=True
    )
    # 模型在哪个设备就把输入也放到哪个设备（GPU 推理显著快于 CPU）
    encoding = {k: v.to(model.device) for k, v in encoding.items() if hasattr(v, "to")}
    offsets = encoding["offset_mapping"][0].tolist()

    with torch.no_grad():
        logits = model(
            input_ids=encoding["input_ids"],
            attention_mask=encoding["attention_mask"],
        ).logits

    probs = torch.softmax(logits, dim=-1)
    pred_ids = torch.argmax(logits, dim=-1)[0].tolist()
    token_probs = probs[0].max(dim=-1).values.tolist()
    id2label = model.config.id2label

    # 1) token 预测回映到字符级：char_label[i]（完整 BIO 标签）/ char_prob[i]
    #    保留 B/I 前缀，供 _decode_spans 做严格的 BIO 结构解码
    char_label: dict[int, str | None] = {}
    char_prob: dict[int, float] = {}
    for tok_idx, (start, end) in enumerate(offsets):
        if start == end:  # 特殊 token（[CLS]/[SEP]）
            continue
        label = id2label.get(pred_ids[tok_idx], "O")
        prob = token_probs[tok_idx]
        for i in range(start, min(end, len(sentence))):
            char_label[i] = label
            char_prob[i] = prob

    # 2) 按 BIO 结构解码出实体跨度（_decode_spans 见下方定义）
    decoded = _decode_spans(sentence, char_label, char_prob)
    if with_position:
        return decoded
    return [(term, etype, conf) for term, etype, conf, _, _ in decoded]


def _decode_spans(
    sentence: str,
    char_label: dict[int, str | None],
    char_prob: dict[int, float],
) -> list[tuple[str, str, float, int, int]]:
    """按严格 BIO 结构把字符级标签解码成实体跨度。

    返回 (实体文本, 实体类型, 置信度, 起始位置, 结束位置)。
    与旧实现（忽略 B/I 差异、把连续同类型字符直接合并）的区别：
        - 一个实体必须由 B-xxx 开头、紧跟若干个 I-xxx 组成；
        - 遇到 O 或下一个 B-xxx（即使同类型）即断开。
    这样"头痛、恶心"两个相邻 Symptom 不会被错误吞并成"头痛、恶心"一整段，
    从源头修掉边界吞字问题。

    同时仍保留 _trim_boundary 两端虚词/标点剥离，修正"的流感疫苗"这类吞字边界。
    """
    spans: list[tuple[str, str, float, int, int]] = []
    i, n = 0, len(sentence)
    while i < n:
        label = char_label.get(i)
        if label is None or not label.startswith("B-"):
            i += 1
            continue
        entity_type = label.split("-", 1)[1]
        j = i + 1
        while j < n and char_label.get(j) == f"I-{entity_type}":
            j += 1
        # 剥离两端标点/虚词，修正"的流感疫苗"这类吞字边界
        raw = sentence[i:j]
        term = _trim_boundary(raw)
        if not term:
            i = j
            continue
        # 修剪后置信度只统计保留下来的字符，避免把被剥掉的噪声概率也算进去
        lead = raw.index(term)
        s = i + lead
        e = s + len(term)
        confidence = sum(char_prob[k] for k in range(s, e)) / max(e - s, 1)
        spans.append((term, entity_type, round(confidence, 4), s, e))
        i = j
    return spans


def _make_triple(
    subject: str,
    subject_type: str,
    relation: str,
    object_: str,
    object_type: str,
    document_id: str | None,
    source_text: str,
    confidence: float,
) -> dict[str, Any]:
    """构造一条深度学习层三元组（结构与规则层一致）。"""
    return {
        "subject": subject,
        "subject_type": subject_type,
        "subject_type_label": schema.entity_type_label(subject_type),
        "relation": relation,
        "relation_label": schema.relation_label(relation),
        "object": object_,
        "object_type": object_type,
        "object_type_label": schema.entity_type_label(object_type),
        "source_document_id": document_id,
        "source_text": source_text,
        "confidence": confidence,
        "layer": "deep_learning",
    }


def _split_confident(spans: list[tuple[str, str, float]]) -> tuple[list[tuple[str, str, float]], list[tuple[str, str, float]]]:
    """把原始预测拆成「高置信实体」与「需复核的碎片/低置信实体」两组。

    判定标准（与 MIN_ENTITY_LEN / CONFIDENT_THRESHOLD 一致）：
        - 长度 >= 2 且 置信度 >= 阈值  -> 高置信（直接产出）
        - 其余（单字碎片 或 低置信）   -> 需复核（整句路由给 LLM）
    """
    confident: list[tuple[str, str, float]] = []
    uncertain: list[tuple[str, str, float]] = []
    for term, etype, conf in spans:
        if len(term) >= MIN_ENTITY_LEN and conf >= CONFIDENT_THRESHOLD:
            confident.append((term, etype, conf))
        else:
            uncertain.append((term, etype, conf))
    return confident, uncertain


def _is_pure_ascii_word(term: str) -> bool:
    """判断是否为「纯 ASCII 拉丁词」（不含中文字符）。

    例如 photophobia / DSM-IV / cytosine arabinoside 是纯 ASCII 词；
    而 1957、H2N2 这类纯数字/型号因不含字母不算在内。
    训练语料（CMeEE）几乎不覆盖英文词，模型对英文实体边界毫无概念，
    识别结果基本不可信，因此默认交给 LLM 复核而不是直接产出。
    """
    if not term:
        return False
    # 含任意 CJK 字符 => 不是纯 ASCII 词
    if any("\u4e00" <= ch <= "\u9fff" for ch in term):
        return False
    # 需至少含一个字母才算是"英文词"（纯数字如 1957 不在此列）
    return any(ch.isalpha() for ch in term)


def _split_clauses(sentence: str) -> list[tuple[int, int, str]]:
    """按标点把句子切成子句，返回 [(起始, 结束, 文本)]。

    用于「同子句优先」的主语匹配：同一子句内的疾病与客体实体，
    语义上比跨子句的更可能构成真实关系。
    """
    import re

    clauses: list[tuple[int, int, str]] = []
    start = 0
    for m in re.finditer(r"[，。；：、！？!?；;,.]", sentence):
        text = sentence[start:m.start()].strip()
        if text:
            clauses.append((start, m.start(), text))
        start = m.end()
    text = sentence[start:].strip()
    if text:
        clauses.append((start, len(sentence), text))
    return clauses


def _clause_containing(clauses: list[tuple[int, int, str]], pos: int) -> int | None:
    """返回位置 pos 所在子句的下标；不在任何子句内返回 None。"""
    for idx, (s, e, _) in enumerate(clauses):
        if s <= pos < e:
            return idx
    return None


def predict_record(record: dict[str, Any]) -> dict[str, Any]:
    """深度学习层主入口：返回 {'triples': [...], 'llm_sentences': [...]}。

    与旧 extract() 不同，本函数把「DL 不确定的句子」显式返回给编排层，
    以便路由到 LLM 层复核，而不是直接丢弃或产出低质量三元组：
        - 高置信实体 -> 组三元组放入 triples；
        - 出现碎片(单字) 或 低置信实体的句子 -> 整句加入 llm_sentences。
    """
    if not AVAILABLE:
        return {"triples": [], "llm_sentences": []}

    title = (record.get("title") or "").strip()
    document_id = record.get("document_id")
    if not title:
        return {"triples": [], "llm_sentences": []}

    try:
        tokenizer, model = _get_model()
        triples: list[dict[str, Any]] = []
        llm_sentences: list[str] = []
        for sent in _sentences_of(record):
            # 带位置解码，供「同子句主语匹配」使用
            decoded = _predict_sentence(tokenizer, model, sent, with_position=True)

            # 1) 噪声过滤：英文词（非标题内）与动词/泛指误判词（_NOISE_TERMS）直接丢弃。
            #    注意：噪声词不触发路由——它们没有信息量，不值得为它们额外调用慢速 LLM。
            #    （旧逻辑曾把"治疗/症状/患者"等高频词命中即整句路由，导致几乎每句都调 LLM，
            #     单篇从 <1s 拖到 30~60s，是本层慢的根因。）
            #    之后再做内容级过滤：剥尾部连接词/语气词 -> 词面类型自洽校验 -> Symptom 黑名单。
            #    被过滤的实体不产出三元组（LLM 层整篇抽取会补全真实实体）。
            kept: list[tuple[str, str, float, int, int]] = []
            for term, etype, conf, start, end in decoded:
                if (_is_pure_ascii_word(term) and term not in title) or term in _NOISE_TERMS:
                    continue
                term = _strip_filler_tail(term)
                if not term:
                    continue
                if etype == "Symptom" and term in _SYMPTOM_BLACKLIST:
                    continue
                if not _plausible_entity(term, etype):
                    continue
                kept.append((term, etype, conf, start, end))

            # 2) 置信度分层：高置信实体 -> 产出三元组；碎片/低置信实体 -> 仅作路由信号。
            #    注意：只对高置信实体组三元组，低置信实体仅作为"DL 不确定"的路由信号。
            confident: list[tuple[str, str, float, int, int]] = []
            uncertain: list[tuple[str, str, float, int, int]] = []
            for term, etype, conf, start, end in kept:
                if len(term) >= MIN_ENTITY_LEN and conf >= CONFIDENT_THRESHOLD:
                    confident.append((term, etype, conf, start, end))
                else:
                    uncertain.append((term, etype, conf, start, end))

            # 3) 主语关联约束（替代旧的"句内全部 Disease × 全部客体"笛卡尔积）：
            #    - 句内高置信 Disease 作主语候选；
            #    - 同一子句内的 Disease 优先（语义上更可能构成真实关系）；
            #    - 子句内无 Disease 则回退整句 Disease；
            #    - 整句都没有 Disease 时，若文档标题是疾病名则兜底用标题作主语；
            #    - 以上都不满足 -> 不产出，由第 4 步的路由判定统一决定是否交 LLM
            # 注意：推导式解包变量名必须与过滤条件一致，避免与外层循环变量冲突
            diseases = [
                (term, etype, conf, start, end)
                for term, etype, conf, start, end in confident
                if etype == "Disease"
            ]
            # 标题疾病名兜底：仅当标题看起来像疾病时才允许作主语，位置用 -1 表示"标题"
            title_subject = (title, "Disease", 0.8, -1, -1) if _is_disease_title(title) else None
            clauses = _split_clauses(sent)
            sent_count = 0  # 本句 DL 产出的三元组数，供路由判定
            for term, entity_type, conf, start, end in confident:
                relation = TYPE_TO_RELATION.get(entity_type)
                if not relation or not term:
                    continue
                obj_clause = _clause_containing(clauses, start)
                same_clause = [d for d in diseases if _clause_containing(clauses, d[3]) == obj_clause]
                if same_clause:
                    subj_pool = same_clause
                elif diseases:
                    subj_pool = diseases
                elif title_subject is not None:
                    subj_pool = [title_subject]
                else:
                    # 无疾病主语且标题非疾病名：本句不产出，交给第 4 步路由判定
                    continue
                for dterm, _, dconf, _, _ in subj_pool:
                    if dterm == term:  # 自环过滤：主语与客体相同无意义
                        continue
                    triples.append(_make_triple(dterm, "Disease", relation, term, entity_type,
                                                document_id, sent, round(conf * dconf, 4)))
                    sent_count += 1

            # 4) 路由判定（"DL 做不好"才交给 LLM）：
            #    仅当本句 DL 一条三元组都产不出，且句中仍存在实体信号
            #    （高置信客体、缺主语，或低置信/碎片实体）时才路由 LLM 复核。
            #    噪声词与 DL 已能产出的句子不再白白消耗 LLM 调用，是提速的关键。
            if sent_count == 0 and (confident or uncertain):
                if sent not in llm_sentences:
                    llm_sentences.append(sent)
        return {"triples": triples, "llm_sentences": llm_sentences}
    except Exception as exc:  # 推理异常时降级，不中断整体流水线
        print(f"WARN: 深度学习层推理失败（{exc}），本层返回空结果", file=sys.stderr)
        return {"triples": [], "llm_sentences": []}


def extract(record: dict[str, Any]) -> list[dict[str, Any]]:
    """兼容旧接口：只返回深度学习层确定产出的三元组（不包含路由信号）。"""
    return predict_record(record)["triples"]