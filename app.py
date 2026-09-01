# -*- coding: utf-8 -*-
"""问卷批量导入工具 - Web 控制台。

启动：python app.py  →  http://127.0.0.1:8765
"""
from __future__ import annotations

import csv
import io
import json
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote

from flask import Flask, Response, jsonify, request, send_from_directory

from core import store as db
from core.importer import Importer
from core.parser import UnsupportedFormat

BASE = Path(__file__).resolve().parent
# 路径记在 data/imports.db 里，换机器第一次打开是空的，在页面上选即可。
DEFAULT_LIST = ""
DEFAULT_QDIR = ""
DEFAULT_PHOTO = ""

app = Flask(__name__, static_folder="static", template_folder="templates")

# ------------------------------------------------------------------ 全局状态

STATE = {
    "list_path": db.get_meta("list_path", DEFAULT_LIST),
    "q_dir": db.get_meta("q_dir", DEFAULT_QDIR),
    "photo_dir": db.get_meta("photo_dir", DEFAULT_PHOTO),
}
LOG_Q: "queue.Queue[dict]" = queue.Queue(maxsize=5000)
LOCK = threading.Lock()
CHOOSE_LOCK = threading.Lock()
JOB = {"running": False, "total": 0, "done": 0, "ok": 0, "warn": 0,
       "fail": 0, "skip": 0, "started": 0, "message": ""}


def emit(level: str, msg: str, **kw):
    rec = {"level": level, "msg": msg, "ts": time.strftime("%H:%M:%S"), **kw}
    LOG_Q.put(rec)
    return rec


def logger(level, msg):
    emit(level, msg)


def get_importer() -> Importer:
    return Importer(STATE["list_path"], STATE["q_dir"],
                    photo_dir=STATE.get("photo_dir") or "", logger=logger)


def push_progress():
    LOG_Q.put({"level": "progress", "job": dict(JOB), "ts": time.strftime("%H:%M:%S")})


# ------------------------------------------------------------------ 页面

@app.get("/")
def index():
    return send_from_directory("templates", "index.html")


# ------------------------------------------------------------------ 配置 / 数据

@app.get("/api/config")
def api_get_config():
    return jsonify({"list_path": STATE["list_path"], "q_dir": STATE["q_dir"],
                    "photo_dir": STATE.get("photo_dir") or "",
                    "exists": {"list": os.path.exists(STATE["list_path"]),
                               "qdir": os.path.isdir(STATE["q_dir"]),
                               "photos": bool(STATE.get("photo_dir")) and os.path.isdir(STATE["photo_dir"])},
                    "platform": sys.platform})


@app.get("/api/choose")
def api_choose_get():
    """GET 绝不能弹窗：浏览器预取、刷新未完成的请求、地址栏误开都会打到这里。"""
    return jsonify({"ok": False, "error": "请在页面上点「选文件/选目录」，不要直接打开此地址"}), 405


@app.post("/api/choose")
def api_choose():
    """点按钮后才弹出 macOS 文件/目录选择框。同时只允许一个。"""
    if sys.platform != "darwin":
        return jsonify({"ok": False, "error": "此功能仅支持 macOS，请手动输入路径"}), 400
    data = request.get_json(silent=True) or {}
    kind = (data.get("type") or request.args.get("type") or "file").strip()
    for_what = data.get("for") or request.args.get("for") or ""
    if kind == "dir":
        prompt = "请选择照片目录" if for_what == "photo" else "请选择问卷目录"
        inner = f"choose folder with prompt \"{prompt}\""
    else:
        inner = "choose file with prompt \"请选择 xlsx / xls / csv 表格文件\""
    # SystemUIServer：把选择框提到最前，避免藏在浏览器/Cursor 后面
    script = (
        'tell application "SystemUIServer"\n'
        '  activate\n'
        f'  set f to {inner}\n'
        '  return POSIX path of f\n'
        'end tell'
    )
    if not CHOOSE_LOCK.acquire(blocking=False):
        return jsonify({"ok": False, "error": "已有一个选择窗口打开，请先完成或取消"}), 409
    try:
        p = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "选择超时"}), 500
    finally:
        CHOOSE_LOCK.release()
    path = (p.stdout or "").strip()
    if p.returncode != 0 or not path:
        return jsonify({"ok": False, "error": "已取消" if not path else (p.stderr or "")[:200]})
    if kind == "file":
        from pathlib import Path
        from core.parser import read_table, UnsupportedFormat
        p_path = Path(path)
        if not p_path.exists():
            return jsonify({"ok": False, "error": f"所选路径不存在：{path}"}), 400
        if p_path.is_file():
            ext = p_path.suffix.lower().lstrip(".")
            if ext not in ("xlsx", "xlsm", "xls", "csv"):
                return jsonify({"ok": False, "error":
                    f"不支持的文件格式：.{ext}（仅支持 xlsx / xls / csv）"}), 400
            try:
                gen = read_table(path)
                first = next(gen, None)
                if first is None:
                    return jsonify({"ok": False, "error": "文件为空或无数据"}), 400
            except UnsupportedFormat as e:
                return jsonify({"ok": False, "error": str(e)}), 400
            except Exception as e:
                return jsonify({"ok": False, "error":
                    f"无法读取所选文件：{e}"}), 400
    return jsonify({"ok": True, "path": path})


