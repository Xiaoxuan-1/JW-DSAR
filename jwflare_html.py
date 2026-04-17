"""重点活动区预报（JW-Flare）HTML 小节。"""
from __future__ import annotations

import html
from typing import Any, Dict, List


def html_jwflare_forecast_section(
    rows: List[Dict[str, Any]],
    disclaimer: str = "",
    *,
    embedded: bool = False,
) -> str:
    """
    rows 每项可含：noaa, verdict, p_flare, p_none, detail, error
    embedded=True：作为 §3 子块时使用 h3，弱化与顶栏分割（避免「双层底布」感）。
    """
    if not rows:
        return ""

    # 与正文一致：阿拉伯数字用 lining figures，避免衬线体旧式数字观感怪异
    # 单引号包裹 lnum，避免在 style="..." 内与外层双引号冲突导致 WeasyPrint 解析空值
    num_style = "font-variant-numeric: lining-nums; font-feature-settings: 'lnum' 1;"

    body_rows = []
    for r in rows:
        noaa = html.escape(str(r.get("noaa", "")))
        if r.get("error"):
            verdict = "—"
            p_f = "—"
            p_n = "—"
            detail = html.escape(str(r.get("detail", "")))
        else:
            verdict = html.escape(str(r.get("verdict", "—")))
            pf = r.get("p_flare")
            pn = r.get("p_none")
            p_f = f"{float(pf):.4f}" if pf is not None else "—"
            p_n = f"{float(pn):.4f}" if pn is not None else "—"
            detail = html.escape(str(r.get("detail", "")))
        body_rows.append(
            f"<tr><td style=\"padding:0.45rem 0.6rem;border:1px solid #e2e8f0;text-align:center;{num_style}\">{noaa}</td>"
            f"<td style=\"padding:0.45rem 0.6rem;border:1px solid #e2e8f0;text-align:center;\">{verdict}</td>"
            f"<td style=\"padding:0.45rem 0.6rem;border:1px solid #e2e8f0;text-align:center;{num_style}\">{p_f}</td>"
            f"<td style=\"padding:0.45rem 0.6rem;border:1px solid #e2e8f0;text-align:center;{num_style}\">{p_n}</td>"
            f"<td style=\"padding:0.45rem 0.6rem;border:1px solid #e2e8f0;text-align:left;font-size:0.88rem;{num_style}\">"
            f"{detail}</td></tr>"
        )

    disc = ""
    if disclaimer:
        disc = f'<p style="color:#94a3b8;font-size:0.82rem;margin:0.35rem 0 0 0;">{html.escape(disclaimer)}</p>'

    if embedded:
        sec_style = "margin:0.75rem 0;padding:0;border:none;"
        title_tag = (
            '<h3 style="color:#1e293b;font-size:1.05em;margin:0 0 0.4rem 0;font-weight:600;">'
            "重点活动区预报（JW-Flare）：</h3>"
        )
        intro_style = "color:#64748b;font-size:0.88rem;margin:0 0 0.5rem 0;"
    else:
        sec_style = "margin-top:2rem;padding-top:1.25rem;border-top:1px solid #e2e8f0;"
        title_tag = (
            '<h2 style="color:#0f172a;font-size:1.15em;margin:0 0 0.5rem 0;">重点活动区预报（JW-Flare）：</h2>'
        )
        intro_style = "color:#64748b;font-size:0.9rem;margin:0 0 0.75rem 0;"

    return (
        f'<section class="jwdsar-jwflare" style="{sec_style}">'
        f"{title_tag}"
        f'<p style="{intro_style}">'
        "由 JW-Flare 模型推断<strong>未来约 24 小时</strong>是否可能发生<strong>X级耀斑</strong>的极端事件。"
        "</p>"
        f"{disc}"
        '<div><table style="width:100%;border-collapse:collapse;font-size:0.92rem;">'
        "<thead><tr>"
        '<th style="padding:0.5rem;border:1px solid #e2e8f0;background:#f0f9ff;color:#0f172a;">NOAA</th>'
        '<th style="padding:0.5rem;border:1px solid #e2e8f0;background:#f0f9ff;color:#0f172a;">预报结论</th>'
        '<th style="padding:0.5rem;border:1px solid #e2e8f0;background:#f0f9ff;color:#0f172a;">P(X级爆发)</th>'
        '<th style="padding:0.5rem;border:1px solid #e2e8f0;background:#f0f9ff;color:#0f172a;">P(无X级爆发)</th>'
        '<th style="padding:0.5rem;border:1px solid #e2e8f0;background:#f0f9ff;color:#0f172a;">说明</th>'
        "</tr></thead><tbody>"
        + "".join(body_rows)
        + "</tbody></table></div></section>"
    )
