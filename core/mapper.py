# -*- coding: utf-8 -*-
"""映射：把 Excel 问卷里的答案，翻译成线上问卷系统认识的题目 ID 与答案值。"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from datetime import datetime

from .logic import compute_visibility
from .parser import ParsedItem, norm_text

# 这些题型 Excel 里没有对应数据；有本地照片时会自动上传，否则需人工补
MANUAL_TYPES = {"image", "video", "audio", "file", "media", "location"}

# Excel 有、线上问卷没有的题，预览里不当「未匹配」报出来
EXCEL_ONLY_QUESTIONS = {"走访星期"}

OPT_SCORE_RE = re.compile(r"^(?P<label>.*?)\((?P<score>[-+0-9.]+)\)\s*$")

# 已知「Excel 有 / 线上无」但能由另一道已匹配题派生出来的字段。
# key = 规范化后的 Excel 题干；value = 派生规则 (callable(excel_answer, rep) -> str|None)
DERIVED_FROM_SIBLING: dict[str, callable] = {}


def _weekday_from_visit_date(excel_answer: str, rep: "MapReport") -> str | None:
    """从「走访日期」的答案里反推星期（中文「星期一」）。"""
    iso = _parse_date(excel_answer or "")
    if not iso:
        for m in rep.items:
            dv = (m.answer_payload or {}).get("date_value")
            if dv:
                iso = str(dv)
                break
    if not iso:
        return None
    try:
        dt = datetime.strptime(iso, "%Y-%m-%d")
    except ValueError:
        return None
    return "星期" + "一二三四五六日"[dt.weekday()]


def _install_default_derived_rules():
    """初始化默认的派生规则。"""
    DERIVED_FROM_SIBLING.clear()
    DERIVED_FROM_SIBLING["走访星期"] = _weekday_from_visit_date


_install_default_derived_rules()


@dataclass
class MatchResult:
    question_id: int
    question_text: str
    qtype: str
    required: bool
    answer_payload: dict          # 直接写进 answers[] 的字段
    excel_answer: str | None      # Excel 里的原始答案
    match_kind: str               # exact | prefix | fuzzy | absent
    manual: bool = False          # 需要人工补填
    visible: bool = True          # 跳转逻辑判定：不可见的题不能提交
    note: str = ""


@dataclass
class MapReport:
    items: list[MatchResult] = field(default_factory=list)
    skipped_sections: list[str] = field(default_factory=list)
    unmatched_excel: list[ParsedItem] = field(default_factory=list)
    manual_required: list[MatchResult] = field(default_factory=list)
    missing_required: list[MatchResult] = field(default_factory=list)
    hidden: list[MatchResult] = field(default_factory=list)

    @property
    def answerable(self) -> list[MatchResult]:
        return [m for m in self.items if not m.manual]


# ---------------------------------------------------------------- 值转换

def _parse_time(v: str) -> str | None:
    """'11时14分' / '11:14' / '11点14' -> '11:14'"""
    if not v:
        return None
    m = re.search(r"(\d{1,2})\s*(?:时|点|:|：)\s*(\d{1,2})", v)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if 0 <= h < 24 and 0 <= mi < 60:
            return f"{h:02d}:{mi:02d}"
    m = re.match(r"^(\d{2})(\d{2})$", v.strip())
    if m:
        return f"{m.group(1)}:{m.group(2)}"
    return None


def _parse_date(v: str) -> str | None:
    """'8月4日2026年' / '2026-08-04' / '2026年8月4日' -> '2026-08-04'"""
    if not v:
        return None
    s = v.strip()
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", s)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.match(r"^(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*(\d{4})\s*年?$", s)
    if m:
        return f"{int(m.group(3)):04d}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    m = re.match(r"^(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?$", s)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.match(r"^(\d{4})(\d{2})(\d{2})$", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return None


def _parse_number(v: str):
    if v is None:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", str(v).replace(",", ""))
    if not m:
        return None
    f = float(m.group(0))
    return int(f) if f.is_integer() else f


def split_option_label(raw: str) -> tuple[str, str | None]:
    """'是(2)' -> ('是', '2')；'5分(0)' -> ('5分', '0')；'星期一' -> ('星期一', None)"""
    if raw is None:
        return "", None
    s = str(raw).strip()
    m = OPT_SCORE_RE.match(s)
    if m:
        return m.group("label").strip(), m.group("score")
    return s, None


# ---------------------------------------------------------------- 选项匹配

def match_option(raw_answer: str, options: list[dict]) -> list[int]:
    """返回命中的 option id 列表。支持多选（顿号/逗号/分号分隔）。"""
    if not raw_answer or not options:
        return []
    # 先尝试整体匹配，再按分隔符拆分
    parts = [raw_answer]
    for sep in ("、", ";", "；", ",", "，"):
        if sep in raw_answer:
            parts = [p.strip() for p in raw_answer.split(sep) if p.strip()]
            break
    ids: list[int] = []
    for part in parts:
        hit = _match_single_option(part, options)
        if hit is not None:
            ids.append(hit)
    return ids


def _match_single_option(part: str, options: list[dict]) -> int | None:
    label, _score = split_option_label(part)
    cands = [norm_text(label), norm_text(label).rstrip("分"), norm_text(part)]
    # '5分' -> '5'
    stripped = re.sub(r"分$", "", norm_text(label))
    if stripped:
        cands.append(stripped)
    cands = [c for c in dict.fromkeys(cands) if c]

    for c in cands:
        for o in options:
            if norm_text(o.get("text")) == c:
                return o["id"]
    # 用 value 兜底
    for c in cands:
        for o in options:
            if norm_text(o.get("value")) == c:
                return o["id"]
    # 前缀包含
    for c in cands:
        for o in options:
            ot = norm_text(o.get("text"))
            if ot and (ot.startswith(c) or c.startswith(ot)):
                return o["id"]
    # 模糊
    texts = [norm_text(o.get("text")) for o in options]
    for c in cands:
        m = difflib.get_close_matches(c, texts, n=1, cutoff=0.82)
        if m:
            return options[texts.index(m[0])]["id"]
    return None


# ---------------------------------------------------------------- 题目匹配

def build_question_index(questions: list[dict]) -> tuple[dict[str, list[dict]], list[str]]:
    idx: dict[str, list[dict]] = {}
    for q in questions:
        idx.setdefault(norm_text(q.get("text")), []).append(q)
    return idx, list(idx.keys())


def find_question(item: ParsedItem, idx: dict[str, list[dict]], keys: list[str]):
    nq = norm_text(item.raw_question)
    if not nq:
        return None, ""
    if nq in idx:
        return idx[nq][0], "exact"
    head = nq[:18]
    cands = [k for k in keys if k.startswith(head) or (head and head in k)]
    if len(cands) == 1:
        return idx[cands[0]][0], "prefix"
    if cands:
        # 多个候选时选最长的公共前缀中最接近的
        best = max(cands, key=lambda k: difflib.SequenceMatcher(None, nq, k).ratio())
        ratio = difflib.SequenceMatcher(None, nq, best).ratio()
        if ratio >= 0.72:
            return idx[best][0], "prefix"
    m = difflib.get_close_matches(nq, keys, n=1, cutoff=0.80)
    if m:
        return idx[m[0]][0], "fuzzy"
    return None, ""


# ---------------------------------------------------------------- 主流程

def build_answers(parsed: list[ParsedItem], questions: list[dict]) -> MapReport:
    idx, keys = build_question_index(questions)
    rep = MapReport()
    used: set[int] = set()

    for item in parsed:
        q, kind = find_question(item, idx, keys)
        if q is None:
            # 分组标题 / 说明文字：既匹配不上题目、也没有答案，属于正常噪音
            if not item.answer:
                rep.skipped_sections.append(item.raw_question[:40])
                continue
            # 线上根本没有的题（如走访星期）：不当未匹配
            nq = norm_text(item.raw_question)
            if nq in EXCEL_ONLY_QUESTIONS:
                derived = None
                deriv_fn = DERIVED_FROM_SIBLING.get(nq)
                if deriv_fn:
                    derived = deriv_fn(item.answer, rep)
                extra = f"，与走访日期一致（{derived}）" if derived else ""
                rep.skipped_sections.append(
                    f"{item.raw_question[:30]}（线上无此题，将忽略{extra}）")
                continue
            # 可由兄弟题派生：派出来了就当冗余校验，不算未匹配
            deriv_fn = DERIVED_FROM_SIBLING.get(nq)
            if deriv_fn:
                derived = deriv_fn(item.answer, rep)
                if derived:
                    rep.skipped_sections.append(
                        f"{item.raw_question[:30]}（与 {derived} 一致，已忽略）")
                    continue
                rep.skipped_sections.append(
                    f"{item.raw_question[:30]}（无法从其它题派生，已忽略）")
                continue
            rep.unmatched_excel.append(item)
            continue
        qid = q["id"]
        if qid in used:
            continue
        used.add(qid)

        qtype = str(q.get("type") or "").lower()
        required = bool(q.get("required"))
        excel_ans = item.answer
        payload: dict = {}
        manual = False
        note = ""

        if qtype in MANUAL_TYPES:
            manual = True
            note = "照片/定位类题目需人工在系统内补填"
        elif qtype in ("single", "choice", "multi"):
            ids = match_option(excel_ans, q.get("options") or [])
            payload["selected_option_ids"] = ids
            if excel_ans and not ids:
                note = f"选项未识别：{excel_ans}"
        elif qtype in ("text", "short_text", "long_text"):
            payload["text_value"] = excel_ans or None
        elif qtype == "number":
            payload["number_value"] = _parse_number(excel_ans)
        elif qtype in ("date", "visit_date"):
            dv = _parse_date(excel_ans or "")
            payload["date_value"] = dv
            if excel_ans and not dv:
                note = f"日期无法解析：{excel_ans}"
        elif qtype in ("time", "visit_time"):
            tv = _parse_time(excel_ans or "")
            payload["time_value"] = tv
            if excel_ans and not tv:
                note = f"时间无法解析：{excel_ans}"
        else:
            payload["text_value"] = excel_ans or None
            note = f"未专门处理的题型 {qtype}，按文本写入"

        m = MatchResult(
            question_id=qid, question_text=q.get("text", ""), qtype=qtype,
            required=required, answer_payload=payload, excel_answer=excel_ans,
            match_kind=kind, manual=manual, note=note,
        )
        rep.items.append(m)

    # 系统里存在、但 Excel 完全没有覆盖的题目
    for q in questions:
        qid = q["id"]
        if qid in used:
            continue
        qtype = str(q.get("type") or "").lower()
        manual = qtype in MANUAL_TYPES
        m = MatchResult(
            question_id=qid, question_text=q.get("text", ""), qtype=qtype,
            required=bool(q.get("required")), answer_payload={},
            excel_answer=None, match_kind="absent", manual=manual,
            note="Excel 中未出现该题目",
        )
        rep.items.append(m)

    # 第二轮：按跳转逻辑计算可见性（依赖上面解析出的答案）
    answers_map = {}
    for m in rep.items:
        base = base_answer(m.question_id)
        base.update(m.answer_payload or {})
        answers_map[m.question_id] = base

    vis = compute_visibility(questions, answers_map)
    for m in rep.items:
        v = vis.get(m.question_id)
        if v is not None:
            m.visible = bool(v.get("visible", True))
            m.required = bool(v.get("required", m.required))

    rep.manual_required = [
        m for m in rep.items
        if m.manual and m.visible and not _has_value(m.answer_payload)
    ]
    rep.hidden = [m for m in rep.items if not m.visible]
    rep.missing_required = [
        m for m in rep.items
        if m.visible and m.required and not m.manual and not _has_value(m.answer_payload)
    ]
    return rep


def _has_value(payload: dict) -> bool:
    if payload.get("selected_option_ids"):
        return True
    if payload.get("media_file_ids"):
        return True
    if payload.get("location_lat") is not None and payload.get("location_lng") is not None:
        return True
    for k in ("text_value", "number_value", "date_value", "time_value"):
        v = payload.get(k)
        if v is not None and str(v).strip() != "":
            return True
    return False


def has_answer_value(answer: dict | None, qtype: str) -> bool:
    """判断服务器上的答案对象是否已有值（覆盖各题型）。"""
    a = answer or {}
    qt = str(qtype or "").lower()
    if qt in MANUAL_TYPES:
        if qt == "location":
            return a.get("location_lat") is not None and a.get("location_lng") is not None
        return bool(a.get("media_file_ids"))
    if qt in ("single", "choice", "multi"):
        return bool(a.get("selected_option_ids"))
    for k in ("text_value", "number_value", "date_value", "time_value"):
        v = a.get(k)
        if v is not None and str(v).strip() != "":
            return True
    return False


def base_answer(question_id: int) -> dict:
    """与前端 kf() 对齐的答案骨架。"""
    return {
        "question": question_id,
        "text_value": None,
        "number_value": None,
        "date_value": None,
        "time_value": None,
        "location_lat": None,
        "location_lng": None,
        "location_address": None,
        "selected_option_ids": [],
        "media_file_ids": [],
        "media_files": [],
    }


def to_api_answers(report: MapReport) -> list[dict]:
    """生成可直接 PUT 到 draft 接口的 answers 数组。

    跳过：跳转逻辑判定为不可见的、没有值的。
    本地照片/定位一旦写入 payload，会随草稿一起提交。
    """
    out = []
    for m in report.items:
        if not m.visible or not _has_value(m.answer_payload):
            continue
        ans = base_answer(m.question_id)
        ans.update({k: v for k, v in m.answer_payload.items()})
        out.append(ans)
    return out
