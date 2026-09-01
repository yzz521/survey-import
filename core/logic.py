# -*- coding: utf-8 -*-
"""问卷跳转逻辑（skip logic）可见性计算。

对齐前端 Gi / AN 的实现：题目是否可见，取决于「指向它的逻辑」在当前答案下是否命中。
不可见的题目提交答案会被服务器拒绝（VALIDATION_ERROR）。
"""
from __future__ import annotations


def to_id(v):
    """对齐前端 Dt()：能转成数字就转，否则原样返回。"""
    if v is None or v == "":
        return None
    try:
        f = float(v)
        return int(f) if f.is_integer() else f
    except (TypeError, ValueError):
        return v


def to_num(v):
    if v is None or v == "":
        return None
    try:
        f = float(v)
        return f
    except (TypeError, ValueError):
        return None


def _effect(logic: dict) -> str:
    return str(logic.get("effect") or "show").lower()


def _answer_option_ids(ans: dict) -> list:
    ids = []
    for x in (ans.get("selected_option_ids") or []):
        i = to_id(x)
        if i is not None:
            ids.append(i)
    for x in (ans.get("selected_options") or []):
        if isinstance(x, dict):
            raw = x.get("id") if x.get("id") is not None else x.get("question_id")
        else:
            raw = x
        i = to_id(raw)
        if i is not None:
            ids.append(i)
    return ids


def condition_match(logic: dict, answers: dict, src_question: dict) -> bool:
    """对齐前端 AN()：判断某条逻辑在当前答案下是否触发。"""
    src_id = to_id(logic.get("from_question_id") or logic.get("from_question")
                   or src_question.get("id"))
    ans = answers.get(src_id)
    if not ans:
        return False

    trig_opt = to_id(logic.get("trigger_option_id") if logic.get("trigger_option_id") is not None
                     else logic.get("trigger_option"))
    trig_text = str(logic.get("trigger_text") or "").strip()
    trig_num = to_num(logic.get("trigger_number"))

    ok = True
    if trig_opt is not None:
        ok = ok and (trig_opt in _answer_option_ids(ans))
    if trig_text:
        ok = ok and (trig_text.lower() in str(ans.get("text_value") or "").lower())
    if trig_num is not None:
        ok = ok and (to_num(ans.get("number_value")) == trig_num)
    # 没有任何触发条件时视为无条件命中
    if trig_opt is None and not trig_text and trig_num is None:
        return True
    return ok


def compute_visibility(questions: list[dict], answers: dict) -> dict:
    """对齐前端 Gi()：返回 {question_id: {"visible": bool, "required": bool}}。"""
    incoming: dict = {}
    for f in questions:
        for v in (f.get("outgoing_logics") or f.get("logics") or []):
            if v.get("is_active") is False:
                continue
            tgt = v.get("goto_question_id") if v.get("goto_question_id") is not None else v.get("goto_question")
            tgt = to_id(tgt)
            if tgt is None:
                continue
            incoming.setdefault(tgt, []).append((v, f))

    out: dict = {}
    for f in questions:
        qid = to_id(f.get("id"))
        lst = sorted(incoming.get(qid, []),
                     key=lambda x: (int(x[0].get("order") or 0), int(x[0].get("id") or 0)))
        has_show = any(_effect(v) == "show" for v, _ in lst)
        has_hide = any(_effect(v) == "hide" for v, _ in lst)
        has_require = any(_effect(v) == "require" for v, _ in lst)
        has_optional = any(_effect(v) == "optional" for v, _ in lst)

        visible = not has_show
        if has_hide and not has_show:
            visible = True
        required = False if (has_require or has_optional) else bool(f.get("required"))

        for v, src in lst:
            if condition_match(v, answers, src):
                e = _effect(v)
                if e == "hide":
                    visible = False
                elif e == "show":
                    visible = True
                elif e == "require":
                    required = True
                elif e == "optional":
                    required = False

        out[qid] = {"visible": visible, "required": required}
    return out