@app.post("/api/config")
def api_set_config():
    data = request.get_json(force=True) or {}
    lp = (data.get("list_path") or "").strip()
    qd = (data.get("q_dir") or "").strip()
    pd = (data.get("photo_dir") if "photo_dir" in data else None)
    if lp:
        STATE["list_path"] = lp
        db.set_meta("list_path", lp)
    if qd:
        STATE["q_dir"] = qd
        db.set_meta("q_dir", qd)
    if pd is not None:
        STATE["photo_dir"] = pd.strip()
        db.set_meta("photo_dir", STATE["photo_dir"])
    emit("info", f"数据源已更新：清单={STATE['list_path']} 问卷目录={STATE['q_dir']}"
                 f" 照片目录={STATE.get('photo_dir') or '（未设置）'}")
    return api_reload()


@app.post("/api/reload")
def api_reload():
    if not os.path.exists(STATE["list_path"]):
        return jsonify({"ok": False, "error": f"清单文件不存在：{STATE['list_path']}"}), 400
    if not os.path.isdir(STATE["q_dir"]):
        return jsonify({"ok": False, "error": f"问卷目录不存在：{STATE['q_dir']}"}), 400
    if STATE.get("photo_dir") and not os.path.isdir(STATE["photo_dir"]):
        return jsonify({"ok": False, "error": f"照片目录不存在：{STATE['photo_dir']}"}), 400
    try:
        imp = get_importer()
        r = imp.reload()
        return jsonify({"ok": True, **r})
    except UnsupportedFormat as e:
        return jsonify({"ok": False, "error": f"清单文件格式：{e}"}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": f"{e}\n{traceback.format_exc()[-400:]}"}), 500


@app.get("/api/stores")
def api_stores():
    try:
        imp = get_importer()
        rows = [vars(v) for v in imp.stores()]
        return jsonify({"ok": True, "rows": rows, "stats": db.stats(),
                        "job": dict(JOB)})
    except Exception as e:
        return jsonify({"ok": False, "error": f"{e}\n{traceback.format_exc()[-400:]}"}), 500


@app.get("/api/stats")
def api_stats():
    return jsonify({"ok": True, "stats": db.stats(), "job": dict(JOB)})


@app.get("/api/preview/<code>")
def api_preview(code: str):
    imp = get_importer()
    r = imp.preview(code)
    if not r.get("ok"):
        emit("error", f"[{code}] 预览失败：{r.get('error','')[:160]}")
    else:
        emit("info", f"[{code}] 预览完成：{r.get('answerable')}/{r.get('total_questions')} 题可导入")
    return jsonify(r)


@app.get("/api/history/<code>")
def api_history(code: str):
    return jsonify({"ok": True, "rows": db.history(code)})


# ------------------------------------------------------------------ 执行

@app.post("/api/submit")
def api_submit():
    """仅提交已写好的草稿，不重新导入（避免冲掉人工补的照片/定位）。"""
    global JOB
    if JOB["running"]:
        return jsonify({"ok": False, "error": "已有任务在运行"}), 409
    data = request.get_json(force=True) or {}
    codes = data.get("codes") or []
    force = bool(data.get("force"))
    concurrency = max(1, min(int(data.get("concurrency") or 3), 8))
    if not codes:
        return jsonify({"ok": False, "error": "未选择门店"}), 400
    emit("info", f"⚠️ 提交任务启动 {len(codes)} 家（不重新导入，仅送审；提交后需审核打回才能改）")
    JOB.update({"running": True, "total": len(codes), "done": 0, "ok": 0,
                "warn": 0, "fail": 0, "skip": 0, "started": time.time(),
                "message": "提交中"})
    push_progress()
    t = threading.Thread(target=_submit_job, args=(codes, force, concurrency),
                         daemon=True)
    t.start()
    return jsonify({"ok": True, "total": len(codes)})


def _submit_job(codes, force, concurrency):
    global JOB
    imp = get_importer()
    counter = {"ok": 0, "warn": 0, "fail": 0, "skip": 0, "submitted": 0}
    emit("info", f"任务开始：共 {len(codes)} 家，仅提交不导入，强制覆盖={force}")

    def work(code):
        return imp.submit_one(code, force=force)

    try:
        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            futs = {ex.submit(work, c): c for c in codes}
            for fu in as_completed(futs):
                code = futs[fu]
                try:
                    r = fu.result()
                except Exception as e:
                    r = {"ok": False, "status": "failed", "store_code": code,
                         "message": str(e)}
                st = r.get("status", "failed")
                if st == "submitted":
                    counter["submitted"] += 1
                if st == "blocked":
                    counter["warn"] += 1
                elif st == "skipped":
                    counter["skip"] += 1
                elif st == "submitted":
                    pass
                else:
                    counter["fail"] += 1
                JOB["done"] += 1
                JOB["ok"] = counter.get("ok", 0)
                JOB["warn"] = counter["warn"]
                JOB["fail"] = counter["fail"]
                JOB["skip"] = counter["skip"]
                lvl = ("error" if st == "failed"
                       else "warn" if st in ("blocked", "skipped") else "info")
                msg = r.get("message", "")
                if st == "blocked":
                    msg = f"{msg}（缺 {len(r.get('blocking', []))} 道必填）"
                emit(lvl, f"[{JOB['done']}/{JOB['total']}] {code} → {msg[:160]}")
                push_progress()
    except Exception as e:
        emit("error", f"任务异常：{e}\n{traceback.format_exc()[-400:]}")
    finally:
        JOB["running"] = False
        JOB["message"] = "完成"
        emit("info", f"任务结束：已提交 {counter['submitted']}，阻塞 {counter['warn']}，"
                     f"跳过 {counter['skip']}，失败 {counter['fail']}")
        push_progress()


@app.post("/api/run")
def api_run():
    global JOB
    if JOB["running"]:
        return jsonify({"ok": False, "error": "已有任务在运行"}), 409
    data = request.get_json(force=True) or {}
    codes = data.get("codes") or []
    force = bool(data.get("force"))
    do_submit = bool(data.get("submit"))
    concurrency = max(1, min(int(data.get("concurrency") or 3), 8))
    if not codes:
        return jsonify({"ok": False, "error": "未选择门店"}), 400
    if do_submit:
        emit("warn", "⚠️ 本次任务包含【提交】操作：提交后需审核打回才能再修改")
    JOB.update({"running": True, "total": len(codes), "done": 0, "ok": 0,
                "warn": 0, "fail": 0, "skip": 0, "started": time.time(),
                "message": "运行中"})
    push_progress()
    t = threading.Thread(target=_run_job, args=(codes, force, do_submit, concurrency),
                         daemon=True)
    t.start()
    return jsonify({"ok": True, "total": len(codes)})


def _run_job(codes, force, do_submit, concurrency):
    global JOB
    imp = get_importer()
    emit("info", f"任务开始：共 {len(codes)} 家门店，并发 {concurrency}，"
                 f"强制覆盖={force}，提交={do_submit}")
    counter = {"ok": 0, "warn": 0, "fail": 0, "skip": 0, "submitted": 0}

    def work(code):
        return imp.import_one(code, force=force, do_submit=do_submit)

    try:
        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            futs = {ex.submit(work, c): c for c in codes}
            for fu in as_completed(futs):
                code = futs[fu]
                try:
                    r = fu.result()
                except Exception as e:
                    r = {"ok": False, "status": "failed", "store_code": code,
                         "message": str(e)}
                st = r.get("status", "failed")
                if st == "ok":
                    counter["ok"] += 1
                elif st == "warning":
                    counter["warn"] += 1
                elif st == "skipped":
                    counter["skip"] += 1
                else:
                    counter["fail"] += 1
                if st == "submitted":
                    counter["submitted"] += 1
                JOB["done"] += 1
                JOB.update(counter)
                lvl = "error" if st == "failed" else ("warn" if st in ("warning", "skipped") else "info")
                emit(lvl, f"[{JOB['done']}/{JOB['total']}] {code} → {r.get('message','')[:180]}")
                push_progress()
    except Exception as e:
        emit("error", f"任务异常：{e}\n{traceback.format_exc()[-400:]}")
    finally:
        JOB["running"] = False
        JOB["message"] = "完成"
        emit("info", f"任务结束：成功 {counter['ok']}，有缺失 {counter['warn']}，"
                     f"跳过 {counter['skip']}，失败 {counter['fail']}")
        push_progress()


@app.get("/api/stream")
def api_stream():
    def gen():
        # 先发一条心跳，避免代理缓冲
        yield f"data: {json.dumps({'level':'hello','ts':time.strftime('%H:%M:%S')})}\n\n"
        last = 0.0
        while True:
            try:
                rec = LOG_Q.get(timeout=15)
                yield f"data: {json.dumps(rec, ensure_ascii=False)}\n\n"
                last = time.time()
            except queue.Empty:
                yield ": keepalive\n\n"
    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ------------------------------------------------------------------ 导出

@app.get("/api/export")
def api_export():
    rows = db.latest_runs(limit=100000)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["门店编号", "门店名称", "Wave", "状态", "说明", "已填题数",
                "总题数", "缺失必答", "需人工补填", "已提交", "时间"])
    for r in rows:
        w.writerow([r["store_code"], r["store_name"], r["wave"], r["status"],
                    (r["message"] or "").replace("\n", " "), r["matched"], r["total"],
                    r["missing_required"], r["manual_required"], r["submitted"],
                    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["created_at"] or 0))])
    buf.seek(0)
    # 文件名必须做 RFC 5987 百分号编码，否则中文会触发 latin-1 编码异常
    name = quote(time.strftime("导入结果_%Y%m%d_%H%M%S.csv"))
    return Response(buf.getvalue(), mimetype="text/csv; charset=utf-8-sig",
                    headers={"Content-Disposition":
                             f"attachment; filename=import_results.csv; filename*=UTF-8''{name}"})


