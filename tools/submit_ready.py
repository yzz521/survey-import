# -*- coding: utf-8 -*-
"""提交就绪度预检：检查每家店的草稿是否已满足所有可见必答题。

提交是不可逆操作；缺任何一道 visible+required 的题都会让服务器拒绝。
这个工具只做评估，方便人工补完素材后再次跑一次。

用法：python tools/submit_ready.py [--concurrency 5]
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.importer import Importer
from core.mapper import MANUAL_TYPES

BASE = "http://127.0.0.1:8765"


def _has_value(a: dict | None, qtype: str) -> bool:
    """判断服务器答案对象是否有值（覆盖各题型）。"""
    a = a or {}
    if qtype in MANUAL_TYPES:
        if qtype == "location":
            return a.get("location_lat") is not None and a.get("location_lng") is not None
        return bool(a.get("media_file_ids"))
    if qtype in ("single", "choice", "multi"):
        return bool(a.get("selected_option_ids"))
    for k in ("text_value", "number_value", "date_value", "time_value"):
        v = a.get(k)
        if v is not None and str(v).strip() != "":
            return True
    return False


def check_one(imp, code) -> dict:
    r = imp.check_ready(code)
    if not r.get("ok"):
        return {"code": code, "name": code, "status": "error",
                "blocking": [], "error": r.get("error", "")}
    return {"code": r["code"], "name": r.get("name", ""),
            "status": r["status"], "blocking": r.get("blocking", [])}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--concurrency", type=int, default=5)
    ap.add_argument("--codes", nargs="*")
    args = ap.parse_args()

    cfg = requests.get(BASE + "/api/config", timeout=20).json()
    imp = Importer(cfg["list_path"], cfg["q_dir"],
                   photo_dir=cfg.get("photo_dir") or "")

    rows = requests.get(BASE + "/api/stores", timeout=30).json()["rows"]
    done = [r for r in rows if r["has_file"] and r["last_status"] in ("ok", "warning")]
    if args.codes:
        done = [r for r in done if r["code"] in set(args.codes)]
    print(f"待检查 {len(done)} 家（并发 {args.concurrency}）\n")

    results = []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futs = {ex.submit(check_one, imp, r["code"]): r["code"] for r in done}
        for i, fu in enumerate(as_completed(futs), 1):
            res = fu.result()
            results.append(res)
            if i % 25 == 0 or i == len(done):
                ready = sum(1 for x in results if x["status"] == "ready")
                print(f"  {i}/{len(done)}  就绪 {ready} 家  [{int(time.time()-t0)}s]",
                      flush=True)

    ready_list = [r for r in results if r["status"] == "ready"]
    blocked_list = [r for r in results if r["status"] == "blocked"]

    print(f"\n检查完成，用时 {(time.time()-t0)/60:.1f} 分钟\n")
    print("=" * 56)
    print("提交就绪度汇总")
    print("=" * 56)
    print(f"  可提交（ready）   {len(ready_list):>4} 家")
    print(f"  阻塞中（blocked）  {len(blocked_list):>4} 家")

    if not blocked_list:
        print("\n✅ 全部就绪，可在页面上直接批量提交")
        return

    # 汇总阻塞原因
    from collections import Counter
    reason = Counter()
    for r in blocked_list:
        for b in r["blocking"]:
            reason[(b["qid"], b["type"], b["text"])] += 1
    print(f"\n阻塞原因 TOP（共 {len(reason)} 种题型）:")
    for (qid, t, txt), n in reason.most_common(20):
        print(f"  {n:>3} 家缺  Q{qid} [{t}]  {txt}")
    if len(reason) > 20:
        print(f"  …另 {len(reason)-20} 种")

    out = Path(__file__).resolve().parent.parent / "导出结果"
    try:
        out.mkdir(parents=True, exist_ok=True)
    except (PermissionError, FileExistsError, OSError):
        pass
    ts = time.strftime("%Y%m%d_%H%M")
    p = out / f"提交阻塞明细_{ts}.csv"
    with p.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, ["code", "name", "status", "qid", "type", "text"])
        w.writeheader()
        for r in results:
            if not r["blocking"]:
                w.writerow({"code": r["code"], "name": r["name"],
                            "status": r["status"], "qid": "", "type": "", "text": ""})
            for b in r["blocking"]:
                w.writerow({"code": r["code"], "name": r["name"],
                            "status": r["status"], **b})
    print(f"\n明细已导出：{p.name}")
    print(f"补救方向：用「待人工补填清单」定位每家缺哪一题，进系统上传对应素材")


if __name__ == "__main__":
    main()