"""JW-Flare 预报：下载 FITS → 裁剪 → 参量 → HTTP 推理 → HTML。"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)


def _verdict_label(code: str | None) -> str:
    if code == "A":
        return "可能有X级耀斑爆发"
    if code == "B":
        return "可能无X级耀斑爆发"
    return "未判定"


def _row_detail_text(raw: str) -> str:
    rs = (raw or "")[:120]
    return f"模型：JW-Flare；序列 15 幅磁图；原始输出: {rs!r}"


def _jwflare_rows_to_prompt_text(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return ""
    lines: List[str] = []
    for r in rows:
        noaa = r.get("noaa", "—")
        if r.get("error"):
            lines.append(f"- NOAA {noaa}：不可用 — {str(r.get('detail', ''))[:400]}")
        else:
            pf, pn = r.get("p_flare"), r.get("p_none")
            raw = r.get("raw_excerpt", "")
            lines.append(
                f"- NOAA {noaa}：{r.get('verdict', '')}；"
                f"P(X级爆发)={pf if pf is not None else '—'}；P(无X级爆发)={pn if pn is not None else '—'}；"
                f"原始输出: {raw!r}"
            )
    return "\n".join(lines)


def build_jwflare_forecast_bundle(
    solar_data: Dict[str, Any], report_date: str
) -> Tuple[str, str]:
    """
    返回 (html_fragment, prompt_summary)。
    未启用或无可用数据时 ("", "")；错误配置时仍可能返回错误提示 HTML 与摘要。
    """
    from jwflare_config import (
        ar_crop_dir,
        jwflare_allow_path_fallback,
        jwflare_api_base,
        jwflare_data_root,
        jwflare_enabled,
        jwflare_http_timeout_s,
        jwflare_max_regions,
        jwflare_model,
        jwflare_transport,
        jwflare_upload_format,
        paths_for_date,
    )

    if not jwflare_enabled():
        return "", ""

    from jwflare_html import html_jwflare_forecast_section

    root = jwflare_data_root()
    if not root:
        rows_err = [
            {
                "noaa": "—",
                "error": True,
                "verdict": "",
                "detail": "未设置环境变量 JWFLARE_DATA_ROOT（或 HMI_FITS_CACHE）。",
            }
        ]
        return (
            html_jwflare_forecast_section(rows_err, embedded=True),
            _jwflare_rows_to_prompt_text(rows_err),
        )

    import imageio.v2 as imageio

    from jwflare_client import parse_ab_from_response, post_jwflare_inference
    from jwflare_hmi import ensure_fits_for_report_day
    from jwflare_infer_params import (
        build_full_jwflare_user_query,
        nl_length_and_unsigned_flux,
        png_to_magnetogram_minus_offset,
    )
    from jwflare_regions import select_key_regions
    from jwflare_track import track_ar_to_png_sequence

    api_base = jwflare_api_base()
    model = jwflare_model()
    max_r = jwflare_max_regions()
    timeout = jwflare_http_timeout_s()
    transport = jwflare_transport()
    upload_format = jwflare_upload_format()
    allow_path_fallback = jwflare_allow_path_fallback()

    regions = solar_data.get("active_regions") or []
    flares = solar_data.get("flares") or []
    if not regions:
        return "", ""

    noaa_list = select_key_regions(regions, flares, max_r)
    if not noaa_list:
        return "", ""

    full_disk_dir, _ = paths_for_date(root, report_date)
    fits_list, fits_err = ensure_fits_for_report_day(report_date, full_disk_dir)
    if fits_err and not fits_list:
        rows_err = [{"noaa": "—", "error": True, "verdict": "", "detail": fits_err}]
        return (
            html_jwflare_forecast_section(rows_err, embedded=True),
            _jwflare_rows_to_prompt_text(rows_err),
        )

    rows: List[Dict[str, Any]] = []
    by_noaa = {str(r.get("NOAA Number", "")).strip(): r for r in regions}

    for noaa in noaa_list:
        row_in = by_noaa.get(noaa)
        if not row_in:
            rows.append({"noaa": noaa, "error": True, "detail": "活动区不在当日列表中。"})
            continue

        out_dir = ar_crop_dir(root, report_date, noaa)
        os.makedirs(out_dir, exist_ok=True)

        try:
            pngs, terr = track_ar_to_png_sequence(row_in, fits_list, out_dir)
        except Exception as e:
            logger.exception("track_ar_to_png_sequence")
            rows.append({"noaa": noaa, "error": True, "detail": f"裁剪失败: {e}"})
            continue

        if terr or len(pngs) != 15:
            rows.append(
                {
                    "noaa": noaa,
                    "error": True,
                    "detail": terr or f"PNG 数量 {len(pngs)}≠15",
                }
            )
            continue

        frames = []
        try:
            for p in pngs:
                arr = imageio.imread(p)
                img = png_to_magnetogram_minus_offset(arr)
                frames.append(nl_length_and_unsigned_flux(img))
        except Exception as e:
            rows.append({"noaa": noaa, "error": True, "detail": f"参量计算失败: {e}"})
            continue

        try:
            query = build_full_jwflare_user_query(frames)
        except Exception as e:
            rows.append({"noaa": noaa, "error": True, "detail": f"构建查询失败: {e}"})
            continue

        j, err = post_jwflare_inference(
            api_base,
            model,
            pngs,
            query,
            timeout_s=timeout,
            transport=transport,
            upload_format=upload_format,
            allow_path_fallback=allow_path_fallback,
        )
        if err or not j:
            rows.append({"noaa": noaa, "error": True, "detail": err or "无响应"})
            continue

        label, p_a, p_b, raw = parse_ab_from_response(j)
        raw_s = raw if isinstance(raw, str) else str(raw)
        raw_excerpt = raw_s[:120]
        rows.append(
            {
                "noaa": noaa,
                "verdict": _verdict_label(label),
                "p_flare": p_a,
                "p_none": p_b,
                "detail": _row_detail_text(raw_s),
                "raw_excerpt": raw_excerpt,
            }
        )

    html_frag = html_jwflare_forecast_section(rows, embedded=True)
    prompt = _jwflare_rows_to_prompt_text(rows)
    return html_frag, prompt


def build_jwflare_forecast_html(solar_data: Dict[str, Any], report_date: str) -> str:
    """兼容旧调用：仅返回 HTML 片段（与 bundle 首元相同）。"""
    h, _ = build_jwflare_forecast_bundle(solar_data, report_date)
    return h