@app.get("/api/export/manual")
def api_export_manual():
    """导出需要人工补填的题目清单（照片/定位等）。"""
    rows = db.latest_runs(limit=100000)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["门店编号", "门店名称", "题号", "题型", "题目"])
    n = 0
    for r in rows:
        try:
            detail = json.loads(r["detail"] or "{}")
        except Exception:
            detail = {}
        for m in detail.get("manual_required", []):
            w.writerow([r["store_code"], r["store_name"], m.get("id"),
                        m.get("type"), m.get("text")])
            n += 1
    buf.seek(0)
    name = quote(time.strftime("待人工补填_%Y%m%d_%H%M%S.csv"))
    return Response(buf.getvalue(), mimetype="text/csv; charset=utf-8-sig",
                    headers={"Content-Disposition":
                             f"attachment; filename=manual_required.csv; filename*=UTF-8''{name}"})


@app.post("/api/history/clear")
def api_clear():
    db.clear()
    emit("info", "已清空导入记录（不影响服务器上已保存的问卷）")
    return jsonify({"ok": True})


# ------------------------------------------------------------------ 启动

def main():
    port = int(os.environ.get("PORT", "8765"))
    url = f"http://127.0.0.1:{port}"
    print(f"\n  问卷批量导入工具  →  {url}\n  （关闭窗口或 Ctrl+C 结束）\n")
    try:
        from waitress import serve
        # threads 需容纳并发导入 + SSE 长连接
        serve(app, host="127.0.0.1", port=port, threads=16,
              channel_timeout=300, ident="survey-import")
    except ImportError:
        app.run(host="127.0.0.1", port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
