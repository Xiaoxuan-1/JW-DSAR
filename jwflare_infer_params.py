"""从活动区磁图裁块计算 JW-Flare prompt 中的 NL / unsigned flux（与 function.py 一致）。"""
from __future__ import annotations

from typing import List, Tuple

import numpy as np

from function import NLfeat, fluxValues


def format_infer_spaced_number(x: float) -> str:
    """与 infer_JWflare_series_A 示例一致：数字与符号间加空格，如 6 9 9 . 0 0 0"""
    s = f"{float(x):.3f}"
    return " ".join(list(s))


def png_to_magnetogram_minus_offset(arr: np.ndarray) -> np.ndarray:
    """与 function.process_txt 一致：灰度 float，零通量偏移 -128。"""
    img = arr.astype(float)
    if img.ndim == 3 and img.shape[2] == 4:
        w = np.array([0.299, 0.587, 0.114, 0])
        gray = np.sum(img[:, :, :3] * w[:3], axis=2)
    elif img.ndim == 3 and img.shape[2] == 3:
        w = np.array([0.299, 0.587, 0.114])
        gray = np.sum(img * w, axis=2)
    elif img.ndim == 2:
        gray = img
    else:
        raise ValueError(f"Unexpected image shape: {img.shape}")
    return gray - 128.0


def nl_length_and_unsigned_flux(image_minus_128: np.ndarray) -> Tuple[float, float]:
    """NL length 取 NLfeat 第一分量；unsigned flux 取 fluxValues 第四分量。"""
    nl = NLfeat(image_minus_128)
    pos_sum, neg_sum, _sign, unsign = fluxValues(image_minus_128)
    nl_len = float(nl[0])
    # function 中 name_list 的 Total unsigned flux 对应 fluxValues 最后一项 unsignSum
    unsigned = float(unsign)
    return nl_len, unsigned


def build_jwflare_query_suffix(frames: List[Tuple[float, float]]) -> str:
    """拼接 15 段 <image>The physical parameters: ..."""
    parts: List[str] = []
    for nl, flux in frames:
        parts.append(
            "<image>The physical parameters: Magnetic Neutral Line(NL) length: "
            f"{format_infer_spaced_number(nl)} pixel, Total unsigned flux: {format_infer_spaced_number(flux)} Wb"
        )
    return "".join(parts)


JWFLARE_QUERY_PREFIX = (
    "Given a set of 15 magnetograms of the solar active region, Please follow the steps below to conduct "
    "a step-by-step analysis of the sequence of 15 solar images and predict whether a flare is likely to "
    "erupt in the nxt 24 hours: Step 1: Analyze key features of each solar active region magnetogram."
    "Step 2: Compare the sequence of images to identify dynamic changes (e.g., magnetic neutral line length, "
    "total unsigned magnetic flux) and their potential impact on flare likelihood."
    "Step 3: Relate these changes to historical data or physical laws to infer if they align with typical "
    "flare eruption characteristics."
    "Step 4: Summarize your conclusions and assess the likelihood of a flare eruption."
)

JWFLARE_QUERY_SUFFIX = (
    ". Return the answer as one of the following options: 'A: Flare', 'B: None'. "
    "Only return the Option Letters, not the Description."
)


def build_full_jwflare_user_query(frames: List[Tuple[float, float]]) -> str:
    if len(frames) != 15:
        raise ValueError(f"expected 15 frames, got {len(frames)}")
    return JWFLARE_QUERY_PREFIX + build_jwflare_query_suffix(frames) + JWFLARE_QUERY_SUFFIX
