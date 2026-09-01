# -*- coding: utf-8 -*-
"""导入编排：清单 -> 问卷文件 -> 线上问卷草稿。"""
from __future__ import annotations

import traceback
from dataclasses import dataclass, field
from pathlib import Path

from .client import SurveyAPIError, SurveyClient, parse_media_id
from .logic import compute_visibility
from .mapper import base_answer, build_answers, has_answer_value, to_api_answers, _has_value
from .parser import index_questionnaire_dir, parse_store_list, parse_survey_file, UnsupportedFormat
from .photos import classify_question, file_for_role, index_photo_dir
from . import store as db

# 可继续填写的状态
EDITABLE = {"", "active", "issued", "unlocked", "editable", "draft", "needs_revision"}
# 已提交，默认不再覆盖
SUBMITTED = {"submitted", "submitted_for_review", "resubmitted",
             "resubmitted_for_review", "pending_review"}


@dataclass
class StoreView:
    code: str
    name: str
    region: str
    wave: str
    url: str
    has_file: bool
    file_name: str = ""
    has_photo: bool = False
    photo_summary: str = ""
    last_status: str = ""
    last_message: str = ""
    detail: dict = field(default_factory=dict)


class Importer:
    def __init__(self, list_path: str, q_dir: str, photo_dir: str = "", logger=None):
        self.list_path = str(list_path)
        self.q_dir = str(q_dir)
        self.photo_dir = str(photo_dir or "")
        self.log = logger or (lambda *a, **k: None)
        self._tasks: dict[str, object] = {}
        self._files: dict[str, Path] = {}
        self._photos: dict = {}
        self._loaded = False

    # ------------------------------------------------ 载入

    def reload(self) -> dict:
        tasks = parse_store_list(self.list_path)
        self._tasks = {t.store_code: t for t in tasks}
        self._files = index_questionnaire_dir(self.q_dir)
        self._photos = index_photo_dir(self.photo_dir) if self.photo_dir else {}
        self._loaded = True
        matched = sum(1 for c in self._tasks if c in self._files)
        with_photo = sum(1 for c in self._tasks if c in self._photos)
        self.log("info",
                 f"清单载入 {len(self._tasks)} 家门店，问卷目录 {len(self._files)} 份，可匹配 {matched} 家"
                 + (f"，照片目录 {len(self._photos)} 家有图（清单内 {with_photo} 家）"
                    if self.photo_dir else ""))
        return {"stores": len(self._tasks), "files": len(self._files),
                "matched": matched, "photos": len(self._photos),
                "photos_matched": with_photo}

    def _ensure(self):
        if not self._loaded:
            self.reload()

    def stores(self) -> list[StoreView]:
        self._ensure()
        last = {r["store_code"]: r for r in db.latest_runs(limit=100000)}
        views = []
        for code, t in self._tasks.items():
            p = self._files.get(code)
            ph = self._photos.get(code)
            r = last.get(code)
            photo_bits = []
            if ph:
                if ph.storefront:
                    photo_bits.append("门头照")
                if ph.negative:
                    photo_bits.append("形象负面")
                if ph.gps:
                    photo_bits.append("含GPS")
            views.append(StoreView(
                code=code, name=t.store_name, region=t.region, wave=t.wave,
                url=t.url, has_file=p is not None, file_name=p.name if p else "",
                has_photo=ph is not None and bool(ph.files),
                photo_summary="、".join(photo_bits),
                last_status=r["status"] if r else "",
                last_message=(r["message"] or "") if r else "",
            ))
        # 有问卷文件的排前面
        views.sort(key=lambda v: (not v.has_file, v.code))
        return views

    def get_task(self, code: str):
        self._ensure()
        return self._tasks.get(code)

    def get_file(self, code: str):
        self._ensure()
        return self._files.get(code)

    def _photo_plan(self, code: str, questions: list[dict]) -> list[dict]:
        """预览用：本地照片能填哪些题（不上传）。"""
        ph = self._photos.get(code)
        t = self._tasks.get(code)
        plan = []
        for q in questions:
            qtype = str(q.get("type") or "").lower()
            role = classify_question(q.get("text") or "", qtype)
            if not role:
                continue
            rec = {
                "id": q["id"],
                "text": (q.get("text") or "")[:60],
                "type": qtype,
                "role": role,
                "required": bool(q.get("required")),
                "file": "",
                "gps": None,
                "status": "missing",
            }
            if role == "location":
                if ph and ph.gps:
                    rec["gps"] = [round(ph.gps[0], 6), round(ph.gps[1], 6)]
                    rec["file"] = ph.gps_from
                    rec["status"] = "ready"
                    rec["note"] = f"将从 {ph.gps_from} 的 EXIF 写入定位"
                else:
                    rec["note"] = "本地照片无 GPS，需人工在系统内定位"
            else:
                f = file_for_role(ph, role)
                if f:
                    rec["file"] = f.name
                    rec["status"] = "ready"
                    rec["note"] = f"将上传 {f.name}"
                elif role == "negative":
                    rec["status"] = "optional"
                    rec["note"] = "未找到「店铺形象负面照片」（无违规时可空）"
                else:
                    rec["note"] = "本地无对应照片，需人工补传"
            if t and role == "location" and rec["status"] == "ready" and t.address:
                rec["address"] = t.address
            plan.append(rec)
        return plan

    def _apply_local_media(self, code: str, questions: list[dict], rep,
                           cli: SurveyClient, draft_answers: dict) -> list[str]:
        """上传本地照片、写入定位，更新 MatchResult.payload。返回日志行。"""
        ph = self._photos.get(code)
        t = self._tasks.get(code)
        logs: list[str] = []

        for m in rep.items:
            if not m.visible:
                continue
            role = classify_question(m.question_text, m.qtype)
            if not role:
                continue

            # 草稿里已经有值：不重复上传，避免冲掉人工补的
            old = draft_answers.get(m.question_id) or {}
            if m.qtype == "location":
                if old.get("location_lat") is not None and old.get("location_lng") is not None:
                    m.answer_payload["location_lat"] = old["location_lat"]
                    m.answer_payload["location_lng"] = old["location_lng"]
                    if old.get("location_address"):
                        m.answer_payload["location_address"] = old["location_address"]
                    m.manual = False
                    m.note = "草稿已有定位，沿用"
                    logs.append(m.note)
                    continue
                if ph and ph.gps:
                    lat, lng = ph.gps
                    m.answer_payload["location_lat"] = lat
                    m.answer_payload["location_lng"] = lng
                    addr = (t.address if t else "") or ""
                    if addr:
                        m.answer_payload["location_address"] = addr
                    m.manual = False
                    m.note = f"定位取自 {ph.gps_from} EXIF"
                    logs.append(m.note)
                continue

            if old.get("media_file_ids"):
                m.answer_payload["media_file_ids"] = list(old["media_file_ids"])
                if old.get("media_files"):
                    m.answer_payload["media_files"] = old["media_files"]
                m.manual = False
                m.note = "草稿已有照片，沿用"
                logs.append(m.note)
                continue

            f = file_for_role(ph, role)
            if not f:
                continue
            try:
                payload = cli.upload_media(m.question_id, f, media_type="image")
                mid = parse_media_id(payload)
                if mid is None:
                    logs.append(f"{m.question_text[:20]} 上传成功但未返回文件编号")
                    self.log("warn", f"[{code}] 上传 {f.name} 未返回 media id: {str(payload)[:180]}")
                    continue
                m.answer_payload["media_file_ids"] = [mid]
                m.manual = False
                m.note = f"已上传 {f.name}"
                logs.append(m.note)
                self.log("info", f"[{code}] 已上传 {f.name} → Q{m.question_id}")
            except SurveyAPIError as e:
                logs.append(f"{f.name} 上传失败：{e}")
                self.log("error", f"[{code}] 上传 {f.name} 失败：{e}")
        return logs

    def _merge_draft(self, draft: dict, excel_answers: list[dict],
                     questions: list[dict]) -> list[dict]:
        """Excel 答案覆盖对应题；保留草稿里已有的照片/定位（Excel 没覆盖到的）。"""
        qtypes = {q["id"]: str(q.get("type") or "").lower() for q in questions}
        merged = {}
        for a in (draft.get("answers") or []):
            qid = a.get("question")
            if qid is None:
                continue
            merged[qid] = a
        for a in excel_answers:
            qid = a.get("question")
            if qid is None:
                continue
            old = merged.get(qid) or {}
            new = dict(a)
            # Excel 文本答案不要把已有媒体冲掉
            if not new.get("media_file_ids") and old.get("media_file_ids"):
                new["media_file_ids"] = old["media_file_ids"]
                if old.get("media_files"):
                    new["media_files"] = old["media_files"]
            if new.get("location_lat") is None and old.get("location_lat") is not None:
                new["location_lat"] = old["location_lat"]
                new["location_lng"] = old.get("location_lng")
                new["location_address"] = old.get("location_address")
            merged[qid] = new
        # 丢掉线上已不可见、且本次也没值的题——由 to_api_answers 已经按可见性筛过
        # 这里只返回合并后的列表
        out = []
        for qid, a in merged.items():
            qt = qtypes.get(qid, "")
            if qt == "title":
                continue
            out.append(a)
        return out

    # ------------------------------------------------ 预览（不写入）

    def preview(self, code: str) -> dict:
        self._ensure()
        t = self._tasks.get(code)
        f = self._files.get(code)
        if not t:
            return {"ok": False, "error": "清单中找不到该门店"}
        if not f:
            return {"ok": False, "error": "问卷目录中找不到对应文件"}
        cli = SurveyClient(t.url, t.tenant_key, t.public_id, t.access_code,
                           logger=lambda *a, **k: None)
        try:
            info = cli.unlock()
            q = cli.questionnaire()
            parsed = parse_survey_file(f)
            rep = build_answers(parsed, q.get("questions") or [])
            photo_plan = self._photo_plan(code, q.get("questions") or [])
            # 预览里把「本地能填」的题从人工补填里剔除（实际导入时才会上传）
            ready_ids = {p["id"] for p in photo_plan if p["status"] == "ready"}
            still_manual = [
                m for m in rep.manual_required
                if m.question_id not in ready_ids and m.required
            ]
            answers = to_api_answers(rep)
            total = len(q.get("questions") or [])
            return {
                "ok": True,
                "store_code": code,
                "store_name": t.store_name,
                "server_store": f"{info.store.get('code','')} {info.store.get('name','')}".strip(),
                "title": info.questionnaire_title,
                "status": info.status,
                "entry_mode": info.entry_mode,
                "total_questions": total,
                "answerable": len(answers),
                "manual_required": len(still_manual),
                "manual_questions": [
                    {"id": m.question_id, "text": m.question_text[:60], "type": m.qtype,
                     "required": m.required}
                    for m in still_manual
                ],
                "photo_plan": photo_plan,
                "photo_ready": sum(1 for p in photo_plan if p["status"] == "ready"),
                "missing_required": [
                    {"id": m.question_id, "text": m.question_text[:60], "type": m.qtype}
                    for m in rep.missing_required
                ],
                "unmatched_excel": [
                    {"text": it.raw_question[:60], "answer": (it.answer or "")[:40]}
                    for it in rep.unmatched_excel
                ],
                "skipped_sections": list(rep.skipped_sections),
                "notes": [
                    {"id": m.question_id, "text": m.question_text[:60], "note": m.note}
                    for m in rep.items if m.note and not m.manual
                ],
                "match_kinds": _count_kinds(rep),
            }
        except SurveyAPIError as e:
            return {"ok": False, "error": str(e), "code": e.code}
        except Exception as e:
            return {"ok": False, "error": f"{e}\n{traceback.format_exc()[-500:]}"}
        finally:
            cli.close()

    # ------------------------------------------------ 执行导入

    def import_one(self, code: str, force: bool = False, do_submit: bool = False) -> dict:
        self._ensure()
        t = self._tasks.get(code)
        f = self._files.get(code)
        if not t:
            return self._fail(code, "", "", "清单中找不到该门店")
        if not f:
            return self._fail(code, t.store_name, t.wave, "问卷目录中找不到对应文件")

        cli = SurveyClient(t.url, t.tenant_key, t.public_id, t.access_code)
        try:
            info = cli.unlock()
            if info.status in SUBMITTED and not force:
                db.record_run(code, t.store_name, t.wave, "skipped",
                              f"该报告已提交（{info.status}），已跳过；如需覆盖请勾选强制",
                              submitted=1)
                return {"ok": True, "status": "skipped", "store_code": code,
                        "message": "已提交，跳过"}

            q = cli.questionnaire()
            questions = q.get("questions") or []
            parsed = parse_survey_file(f)
            rep = build_answers(parsed, questions)

            draft = cli.get_draft()
            draft_map = {a["question"]: a for a in (draft.get("answers") or [])
                         if a.get("question") is not None}
            photo_logs = self._apply_local_media(code, questions, rep, cli, draft_map)
            still_manual = [
                m for m in rep.items
                if m.manual and m.visible and m.required and not _has_value(m.answer_payload)
            ]
            rep.manual_required = still_manual
            answers = to_api_answers(rep)

            # 上传可能改了草稿版本，保存前再读一次
            draft = cli.get_draft()
            answers = self._merge_draft(draft, answers, questions)
            answers_map = {a["question"]: a for a in answers}
            for qu in questions:
                answers_map.setdefault(qu["id"], base_answer(qu["id"]))
            vis = compute_visibility(questions, answers_map)
            qtypes = {qu["id"]: str(qu.get("type") or "").lower() for qu in questions}
            answers = [
                a for a in answers
                if vis.get(a.get("question"), {}).get("visible", True)
                and has_answer_value(a, qtypes.get(a.get("question"), ""))
            ]

            revision = draft.get("draft_revision", 0)
            res = cli.save_draft(
                answers,
                draft_revision=revision,
                questionnaire_version_id=q.get("version_id") or q.get("questionnaire_version_id"),
                current_page_id=draft.get("current_page_id"),
            )
            body = res.get("draft") or res.get("submission") or res
            new_rev = body.get("draft_revision", revision)

            submitted = False
            if do_submit:
                cli.submit(new_rev, current_page_id=draft.get("current_page_id"))
                submitted = True

            missing = rep.missing_required
            extra = ("；" + "，".join(photo_logs[:4])) if photo_logs else ""
            if still_manual:
                extra += f"；仍需人工 {len(still_manual)} 题"
            status = "submitted" if submitted else ("warning" if missing else "ok")
            if submitted:
                msg = "已保存并提交（提交后需审核打回才能再改）" + extra
            elif missing:
                msg = f"草稿已保存，但有 {len(missing)} 道必答题未填" + extra
            else:
                msg = "草稿已保存" + extra

            db.record_run(
                code, t.store_name, t.wave, status, msg,
                matched=len(answers), total=len(questions),
                missing_required=len(missing), manual_required=len(rep.manual_required),
                submitted=1 if submitted else 0,
                detail={
                    "unmatched_excel": [it.raw_question[:60] for it in rep.unmatched_excel],
                    "missing_required": [
                        {"id": m.question_id, "text": m.question_text[:80]} for m in missing
                    ],
                    "manual_required": [
                        {"id": m.question_id, "text": m.question_text[:80], "type": m.qtype}
                        for m in rep.manual_required
                    ],
                    "answers": len(answers),
                },
            )
            return {
                "ok": True, "status": status, "store_code": code, "store_name": t.store_name,
                "message": msg, "answers": len(answers), "total": len(questions),
                "missing_required": len(missing), "manual_required": len(rep.manual_required),
            }
        except SurveyAPIError as e:
            return self._fail(code, t.store_name, t.wave, str(e))
        except Exception as e:
            return self._fail(code, t.store_name, t.wave,
                              f"{e}\n{traceback.format_exc()[-400:]}")
        finally:
            cli.close()

    def _fail(self, code, name, wave, msg) -> dict:
        self.log("error", f"[{code}] {msg[:200]}")
        db.record_run(code, name, wave, "failed", msg[:400])
        return {"ok": False, "status": "failed", "store_code": code, "message": msg[:400]}

    # ------------------------------------------------ 提交相关（不重新导入）

    def check_ready(self, code: str) -> dict:
        """评估单家店草稿是否满足提交条件，返回阻塞清单。"""
        self._ensure()
        t = self._tasks.get(code)
        if not t:
            return {"ok": False, "error": "清单中找不到该门店", "code": code}
        cli = SurveyClient(t.url, t.tenant_key, t.public_id, t.access_code)
        try:
            info = cli.unlock()
            if info.status in SUBMITTED:
                return {"ok": True, "code": code, "name": t.store_name,
                        "status": "submitted", "blocking": [],
                        "draft_revision": cli.get_draft().get("draft_revision", 0)}
            q = cli.questionnaire()
            questions = q.get("questions") or []
            answers = (cli.get_draft().get("answers") or [])
            answers_map = {a["question"]: a for a in answers}
            for qu in questions:
                answers_map.setdefault(qu["id"], base_answer(qu["id"]))
            vis = compute_visibility(questions, answers_map)
            blocking = []
            for qu in questions:
                v = vis.get(qu["id"], {"visible": True, "required": bool(qu.get("required"))})
                if not (v["visible"] and v["required"]):
                    continue
                if not has_answer_value(answers_map.get(qu["id"]), qu["type"]):
                    blocking.append({"qid": qu["id"], "type": qu["type"],
                                     "text": (qu.get("text") or "")[:60]})
            return {"ok": True, "code": code, "name": t.store_name,
                    "status": "ready" if not blocking else "blocked",
                    "blocking": blocking,
                    "draft_revision": cli.get_draft().get("draft_revision", 0)}
        except SurveyAPIError as e:
            return {"ok": False, "code": code, "error": str(e), "code_api": e.code}
        finally:
            cli.close()

    def submit_one(self, code: str, force: bool = False) -> dict:
        """仅提交、不重新导入；自动做就绪度预检。"""
        self._ensure()
        t = self._tasks.get(code)
        if not t:
            return {"ok": False, "status": "failed", "store_code": code,
                    "message": "清单中找不到该门店"}
        cli = SurveyClient(t.url, t.tenant_key, t.public_id, t.access_code)
        try:
            info = cli.unlock()
            if info.status in SUBMITTED and not force:
                db.record_run(code, t.store_name, t.wave, "skipped",
                              "已提交，跳过（勾选强制可覆盖）", submitted=1)
                return {"ok": True, "status": "skipped", "store_code": code,
                        "message": "已提交，跳过"}

            q = cli.questionnaire()
            questions = q.get("questions") or []
            draft = cli.get_draft()
            answers_map = {a["question"]: a for a in (draft.get("answers") or [])}
            for qu in questions:
                answers_map.setdefault(qu["id"], base_answer(qu["id"]))
            vis = compute_visibility(questions, answers_map)
            blocking = []
            for qu in questions:
                v = vis.get(qu["id"], {"visible": True, "required": bool(qu.get("required"))})
                if not (v["visible"] and v["required"]):
                    continue
                if not has_answer_value(answers_map.get(qu["id"]), qu["type"]):
                    blocking.append({"qid": qu["id"], "type": qu["type"],
                                     "text": (qu.get("text") or "")[:60]})
            if blocking and not force:
                db.record_run(code, t.store_name, t.wave, "blocked",
                              f"草稿尚有 {len(blocking)} 道必填项未补（照片/定位/录像等）",
                              detail={"blocking": blocking})
                return {"ok": True, "status": "blocked", "store_code": code,
                        "message": f"草稿尚有 {len(blocking)} 道必填项未补",
                        "blocking": blocking}

            revision = draft.get("draft_revision", 0)
            cli.submit(revision, current_page_id=draft.get("current_page_id"))
            db.record_run(code, t.store_name, t.wave, "submitted",
                          "已提交送审", submitted=1)
            return {"ok": True, "status": "submitted", "store_code": code,
                    "message": "已提交送审"}
        except SurveyAPIError as e:
            return self._fail(code, t.store_name, t.wave, str(e))
        except Exception as e:
            return self._fail(code, t.store_name, t.wave,
                              f"{e}\n{traceback.format_exc()[-400:]}")
        finally:
            cli.close()

    # ------------------------------------------------ 批量

    def import_many(self, codes: list[str], force: bool = False,
                    do_submit: bool = False, sleep: float = 0.6):
        import time
        total = len(codes)
        for i, code in enumerate(codes, 1):
            self.log("info", f"({i}/{total}) 开始导入 {code}")
            r = self.import_one(code, force=force, do_submit=do_submit)
            yield i, total, r
            if i < total and sleep:
                time.sleep(sleep)


def _count_kinds(rep) -> dict:
    out: dict[str, int] = {}
    for m in rep.items:
        out[m.match_kind] = out.get(m.match_kind, 0) + 1
    return out
