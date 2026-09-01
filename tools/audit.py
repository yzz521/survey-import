# -*- coding: utf-8 -*-
"""全量对账：逐题比对每家店的「Excel 原值」与「服务器落库值」。

分类每道题的归属：
  match           值完全一致
  mismatch        值不一致（需人工核查，重点关注）
  no_such_question  Excel 有、但线上问卷没有这道题（忽略）
  hidden_by_logic  因跳转逻辑不可见而未提交（符合业务规则）
  manual           照片/定位类，需人工补填
  empty            Excel 里就没填

用法：python tools/audit.py [--concurrency 5]
"""
from __future__ import annotations

import argparse
import csv
import json
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.client import SurveyClient
from core.mapper import MANUAL_TYPES, build_answers, to_api_answers
from core.parser import index_questionnaire_dir, parse_store_list, parse_survey_file

BASE = "http://127.0.0.1:8765"


def norm_txt(v) -> str:
    return str(v).strip() if v is not None else ""


def compare_value(m, srv: dict) -> bool:
    """比对单个 MatchResult 与服务器答案的值是否一致。"""
    p = m.answer_payload or {}
    if p.get("selected_option_ids"):
        a = sorted(str(x) for x in p["selected_option_ids"])
        b = sorted(str(x) for x in (srv.get("selected_option_ids") or []))
        return a == b
    if p.get("text_value") is not None:
        return norm_txt(p["text_value"]) == norm_txt(srv.get("text_value"))
    if p.get("number_value") is not None:
        try:
            return abs(float(p["number_value"]) - float(srv.get("number_value") or 0)) < 1e-9
        except (TypeError, ValueError):
            return False
    if p.get("date_value"):
        # 服务器可能返回带时间的完整串，只取日期部分
        return norm_txt(p["date_value"])[:10] == norm_txt(srv.get("date_value"))[:10]
    if p.get("time_value"):
        # Excel '18:03' vs 服务器 '18:03:00'，只取时分
        return norm_txt(p["time_value"])[:5] == norm_txt(srv.get("time_value"))[:5]
    return True


def audit_one(task, qfile: Path) -> dict:
    cli = SurveyClient(task.url, task.tenant_key, task.public_id, task.access_code)
    try:
        cli.unlock()
        q = cli.questionnaire()
        questions = q.get("questions") or []
        qmap = {x["id"]: x for x in questions}
        srv_answers = {a["question"]: a for a in
                       (cli.get_draft().get("answers") or [])}

        parsed = parse_survey_file(qfile)
        rep = build_answers(parsed, questions)

        res = {"code": task.store_code, "name": task.store_name,
               "counts": Counter(), "mismatches": []}

        for m in rep.items:
            # 照片/定位先归类（即使 Excel 没填，也属「待人工补填」）
            if m.manual or m.qtype in MANUAL_TYPES:
                res["counts"]["manual"] += 1
                continue
            if m.match_kind == "absent":
                continue  # 线上有、Excel 没有 → 不算差异
            if not m.excel_answer:
                res["counts"]["empty"] += 1
                continue
            if not m.visible:
                res["counts"]["hidden_by_logic"] += 1
                continue

            srv = srv_answers.get(m.question_id)
            if srv is None:
                res["counts"]["missing_on_server"] += 1
                res["mismatches"].append({
                    "qid": m.question_id, "type": m.qtype,
                    "text": m.question_text[:60],
                    "excel": str(m.excel_answer)[:80], "server": "<未写入>",
                })
                continue
            if compare_value(m, srv):
                res["counts"]["match"] += 1
            else:
                res["counts"]["mismatch"] += 1
                srv_show = (srv.get("selected_option_ids")
                            or srv.get("text_value")
                            or srv.get("number_value")
                            or srv.get("date_value")
                            or srv.get("time_value"))
                res["mismatches"].append({
                    "qid": m.question_id, "type": m.qtype,
                    "text": m.question_text[:60],
                    "excel": str(m.excel_answer)[:80],
                    "server": str(srv_show)[:80],
                })
        res["counts"]["no_such_question"] = len(rep.unmatched_excel)
        return res
    finally:
        cli.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--concurrency", type=int, default=5)
    ap.add_argument("--codes", nargs="*", help="只核对指定门店")
    args = ap.parse_args()

    cfg = requests.get(BASE + "/api/config", timeout=20).json()
    list_path, q_dir = cfg["list_path"], cfg["q_dir"]
    tasks = {t.store_code: t for t in parse_store_list(list_path)}
    files = index_questionnaire_dir(q_dir)

    rows = requests.get(BASE + "/api/stores", timeout=30).json()["rows"]
    done = [r for r in rows if r["has_file"] and r["last_status"] in ("ok", "warning")]
    if args.codes:
        done = [r for r in done if r["code"] in set(args.codes)]
    print(f"待对账 {len(done)} 家（并发 {args.concurrency}）\n")

    total = Counter()
    all_mismatch = []
    per_store = []
    t0 = time.time()

    def work(r):
        return r["code"], audit_one(tasks[r["code"]], files[r["code"]])

    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futs = [ex.submit(work, r) for r in done]
        for i, fu in enumerate(as_completed(futs), 1):
            code, res = fu.result()
            total.update(res["counts"])
            for mm in res["mismatches"]:
                all_mismatch.append({"code": code, "name": res["name"], **mm})
            per_store.append({"code": code, "name": res["name"],
                              **{k: v for k, v in res["counts"].items()}})
            if i % 20 == 0 or i == len(done):
                print(f"  {i}/{len(done)}  已比 {total['match']} 题，"
                      f"不一致 {total['mismatch']} 题  [{int(time.time()-t0)}s]", flush=True)

    print(f"\n对账完成，用时 {(time.time()-t0)/60:.1f} 分钟\n")
    print("=" * 62)
    print("全量对账汇总（按题目归类）")
    print("=" * 62)
    labels = [("match", "值完全一致"),
              ("mismatch", "★ 值不一致（需核查）"),
              ("hidden_by_logic", "跳转逻辑隐藏（正常）"),
              ("manual", "照片/定位（待人工）"),
              ("no_such_question", "线上无此题（正常忽略）"),
              ("empty", "Excel 未填"),
              ("missing_on_server", "★ 未写入服务器")]
    for k, lab in labels:
        print(f"  {lab:26s} {total.get(k, 0):6d}")

    out = Path(__file__).resolve().parent.parent / "导出结果"
    try:
        out.mkdir(parents=True, exist_ok=True)
    except (PermissionError, FileExistsError, OSError):
        pass  # 目录已存在即可
    ts = time.strftime("%Y%m%d_%H%M")

    if all_mismatch:
        p = out / f"值不一致明细_{ts}.csv"
        with p.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, ["code", "name", "qid", "type", "text", "excel", "server"])
            w.writeheader()
            w.writerows(all_mismatch)
        print(f"\n⚠️  值不一致明细已导出：{p.name}（{len(all_mismatch)} 条）")
    else:
        print("\n✅ 未发现任何值不一致——Excel 与服务器数据完全一致")

    p2 = out / f"逐店对账_{ts}.csv"
    with p2.open("w", newline="", encoding="utf-8-sig") as f:
        keys = ["code", "name", "match", "mismatch", "hidden_by_logic", "manual",
                "no_such_question", "empty", "missing_on_server"]
        w = csv.DictWriter(f, keys, extrasaction="ignore")
        w.writeheader()
        for row in sorted(per_store, key=lambda x: x["code"]):
            w.writerow(row)
    print(f"逐店对账已导出：{p2.name}（{len(per_store)} 家）")


if __name__ == "__main__":
    main()
