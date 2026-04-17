"""JW-Flare 预报：环境变量与数据根目录约定。"""
from __future__ import annotations

import os
from typing import Optional


def _truthy(v: Optional[str]) -> bool:
    if v is None:
        return False
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def jwflare_enabled() -> bool:
    return _truthy(os.getenv("JWFLARE_ENABLED"))


def jwflare_data_root() -> str:
    root = os.getenv("JWFLARE_DATA_ROOT") or os.getenv("HMI_FITS_CACHE") or ""
    return os.path.abspath(root.strip()) if root.strip() else ""


def jwflare_api_base() -> str:
    """OpenAI 兼容 API 根，如 http://127.0.0.1:2222/v1"""
    return (os.getenv("JWFLARE_API_BASE") or "http://localhost:2222/v1").rstrip("/")


def jwflare_model() -> str:
    return os.getenv("JWFLARE_MODEL") or "qwen2-vl-7b-instruct"


def jwflare_max_regions() -> int:
    try:
        return max(1, min(10, int(os.getenv("JWFLARE_MAX_REGIONS", "3"))))
    except ValueError:
        return 3


def jwflare_http_timeout_s() -> int:
    try:
        return max(30, int(os.getenv("JWFLARE_HTTP_TIMEOUT", "300")))
    except ValueError:
        return 300


def jwflare_transport() -> str:
    mode = (os.getenv("JWFLARE_TRANSPORT") or "auto").strip().lower()
    if mode in ("auto", "upload", "path"):
        return mode
    return "auto"


def jwflare_upload_format() -> str:
    fmt = (os.getenv("JWFLARE_UPLOAD_FORMAT") or "base64").strip().lower()
    if fmt in ("multipart", "base64"):
        return fmt
    return "base64"


def jwflare_allow_path_fallback() -> bool:
    return _truthy(os.getenv("JWFLARE_ALLOW_PATH_FALLBACK", "1"))


def paths_for_date(data_root: str, report_date: str) -> tuple[str, str]:
    """report_date: YYYY-MM-DD -> (full_disk_dir, ar_root_dir)."""
    base = os.path.join(data_root, report_date)
    return os.path.join(base, "full_disk"), os.path.join(base, "ar")


def ar_crop_dir(data_root: str, report_date: str, noaa: str) -> str:
    _, ar_root = paths_for_date(data_root, report_date)
    return os.path.join(ar_root, str(noaa).strip())

