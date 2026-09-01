# -*- coding: utf-8 -*-
"""Vantara 外部填报接口客户端（逆向自 bare-client 前端，无浏览器依赖）。"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

import requests


class SurveyAPIError(Exception):
    def __init__(self, message: str, status: int | None = None, code: str = "", payload=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.payload = payload


def parse_media_id(payload) -> str | None:
    """从上传接口返回里抠 media_file_id（字段名不统一，按前端逻辑兜底）。"""
    if not isinstance(payload, dict):
        return None
    inner = payload.get("media_file") or payload.get("media") or payload
    if not isinstance(inner, dict):
        inner = payload
    for k in ("id", "media_file_id", "file_id"):
        v = inner.get(k)
        if v is not None and str(v).strip() != "":
            return v
        v = payload.get(k)
        if v is not None and str(v).strip() != "":
            return v
    return None


@dataclass
class UnlockInfo:
    raw: dict = field(default_factory=dict)

    @property
    def csrf_token(self) -> str:
        return self.raw.get("csrf_token") or ""

    @property
    def display_name(self) -> str:
        return self.raw.get("display_name") or ""

    @property
    def questionnaire_title(self) -> str:
        return self.raw.get("questionnaire_title") or ""

    @property
    def status(self) -> str:
        return self.raw.get("status") or ""

    @property
    def submission_status(self):
        return self.raw.get("submission_status")

    @property
    def entry_mode(self) -> str:
        return self.raw.get("entry_mode") or ""

    @property
    def store(self) -> dict:
        return self.raw.get("store") or {}


class SurveyClient:
    """一家门店的一次访问会话。"""

    def __init__(self, base_url: str, tenant_key: str, public_id: str,
                 access_code: str, timeout: int = 30, logger=None):
        u = urlparse(base_url)
        self.base = f"{u.scheme}://{u.netloc}"
        self.tenant_key = tenant_key
        self.public_id = public_id
        self.access_code = access_code
        self.timeout = timeout
        self.log = logger or (lambda *a, **k: None)
        self._s = requests.Session()
        # 不要在 Session 上写死 Content-Type，否则 multipart 上传会被打成 JSON。
        self._s.headers.update({
            "X-Tenant-Key": self.tenant_key,
            "Accept": "application/json",
            "Referer": f"{self.base}/external/{self.tenant_key}/{self.public_id}",
        })
        self.unlock_info: UnlockInfo | None = None

    # -------------------------------------------------- 内部

    def _url(self, suffix: str = "") -> str:
        s = (suffix or "").strip("/")
        u = f"{self.base}/api/external/v1/surveys/{self.public_id}/"
        return f"{u}{s}/" if s else u

    def _csrf_headers(self, method: str, suffix: str) -> dict:
        headers = {}
        if method.upper() in ("POST", "PUT", "PATCH", "DELETE") and suffix != "unlock":
            csrf = self.unlock_info.csrf_token if self.unlock_info else ""
            if csrf:
                headers["X-External-CSRF"] = csrf
        return headers

    def _request(self, method: str, suffix: str = "", body=None, retry: int = 2):
        headers = {"Content-Type": "application/json"}
        headers.update(self._csrf_headers(method, suffix))
        last_err = None
        for attempt in range(retry + 1):
            try:
                r = self._s.request(
                    method.upper(), self._url(suffix),
                    json=body if body is not None else None,
                    headers=headers, timeout=self.timeout,
                )
            except requests.RequestException as e:
                last_err = SurveyAPIError(f"网络异常: {e}", code="NETWORK")
                if attempt < retry:
                    time.sleep(1.2 * (attempt + 1))
                    continue
                raise last_err

            if r.status_code in (429, 500, 502, 503, 504) and attempt < retry:
                time.sleep(1.5 * (attempt + 1))
                continue

            try:
                data = r.json()
            except Exception:
                data = None

            if not r.ok:
                code = ""
                if isinstance(data, dict):
                    code = str(data.get("code") or "")
                    if not code and isinstance(data.get("detail"), dict):
                        code = str(data["detail"].get("code") or "")
                raise SurveyAPIError(
                    f"HTTP {r.status_code}: {(r.text or '')[:200]}",
                    status=r.status_code, code=code, payload=data,
                )
            return data if data is not None else {}
        raise last_err or SurveyAPIError("请求失败")

    # -------------------------------------------------- 接口

    def unlock(self) -> UnlockInfo:
        data = self._request("POST", "unlock", {"code": self.access_code})
        self.unlock_info = UnlockInfo(data)
        return self.unlock_info

    def session(self) -> dict:
        return self._request("GET", "session")

    def questionnaire(self) -> dict:
        return self._request("GET", "questionnaire")

    def get_draft(self) -> dict:
        return self._request("GET", "draft")

    def save_draft(self, answers: list[dict], draft_revision,
                   questionnaire_version_id=None, current_page_id=None) -> dict:
        body = {
            "draft_revision": draft_revision,
            "questionnaire_version_id": questionnaire_version_id,
            "current_page_id": current_page_id,
            "answers": answers,
        }
        return self._request("PUT", "draft", body)

    def submit(self, draft_revision, current_page_id=None) -> dict:
        body = {"draft_revision": draft_revision, "current_page_id": current_page_id}
        return self._request("POST", "submit", body)

    def upload_media(self, question_id, file_path, media_type: str = "image") -> dict:
        """POST /surveys/{id}/media/  multipart：question + media_type + file。

        返回接口原始 JSON；调用方用 parse_media_id() 取文件编号。
        """
        from pathlib import Path
        p = Path(file_path)
        if not p.is_file():
            raise SurveyAPIError(f"照片文件不存在：{file_path}", code="FILE_MISSING")
        mime = {
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
            ".webp": "image/webp", ".bmp": "image/bmp", ".gif": "image/gif",
            ".heic": "image/heic", ".heif": "image/heif",
        }.get(p.suffix.lower(), "application/octet-stream")
        headers = self._csrf_headers("POST", "media")
        timeout = max(self.timeout, 120)
        last_err = None
        for attempt in range(3):
            try:
                with p.open("rb") as f:
                    r = self._s.post(
                        self._url("media"),
                        headers=headers,
                        data={"question": str(question_id), "media_type": media_type},
                        files={"file": (p.name, f, mime)},
                        timeout=timeout,
                    )
            except requests.RequestException as e:
                last_err = SurveyAPIError(f"上传网络异常: {e}", code="NETWORK")
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise last_err
            try:
                data = r.json()
            except Exception:
                data = None
            if r.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            if not r.ok:
                raise SurveyAPIError(
                    f"上传失败 HTTP {r.status_code}: {(r.text or '')[:200]}",
                    status=r.status_code, payload=data,
                )
            return data if isinstance(data, dict) else {"raw": data}
        raise last_err or SurveyAPIError("上传失败")

    def close(self):
        try:
            self._s.close()
        except Exception:
            pass
