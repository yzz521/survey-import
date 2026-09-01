# -*- coding: utf-8 -*-
"""批量导入命令行入口（含失败自动重试）。

用法：
    python tools/batch_run.py              # 导入所有「有问卷且未处理」的门店
    python tools/batch_run.py --retry-failed   # 只重试上次失败的
    python tools/batch_run.py --concurrency 5
"""
from __future__ import annotations

import argparse
import json
import sys
import time

import requests

BASE = "http://127.0.0.1:8765"


def api_get(path, **kw):
    return requests.get(BASE + path, timeout=30, **kw).json()


def run_batch(codes, concurrency=3, submit=False, force=False, label=""):
    if not codes:
        return {}
    r = requests.post(BASE + "/api/run", timeout=30, json={
        "codes": codes, "concurrency": concurrency, "submit": submit, "force": force,
    }).json()
    if not r.get("ok"):
        print(f"启动失败：{r.get('error')}")
        return {}
    print(f"{label}启动 {len(codes)} 家（并发 {concurrency}）")
    t0, last = time.time(), -1
    while True:
        j = api_get("/api/stats")["job"]
        if j["done"] != last:
            last = j["done"]
            print(f"  {last}/{j['total']}  成功 {j['ok']}  有缺失 {j['warn']}  "
                  f"失败 {j['fail']}  跳过 {j['skip']}   [{int(time.time()-t0)}s]", flush=True)
        if not j["running"]:
            break
        if time.time() - t0 > 3600:
            print("  !! 超时中断")
            break
        time.sleep(4)
    print(f"  本轮用时 {(time.time()-t0)/60:.1f} 分钟", flush=True)
    return api_get("/api/stats")["stats"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--submit", action="store_true", help="导入后直接提交（不可逆）")
    ap.add_argument("--force", action="store_true", help="覆盖已提交的报告")
    ap.add_argument("--retry-failed", action="store_true", help="只重试上次失败的")
    ap.add_argument("--retries", type=int, default=2, help="失败重试轮数")
    args = ap.parse_args()

    rows = api_get("/api/stores")["rows"]
    has_file = [r for r in rows if r["has_file"]]

    if args.retry_failed:
        codes = [r["code"] for r in has_file if r["last_status"] == "failed"]
    else:
        codes = [r["code"] for r in has_file if not r["last_status"]]

    print(f"清单 {len(rows)} 家 / 有问卷 {len(has_file)} 家 / 本轮待处理 {len(codes)} 家")
    if not codes:
        print("没有需要处理的门店")
        return

    run_batch(codes, args.concurrency, args.submit, args.force, label="第 1 轮：")

    for i in range(args.retries):
        rows = api_get("/api/stores")["rows"]
        failed = [r["code"] for r in rows if r["has_file"] and r["last_status"] == "failed"]
        if not failed:
            break
        print(f"\n有 {len(failed)} 家失败，准备第 {i+2} 轮重试…")
        time.sleep(3)
        run_batch(failed, max(1, args.concurrency - 1), args.submit, args.force,
                  label=f"第 {i+2} 轮：")

    # 汇总
    rows = api_get("/api/stores")["rows"]
    hf = [r for r in rows if r["has_file"]]
    from collections import Counter
    c = Counter(r["last_status"] or "未处理" for r in hf)
    print("\n" + "=" * 56)
    print("最终汇总（有问卷的门店）")
    print("=" * 56)
    for k, v in c.most_common():
        print(f"  {k:10s} {v} 家")
    still = [r for r in hf if r["last_status"] == "failed"]
    if still:
        print(f"\n仍失败的 {len(still)} 家：")
        for r in still[:20]:
            print(f"  {r['code']} {r['name']} → {(r['last_message'] or '')[:120]}")


if __name__ == "__main__":
    main()
