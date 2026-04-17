"""手工联调 JW-Flare 序列推理示例（OpenAI 兼容 HTTP）。

用法示例:
python scripts/infer_JWflare_series_A.py \
  --api-base http://127.0.0.1:2222/v1 \
  --model qwen2-vl-7b-instruct \
  --query-file test/query.txt \
  --image path/to/1.png --image path/to/2.png
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import List

import requests


def _chat_completions_url(api_base: str) -> str:
    return f"{api_base.rstrip('/')}/chat/completions"


def _load_query(query_file: str | None, query_text: str | None) -> str:
    if query_text and query_text.strip():
        return query_text.strip()
    if query_file:
        return Path(query_file).read_text(encoding="utf-8").strip()
    raise SystemExit("请通过 --query 或 --query-file 提供推理文本。")


def _normalize_images(items: List[str]) -> List[str]:
    if not items:
        raise SystemExit("至少提供一个 --image。")
    paths = [str(Path(p).expanduser().resolve()) for p in items]
    for p in paths:
        if not Path(p).is_file():
            raise SystemExit(f"图片不存在: {p}")
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description="JW-Flare sequence inference test client.")
    parser.add_argument("--api-base", type=str, default="http://127.0.0.1:2222/v1")
    parser.add_argument("--model", type=str, default="qwen2-vl-7b-instruct")
    parser.add_argument("--query", type=str, default=None)
    parser.add_argument("--query-file", type=str, default=None)
    parser.add_argument("--image", dest="images", action="append", default=[])
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()

    query = _load_query(args.query_file, args.query)
    image_paths = _normalize_images(args.images)
    url = _chat_completions_url(args.api_base)

    payload = {
        "model": args.model,
        "images": image_paths,
        "messages": [
            {"role": "system", "content": ""},
            {"role": "user", "content": query},
        ],
        "max_tokens": 4096,
        "temperature": 0.6,
        "presence_penalty": 2,
        "frequency_penalty": 0,
        "logprobs": True,
        "stream": False,
    }

    try:
        resp = requests.post(
            url,
            headers={"accept": "application/json", "Content-Type": "application/json"},
            json=payload,
            timeout=args.timeout,
        )
    except requests.RequestException as e:
        raise SystemExit(f"Request failed: {e}")

    if resp.status_code != 200:
        raise SystemExit(f"HTTP {resp.status_code}: {resp.text[:1000]}")

    data = resp.json()
    print(json.dumps(data, ensure_ascii=False, indent=2))

    try:
        ch = data.get("choices", [{}])[0]
        content = (
            ch.get("message", {}).get("content")
            if isinstance(ch.get("message"), dict)
            else ch.get("text") or ch.get("message")
        )
        print("\nResponse:", content)
        for item in (ch.get("logprobs") or {}).get("content") or []:
            if not isinstance(item, dict):
                continue
            tok = item.get("token")
            logp = item.get("logprob")
            if tok == "A" and logp is not None:
                print(f"P(A=Flare)={math.exp(float(logp)):.6f}")
            elif tok == "B" and logp is not None:
                print(f"P(B=None)={math.exp(float(logp)):.6f}")
    except Exception:
        pass


if __name__ == "__main__":
    main()
