"""JW-Flare：OpenAI 兼容 HTTP（本机/局域网）。"""
from __future__ import annotations

import base64
import json
import logging
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)


def chat_completions_url(api_base: str) -> str:
    base = api_base.rstrip("/")
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    return f"{base}/chat/completions"


def _build_payload(model: str, user_query: str) -> Dict[str, Any]:
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": ""},
            {"role": "user", "content": user_query},
        ],
        "max_tokens": 2048,
        "temperature": 0.6,
        "presence_penalty": 2,
        "frequency_penalty": 0,
        "logprobs": True,
        "stream": False,
    }


def _json_from_response(r: requests.Response) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}: {r.text[:800]}"
    try:
        return r.json(), None
    except Exception as e:
        return None, f"JSON parse: {e}: {r.text[:400]}"


def _post_with_path_payload(
    url: str,
    model: str,
    image_paths: List[str],
    user_query: str,
    timeout_s: int,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    payload = _build_payload(model, user_query)
    payload["images"] = image_paths
    try:
        r = requests.post(
            url,
            headers={"accept": "application/json", "Content-Type": "application/json"},
            json=payload,
            timeout=timeout_s,
        )
    except requests.RequestException as e:
        return None, str(e)
    return _json_from_response(r)


def _post_with_base64_payload(
    url: str,
    model: str,
    image_paths: List[str],
    user_query: str,
    timeout_s: int,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    payload = _build_payload(model, user_query)
    image_blobs: List[Dict[str, str]] = []
    for p in image_paths:
        file_path = Path(p)
        if not file_path.is_file():
            return None, f"image not found: {p}"
        b64 = base64.b64encode(file_path.read_bytes()).decode("ascii")
        image_blobs.append(
            {
                "name": file_path.name,
                "mime_type": "image/png",
                "data": b64,
            }
        )
    payload["images_base64"] = image_blobs
    try:
        r = requests.post(
            url,
            headers={"accept": "application/json", "Content-Type": "application/json"},
            json=payload,
            timeout=timeout_s,
        )
    except requests.RequestException as e:
        return None, str(e)
    return _json_from_response(r)


def _post_with_multipart_payload(
    url: str,
    model: str,
    image_paths: List[str],
    user_query: str,
    timeout_s: int,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    payload = _build_payload(model, user_query)
    files = []
    file_handles = []
    try:
        for p in image_paths:
            fp = Path(p)
            if not fp.is_file():
                return None, f"image not found: {p}"
            fh = open(fp, "rb")
            file_handles.append(fh)
            files.append(("images", (fp.name, fh, "image/png")))

        data = {"payload": json.dumps(payload, ensure_ascii=False)}
        r = requests.post(url, data=data, files=files, timeout=timeout_s)
    except requests.RequestException as e:
        return None, str(e)
    finally:
        for fh in file_handles:
            try:
                fh.close()
            except Exception:
                pass
    return _json_from_response(r)


def post_jwflare_inference(
    api_base: str,
    model: str,
    image_paths: List[str],
    user_query: str,
    timeout_s: int = 300,
    transport: str = "auto",
    upload_format: str = "multipart",
    allow_path_fallback: bool = True,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    返回 (json_dict, error_message)。
    """
    url = chat_completions_url(api_base)
    mode = (transport or "auto").strip().lower()
    fmt = (upload_format or "multipart").strip().lower()
    if mode not in ("auto", "upload", "path"):
        mode = "auto"
    if fmt not in ("multipart", "base64"):
        fmt = "multipart"

    upload_fn = _post_with_multipart_payload if fmt == "multipart" else _post_with_base64_payload

    if mode == "path":
        return _post_with_path_payload(url, model, image_paths, user_query, timeout_s)

    if mode == "upload":
        return upload_fn(url, model, image_paths, user_query, timeout_s)

    j, err = upload_fn(url, model, image_paths, user_query, timeout_s)
    if j is not None:
        return j, None
    if allow_path_fallback:
        logger.warning("JW-Flare upload mode failed, fallback to path mode: %s", err)
        return _post_with_path_payload(url, model, image_paths, user_query, timeout_s)
    return None, f"upload failed: {err}"


def parse_ab_from_response(j: Dict[str, Any]) -> Tuple[Optional[str], Optional[float], Optional[float], str]:
    """
    从 OpenAI 兼容响应解析 A/B 概率与文本。
    """
    content = ""
    try:
        ch0 = j.get("choices", [{}])[0]
        msg = ch0.get("message")
        if isinstance(msg, dict):
            content = str(msg.get("content") or "")
        if not content:
            content = str(ch0.get("text") or ch0.get("message") or "")
    except (IndexError, KeyError, TypeError):
        pass

    p_a: Optional[float] = None
    p_b: Optional[float] = None
    label: Optional[str] = None
    try:
        ch0 = j.get("choices", [{}])[0]
        lp = ch0.get("logprobs") or {}
        items = lp.get("content") or []
        for item in items:
            if not isinstance(item, dict):
                continue
            tok = item.get("token")
            logp = item.get("logprob")
            if tok == "A" and logp is not None:
                p_a = math.exp(float(logp))
            if tok == "B" and logp is not None:
                p_b = math.exp(float(logp))
    except (TypeError, ValueError) as e:
        logger.debug("logprobs parse: %s", e)

    raw = content.strip().upper()
    if "A" in raw[:8] and "B" not in raw[:3]:
        label = "A"
    elif "B" in raw[:8]:
        label = "B"
    elif raw.startswith("A"):
        label = "A"
    elif raw.startswith("B"):
        label = "B"

    if label is None and p_a is not None and p_b is not None:
        label = "A" if p_a >= p_b else "B"

    return label, p_a, p_b, content
