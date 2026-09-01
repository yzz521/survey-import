# -*- coding: utf-8 -*-
"""本地走访照片：按门店编号索引、按题意匹配、从 EXIF 读 GPS。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .parser import norm_text

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".heic", ".heif"}

# 文件名（规范化后）→ 角色
# 门头照 / 店铺形象负面照片 来自 Excel 填写说明
_STOREFRONT_KEYS = ("门头照", "门头", "店铺外观", "外观照")
_NEGATIVE_KEYS = ("店铺形象负面照片", "形象负面", "负面照片", "扣分证据", "形象扣分")


@dataclass
class StorePhotos:
    code: str
    folder: Path | None = None
    files: list[Path] = field(default_factory=list)
    storefront: Path | None = None
    negative: Path | None = None
    gps: tuple[float, float] | None = None  # (lat, lng)
    gps_from: str = ""


def index_photo_dir(d: str | Path | None) -> dict[str, StorePhotos]:
    """扫描照片目录，返回 {门店编号: StorePhotos}。

    支持两种布局：
      1. {dir}/{门店编号}-店铺名/门头照.jpg
      2. {dir}/{门店编号}-门头照.jpg  （扁平）
    """
    if not d:
        return {}
    root = Path(d)
    if not root.is_dir():
        return {}
    out: dict[str, StorePhotos] = {}

    def bucket(code: str) -> StorePhotos:
        code = code.upper()
        if code not in out:
            out[code] = StorePhotos(code=code)
        return out[code]

    code_re = re.compile(r"^([A-Za-z0-9]+)([\-—_].*)?$")

    for p in sorted(root.iterdir()):
        if p.name.startswith(".") or p.name.startswith("~$"):
            continue
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
            m = re.match(r"^([A-Za-z0-9]+)[\-—_]+", p.stem)
            if m:
                b = bucket(m.group(1))
                b.files.append(p)
        elif p.is_dir():
            m = code_re.match(p.name)
            if not m:
                continue
            b = bucket(m.group(1))
            b.folder = p
            for f in sorted(p.rglob("*")):
                if not f.is_file() or f.name.startswith("."):
                    continue
                if f.suffix.lower() not in IMAGE_EXTS:
                    continue
                b.files.append(f)

    for b in out.values():
        _classify(b)
        b.gps, b.gps_from = _first_gps(b)
    return out


def _classify(b: StorePhotos) -> None:
    for f in b.files:
        role = classify_filename(f)
        if role == "storefront" and b.storefront is None:
            b.storefront = f
        elif role == "negative" and b.negative is None:
            b.negative = f
    # 文件夹里只有一张图、文件名对不上时，默认当门头照
    if b.storefront is None and len(b.files) == 1 and classify_filename(b.files[0]) != "negative":
        b.storefront = b.files[0]


def classify_filename(path: Path) -> str:
    n = norm_text(path.stem)
    # 去掉门店编号前缀：Y10018017门头照
    n = re.sub(r"^[A-Za-z0-9]+", "", n)
    for k in _NEGATIVE_KEYS:
        if norm_text(k) in n:
            return "negative"
    for k in _STOREFRONT_KEYS:
        if norm_text(k) in n:
            return "storefront"
    return "other"


def classify_question(text: str, qtype: str) -> str:
    """题目 → storefront / negative / location / image_other / ''。

    只认媒体题和定位题，避免「店铺外观是否清洁」这类单选题被误当成照片题。
    """
    qt = (qtype or "").lower()
    if qt == "location":
        return "location"
    if qt not in ("image", "media", "file", "video", "audio"):
        return ""
    n = norm_text(text or "")
    if "形象" in n and any(k in n for k in ("扣分", "证据", "负面")):
        return "negative"
    if "门头" in n or "店铺外观" in n or "外观照片" in n:
        return "storefront"
    return "image_other"


def gps_from_image(path: Path) -> tuple[float, float] | None:
    """从 JPEG EXIF 读 WGS84 经纬度。读不到返回 None。"""
    try:
        from PIL import Image
        from PIL.ExifTags import GPSTAGS
    except Exception:
        return None
    try:
        with Image.open(path) as im:
            exif = im.getexif()
            if not exif:
                return None
            gps = exif.get_ifd(0x8825) if hasattr(exif, "get_ifd") else None
            if not gps:
                return None
            tagged = {GPSTAGS.get(k, k): v for k, v in gps.items()}
            lat = _dms_to_deg(tagged.get("GPSLatitude"), tagged.get("GPSLatitudeRef"))
            lng = _dms_to_deg(tagged.get("GPSLongitude"), tagged.get("GPSLongitudeRef"))
            if lat is None or lng is None:
                return None
            if not (-90 <= lat <= 90 and -180 <= lng <= 180):
                return None
            if lat == 0 and lng == 0:
                return None
            return (lat, lng)
    except Exception:
        return None


def _dms_to_deg(dms, ref) -> float | None:
    if dms is None:
        return None
    try:
        d, m, s = (float(x) for x in dms)
    except Exception:
        return None
    val = d + m / 60.0 + s / 3600.0
    if str(ref or "").upper() in ("S", "W"):
        val = -val
    return val


def _first_gps(b: StorePhotos) -> tuple[tuple[float, float] | None, str]:
    ordered = []
    if b.storefront:
        ordered.append(b.storefront)
    ordered.extend(f for f in b.files if f not in ordered)
    for f in ordered:
        gps = gps_from_image(f)
        if gps:
            return gps, f.name
    return None, ""


def file_for_role(photos: StorePhotos | None, role: str) -> Path | None:
    if not photos:
        return None
    if role == "storefront":
        return photos.storefront
    if role == "negative":
        return photos.negative
    return None
