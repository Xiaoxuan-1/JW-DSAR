#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Tuple

import requests


def _chat_completions_url(api_base: str) -> str:
    return f"{api_base.rstrip('/')}/chat/completions"


def _build_payload(model: str, query: str) -> Dict[str, Any]:
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": ""},
            {"role": "user", "content": query},
        ],
        "max_tokens": 16,
        "temperature": 0.1,
        "logprobs": True,
        "stream": False,
    }


def _short(text: str, n: int = 800) -> str:
    t = (text or "").replace("\r\n", "\n")
    return t if len(t) <= n else (t[:n] + " ...<truncated>")


def _print_result(name: str, status: int | None, body: str) -> None:
    print("\n" + "=" * 80)
    print(f"[{name}] status={status}")
    print("-" * 80)
    print(_short(body))


def _try_json(url: str, payload: Dict[str, Any], timeout: int) -> Tuple[int | None, str]:
    try:
        r = requests.post(
            url,
            headers={"accept": "application/json", "Content-Type": "application/json"},
            json=payload,
            timeout=timeout,
        )
        return r.status_code, r.text
    except Exception as e:
        return None, f"request failed: {e}"


def _make_tiny_png_file() -> Path:
    # 1x1 PNG (black pixel)
    tiny_png_b64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
        "/w8AAwMB/6qX3u8AAAAASUVORK5CYII="
    )
    b = base64.b64decode(tiny_png_b64)
    d = Path(tempfile.mkdtemp(prefix="jwflare-upload-test-"))
    p = d / "tiny.png"
    p.write_bytes(b)
    return p


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe JW-Flare upload support.")
    parser.add_argument("--api-base", type=str, default=os.getenv("JWFLARE_API_BASE", "http://127.0.0.1:2222/v1"))
    parser.add_argument("--model", type=str, default=os.getenv("JWFLARE_MODEL", "qwen2-vl-7b-instruct"))
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--query", type=str, default="Reply with a single letter A.")
    args = parser.parse_args()

    url = _chat_completions_url(args.api_base)
    payload = _build_payload(args.model, args.query)

    # Prepare a tiny PNG for upload modes
    tiny_png = _make_tiny_png_file()
    abs_path = str(tiny_png.resolve())

    # 1) path mode (legacy contract): images are server-local file paths
    payload_path = dict(payload)
    payload_path["images"] = [abs_path]
    st, body = _try_json(url, payload_path, args.timeout)
    _print_result("path(JSON images=[abs_path])", st, body)

    # 2) upload mode (multipart): payload as form field + images as files
    try:
        with open(tiny_png, "rb") as f:
            files = [("images", (tiny_png.name, f, "image/png"))]
            data = {"payload": json.dumps(payload, ensure_ascii=False)}
            r = requests.post(url, data=data, files=files, timeout=args.timeout)
            _print_result("upload(multipart payload+files)", r.status_code, r.text)
    except Exception as e:
        _print_result("upload(multipart payload+files)", None, f"request failed: {e}")

    # 3) upload mode (base64): images_base64 in JSON
    payload_b64 = dict(payload)
    payload_b64["images_base64"] = [
        {"name": tiny_png.name, "mime_type": "image/png", "data": base64.b64encode(tiny_png.read_bytes()).decode("ascii")}
    ]
    st, body = _try_json(url, payload_b64, args.timeout)
    _print_result("upload(base64 JSON images_base64=[...])", st, body)

    print("\nDone.")


if __name__ == "__main__":
    main()

