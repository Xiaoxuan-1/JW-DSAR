"""
JW-DSAR MCP 服务器（stdio）：供 Claude Code、OpenClaw 等客户端调用日报生成与数据查询。

运行（工作目录建议为项目根）：
  python mcp_server.py
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_ROOT = Path(__file__).resolve().parent
os.chdir(str(_ROOT))

try:
    from dotenv import load_dotenv

    _env = _ROOT / ".env"
    if _env.exists():
        load_dotenv(_env)
except ImportError:
    pass

from mcp.server.fastmcp import FastMCP

import app_scheduled as jw

mcp = FastMCP(
    "jw-dsar",
    instructions=(
        "JW-DSAR（金乌每日太阳活动报告）：从 NOAA/SWPC 等源聚合数据，可选 JW-Flare，"
        "调用 Qwen 生成中文太阳活动日报（HTML/PDF）。"
        "生成完整日报可能耗时数分钟；当天 UTC 日报在 23:50 前可能被业务规则拒绝。"
    ),
)

_MAX_DEFAULT_CHARS = 48_000


def _truncate(s: str, max_chars: int) -> str:
    if len(s) <= max_chars:
        return s
    return s[: max_chars - 20] + "\n…(已截断)…"


def _strip_heavy(obj: Any, depth: int = 0) -> Any:
    if depth > 14:
        return "<max_depth>"
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            ks = str(k)
            if ks == "url" and isinstance(v, str):
                if v.startswith("data:") or len(v) > 800:
                    out[ks] = f"<omitted len={len(v)}>"
                    continue
            out[ks] = _strip_heavy(v, depth + 1)
        return out
    if isinstance(obj, list):
        if len(obj) > 80:
            return [_strip_heavy(x, depth + 1) for x in obj[:80]] + [f"… +{len(obj) - 80} more"]
        return [_strip_heavy(x, depth + 1) for x in obj]
    if isinstance(obj, str) and len(obj) > 2000:
        return obj[:1500] + f"…(+{len(obj) - 1500} chars)"
    return obj


def _is_generate_error(html: str) -> bool:
    t = (html or "").lstrip()
    return t.startswith("<div class='error'>") or t.startswith('<div class="error">')


def _newest_report_html() -> Optional[tuple[str, Path]]:
    htmls = list(jw.REPORTS_PATH.glob("report_*.html"))
    if not htmls:
        return None
    newest = max(htmls, key=lambda p: p.stat().st_mtime)
    ds = newest.stem.replace("report_", "")
    return ds, newest


def _validate_utc_date(s: Optional[str]) -> Optional[str]:
    if s is None or not str(s).strip():
        return None
    t = str(s).strip()[:10]
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", t):
        raise ValueError("report_utc_date 须为 YYYY-MM-DD")
    datetime.strptime(t, "%Y-%m-%d")
    return t


@mcp.tool(
    name="jwdsar_list_reports",
    description="列出本地已保存的日报 UTC 日期（YYYY-MM-DD，新到旧）。",
)
def jwdsar_list_reports() -> str:
    dates = jw.get_all_report_dates()
    return json.dumps({"report_dates": dates, "reports_dir": str(jw.REPORTS_PATH)}, ensure_ascii=False, indent=2)


@mcp.tool(
    name="jwdsar_fetch_solar_data",
    description=(
        "获取指定 UTC 日的太阳数据快照（摘要版，省略大图 data-uri）。"
        "report_utc_date 为空时使用 NOAA 实时当日模式。"
    ),
)
def jwdsar_fetch_solar_data(
    report_utc_date: Optional[str] = None,
    max_json_chars: int = _MAX_DEFAULT_CHARS,
) -> str:
    try:
        d = _validate_utc_date(report_utc_date)
    except ValueError as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)
    raw = jw.fetch_solar_data(report_utc_date=d)
    slim = _strip_heavy(raw)
    text = json.dumps(slim, ensure_ascii=False, indent=2)
    return _truncate(text, max(4000, min(max_json_chars, 200_000)))


@mcp.tool(
    name="jwdsar_generate_report",
    description=(
        "生成完整太阳活动日报（写 report_YYYY-MM-DD.html，并尝试导出 PDF）。"
        "report_utc_date 为空则按业务规则使用当日；可能耗时很长。"
        "返回输出路径与 HTML 片段预览。"
    ),
)
def jwdsar_generate_report(
    report_utc_date: Optional[str] = None,
    sdo_resolution: Optional[int] = None,
    preview_max_chars: int = 8000,
) -> str:
    try:
        d = _validate_utc_date(report_utc_date)
    except ValueError as e:
        return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False, indent=2)

    html = jw.generate_report(
        report_utc_date=d,
        hmi_continuum_image_path=None,
        sdo_resolution=sdo_resolution,
    )
    if _is_generate_error(html):
        plain = re.sub(r"<[^>]+>", " ", html)
        plain = re.sub(r"\s+", " ", plain).strip()
        return json.dumps({"ok": False, "error": plain[:2000]}, ensure_ascii=False, indent=2)

    newest = _newest_report_html()
    out: dict[str, Any] = {
        "ok": True,
        "preview": _truncate(html, max(500, min(preview_max_chars, 50_000))),
    }
    if newest:
        ds, p = newest
        out["report_utc_date"] = ds
        out["html_path"] = str(p.resolve())
        pdf = p.with_suffix(".pdf")
        out["pdf_path"] = str(pdf.resolve()) if pdf.exists() else None
    return json.dumps(out, ensure_ascii=False, indent=2)


@mcp.tool(
    name="jwdsar_get_report_preview",
    description="读取已保存日报 HTML 的预览（默认截断，避免撑爆上下文）。",
)
def jwdsar_get_report_preview(
    report_utc_date: str,
    max_chars: int = 12000,
) -> str:
    try:
        d = _validate_utc_date(report_utc_date)
    except ValueError as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)
    if not d:
        return json.dumps({"error": "需要 report_utc_date"}, ensure_ascii=False)
    html = jw.load_report(d)
    if _is_generate_error(html) and "未找到" in html:
        return json.dumps(
            {"error": f"未找到 {d} 的日报", "reports_dir": str(jw.REPORTS_PATH)},
            ensure_ascii=False,
            indent=2,
        )
    p = jw.REPORTS_PATH / f"report_{d}.html"
    return json.dumps(
        {
            "report_utc_date": d,
            "html_path": str(p.resolve()) if p.exists() else None,
            "preview": _truncate(html, max(500, min(max_chars, 200_000))),
        },
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool(
    name="jwdsar_server_info",
    description="当前进程环境摘要（报告目录、UTC 时间、是否禁用 Qwen）。",
)
def jwdsar_server_info() -> str:
    return json.dumps(
        {
            "reports_dir": str(jw.REPORTS_PATH.resolve()),
            "utc_now": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "jwdsar_disable_qwen": bool(jw.JWDSAR_DISABLE_QWEN),
            "project_root": str(_ROOT.resolve()),
        },
        ensure_ascii=False,
        indent=2,
    )


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
