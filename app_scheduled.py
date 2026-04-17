import argparse
import gradio as gr
from bs4 import BeautifulSoup, NavigableString
from datetime import datetime, timezone, timedelta
import os
import base64
import json
import hashlib
import tarfile
from pathlib import Path
import logging
try:
    from dashscope import Generation, MultiModalConversation  # type: ignore
    import dashscope  # type: ignore
except Exception:  # pragma: no cover
    Generation = None  # type: ignore
    MultiModalConversation = None  # type: ignore
    dashscope = None  # type: ignore
from typing import Dict, List, Any, cast, Optional, Tuple
import markdown
import re
import requests
import math

from noaa_srs import (
    build_full_disk_image_list,
    html_sdo_gallery_section,
    normalize_noaa_region_id,
)

# 项目根目录下的 .env（与手动设置环境变量等价）
_root = os.path.dirname(os.path.abspath(__file__))
_dotenv_path = os.path.join(_root, ".env")
try:
    from dotenv import load_dotenv

    if os.path.exists(_dotenv_path):
        load_dotenv(_dotenv_path)
except ImportError:
    pass
# 设置 DashScope API 基础 URL（支持 qwen3-max）
if dashscope is not None:
    dashscope.base_http_api_url = "https://dashscope.aliyuncs.com/api/v1"

# 设置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

JWDSAR_DISABLE_QWEN = bool(
    str(os.getenv("JWDSAR_DISABLE_QWEN", "0")).strip().lower() in ("1", "true", "yes", "y")
)

def _jwdsar_http_cache_dir() -> str:
    """HTTP 源数据缓存目录（存在则复用，避免重复下载）。"""
    base = os.getenv("JWDSAR_HTTP_CACHE_DIR")
    if base and str(base).strip():
        return str(base).strip()
    return os.path.join(_root, "data", "http_cache")


def _safe_cache_name(url: str) -> str:
    """将 URL 映射为稳定的文件名片段。"""
    u = str(url or "").strip()
    h = hashlib.sha1(u.encode("utf-8")).hexdigest()[:12]
    tail = re.sub(r"[^A-Za-z0-9._-]+", "_", u.split("/")[-1] or "resource")
    return f"{tail}.{h}"


def _read_json_cache(path: str) -> Optional[Any]:
    try:
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as ex:
        logger.warning("读取缓存失败 %s: %s", path, ex)
    return None


def _write_json_cache(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def _load_day_snapshot_or_download_json(
    *,
    snapshot_path: Optional[str],
    url: str,
    timeout: int = 60,
    expect_type: Optional[type] = None,
    source_name: str = "json",
) -> Optional[Any]:
    """
    按“冻结快照”策略读取 JSON：
    1) 若 data/YYYY-MM-DD 下快照存在且类型有效，直接复用；
    2) 否则下载一次并写入快照，后续运行复用本地。
    """
    if snapshot_path:
        cached = _read_json_cache(snapshot_path)
        if cached is not None and (expect_type is None or isinstance(cached, expect_type)):
            logger.info("%s snapshot local hit: %s", source_name, snapshot_path)
            return cached
    try:
        r = requests.get(url, timeout=timeout)
        if r.status_code != 200:
            logger.warning("%s 下载失败：HTTP %s", source_name, r.status_code)
            return None
        data = r.json()
        if expect_type is not None and not isinstance(data, expect_type):
            logger.warning("%s 下载结果类型异常：%s", source_name, type(data).__name__)
            return None
        if snapshot_path:
            try:
                _write_json_cache(snapshot_path, data)
            except Exception as ex:
                logger.warning("%s 快照写入失败 %s: %s", source_name, snapshot_path, ex)
        return data
    except Exception as ex:
        logger.warning("%s 下载失败: %s", source_name, ex)
        return None


def _http_get_json_cached(url: str, *, cache_key: str, timeout: int = 60) -> Optional[Any]:
    """
    下载 JSON 源数据；若缓存文件已存在且非空，则直接复用并跳过下载。
    """
    cache_dir = _jwdsar_http_cache_dir()
    os.makedirs(cache_dir, exist_ok=True)
    cache_path = os.path.join(cache_dir, f"{cache_key}.json")
    cached = _read_json_cache(cache_path)
    if cached is not None:
        return cached
    try:
        r = requests.get(url, timeout=timeout)
        if r.status_code != 200:
            return None
        data = r.json()
        _write_json_cache(cache_path, data)
        return data
    except Exception as ex:
        logger.warning("下载 JSON 失败 %s: %s", url, ex)
        return None


def _read_secret_file(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    p = str(path).strip()
    if not p:
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            v = f.read().strip()
        return v or None
    except Exception as ex:
        logger.warning("读取密钥文件失败: %s (%s)", p, ex)
        return None


def _warn_if_dotenv_permissions_too_open(path: str) -> None:
    """提示 .env 权限过宽，避免密钥被其他用户读取（不阻断运行）。"""
    try:
        st = os.stat(path)
        if (st.st_mode & 0o077) != 0:
            logger.warning(
                ".env 权限过宽（建议 chmod 600 %s），当前 mode=%o",
                path,
                st.st_mode & 0o777,
            )
    except Exception:
        return


if os.path.exists(_dotenv_path):
    _warn_if_dotenv_permissions_too_open(_dotenv_path)

# 报告日：触发时刻所在 UTC 日；与计划中的 cron（UTC 23:50 ≈ 北京次日 07:50）一致
REPORT_SCHEDULE_NOTE = (
    "报告日：触发时刻所在的 UTC 日历日。建议 crontab 使用 UTC，在每日 23:50 执行 "
    "（对应北京时间次日 07:50）。活动区分类：McIntosh 列使用 solar_regions.json 的 spot_class，"
    "Hale 使用 mag_class（不再合并 SRS 文本）。"
)

# 日报 Markdown 输出契约（与已生成 HTML 结构对齐，减少模型漂移）
REPORT_SYSTEM_VL = (
    "你是资深太阳物理与空间天气分析助手。用户消息中可能包含按顺序给出的 NASA SDO 全日面示意图（多波段）"
    "以及文字数据与历史日报节选。"
    "你必须严格遵守用户给出的「输出结构」：章节编号、标题措辞、§2 表格列名与列数不得改动；"
    "不得编造数据中未出现的数值；图像为近实时浏览图，与具体耀斑时刻未必逐点对齐。"
    "勿在正文中撰写「全日面影像」整节（该节由程序在网页末尾追加）。"
    "「重点活动区预报（JW-Flare）」表格由程序插入在 §3 大模型分析内；"
    "你须在 §3 正文中结合用户消息中的 JW-Flare 摘要做分析，勿在 Markdown 中手写该 HTML 表，勿逐格复述概率。"
    "请严格区分时间口径：报告中 NOAA/事件/GOES 是“报告日当日观测”，而 JW-Flare 是“未来24小时预测”。"
    "禁止用“当日观测”去验证“当日 JW-Flare 预测是否命中”；若需验证，只能用“当日观测”评估“前一日 JW-Flare 预测”。"
    "若缺少前一日预测证据，请明确写“无法完成命中验证”，但仍可基于当日磁场与活动水平评价当日 JW-Flare 预测是否合理。"
)

REPORT_SYSTEM_TEXT = (
    "你是资深太阳物理与空间天气分析助手。请仅依据用户提供的文本数据作答，不编造。"
    "你必须严格遵守用户给出的「输出结构」：章节编号、标题措辞、§2 表格列名与列数不得改动。"
    "勿在正文中撰写「全日面影像」整节。"
    "JW-Flare 预报表由程序插入 §3；你须在 §3 结合摘要分析，勿手写该表、勿逐格抄概率。"
    "请严格区分时间口径：NOAA/事件/GOES 是报告日当日观测，JW-Flare 是未来24小时预测。"
    "不要用当日观测验证当日预测；若需验证，只能用当日观测回看前一日预测。"
    "若无前一日预测证据，请明确“无法完成命中验证”，并仅做合理性评估。"
)


def _report_output_instructions(rd: str) -> str:
    """固定版式说明 + few-shot 锚点（节选）。"""
    return f"""
【输出结构（必须严格遵守）】
1. 只使用下面规定的章节标题与编号（共 6 节），使用 Markdown。不要增加额外的 `#` 级主标题，不要另起与下列编号冲突的章节。
2. 除表格与列表外可使用正常段落；全文中文。
3. **禁止**在文中包含「全日面影像」「SDO 图库」等独立章节（该内容由程序追加到网页）。
3b. **JW-Flare**：程序会在 **§3 大模型分析** 内插入「重点活动区预报（JW-Flare）」表格（若已启用）；你**不要**在 Markdown 中手写该表或重复表格中的数字。若用户消息提供了 JW-Flare 摘要，请在 §3 正文中概括性引用其结论并展开物理含义，避免空洞罗列。
3c. **JW-Flare 时间口径（必须遵守）**：当日 NOAA/事件/GOES 仅代表“报告日当天观测”；JW-Flare 表格代表“未来24小时预测”。禁止用“当日观测”验证“当日预测命中”。若要做命中验证，只能用“当日观测”回看“前一日 JW-Flare 预测”（若用户消息提供了前一日报告/摘要）。若缺少前一日预测证据，请明确说明“无法完成命中验证”，并改为“合理性评估”。
4. 报告主体以报告 UTC 当日为主；仅在 §3 的 JW-Flare 预测评估中允许引用“前一日预测”作验证口径说明。
5. **列表样式（仅限 §3 该小节）**：在 **§3 的「### 活动区演化与磁场结构：」** 小节中若需要逐条点评多个 NOAA 活动区，请使用 **Markdown 无序列表**（每条以 `- ` 开头，渲染为圆点列表）；不要使用数字编号。

【必须输出的章节与标题（逐字）】
# 太阳活动日报 - {rd}

## 1. 总体评估
（1～3 段，概述当日太阳活动水平、有无 C 级以上耀斑、整体风险印象。）

## 2. 活动区域详情
必须使用 **GitHub 风格管道表**，且表头**逐字**为下列 8 列（不得增删、不改列名）：

| NOAA编号 | 位置 | Hale分类 | McIntosh分类 | 面积 | 黑子数 | 耀斑(当日) | 风险评估 |
|---------|------|---------|-------------|------|--------|------------|----------|

每个活动区一行；「耀斑(当日)」列写当日 C+ 事件简称或「无」；数据缺失写「数据未提供」或「N/A」与上文一致。

## 3. 大模型分析
先写一小段总起；若用户消息含 **JW-Flare 预报摘要**，请结合摘要与 NOAA/图像做论述（概括趋势与重点区域，勿逐格抄表）。
并在本节中明确时间口径：当日观测 vs 未来24小时预测；若消息含前一日预测证据，可做“昨日预测-今日观测”验证，否则仅做合理性评价。

本节正文中**必须出现且仅出现**以下两个三级小标题（逐字）：
### 活动区演化与磁场结构：
### 耀斑事件分析：

在这两个小标题下分别展开分析（可按数据删减，但需有分析性内容，勿只重复表格）。其中在「### 活动区演化与磁场结构：」小节内若需要逐条点评多个 NOAA 活动区，请使用 Markdown 无序列表逐条写出（例如 `- NOAA xxxx(...)：...`）。

## 4. 空间天气影响预测
**必须恰好三条**，格式与下面一致（冒号后为结论与简短解释）：
- **耀斑活动风险**:
- **日冕物质抛射风险**:
- **地磁暴可能性**:

## 5. 未来重点关注区域
使用列表，每条尽量包含 **NOAA 编号** 与关注原因。

## 6. 建议
若干条列表项，面向监测与科研关注；勿以行政机构口吻下达指令。

【版式示例（节选，说明语气与表格样式；请用上方「数据」替换示例中的假数据）】
# 太阳活动日报 - {rd}

## 1. 总体评估
当日太阳活动水平中等。可见日面上有数个活动区，其中 NOAA 1234 产生 C 级耀斑，…

## 2. 活动区域详情

| NOAA编号 | 位置 | Hale分类 | McIntosh分类 | 面积 | 黑子数 | 耀斑(当日) | 风险评估 |
|---------|------|---------|-------------|------|--------|------------|----------|
| 1234 | N12E05 | Beta | Dao | 80 | 5 | C1.0 | 中 |

（其余章节略，按前述要求写完。）
"""


def _warn_if_report_markdown_looks_wrong(md: str) -> None:
    """低成本版式检查，不阻断保存。"""
    if not md or md.strip().startswith("Error:"):
        return
    checks = [
        ("## 2. 活动区域详情", "## 2."),
        ("| NOAA编号 | 位置 | Hale分类 | McIntosh分类 | 面积 | 黑子数 | 耀斑(当日) | 风险评估 |", "表头"),
        ("## 3. 大模型分析", "## 3."),
        ("## 4. 空间天气影响预测", "## 4."),
        ("## 5. 未来重点关注区域", "## 5."),
    ]
    for needle, name in checks:
        if needle not in md:
            logger.warning("日报版式检查未通过：缺少 %s", name)


SECTION3_H2_TEXT = "3. 大模型分析"


def _inject_jwflare_after_section3_intro(html_content: str, fragment: str) -> str:
    """在「3. 大模型分析」下第一段 <p> 之后插入 JW-Flare HTML；失败则附于文末。"""
    if not (fragment and fragment.strip()):
        return html_content
    soup = BeautifulSoup(html_content, "html.parser")
    target = None
    for h2 in soup.find_all("h2"):
        if h2.get_text(strip=True) == SECTION3_H2_TEXT:
            target = h2
            break
    if not target:
        logger.warning("未找到「%s」标题，JW-Flare 表附于正文末尾", SECTION3_H2_TEXT)
        return html_content.rstrip() + fragment

    insert_after = target
    p = target.find_next_sibling("p")
    if p is not None:
        insert_after = p

    wrap = BeautifulSoup(f"<body>{fragment}</body>", "html.parser")
    ref = insert_after
    for child in list(wrap.body.children):
        if isinstance(child, str) and not str(child).strip():
            continue
        ref.insert_after(child)
        ref = child
    return str(soup)


def _wrap_report_html_for_mathjax(inner_html: str) -> str:
    """为报告注入 MathJax 3，在浏览器中渲染 $...$ / $$...$$ 等 LaTeX（纯 Markdown 不会处理公式）。"""
    if not inner_html or not inner_html.strip():
        return inner_html
    # 冻结 HTML 默认禁止外链脚本，避免“离线/跨时刻打开同一文件”出现外部依赖差异。
    enable_cdn = str(os.getenv("JWDSAR_ENABLE_MATHJAX_CDN", "0")).strip().lower() in ("1", "true", "yes", "y")
    if not enable_cdn:
        return inner_html
    s = inner_html.lstrip()
    if s.startswith("<div class='error'>") or s.startswith('<div class="error">'):
        return inner_html
    if "MathJax-script" in inner_html:
        return inner_html
    # tex-chtml：与常见行内公式 $...$ 兼容；需联网加载 CDN
    mj = (
        "<script>\n"
        "window.MathJax = {\n"
        "  tex: {\n"
        "    inlineMath: [['$', '$'], ['\\\\(', '\\\\)']],\n"
        "    displayMath: [['$$', '$$'], ['\\\\[', '\\\\]']],\n"
        "    processEscapes: true\n"
        "  },\n"
        "  options: { skipHtmlTags: ['script', 'noscript', 'style', 'textarea', 'pre', 'code'] }\n"
        "};\n"
        "</script>\n"
        '<script defer src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-chtml.js" id="MathJax-script"></script>\n'
    )
    return f'<div class="jwdsar-report-math">{mj}{inner_html}</div>'


def _try_convert_latex_to_mathml(latex: str, *, display: bool = False) -> Optional[str]:
    try:
        from latex2mathml.converter import convert
    except ImportError:
        return None
    s = latex.strip()
    try:
        return convert(s, display=("block" if display else "inline"))
    except TypeError:
        try:
            return convert(s)
        except Exception:
            return None
    except Exception:
        return None


def _preprocess_latex_for_mathml(latex: str) -> str:
    """预处理 LaTeX，移除或替换 latex2mathml 不支持的命令。"""
    # 将 \text{...} 替换为空格 + 内容（仅保留文本，不包装）
    s = re.sub(r"\\text\{([^}]*)\}", r" \1 ", latex)
    # 其他可能需要处理的命令...
    return s


def _replace_dollar_tex_in_string(s: str) -> str:
    if "$" not in s:
        return s

    def repl_dd(m) -> str:
        inner = m.group(1).strip()
        inner = _preprocess_latex_for_mathml(inner)
        ml = _try_convert_latex_to_mathml(inner, display=True)
        return f'<div class="jwdsar-math-display">{ml}</div>' if ml else m.group(0)

    def repl_i(m) -> str:
        inner = m.group(1).strip()
        inner = _preprocess_latex_for_mathml(inner)
        ml = _try_convert_latex_to_mathml(inner, display=False)
        return ml if ml else m.group(0)

    out = re.sub(r"\$\$([\s\S]+?)\$\$", repl_dd, s)
    out = re.sub(r"\$([^\$\n]+?)\$", repl_i, out)
    return out


def _inject_mathml_from_dollars(soup: BeautifulSoup) -> None:
    for text in list(soup.find_all(string=True)):
        if not isinstance(text, NavigableString):
            continue
        parent = getattr(text, "parent", None)
        if parent is not None and parent.name in ("script", "style", "math", "noscript"):
            continue
        raw = str(text)
        if "$" not in raw:
            continue
        new_s = _replace_dollar_tex_in_string(raw)
        if new_s == raw:
            continue
        frag = BeautifulSoup(f"<body>{new_s}</body>", "html.parser")
        parts = list(frag.body.contents) if frag.body else []
        if not parts:
            continue
        for node in parts:
            text.insert_before(node)
        text.extract()


def _html_for_weasyprint(body_html: str, solar_data: Optional[Dict[str, Any]]) -> str:
    """供 WeasyPrint：去 MathJax、图库紧凑版、$...$ → MathML。"""
    soup = BeautifulSoup(body_html, "html.parser")
    for tag in soup.find_all("script"):
        tag.decompose()
    wrap = soup.find("div", class_="jwdsar-report-math")
    if wrap is not None:
        wrap.unwrap()
    gal = soup.select_one("section#jwdsar-sdo-gallery")
    if gal is not None and solar_data:
        imgs = solar_data.get("full_disk_images")
        if imgs:
            reg = solar_data.get("active_regions")
            rep = BeautifulSoup(
                html_sdo_gallery_section(
                    cast(List[Dict[str, str]], imgs),
                    cast(Optional[List[Dict[str, str]]], reg),
                    compact_for_pdf=True,
                ),
                "html.parser",
            )
            new_sec = rep.find("section")
            if new_sec is not None:
                gal.replace_with(new_sec)
    _inject_mathml_from_dollars(soup)
    return str(soup)


def _report_day_fields(now_utc: datetime) -> Dict[str, str]:
    d = now_utc.strftime("%Y-%m-%d")
    return {
        "date": d,
        "report_utc_date": d,
        "_report_schedule_note": REPORT_SCHEDULE_NOTE,
    }


# 从环境变量获取配置（支持 .env）
# 推荐把真实 Key 放在单独文件里，然后用 DASHSCOPE_API_KEY_FILE 指向该文件路径（避免直接写入 .env）。
DASHSCOPE_API_KEY = os.getenv("DASHSCOPE_API_KEY") or _read_secret_file(os.getenv("DASHSCOPE_API_KEY_FILE"))
DASHSCOPE_VL_MODEL = os.getenv("DASHSCOPE_VL_MODEL", "qwen3.6-plus")
DASHSCOPE_TEXT_MODEL = os.getenv("DASHSCOPE_TEXT_MODEL", DASHSCOPE_VL_MODEL)
MAX_VL_DISK_IMAGES = max(1, int(os.getenv("MAX_VL_DISK_IMAGES", "10")))
PRIOR_REPORT_MAX_CHARS = int(os.getenv("PRIOR_REPORT_MAX_CHARS", "6000"))


def _parse_event_dt_utc(ev: Dict[str, Any]) -> Optional[datetime]:
    for k in ("max_datetime", "begin_datetime", "end_datetime"):
        v = ev.get(k)
        if not v:
            continue
        s = str(v).replace("Z", "+00:00") if str(v).endswith("Z") else str(v)
        try:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)
            return dt
        except Exception:
            continue
    return None


def _event_class_label(ev: Dict[str, Any]) -> str:
    p1 = ev.get("particulars1")
    if p1:
        return str(p1).strip()
    return str(ev.get("type") or "UNK")


def _is_c_class_or_above(class_label: str) -> bool:
    """仅 C / M / X 视为耀斑；B / A 级及以下不统计、不写入。"""
    s = (class_label or "").strip()
    if not s:
        return False
    return s[0].upper() in ("C", "M", "X")


def _parse_swpc_time_tag_utc(s: Any) -> Optional[datetime]:
    """解析 GOES x-rays JSON 的 time_tag 等为 UTC aware datetime。"""
    if s is None:
        return None
    raw = str(s).strip()
    if not raw or raw == "N/A":
        return None
    if raw.endswith("Z"):
        raw = raw.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        try:
            dt = datetime.strptime(raw[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt


def _fetch_edited_events_xra_fla(hours: int = 96, cap: int = 500) -> List[Dict[str, Any]]:
    # 实时数据：不做本地缓存，确保每次运行都下载最新内容
    try:
        r = requests.get(
            "https://services.swpc.noaa.gov/json/edited_events.json",
            timeout=90,
        )
        if r.status_code != 200:
            return []
        data = r.json()
    except Exception as ex:
        logger.warning("edited_events.json 获取失败: %s", ex)
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    filtered: List[Dict[str, Any]] = []
    for ev in data:
        if ev.get("type") not in ("XRA", "FLA"):
            continue
        dt = _parse_event_dt_utc(ev)
        if not dt or dt < cutoff:
            continue
        filtered.append(ev)
    filtered.sort(
        key=lambda e: _parse_event_dt_utc(e) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return filtered[:cap]


def _parse_report_utc_date_or_today(report_utc_date: Optional[str]) -> datetime:
    """将 YYYY-MM-DD 解析为 UTC datetime（00:00）；为空则取当前 UTC。"""
    if report_utc_date:
        s = str(report_utc_date).strip()[:10]
        try:
            d = datetime.strptime(s, "%Y-%m-%d").date()
            return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
        except ValueError:
            logger.warning("report_utc_date 解析失败: %s，回退到当前 UTC 日", report_utc_date)
    now = datetime.now(timezone.utc)
    return datetime(now.year, now.month, now.day, tzinfo=timezone.utc)


def _fetch_edited_events_xra_fla_for_day(
    report_day_utc: datetime,
    cap: int = 500,
    *,
    archive_day_dir: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """读取 SWPC edited_events.json（本地快照优先），并过滤为报告 UTC 当日。"""
    snapshot_path = os.path.join(archive_day_dir, "edited_events.json") if archive_day_dir else None
    data = _load_day_snapshot_or_download_json(
        snapshot_path=snapshot_path,
        url="https://services.swpc.noaa.gov/json/edited_events.json",
        timeout=90,
        expect_type=list,
        source_name="edited_events.json",
    )
    if not isinstance(data, list):
        return []
    report_d = report_day_utc.date()
    filtered: List[Dict[str, Any]] = []
    for ev in data:
        if ev.get("type") not in ("XRA", "FLA"):
            continue
        dt = _parse_event_dt_utc(ev)
        if not dt or dt.date() != report_d:
            continue
        filtered.append(ev)
    filtered.sort(
        key=lambda e: _parse_event_dt_utc(e) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return filtered[:cap]


def _http_get_text(url: str, *, timeout: int = 60) -> Optional[str]:
    try:
        r = requests.get(url, timeout=timeout)
        if r.status_code != 200:
            return None
        r.encoding = r.encoding or "utf-8"
        return r.text
    except Exception as ex:
        logger.warning("下载失败 %s: %s", url, ex)
        return None


def _ncei_srs_urls(report_utc_date_str: str) -> List[str]:
    """NCEI Solar Region Summary (SRS) 归档候选 URL（不同目录可能大小写/命名略有差异）。"""
    ymd = report_utc_date_str.replace("-", "")
    y, m, _ = report_utc_date_str.split("-")
    base = "https://www.ngdc.noaa.gov/stp/space-weather/swpc-products/daily_reports/solar_region_summaries"
    # NCEI 既存在“按年月子目录”的新路径，也可能存在老的平铺路径；这里两者都试
    bases = [f"{base}/{y}/{m}", base]
    out: List[str] = []
    for b in bases:
        out.extend([f"{b}/{ymd}SRS.txt", f"{b}/{ymd}srs.txt", f"{b}/{ymd}Srs.txt"])
    return out


def _ncei_events_urls(report_utc_date_str: str) -> List[str]:
    """NCEI Solar and Geophysical Event Reports 归档候选 URL。"""
    ymd = report_utc_date_str.replace("-", "")
    y, m, _ = report_utc_date_str.split("-")
    base = "https://www.ngdc.noaa.gov/stp/space-weather/swpc-products/daily_reports/solar_event_reports"
    bases = [f"{base}/{y}/{m}", base]
    out: List[str] = []
    for b in bases:
        out.extend([f"{b}/{ymd}events.txt", f"{b}/{ymd}Events.txt"])
    return out


def _write_text_cache(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _read_text_file_best_effort(path: str) -> Optional[str]:
    for enc in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            with open(path, "r", encoding=enc) as f:
                return f.read()
        except Exception:
            continue
    return None


def _decode_bytes_best_effort(b: bytes) -> Optional[str]:
    for enc in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return b.decode(enc)
        except Exception:
            continue
    return None


def _read_archive_text_from_local_store(
    report_utc_date_str: str,
    *,
    kind: str,
) -> Tuple[Optional[str], Optional[str], List[str]]:
    """
    本地归档读取：
    - data/SRS 或 data/events 下，支持“年压缩包 .tar.gz”与“年目录”两种形态
    - 按报告日匹配 YYYYMMDDSRS.txt / YYYYMMDDevents.txt（含大小写变体）
    返回: (文本内容, 命中来源描述, 已检查项列表)
    """
    ymd = report_utc_date_str.replace("-", "")
    year = ymd[:4]
    if kind == "srs":
        store_dir = os.path.join(_root, "data", "SRS")
        names = [f"{ymd}SRS.txt", f"{ymd}srs.txt", f"{ymd}Srs.txt"]
    else:
        store_dir = os.path.join(_root, "data", "events")
        names = [f"{ymd}events.txt", f"{ymd}Events.txt"]
    wanted = set(n.lower() for n in names)

    checked: List[str] = []
    if not os.path.isdir(store_dir):
        checked.append(f"missing_dir:{store_dir}")
        return None, None, checked

    # 1) 先试根目录直放文件
    for n in names:
        p = os.path.join(store_dir, n)
        checked.append(p)
        if os.path.isfile(p) and os.path.getsize(p) > 0:
            t = _read_text_file_best_effort(p)
            if t:
                return t, p, checked

    # 2) 再试以年份开头的目录/压缩包（如 2026_SRS/2026_events、2024_SRS.tar.gz）
    try:
        entries = sorted(os.listdir(store_dir))
    except Exception:
        entries = []
    candidates = [os.path.join(store_dir, n) for n in entries if str(n).startswith(year)]
    checked.extend(candidates)

    # 2a) 目录：递归查找目标文件
    for p in candidates:
        if not os.path.isdir(p):
            continue
        for root, _, files in os.walk(p):
            for fn in files:
                if fn.lower() not in wanted:
                    continue
                fp = os.path.join(root, fn)
                if not (os.path.isfile(fp) and os.path.getsize(fp) > 0):
                    continue
                t = _read_text_file_best_effort(fp)
                if t:
                    return t, fp, checked

    # 2b) tar.gz：按成员 basename 匹配目标文件
    for p in candidates:
        if not (os.path.isfile(p) and str(p).lower().endswith(".tar.gz")):
            continue
        try:
            with tarfile.open(p, "r:gz") as tf:
                for m in tf.getmembers():
                    if not m.isfile():
                        continue
                    bn = os.path.basename(m.name).lower()
                    if bn not in wanted:
                        continue
                    fobj = tf.extractfile(m)
                    if fobj is None:
                        continue
                    raw = fobj.read()
                    if not raw:
                        continue
                    t = _decode_bytes_best_effort(raw)
                    if t:
                        return t, f"{p}:{m.name}", checked
        except Exception as ex:
            logger.warning("读取本地归档失败 %s: %s", p, ex)
            continue
    return None, None, checked


def _parse_srs_regions(text: str) -> List[Dict[str, str]]:
    """解析 SRS 文本中的 'I. Regions with Sunspots' 表格为活动区列表。"""
    if not text:
        return []
    lines = [ln.rstrip("\n") for ln in text.splitlines()]
    # 定位表头：通常包含 'Nmbr' 与 'Mag Type'
    header_idx = -1
    for i, ln in enumerate(lines):
        if "Nmbr" in ln and "Location" in ln and ("Mag" in ln or "Mag Type" in ln):
            header_idx = i
            break
    if header_idx < 0:
        # 退化：找 'Regions with Sunspots' 段落后第一行表头
        for i, ln in enumerate(lines):
            if "Regions with Sunspots" in ln:
                header_idx = i
                break
    if header_idx < 0:
        return []

    out: List[Dict[str, str]] = []
    # 表格行一般紧随表头后若干行；遇到空行或 'IA.' / 'II.' 等段落结束
    for ln in lines[header_idx + 1 :]:
        s = ln.strip()
        if not s:
            if out:
                break
            continue
        if s.startswith("IA.") or s.startswith("II.") or s.startswith("III.") or s.startswith("COMMENT"):
            break
        # 典型列：Nmbr Location Lo Area Z LL NN MagType
        # 注意：NCEI 文本里经常混用“单空格 + 对齐空格”，用 2+ 空格 split 会把
        # “Nmbr Location” 粘成一列，导致解析失败；因此这里以“空白分词”为主。
        parts = s.split()
        if len(parts) < 8:
            continue
        nmbr = parts[0]
        location = parts[1]
        area = parts[3]
        z = parts[4]
        ll = parts[5]
        nn = parts[6]
        mag = parts[7]
        if len(parts) > 8:
            mag = " ".join(parts[7:]).strip()
        if not re.match(r"^\d{3,6}$", nmbr):
            continue
        if not re.match(r"^[NS]\d{1,2}[EW]\d{1,3}\*?$", str(location)):
            continue
        spots = nn if re.match(r"^\d+$", str(nn)) else "N/A"
        out.append(
            {
                "NOAA Number": normalize_noaa_region_id(nmbr),
                "Position": str(location),
                "Hale Class": str(mag),
                "McIntosh Class": str(z),
                "Area": str(area),
                "Spots": str(spots),
                "Flares": "无",
            }
        )
    return out


def _parse_srs_locations_valid_time_utc(text: str, report_utc_date_str: str) -> Optional[datetime]:
    """
    从 SRS 头部解析 “Locations Valid at DD/HHMMZ” 作为位置有效时刻（UTC）。
    例如：report=2026-04-02 且行含 “Locations Valid at 01/2400Z”
    则有效时刻为 2026-04-02T00:00:00Z（2400 视作次日 00:00）。
    """
    if not text:
        return None
    m = re.search(r"Locations\\s+Valid\\s+at\\s+(\\d{2})/(\\d{4})Z", text)
    if not m:
        return None
    day_s, hhmm_s = m.group(1), m.group(2)
    try:
        base = datetime.strptime(report_utc_date_str[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        day = int(day_s)
        hhmm = int(hhmm_s)
    except Exception:
        return None
    hh = hhmm // 100
    mm = hhmm % 100
    if hh == 24 and mm == 0:
        hh = 0
        mm = 0
        carry_day = 1
    else:
        carry_day = 0
    # 用报告月/年 + 头部的 day 构造日期；若 2400 则 +1 天
    try:
        d0 = datetime(base.year, base.month, day, tzinfo=timezone.utc)
    except Exception:
        return None
    dt = datetime(d0.year, d0.month, d0.day, hh, mm, tzinfo=timezone.utc) + timedelta(days=carry_day)
    return dt


def _parse_events_flares(text: str, report_utc_date_str: str) -> List[Dict[str, str]]:
    """从 events.txt 中粗略提取 XRA/FLA 的 C+ 耀斑信息（用于日报分析与活动区 'Flares' 列）。"""
    if not text:
        return []
    out: List[Dict[str, str]] = []
    for ln in text.splitlines():
        if " XRA " not in f" {ln} " and " FLA " not in f" {ln} ":
            continue
        # 提取等级：C/M/X 开头
        m_cls = re.search(r"\b([CMX]\d+(?:\.\d+)?)\b", ln)
        if not m_cls:
            continue
        cls = m_cls.group(1)
        if not _is_c_class_or_above(cls):
            continue
        # 提取时间：常见为 4 位 HHMM
        m_t = re.search(r"\b(\d{4})\b", ln)
        t_str = "N/A"
        if m_t:
            hhmm = m_t.group(1)
            try:
                hh = int(hhmm[:2])
                mm = int(hhmm[2:])
                if 0 <= hh <= 23 and 0 <= mm <= 59:
                    t_str = f"{report_utc_date_str} {hh:02d}:{mm:02d} UTC"
            except Exception:
                pass
        # 提取 NOAA 区号：优先 4-6 位数字（与 SRS 区号相同），取最后一个出现的
        rid = "N/A"
        nums = re.findall(r"\b(\d{4,6})\b", ln)
        if nums:
            rid = normalize_noaa_region_id(nums[-1])
        out.append(
            {
                "Class": cls,
                "Time": t_str,
                "NOAA Region": rid,
                "Source": "SWPC_EVENTS_TXT",
            }
        )
    return out


def _apply_flare_strings_to_regions_simple(
    regions: List[Dict[str, str]], flares: List[Dict[str, str]]
) -> None:
    """将 flares 列表按 NOAA Region 合并到 regions[*]['Flares']。"""
    by_rid: Dict[str, List[str]] = {}
    for fl in flares:
        rid = str(fl.get("NOAA Region") or "").strip()
        cls = str(fl.get("Class") or "").strip()
        if not rid or rid == "N/A" or not cls:
            continue
        by_rid.setdefault(rid, []).append(cls)
    for r in regions:
        rid = str(r.get("NOAA Number") or "").strip()
        if not rid:
            continue
        arr = by_rid.get(rid)
        if not arr:
            continue
        # 去重并保持相对稳定顺序
        uniq: List[str] = []
        for x in arr:
            if x not in uniq:
                uniq.append(x)
        r["Flares"] = ",".join(uniq)


def _fetch_solar_data_ncei_archive(report_utc_date_str: str) -> Dict[str, Any]:
    """从 NCEI（NGDC）SWPC 长期归档拉取指定日期的 SRS + events 并解析。"""
    logger.info("Fetching solar data from NCEI archive for %s ...", report_utc_date_str)
    # 历史源数据落盘：统一放入 data/YYYY-MM-DD/ 下，便于复用与离线
    day_dir = os.path.join(_root, "data", report_utc_date_str[:10])
    ymd = report_utc_date_str.replace("-", "")
    srs_candidates = [
        os.path.join(day_dir, "SRS.txt"),
        os.path.join(day_dir, f"{ymd}SRS.txt"),
        os.path.join(day_dir, f"{ymd}srs.txt"),
        os.path.join(day_dir, f"{ymd}Srs.txt"),
    ]
    events_candidates = [
        os.path.join(day_dir, "events.txt"),
        os.path.join(day_dir, f"{ymd}events.txt"),
        os.path.join(day_dir, f"{ymd}Events.txt"),
    ]
    report_dt_utc = _parse_report_utc_date_or_today(report_utc_date_str)

    srs_txt = None
    for p in srs_candidates:
        if os.path.isfile(p) and os.path.getsize(p) > 0:
            try:
                with open(p, "r", encoding="utf-8") as f:
                    srs_txt = f.read()
                logger.info("SRS local hit: %s", p)
                break
            except Exception:
                srs_txt = None
    # data/SRS 年归档：支持 tar.gz 与目录两种形态
    if not srs_txt:
        srs_txt, srs_src, srs_checked = _read_archive_text_from_local_store(report_utc_date_str, kind="srs")
        if srs_txt:
            logger.info("SRS archive local hit: %s", srs_src or "data/SRS")
            try:
                _write_text_cache(os.path.join(day_dir, "SRS.txt"), srs_txt)
            except Exception:
                pass
    if not srs_txt:
        checked_s = " | ".join(srs_checked[:40]) if "srs_checked" in locals() else "N/A"
        return {
            "error": (
                f"本地未找到 SRS: {report_utc_date_str}（已检查 data/YYYY-MM-DD 与 data/SRS 年归档）"
                f"；检查项: {checked_s}"
            )
        }

    regions = _parse_srs_regions(srs_txt)
    if not regions:
        return {"error": f"NCEI SRS 解析失败或无活动区: {report_utc_date_str}"}
    valid_dt = _parse_srs_locations_valid_time_utc(srs_txt, report_utc_date_str)

    ev_txt = None
    for p in events_candidates:
        if os.path.isfile(p) and os.path.getsize(p) > 0:
            try:
                with open(p, "r", encoding="utf-8") as f:
                    ev_txt = f.read()
                logger.info("events local hit: %s", p)
                break
            except Exception:
                ev_txt = None
    # data/events 年归档：支持 tar.gz 与目录两种形态
    if not ev_txt:
        ev_txt, ev_src, ev_checked = _read_archive_text_from_local_store(report_utc_date_str, kind="events")
        if ev_txt:
            logger.info("events archive local hit: %s", ev_src or "data/events")
            try:
                _write_text_cache(os.path.join(day_dir, "events.txt"), ev_txt)
            except Exception:
                pass
    if not ev_txt:
        checked_e = " | ".join(ev_checked[:40]) if "ev_checked" in locals() else "N/A"
        logger.warning("本地未找到 events: %s；检查项: %s", report_utc_date_str, checked_e)
    flares: List[Dict[str, str]] = []
    if ev_txt:
        flares = _parse_events_flares(ev_txt, report_utc_date_str)
        _apply_flare_strings_to_regions_simple(regions, flares)
    else:
        checked_e = " | ".join(ev_checked[:40]) if "ev_checked" in locals() else "N/A"
        return {
            "error": (
                f"本地未找到 events: {report_utc_date_str}（已检查 data/YYYY-MM-DD 与 data/events 年归档）"
                f"；检查项: {checked_e}"
            )
        }
    # 指定日期模式也尝试补充 GOES（先复用当日归档，再实时下载并按报告日过滤）
    goes_flares = _goes_xray_flare_rows(report_dt_utc, archive_day_dir=day_dir)
    if goes_flares:
        flares.extend(goes_flares)
    return {
        **_report_day_fields(report_dt_utc),
        "active_regions": regions,
        "flares": flares,
        "_data_source": "NCEI SWPC 长期归档（SRS + events.txt + GOES）",
        **({"_srs_valid_time_utc": valid_dt.isoformat().replace("+00:00", "Z")} if valid_dt else {}),
        # 历史报告不附带 latest SDO 图，以免与日期不符
        "full_disk_images": [],
    }


def _has_complete_local_latest_snapshot(report_utc_date_str: str, resolution: int = 1024) -> Tuple[bool, List[str]]:
    """检查 data/YYYY-MM-DD 是否具备 latest 通道所需完整本地快照（6图+3JSON）。"""
    day_dir = os.path.join(_root, "data", report_utc_date_str[:10])
    ymd = report_utc_date_str.replace("-", "")
    required_json = [
        os.path.join(day_dir, "solar_regions.json"),
        os.path.join(day_dir, "edited_events.json"),
        os.path.join(day_dir, "goes_xrays_7-day.json"),
    ]
    required_imgs = [
        os.path.join(day_dir, f"{ymd}_latest_{resolution}_HMII.jpg"),
        os.path.join(day_dir, f"{ymd}_latest_{resolution}_HMIB.jpg"),
        os.path.join(day_dir, f"{ymd}_latest_{resolution}_0131.jpg"),
        os.path.join(day_dir, f"{ymd}_latest_{resolution}_0171.jpg"),
        os.path.join(day_dir, f"{ymd}_latest_{resolution}_0193.jpg"),
        os.path.join(day_dir, f"{ymd}_latest_{resolution}_0304.jpg"),
    ]
    missing: List[str] = []
    for p in required_json + required_imgs:
        if not (os.path.isfile(p) and os.path.getsize(p) > 0):
            missing.append(p)
    return (len(missing) == 0, missing)


def _goes_xray_flare_rows(
    now_utc: datetime,
    limit: int = 20,
    *,
    archive_day_dir: Optional[str] = None,
) -> List[Dict[str, str]]:
    """仅纳入报告 UTC 当日、C 级及以上的 GOES 通量峰（time_tag 日期对齐）。"""
    flares: List[Dict[str, str]] = []
    report_d = now_utc.date()
    today_d = datetime.now(timezone.utc).date()
    try:
        flare_data: Any = None
        archive_path = (
            os.path.join(archive_day_dir, "goes_xrays_7-day.json")
            if archive_day_dir
            else None
        )
        # 优先复用当日报告目录中的已归档 GOES 原始数据
        if archive_path:
            flare_data = _read_json_cache(archive_path)
            if isinstance(flare_data, list):
                logger.info("GOES archive local hit: %s", archive_path)
            else:
                flare_data = None

        # xrays-7-day.json 仅覆盖最近约 7 天：
        # - 若本地无归档且目标日期超窗，则不下载，直接跳过 GOES。
        if flare_data is None and (report_d < (today_d - timedelta(days=8)) or report_d > (today_d + timedelta(days=1))):
            logger.info("GOES 跳过下载：%s 超出 xrays-7-day 覆盖窗口（today=%s）且本地无归档", report_d, today_d)
            return flares

        # 本地无归档时再下载并落盘归档
        if flare_data is None:
            flare_url = "https://services.swpc.noaa.gov/json/goes/primary/xrays-7-day.json"
            flare_response = requests.get(flare_url, timeout=45)
            if flare_response.status_code != 200:
                return flares
            flare_data = flare_response.json()
            if archive_path:
                try:
                    _write_json_cache(archive_path, flare_data)
                except Exception as ex:
                    logger.warning("GOES 原始响应归档失败: %s", ex)

        if not isinstance(flare_data, list):
            return flares
        candidates: List[Tuple[datetime, Dict[str, str]]] = []
        for flare in flare_data:
            try:
                flux = float(flare.get("flux", 0))
                if flux < 1e-6:
                    continue
                dt = _parse_swpc_time_tag_utc(flare.get("time_tag"))
                if not dt or dt.date() != report_d:
                    continue
                if flux >= 1e-4:
                    flare_class = f"X{flux / 1e-4:.1f}"
                elif flux >= 1e-5:
                    flare_class = f"M{flux / 1e-5:.1f}"
                else:
                    flare_class = f"C{flux / 1e-6:.1f}"
                if not _is_c_class_or_above(flare_class):
                    continue
                row = {
                    "Class": flare_class,
                    "Time": str(flare.get("time_tag", "N/A")),
                    "Flux": f"{flux:.2e}",
                    "NOAA Region": "N/A",
                    "Source": "SWPC_GOES",
                }
                candidates.append((dt, row))
            except Exception:
                continue
        candidates.sort(key=lambda x: x[0])
        for _, row in candidates[-limit:]:
            flares.append(row)
    except Exception as ex:
        logger.warning("GOES X-ray 获取失败: %s", ex)
    return flares


def _apply_event_flare_strings_to_regions(
    regions: List[Dict[str, str]], edited_events: List[Dict[str, Any]], now_utc: datetime
) -> None:
    today_d = now_utc.date()
    by_rid: Dict[int, Dict[str, str]] = {}
    for row in regions:
        try:
            by_rid[int(str(row["NOAA Number"]).strip())] = row
        except Exception:
            continue
    for ev in edited_events:
        rid = ev.get("region")
        if rid is None:
            continue
        try:
            ir = int(rid)
        except Exception:
            continue
        if ir not in by_rid:
            continue
        cls_lbl = _event_class_label(ev)
        if not _is_c_class_or_above(cls_lbl):
            continue
        dt = _parse_event_dt_utc(ev)
        if not dt or dt.date() != today_d:
            continue
        brief = f"{cls_lbl}({dt.strftime('%H:%M')})"
        key = "Flares"
        cur = by_rid[ir].get(key, "无")
        if cur in ("无", "N/A", ""):
            by_rid[ir][key] = brief
        else:
            by_rid[ir][key] = f"{cur} {brief}"


def _build_combined_flare_list_for_analysis(
    edited_events: List[Dict[str, Any]], goes_rows: List[Dict[str, str]], now_utc: datetime
) -> List[Dict[str, str]]:
    today_d = now_utc.date()
    out: List[Dict[str, str]] = []
    for ev in edited_events:
        cls_lbl = _event_class_label(ev)
        if not _is_c_class_or_above(cls_lbl):
            continue
        dt = _parse_event_dt_utc(ev)
        if not dt or dt.date() != today_d:
            continue
        rid = ev.get("region")
        out.append(
            {
                "Class": cls_lbl,
                "Time": dt.strftime("%Y-%m-%d %H:%M UTC"),
                "NOAA Region": str(rid) if rid is not None else "N/A",
                "Source": "SWPC_EVENT",
                "EventType": str(ev.get("type") or ""),
            }
        )
    out.extend(row for row in goes_rows if _is_c_class_or_above(row.get("Class", "")))
    return out


def _fetch_solar_data_noaa_swpc(
    report_utc_date: Optional[str] = None,
    *,
    allow_snapshot_date_mismatch: bool = False,
) -> Dict[str, Any]:
    """NOAA SWPC：solar_regions.json（按 observed_date 过滤为报告 UTC 日）+ edited_events + GOES + SDO。"""
    logger.info("Fetching solar data from NOAA SWPC (JSON-only, no SRS)...")
    print("正在从 NOAA SWPC（solar_regions + 事件库 + GOES）获取数据...")

    report_dt_utc = _parse_report_utc_date_or_today(report_utc_date)
    report_utc_date_str = report_dt_utc.strftime("%Y-%m-%d")
    day_dir = os.path.join(_root, "data", report_utc_date_str[:10])
    regions_url = "https://services.swpc.noaa.gov/json/solar_regions.json"
    data = _load_day_snapshot_or_download_json(
        snapshot_path=os.path.join(day_dir, "solar_regions.json"),
        url=regions_url,
        timeout=45,
        expect_type=list,
        source_name="solar_regions.json",
    )
    if not isinstance(data, list):
        return {"error": "NOAA 活动区数据不可用（本地快照缺失且远程下载失败）"}
    active_regions: List[Dict[str, str]] = []
    skipped_no_obs = 0
    skipped_wrong_day = 0
    # 默认仅纳入 observed_date 与「报告 UTC 日」一致的条目：缺 observed_date 的条目跳过。
    # 在“指定日期 + 本地完整 latest 快照”场景可放宽为：若目标日无条目，则取快照中的最新可用日。

    rows_all: List[Dict[str, Any]] = []
    min_obs: Optional[str] = None
    max_obs: Optional[str] = None
    for region in data:
        try:
            obs = region.get("observed_date")
            if not obs:
                skipped_no_obs += 1
                continue
            obs_s = str(obs).strip()[:10]
            if re.match(r"^\d{4}-\d{2}-\d{2}$", obs_s):
                min_obs = obs_s if (min_obs is None or obs_s < min_obs) else min_obs
                max_obs = obs_s if (max_obs is None or obs_s > max_obs) else max_obs
                rows_all.append(region)
            if obs_s != report_utc_date_str:
                skipped_wrong_day += 1
                continue

            rid = normalize_noaa_region_id(region.get("region"))
            if not rid:
                continue

            mcintosh = str(region.get("spot_class") or "N/A")
            hale = str(region.get("mag_class") or "N/A")
            spots = region.get("number_spots")
            if spots is None:
                spots = region.get("num_spots")
            spots_s = str(spots) if spots is not None else "N/A"

            area = region.get("area")
            area_s = str(area) if area is not None else "N/A"

            pos = str(region.get("location") or "N/A")

            active_regions.append(
                {
                    "NOAA Number": rid,
                    "Position": pos,
                    "position_valid_time_utc": f"{obs_s}T00:00:00Z",
                    "Hale Class": hale,
                    "McIntosh Class": mcintosh,
                    "Area": area_s,
                    "Spots": spots_s,
                    "Flares": "无",
                }
            )
        except Exception as ex:
            logger.error("Error parsing NOAA region: %s", ex)
            continue

    if not active_regions and allow_snapshot_date_mismatch and max_obs:
        logger.warning(
            "solar_regions 快照与目标日期不一致：目标=%s，改用快照最新可用日期=%s",
            report_utc_date_str,
            max_obs,
        )
        for region in rows_all:
            try:
                obs_s = str(region.get("observed_date") or "").strip()[:10]
                if obs_s != max_obs:
                    continue
                rid = normalize_noaa_region_id(region.get("region"))
                if not rid:
                    continue
                mcintosh = str(region.get("spot_class") or "N/A")
                hale = str(region.get("mag_class") or "N/A")
                spots = region.get("number_spots")
                if spots is None:
                    spots = region.get("num_spots")
                spots_s = str(spots) if spots is not None else "N/A"
                area = region.get("area")
                area_s = str(area) if area is not None else "N/A"
                pos = str(region.get("location") or "N/A")
                active_regions.append(
                    {
                        "NOAA Number": rid,
                        "Position": pos,
                        "position_valid_time_utc": f"{obs_s}T00:00:00Z",
                        "Hale Class": hale,
                        "McIntosh Class": mcintosh,
                        "Area": area_s,
                        "Spots": spots_s,
                        "Flares": "无",
                    }
                )
            except Exception:
                continue

    edited = _fetch_edited_events_xra_fla_for_day(report_dt_utc, archive_day_dir=day_dir)
    _apply_event_flare_strings_to_regions(active_regions, edited, report_dt_utc)
    goes_flares = _goes_xray_flare_rows(report_dt_utc, archive_day_dir=day_dir)
    flares = _build_combined_flare_list_for_analysis(edited, goes_flares, report_dt_utc)

    if not active_regions:
        return {
            "error": (
                f"过滤 observed_date={report_utc_date_str} 后无活动区条目。"
                f"（跳过无 observed_date: {skipped_no_obs} 条，非当日: {skipped_wrong_day} 条）"
                + (f"；solar_regions.json 可用日期范围约 {min_obs}..{max_obs}" if min_obs and max_obs else "")
            )
        }

    print(
        f"NOAA：报告日 UTC {report_utc_date_str}，保留 {len(active_regions)} 个活动区"
        f"（跳过无日期 {skipped_no_obs}，非当日 {skipped_wrong_day}）；"
        f"事件 {len(edited)} 条，GOES {len(goes_flares)}，合并耀斑 {len(flares)}"
    )

    # SDO 图像强绑定：NOAA_JSON -> latest（忽略 date_utc）
    imgs = build_full_disk_image_list(1024, date_utc=report_utc_date_str, source="NOAA_JSON")

    out: Dict[str, Any] = {
        **_report_day_fields(report_dt_utc),
        "active_regions": active_regions,
        "flares": flares,
        "_data_source": "NOAA SWPC（solar_regions.json + edited_events + GOES XRS；McIntosh 列=spot_class）",
        "full_disk_images": imgs,
    }
    return out


def fetch_solar_data(report_utc_date: Optional[str] = None) -> Dict[str, Any]:
    """数据获取入口：
    - 指定 report_utc_date：仅使用本地 SRS/events（历史模式）
    - 未指定日期：使用 NOAA SWPC 实时 JSON（当日模式）
    """
    # 强绑定：
    # - 若位置来源是 solar_regions.json（NOAA JSON），则 SDO 用 latest
    # - 若位置来源是本地 SRS，则 SDO 用 browse（按 date_utc）
    if report_utc_date:
        report_dt = _parse_report_utc_date_or_today(report_utc_date)
        ds = report_dt.strftime("%Y-%m-%d")
        # 指定日期：若当天目录具备完整 latest 快照（6 图 + 3 JSON），优先走 latest 通道。
        ok_latest, missing_latest = _has_complete_local_latest_snapshot(ds, resolution=1024)
        if ok_latest:
            out_latest = _fetch_solar_data_noaa_swpc(
                report_utc_date=ds,
                allow_snapshot_date_mismatch=True,
            )
            if isinstance(out_latest, dict) and not out_latest.get("error"):
                out_latest["_positions_source"] = "NOAA_JSON"
                out_latest["_data_source"] = str(out_latest.get("_data_source") or "") + "（指定日期本地完整快照优先：latest）"
                return out_latest
            logger.warning("指定日期 latest 通道失败，回退 browse（SRS/events 本地）: %s", out_latest.get("error") if isinstance(out_latest, dict) else out_latest)
        else:
            logger.info("指定日期 latest 通道条件不足，缺失 %d 项，改走 browse（SRS/events 本地）", len(missing_latest))

        arch = _fetch_solar_data_ncei_archive(ds)
        if isinstance(arch, dict) and not arch.get("error"):
            arch["_positions_source"] = "SRS"
        return arch

    out = _fetch_solar_data_noaa_swpc(report_utc_date=report_utc_date)
    if isinstance(out, dict) and not out.get("error"):
        out["_positions_source"] = "NOAA_JSON"
    return out


def load_prior_report_texts(
    report_utc_date: str,
    reports_dir: str,
    max_days: int = 3,
    max_chars_per_day: Optional[int] = None,
) -> List[Tuple[str, str]]:
    """加载报告日之前的本地 HTML 日报（最多 max_days 份），返回 (UTC 日期, 纯文本) 从近到远。"""
    cap = max_chars_per_day if max_chars_per_day is not None else PRIOR_REPORT_MAX_CHARS
    out: List[Tuple[str, str]] = []
    try:
        base = datetime.strptime(report_utc_date[:10], "%Y-%m-%d").date()
    except ValueError:
        return out
    for i in range(1, max_days + 1):
        d = base - timedelta(days=i)
        ds = d.strftime("%Y-%m-%d")
        path = os.path.join(reports_dir, f"report_{ds}.html")
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                html = f.read()
            soup = BeautifulSoup(html, "html.parser")
            text = re.sub(r"\s+", " ", soup.get_text()).strip()
            if len(text) > cap:
                text = text[:cap] + "…"
            out.append((ds, text))
        except OSError as ex:
            logger.warning("读取历史日报失败 %s: %s", path, ex)
    return out


def _normalize_assistant_content(content: Any) -> Optional[str]:
    """兼容字符串或新版 API 返回的 content 列表（如 [{'text': '...'}]）。"""
    if content is None:
        return None
    if isinstance(content, str):
        s = content.strip()
        return s if s else None
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, dict):
                t = item.get("text")
                if t:
                    parts.append(str(t))
            elif isinstance(item, str) and item.strip():
                parts.append(item)
        if parts:
            return "".join(parts)
    return None


def _extract_dashscope_message_text(response: Any) -> Optional[str]:
    """从 Generation / MultiModalConversation 响应中取助手文本。"""
    if response is None:
        return None
    if getattr(response, "status_code", None) != 200:
        return None
    out = getattr(response, "output", None)
    if out is None:
        return None
    t = getattr(out, "text", None)
    if t:
        s = str(t).strip()
        if s:
            return s
    choices = getattr(out, "choices", None)
    if choices:
        try:
            msg = choices[0].message
            c = getattr(msg, "content", None)
            normalized = _normalize_assistant_content(c)
            if normalized:
                return normalized
        except (AttributeError, IndexError, KeyError, TypeError):
            pass
    return None


def analyze_with_qwen(
    solar_data: Dict[str, Any],
    jwflare_context: Optional[str] = None,
) -> str:
    """优先 MultiModalConversation（VL + SDO 图），否则使用 Generation 纯文本模型。"""
    try:
        if JWDSAR_DISABLE_QWEN:
            return (
                "## 1. 报告概览\n"
                "（已禁用 Qwen 大模型分析：JWDSAR_DISABLE_QWEN=1）\n\n"
                "## 2. 数据概览\n"
                "（已禁用）\n\n"
                "## 3. 大模型分析\n"
                "（已禁用）\n\n"
                "## 4. 风险研判\n"
                "（已禁用）\n\n"
                "## 5. 建议\n"
                "（已禁用）\n\n"
                "## 6. 备注\n"
                "（已禁用）\n"
            )
        if "error" in solar_data:
            return f"Error: {solar_data['error']}"

        if not DASHSCOPE_API_KEY:
            return "错误：未配置 DASHSCOPE_API_KEY 环境变量。"

        print("\n正在调用 Qwen 大模型进行分析...")

        report_date = solar_data.get("report_utc_date") or solar_data.get("date") or ""
        _base = os.path.dirname(os.path.abspath(__file__))
        prior = load_prior_report_texts(str(report_date), os.path.join(_base, "reports"), max_days=3)

        analysis_text: List[str] = []
        if solar_data.get("_data_source"):
            analysis_text.append(f"【数据来源】{solar_data['_data_source']}")
        analysis_text.append("【耀斑阈值】仅统计并记录 C 级及以上耀斑（B/A 级忽略）。")
        if report_date:
            analysis_text.append(f"【报告日（UTC）】{report_date}")
        if solar_data.get("_report_schedule_note"):
            analysis_text.append(f"【定时与报告日说明】{solar_data['_report_schedule_note']}")

        for ds, snippet in prior:
            analysis_text.append(f"【历史日报摘要 {ds}（节选）】{snippet}")

        analysis_text.append(f"日期: {solar_data.get('date', 'N/A')}")
        imgs = solar_data.get("full_disk_images") or []
        if imgs:
            analysis_text.append(
                "【全日面示意图】若为多模态请求，下列波段图像已按顺序附在用户消息中（NASA SDO latest，约近实时）： "
                + "、".join(str(x.get("label", "")) for x in imgs[:MAX_VL_DISK_IMAGES])
            )
        analysis_text.append("\n活动区域（仅报告 UTC 当日）：")

        for region in solar_data.get("active_regions", []):
            region_dict = cast(Dict[str, str], region)
            fl = region_dict.get("Flares") or region_dict.get("Today Flares", "无")
            region_desc = (
                f"NOAA {region_dict.get('NOAA Number', 'N/A')} - "
                f"位置: {region_dict.get('Position', 'N/A')}, "
                f"Hale分类: {region_dict.get('Hale Class', 'N/A')}, "
                f"McIntosh分类: {region_dict.get('McIntosh Class', 'N/A')}, "
                f"面积: {region_dict.get('Area', 'N/A')}, "
                f"黑子数: {region_dict.get('Spots', 'N/A')}, "
                f"耀斑(当日,C+): {fl}"
            )
            analysis_text.append(region_desc)

        if solar_data.get("flares", []):
            analysis_text.append("\n当日耀斑事件（C 级及以上，含 SWPC 事件与 GOES）：")
            for flare in solar_data["flares"]:
                flare_dict = cast(Dict[str, str], flare)
                et = flare_dict.get("EventType", "")
                src = flare_dict.get("Source", "")
                extra = f" [{src}{('/' + et) if et else ''}]" if src else ""
                if src == "SWPC_GOES":
                    flare_desc = (
                        f"{flare_dict.get('Class', 'N/A')} — 时间: {flare_dict.get('Time', 'N/A')} "
                        f"通量: {flare_dict.get('Flux', 'N/A')}"
                    )
                else:
                    flare_desc = (
                        f"{flare_dict.get('Class', 'N/A')}({flare_dict.get('Time', 'N/A')}) - "
                        f"活动区: {flare_dict.get('NOAA Region', 'N/A')}{extra}"
                    )
                analysis_text.append(flare_desc)

        rd = solar_data.get("report_utc_date") or solar_data.get("date", "N/A")
        jw_block = ""
        if jwflare_context and jwflare_context.strip():
            jw_block = (
                "\n\n【JW-Flare 预报摘要（仅供分析参考；正式表格由程序插入第 3 节）】\n"
                f"{jwflare_context.strip()}\n"
            )

        prompt = f"""请根据下列**报告日（UTC）当日**的太阳活动数据撰写日报，使用 Markdown，含表格。
McIntosh 列对应 solar_regions.json 的 spot_class，Hale 对应 mag_class；数值与分类务必引用下列文本中的内容，勿编造；缺失写「数据未提供」。
若提供了历史日报节选，可作趋势与连续性参考，但以当日 NOAA 数据为准。
JW-Flare 为“未来24小时预测”，NOAA/事件/GOES 为“当日观测”。
禁止用“当日观测”验证“当日 JW-Flare 预测是否命中”；如需验证，只能做“昨日预测 vs 今日观测”。
若缺少昨日预测证据，请明确写“无法完成命中验证”，并改为合理性评估。

{chr(10).join(analysis_text)}{jw_block}
{_report_output_instructions(rd)}
"""

        system_vl = REPORT_SYSTEM_VL
        system_text = REPORT_SYSTEM_TEXT

        imgs_cap = imgs[:MAX_VL_DISK_IMAGES]
        user_content: List[Dict[str, str]] = []
        for im in imgs_cap:
            url = str(im.get("url") or "").strip()
            if not url:
                continue
            # DashScope 多模态通常需要可公网访问的 http(s) URL；本地文件/ data URI 仅用于网页展示
            if url.startswith("http://") or url.startswith("https://"):
                user_content.append({"image": url})
            lbl = str(im.get("label") or "").strip()
            if lbl:
                user_content.append({"text": f"（上图波段：{lbl}）"})
        user_content.append({"text": prompt})

        n_img = sum(1 for x in user_content if "image" in x)
        # 统一优先走 MultiModalConversation：有图则图文，无图则纯文本（同一套 messages 格式）
        try:
            logger.info(
                "MultiModalConversation model=%s attached_images=%d",
                DASHSCOPE_VL_MODEL,
                n_img,
            )
            mm_messages = [
                {"role": "system", "content": [{"text": system_vl}]},
                {"role": "user", "content": user_content},
            ]
            vl_resp = MultiModalConversation.call(
                api_key=DASHSCOPE_API_KEY,
                model=DASHSCOPE_VL_MODEL,
                messages=mm_messages,
                result_format="message",
            )
            out_txt = _extract_dashscope_message_text(vl_resp)
            if out_txt:
                logger.info("MultiModalConversation 完成，长度 %d", len(out_txt))
                return out_txt
            logger.warning("MultiModalConversation 响应无可用文本，回退纯文本 Generation")
        except Exception as vl_ex:
            logger.warning("MultiModalConversation 失败，回退纯文本 Generation: %s", vl_ex, exc_info=True)

        try:
            messages = [
                {"role": "system", "content": system_text},
                {"role": "user", "content": prompt},
            ]
            response = Generation.call(
                api_key=DASHSCOPE_API_KEY,
                model=DASHSCOPE_TEXT_MODEL,
                messages=messages,
                result_format="message",
            )
            logger.info("Generation 回退 status=%s", getattr(response, "status_code", None))
            if response is None or getattr(response, "status_code", None) != 200:
                msg = getattr(response, "message", "unknown")
                # 若用户将 DASHSCOPE_TEXT_MODEL 设为 VL 模型但该模型不支持 Generation，
                # 则自动回退到 qwen3-max 再试一次，避免直接产出“API 错误”。
                if getattr(response, "status_code", None) == 400 and DASHSCOPE_TEXT_MODEL == DASHSCOPE_VL_MODEL:
                    try:
                        logger.warning("Generation 400，尝试用 qwen3-max 作为纯文本回退模型")
                        response2 = Generation.call(
                            api_key=DASHSCOPE_API_KEY,
                            model="qwen3-max",
                            messages=messages,
                            result_format="message",
                        )
                        if response2 is not None and getattr(response2, "status_code", None) == 200:
                            out2 = _extract_dashscope_message_text(response2)
                            if out2:
                                return out2
                    except Exception as ex2:
                        logger.warning("qwen3-max 回退也失败: %s", ex2, exc_info=True)
                return f"API 错误: {msg}"
            out_txt = _extract_dashscope_message_text(response)
            if out_txt:
                return out_txt
            try:
                return str(response.output.choices[0].message.content)
            except (AttributeError, IndexError, KeyError) as e:
                return f"解析响应失败: {e}"
        except Exception as api_error:
            logger.error("Generation 调用异常: %s", api_error, exc_info=True)
            return f"API 调用异常: {api_error}"

    except Exception as e:
        error_msg = f"分析过程出错: {str(e)}"
        logger.error("Error in analysis: %s", e, exc_info=True)
        return error_msg


def generate_report(
    report_utc_date: Optional[str] = None,
    *,
    hmi_continuum_image_path: Optional[str] = None,
    sdo_resolution: Optional[int] = None,
) -> str:
    """生成指定 UTC 日期的太阳活动日报并写入 reports/；为空则生成当日。

    hmi_continuum_image_path：本地全日面连续谱 JPEG/PNG，用于替换图库中的 HMI 连续谱图（仅展示与叠标，不参与多模态推理）。
    """
    styled_html = None

    try:
        print("\n=== 开始生成太阳活动日报 ===")
        print(f"生成时间: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC")

        # 若未指定日期，但给了本地连续谱图，则从文件名推断当日
        if not report_utc_date and hmi_continuum_image_path:
            inferred = _extract_yyyymmdd_from_filename(hmi_continuum_image_path)
            if inferred:
                report_utc_date = inferred

        # 指定日期若为“当前 UTC 日”，且尚未到 23:50，则不允许生成当天日报。
        if report_utc_date:
            try:
                d = datetime.strptime(str(report_utc_date).strip()[:10], "%Y-%m-%d").date()
                now_utc = datetime.now(timezone.utc)
                if d == now_utc.date():
                    cutoff = now_utc.replace(hour=23, minute=50, second=0, microsecond=0)
                    if now_utc < cutoff:
                        msg = "无法生成，当天太阳活动未结束（请在 UTC 23:50 后再生成当天日报）"
                        logger.info(msg)
                        return f"<div class='error'>{msg}</div>"
            except ValueError:
                pass
        
        logger.info("Starting to fetch solar data...")
        solar_data = fetch_solar_data(report_utc_date=report_utc_date)
        date_str = (
            solar_data.get("report_utc_date")
            if isinstance(solar_data, dict) and solar_data.get("report_utc_date")
            else datetime.now(timezone.utc).strftime("%Y-%m-%d")
        )
        
        if 'error' in solar_data:
            logger.error(f"Error in fetch_solar_data: {solar_data['error']}")
            print(f"⚠️ 数据获取失败: {solar_data['error']}")
        
        # SDO 图像：位置来源决定图源
        # - NOAA_JSON => latest（date_utc=None）
        # - SRS      => browse（date_utc=date_str）
        chosen_sdo_res = int(sdo_resolution) if sdo_resolution else 1024
        if isinstance(solar_data, dict):
            try:
                pos_src = str(solar_data.get("_positions_source") or "NOAA_JSON")
                use_browse = pos_src == "SRS"
                solar_data["full_disk_images"] = build_full_disk_image_list(
                    chosen_sdo_res,
                    date_utc=date_str,
                    source=pos_src,
                )
                # 透传 SRS 位置有效时刻到图库条目，便于按图像时刻传播位置
                if use_browse and solar_data.get("_srs_valid_time_utc") and isinstance(solar_data["full_disk_images"], list):
                    t0 = str(solar_data.get("_srs_valid_time_utc") or "")
                    for im in solar_data["full_disk_images"]:
                        if isinstance(im, dict) and t0:
                            im["srs_valid_time_utc"] = t0
                logger.info(
                    "SDO 图像分辨率=%s（date=%s, source=%s）",
                    chosen_sdo_res,
                    (date_str if use_browse else "latest"),
                    pos_src,
                )
            except Exception as ex:
                logger.warning("SDO 图像列表构建失败（将跳过图库）：%s", ex, exc_info=True)
                solar_data["full_disk_images"] = []

        # 用本地图像替换图库中的 HMI 连续谱图
        if (
            hmi_continuum_image_path
            and isinstance(solar_data, dict)
            and isinstance(solar_data.get("full_disk_images"), list)
        ):
            rotate_deg = float(os.getenv("JWDSAR_LOCAL_ROTATE_DEG", "0") or "0")
            p_correct = bool(str(os.getenv("JWDSAR_LOCAL_P_ANGLE_CORRECT", "1")).strip().lower() in ("1", "true", "yes", "y"))
            # 约定：除本地 SFMM 连续谱外，其余图像保持 base64(data-uri) 内嵌；
            # SFMM：HTML 内嵌 data-uri 以保证页面可显示，同时落盘到 data/YYYY-MM-DD/ 便于归档。
            img_ref = _local_continuum_for_gallery(
                hmi_continuum_image_path,
                rotate_deg=rotate_deg,
                p_angle_correct=p_correct,
                report_date_str=date_str,
            )
            if not img_ref:
                # 失败必须显式告知，并回退 HMI 连续谱继续生成
                msg = (
                    f"SFMM 连续谱替换失败（{hmi_continuum_image_path}），将回退使用 HMI 连续谱。"
                    "可检查：文件路径/格式是否正确；FITS 是否含有效数据；依赖 pillow/astropy/numpy 是否可用。"
                )
                logger.error(msg)
            else:
                wh = _try_get_image_wh(hmi_continuum_image_path)
                local_res = int(wh[0]) if wh and wh[0] == wh[1] else None
                sfmm_obs_time = _extract_obs_time_utc_from_filename(hmi_continuum_image_path)
                replaced = False
                for im in cast(List[Dict[str, str]], solar_data["full_disk_images"]):
                    if str(im.get("product") or "") == "hmi_igr":
                        im["url"] = img_ref
                        im["page_url"] = ""
                        im["label"] = "SFMM 连续谱"
                        if sfmm_obs_time:
                            im["obs_time_utc"] = sfmm_obs_time
                        # 若位置来自 SRS，则透传位置有效时刻，便于按图像时刻传播（含 B0）
                        if solar_data.get("_srs_valid_time_utc") and not im.get("srs_valid_time_utc"):
                            im["srs_valid_time_utc"] = str(solar_data.get("_srs_valid_time_utc"))
                        # 本地图像的日面半径比例可能与 SDO latest 不同，叠标按每张图各自参数计算
                        im["disk_radius_frac"] = float(os.getenv("JWDSAR_LOCAL_DISK_RADIUS_FRAC", "0.49"))
                        if local_res:
                            im["resolution"] = local_res
                        replaced = True
                        break
                if not replaced:
                    solar_data["full_disk_images"].insert(
                        0,
                        {
                            "label": "SFMM 连续谱",
                            "product": "hmi_igr",
                            "url": img_ref,
                            "page_url": "",
                            **({"obs_time_utc": sfmm_obs_time} if sfmm_obs_time else {}),
                            **(
                                {"srs_valid_time_utc": str(solar_data.get("_srs_valid_time_utc"))}
                                if solar_data.get("_srs_valid_time_utc")
                                else {}
                            ),
                            "disk_radius_frac": float(os.getenv("JWDSAR_LOCAL_DISK_RADIUS_FRAC", "0.49")),
                            **({"resolution": local_res} if local_res else {}),
                        },
                    )
                logger.info("已替换 HMI 连续谱图为本地文件：%s", hmi_continuum_image_path)

        jw_html = ""
        jw_prompt = ""
        try:
            from jwflare_pipeline import build_jwflare_forecast_bundle

            jw_html, jw_prompt = build_jwflare_forecast_bundle(
                cast(Dict[str, Any], solar_data), date_str
            )
        except Exception as jw_pre:
            logger.warning("JW-Flare 预计算未执行: %s", jw_pre, exc_info=True)

        logger.info("Starting analysis with Qwen...")
        if JWDSAR_DISABLE_QWEN:
            logger.info("JWDSAR_DISABLE_QWEN=1，跳过 Qwen 分析")
            analysis = analyze_with_qwen(solar_data, jwflare_context=jw_prompt or None)
        else:
            analysis = analyze_with_qwen(solar_data, jwflare_context=jw_prompt or None)
        
        if not analysis or analysis is None or analysis.startswith("Error:"):
            error_msg = "分析结果为空或出错"
            logger.error(error_msg)
            styled_html = f"<div class='error'>数据获取或分析失败: {solar_data.get('error', '未知错误')}</div>"
        else:
            logger.info(f"Analysis completed, length: {len(analysis)}")
            _warn_if_report_markdown_looks_wrong(analysis)

            try:
                html_content = markdown.markdown(analysis, extensions=['tables', 'fenced_code'])
            except Exception as e:
                logger.error(f"Markdown conversion error: {e}")
                html_content = f"<pre>{analysis}</pre>"

            try:
                html_content = _inject_jwflare_after_section3_intro(html_content, jw_html)
            except Exception as inj_ex:
                logger.warning("JW-Flare 注入 §3 失败，附于文末: %s", inj_ex, exc_info=True)
                html_content = html_content.rstrip() + (jw_html or "")

            styled_html = html_content

            gallery_imgs = solar_data.get("full_disk_images") if isinstance(solar_data, dict) else None
            if gallery_imgs:
                regions = solar_data.get("active_regions") if isinstance(solar_data, dict) else None
                styled_html = styled_html.rstrip() + html_sdo_gallery_section(
                    gallery_imgs,
                    cast(Optional[List[Dict[str, str]]], regions),
                )

            styled_html = _wrap_report_html_for_mathjax(styled_html)
            save_report(
                date_str,
                styled_html,
                cast(Dict[str, Any], solar_data) if isinstance(solar_data, dict) else None,
            )
        
        print("\n=== 日报生成流程结束 ===")
        return styled_html if styled_html else "<div class='error'>报告生成失败</div>"
        
    except Exception as e:
        logger.error(f"Error in generate_report: {str(e)}", exc_info=True)
        return f"<div class='error'>生成报告时发生错误: {str(e)}</div>"


def _extract_yyyymmdd_from_filename(path: str) -> Optional[str]:
    """从文件名中提取 YYYYMMDD（优先匹配 20xxxxxx），返回 YYYY-MM-DD。"""
    if not path:
        return None
    base = os.path.basename(str(path))
    m = re.search(r"(20\d{6})", base)
    if not m:
        return None
    ymd = m.group(1)
    return f"{ymd[0:4]}-{ymd[4:6]}-{ymd[6:8]}"


def _extract_obs_time_utc_from_filename(path: str) -> Optional[str]:
    """
    从文件名中提取观测时刻（UTC）：
    - 支持 YYYYMMDDHHMMSSmmm（如 20260402003944718：日期=20260402，时分秒=003944，毫秒=718）
    返回 ISO8601（带 Z），例如 2026-04-02T00:39:44.718Z。
    """
    if not path:
        return None
    base = os.path.basename(str(path))
    m = re.search(r"(20\d{6})(\d{6})(\d{3})", base)
    if not m:
        return None
    ymd, hms, ms = m.group(1), m.group(2), m.group(3)
    try:
        dt = datetime(
            int(ymd[0:4]),
            int(ymd[4:6]),
            int(ymd[6:8]),
            int(hms[0:2]),
            int(hms[2:4]),
            int(hms[4:6]),
            int(ms) * 1000,
            tzinfo=timezone.utc,
        )
    except Exception:
        return None
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _is_fits_path(path: str) -> bool:
    p = str(path).lower()
    return p.endswith(".fits") or p.endswith(".fit") or p.endswith(".fts")


def _wcs_rotation_deg_from_header(header: Any) -> Optional[float]:
    """从 FITS header 估计图像旋转角（度，顺时针为正），用于把太阳北方向转到图像上方。

    说明：这不是完整的物理“P 角”推导，但对多数含 WCS 的日面图可作为“北向朝上”校正角。
    优先顺序：CROTA2 -> CD 矩阵 -> PC 矩阵。
    """
    try:
        if header is None:
            return None
        # CROTA2：按 FITS/WCS 约定通常为坐标轴旋转角（度，逆时针为正）
        crota2 = header.get("CROTA2")
        if crota2 is not None:
            return -float(crota2)
        # CD matrix
        cd11 = header.get("CD1_1")
        cd12 = header.get("CD1_2")
        if cd11 is not None and cd12 is not None:
            theta = math.degrees(math.atan2(float(cd12), float(cd11)))
            return -theta
        # PC matrix（需要 CDELT，但这里只取方向角）
        pc11 = header.get("PC1_1")
        pc12 = header.get("PC1_2")
        if pc11 is not None and pc12 is not None:
            theta = math.degrees(math.atan2(float(pc12), float(pc11)))
            return -theta
    except Exception:
        return None
    return None


def _encode_jpeg_data_uri_from_pil(im: Any) -> Optional[str]:
    try:
        from io import BytesIO

        buf = BytesIO()
        im.convert("RGB").save(buf, format="JPEG", quality=95, optimize=True)
        b = buf.getvalue()
        enc = base64.b64encode(b).decode("ascii")
        return f"data:image/jpeg;base64,{enc}"
    except Exception:
        return None


def _fits_to_pil_image(path: str) -> Optional[Tuple[Any, Any]]:
    """读取 FITS，返回 (PIL.Image, header)。"""
    try:
        from astropy.io import fits  # optional dependency
        import numpy as np
        from PIL import Image

        with fits.open(path, memmap=False) as hdul:
            # 有些 FITS（例如压缩图像）主 HDU 无 data，数据在后续 HDU 中
            hdu = None
            for cand in hdul:
                if getattr(cand, "data", None) is not None:
                    hdu = cand
                    break
            if hdu is None:
                return None
            data = hdu.data
            header = hdu.header
        if data is None:
            return None
        arr = data
        # squeeze to 2D
        arr = np.asarray(arr)
        # 常见形状：(2,H,W) 或 (H,W)；优先取第一个平面
        while arr.ndim > 2:
            arr = arr[0]
        arr = np.nan_to_num(arr, nan=0.0, posinf=0.0, neginf=0.0)
        # robust scaling (1%~99%)
        vmin = float(np.percentile(arr, 1))
        vmax = float(np.percentile(arr, 99))
        if not (vmax > vmin):
            vmin = float(arr.min())
            vmax = float(arr.max()) if float(arr.max()) > vmin else vmin + 1.0
        norm = (arr - vmin) / (vmax - vmin)
        norm = np.clip(norm, 0.0, 1.0)
        img8 = (norm * 255.0).astype("uint8")
        im = Image.fromarray(img8, mode="L")
        return (im, header)
    except ImportError:
        logger.warning("未安装 astropy/Pillow，无法读取 FITS。可安装 requirements_jwflare.txt 或: pip install astropy pillow")
        return None
    except Exception as ex:
        logger.warning("FITS 读取失败：%s (%s)", path, ex, exc_info=True)
        return None


def _load_local_continuum_pil(path: str, *, rotate_deg: float = 0.0, p_angle_correct: bool = False) -> Optional[Any]:
    """把本地 JPEG/PNG/FITS 读入为 PIL.Image（RGB），用于 data-uri 或落盘外链。"""
    try:
        p = str(path)
        deg = float(rotate_deg or 0.0)
        if _is_fits_path(p):
            got = _fits_to_pil_image(p)
            if not got:
                return None
            im, hdr = got
            if p_angle_correct:
                base_deg = _wcs_rotation_deg_from_header(hdr) or 0.0
            else:
                base_deg = 0.0
            total = base_deg + deg
            if abs(total) >= 1e-6:
                try:
                    from PIL import Image

                    im = im.rotate(-total, resample=Image.Resampling.BICUBIC, expand=False)
                except Exception as ex:
                    logger.warning("FITS 旋转失败（将忽略旋转）：%s (%s)", path, ex)
            return im.convert("RGB")

        # 普通图片：若需要旋转，优先用 Pillow；否则直接打开
        from PIL import Image

        im0 = Image.open(p)
        im0 = im0.convert("RGB")
        if abs(deg) >= 1e-6:
            im0 = im0.rotate(-deg, resample=Image.Resampling.BICUBIC, expand=False)
        return im0
    except Exception as ex:
        logger.warning("本地图像读取失败（将跳过替换）：%s (%s)", path, ex)
        return None


def _save_pil_jpeg(path: str, im: Any, *, quality: int = 95) -> bool:
    try:
        from io import BytesIO

        os.makedirs(os.path.dirname(path), exist_ok=True)
        buf = BytesIO()
        im.save(buf, format="JPEG", quality=int(quality), optimize=True)
        tmp = path + ".part"
        with open(tmp, "wb") as f:
            f.write(buf.getvalue())
        os.replace(tmp, path)
        return True
    except Exception as ex:
        logger.warning("保存 JPEG 失败 %s: %s", path, ex)
        return False


def _estimate_rotation_deg_logpolar_phasecorr(
    moving_rgb: Any,
    fixed_jpg_path: str,
    *,
    size: int = 1024,
    disk_radius_frac: float = 0.46,
) -> Optional[float]:
    """
    估计 moving 相对 fixed 的“仅旋转（绕中心）”角度（度，逆时针为正，PIL.rotate 的同一约定）。
    算法：梯度幅度 -> FFT 幅度谱 -> log-polar -> 相位相关。

    依赖：opencv-python + numpy + Pillow。
    """
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
        from PIL import Image

        # load fixed
        fixed = Image.open(fixed_jpg_path).convert("L").resize((size, size), Image.Resampling.BICUBIC)
        moving = moving_rgb.convert("L").resize((size, size), Image.Resampling.BICUBIC)
        fixed_f = np.asarray(fixed, dtype=np.float32)
        moving_f = np.asarray(moving, dtype=np.float32)

        # gradient magnitude (Scharr is robust)
        def mag(im: np.ndarray) -> np.ndarray:
            gx = cv2.Scharr(im, cv2.CV_32F, 1, 0)
            gy = cv2.Scharr(im, cv2.CV_32F, 0, 1)
            m = cv2.magnitude(gx, gy)
            return m

        fixed_m = mag(fixed_f)
        moving_m = mag(moving_f)

        # disk mask
        h, w = fixed_m.shape
        cx, cy = (w - 1) / 2.0, (h - 1) / 2.0
        rr = float(disk_radius_frac) * min(w, h)
        yy, xx = np.ogrid[:h, :w]
        mask = ((xx - cx) ** 2 + (yy - cy) ** 2) <= (rr * rr)
        fixed_m = np.where(mask, fixed_m, 0.0)
        moving_m = np.where(mask, moving_m, 0.0)

        # fft magnitude spectrum (shifted)
        def spec(im: np.ndarray) -> np.ndarray:
            f = np.fft.fft2(im)
            f = np.fft.fftshift(f)
            s = np.log1p(np.abs(f)).astype(np.float32)
            # normalize
            s -= float(s.min())
            mx = float(s.max()) or 1.0
            s /= mx
            return s

        fixed_s = spec(fixed_m)
        moving_s = spec(moving_m)

        # log-polar transform: use warpPolar if available
        center = (cx, cy)
        max_radius = min(cx, cy)
        flags = cv2.WARP_POLAR_LOG
        fixed_lp = cv2.warpPolar(fixed_s, (w, h), center, max_radius, flags)
        moving_lp = cv2.warpPolar(moving_s, (w, h), center, max_radius, flags)

        # phase correlation gives (dx, dy) to align src2 -> src1
        # Here we want the shift that aligns moving_lp to fixed_lp, so use (fixed_lp, moving_lp).
        (shift_x, shift_y), _ = cv2.phaseCorrelate(fixed_lp, moving_lp)

        # angle corresponds to vertical shift (rows) in warpPolar output (OpenCV convention)
        angle_ccw = 360.0 * (shift_y / float(h))
        # wrap to [-180, 180]
        while angle_ccw > 180:
            angle_ccw -= 360
        while angle_ccw < -180:
            angle_ccw += 360
        return float(angle_ccw)
    except Exception as ex:
        logger.info("SFMM 匹配估角失败（将跳过自动对齐）：%s", ex)
        return None


def _local_continuum_for_gallery(
    path: str,
    *,
    rotate_deg: float = 0.0,
    p_angle_correct: bool = False,
    report_date_str: str,
) -> Optional[str]:
    """
    返回可写入图库 dict['url'] 的字符串：
    - 始终返回 data-uri（自包含 HTML，Web/Gradio 打开不裂图）
    同时：会把 SFMM 连续谱 JPEG 落盘到 data/YYYY-MM-DD/ 便于归档复用。
    """
    im = _load_local_continuum_pil(path, rotate_deg=rotate_deg, p_angle_correct=p_angle_correct)
    if im is None:
        return None
    # 自动匹配估计旋转角（P 角改正/对齐）：将 SFMM 缩放到 1024 与当日 HMIIC(1024) 做匹配
    try:
        safe_date = re.sub(r"[^0-9\-]", "_", str(report_date_str)[:10])
        ymd = safe_date.replace("-", "")
        ref_dir = os.path.join(_root, "data", safe_date)
        ref_path = os.path.join(ref_dir, f"{ymd}_000000_1024_HMIIC.jpg")
        if not os.path.isfile(ref_path):
            # fallback: find any *_1024_HMIIC.jpg in that dir
            for fn in os.listdir(ref_dir) if os.path.isdir(ref_dir) else []:
                if fn.endswith("_1024_HMIIC.jpg") and fn.startswith(ymd + "_"):
                    ref_path = os.path.join(ref_dir, fn)
                    break
        if os.path.isfile(ref_path):
            est = _estimate_rotation_deg_logpolar_phasecorr(im, ref_path, size=1024, disk_radius_frac=0.46)
            if est is not None and abs(est) > 1e-3:
                from PIL import Image

                logger.info("SFMM 匹配估计旋转角=%.3f° (CCW+)", est)
                im = im.rotate(est, resample=Image.Resampling.BICUBIC, expand=False)
    except Exception as ex:
        logger.info("SFMM 自动旋转流程异常（将跳过）：%s", ex)
    uri = _encode_jpeg_data_uri_from_pil(im)
    if not uri:
        return None
    safe_date = re.sub(r"[^0-9\-]", "_", str(report_date_str)[:10])
    out_name = f"sfmm_continuum_{safe_date}.jpg"
    # 固定落盘到 data/YYYY-MM-DD/ 下（与 SRS/events/browse 同目录）
    day_dir = os.path.join(_root, "data", safe_date)
    out_path = os.path.join(day_dir, out_name)
    if not os.path.isfile(out_path) or os.path.getsize(out_path) <= 0:
        if not _save_pil_jpeg(out_path, im):
            logger.warning("SFMM 连续谱落盘失败（将仅内嵌于 HTML）：%s", out_path)
    return uri


def _file_to_data_uri(path: str, *, rotate_deg: float = 0.0, p_angle_correct: bool = False) -> Optional[str]:
    """把本地 JPEG/PNG/FITS 转为 data URI，便于报告 HTML 自包含展示。

    rotate_deg：顺时针为正。
    p_angle_correct：若为 FITS 且 header 含 WCS/旋转信息，则先做“北向朝上”旋转。
    """
    im = _load_local_continuum_pil(path, rotate_deg=rotate_deg, p_angle_correct=p_angle_correct)
    if im is None:
        return None
    return _encode_jpeg_data_uri_from_pil(im)


def _try_get_image_wh(path: str) -> Optional[Tuple[int, int]]:
    """尽量不依赖第三方库，读取 JPEG/PNG 的宽高。"""
    p = str(path)
    if _is_fits_path(p):
        try:
            from astropy.io import fits

            with fits.open(p, memmap=False) as hdul:
                h = None
                for cand in hdul:
                    if getattr(cand, "data", None) is not None:
                        h = cand.header
                        d = cand.data
                        shp = getattr(d, "shape", None)
                        if shp and len(shp) >= 2:
                            # (H,W) or (C,H,W)
                            if len(shp) == 2:
                                return (int(shp[1]), int(shp[0]))
                            return (int(shp[-1]), int(shp[-2]))
                        break
                if h is None:
                    return None
            w = int(h.get("NAXIS1") or 0)
            hh = int(h.get("NAXIS2") or 0)
            return (w, hh) if w > 0 and hh > 0 else None
        except Exception:
            return None
    try:
        with open(p, "rb") as f:
            b = f.read(64 * 1024)
    except Exception:
        return None
    if len(b) < 24:
        return None
    # PNG: 8 bytes signature + IHDR
    if b.startswith(b"\x89PNG\r\n\x1a\n") and len(b) >= 24:
        try:
            w = int.from_bytes(b[16:20], "big")
            h = int.from_bytes(b[20:24], "big")
            return (w, h) if w > 0 and h > 0 else None
        except Exception:
            return None
    # JPEG: scan for SOF0/SOF2
    if b[0:2] != b"\xFF\xD8":
        return None
    i = 2
    while i + 9 < len(b):
        if b[i] != 0xFF:
            i += 1
            continue
        # skip padding FFs
        while i < len(b) and b[i] == 0xFF:
            i += 1
        if i >= len(b):
            break
        marker = b[i]
        i += 1
        # standalone markers
        if marker in (0xD8, 0xD9):
            continue
        if i + 1 >= len(b):
            break
        seglen = int.from_bytes(b[i : i + 2], "big")
        if seglen < 2:
            break
        segstart = i + 2
        # SOF0(0xC0) / SOF2(0xC2)
        if marker in (0xC0, 0xC2) and segstart + 7 <= len(b):
            try:
                h = int.from_bytes(b[segstart + 1 : segstart + 3], "big")
                w = int.from_bytes(b[segstart + 3 : segstart + 5], "big")
                return (w, h) if w > 0 and h > 0 else None
            except Exception:
                return None
        i = segstart + seglen - 2
    return None


# 历史报告存储目录（可用环境变量覆盖，默认相对项目根）
def _resolve_reports_dir(raw_value: Optional[str]) -> Path:
    raw = (raw_value or "reports").strip() or "reports"
    p = Path(raw)
    if not p.is_absolute():
        p = Path(_root) / p
    return p.resolve()


REPORTS_PATH = _resolve_reports_dir(os.getenv("JWDSAR_REPORTS_DIR", "reports"))
REPORTS_DIR = str(REPORTS_PATH)
REPORTS_PATH.mkdir(parents=True, exist_ok=True)

# PDF 导出：与 Gradio 中 .report-container 接近的版式（WeasyPrint）
_PDF_PAGE_CSS = """
@page { size: A4; margin: 12mm 14mm; }
body {
  font-family: "Noto Serif SC", "Source Han Serif SC", "SimSun", "Georgia", serif;
  font-size: 11pt;
  line-height: 1.65;
  color: #1e293b;
  font-variant-numeric: lining-nums;
}
h1 { font-size: 1.35em; color: #0f172a; margin: 0 0 0.6em 0; }
h2 { font-size: 1.12em; color: #1e293b; margin: 1.1em 0 0.5em 0;
     padding-bottom: 0.25em; border-bottom: 1px solid #e2e8f0; }
h3 { font-size: 1.05em; color: #1e293b; margin: 0.9em 0 0.4em 0; }
p { margin: 0.5em 0; }
table { border-collapse: collapse; width: 100%; margin: 0.75em 0; font-size: 0.95em; }
th, td { border: 1px solid #e2e8f0; padding: 6px 8px; text-align: left; }
th { background: linear-gradient(180deg, #f8fafc 0%, #f1f5f9 100%); font-weight: 600; }
img { max-width: 100%; height: auto; }
pre { white-space: pre-wrap; font-size: 0.9em; }
ul, ol { margin: 0.4em 0 0.4em 1.2em; }
.error { color: #b91c1c; }
.jwdsar-sdo-gallery--pdf .jwdsar-sdo-grid-pdf {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-start;
  justify-content: flex-start;
}
.jwdsar-sdo-gallery--pdf .jwdsar-sdo-fig-pdf {
  width: 32%;
  box-sizing: border-box;
  page-break-inside: avoid;
  margin: 0 0 0.5rem 0;
}
.jwdsar-sdo-gallery--pdf .jwdsar-sdo-fig-pdf img {
  max-height: 45mm;
  max-width: 100%;
}
.jwdsar-math-display {
  margin: 0.5em 0;
  text-align: center;
}
math { font-size: 0.95em; }
"""


def _wrap_html_for_pdf(body_html: str) -> str:
    return (
        "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\"/>"
        f"<style>{_PDF_PAGE_CSS}</style></head><body>"
        f"{body_html}</body></html>"
    )


def _try_save_report_pdf(
    date_str: str,
    body_html: str,
    solar_data: Optional[Dict[str, Any]] = None,
) -> None:
    """与 HTML 并列写入 report_{date}.pdf（WeasyPrint + 专用 HTML）；失败仅记日志，不阻断。"""
    try:
        from weasyprint import HTML
    except ImportError:
        logger.warning("未安装 weasyprint，跳过 PDF。请执行: pip install weasyprint")
        return
    except (OSError, RuntimeError) as ex:
        # 常见：已安装 weasyprint 但系统缺少 Pango/Cairo/GObject 等动态库
        logger.warning("WeasyPrint 无法加载系统依赖库，跳过 PDF：%s", ex, exc_info=True)
        return
    pdf_path = REPORTS_PATH / f"report_{date_str}.pdf"
    REPORTS_PATH.mkdir(parents=True, exist_ok=True)
    base_url = REPORTS_PATH.as_uri() + "/"
    pdf_body = _html_for_weasyprint(body_html, solar_data)
    doc = _wrap_html_for_pdf(pdf_body)
    try:
        HTML(string=doc, base_url=base_url).write_pdf(str(pdf_path))
        logger.info("Report PDF saved to %s", pdf_path)
        print(f"📁 PDF 已保存到 {pdf_path}")
    except Exception as e:
        logger.warning("PDF 导出失败（可检查系统是否安装 Pango/Cairo 等 WeasyPrint 依赖）: %s", e, exc_info=True)


def save_report(
    date_str: str,
    html_content: str,
    solar_data: Optional[Dict[str, Any]] = None,
) -> None:
    """保存报告为 HTML，并并列导出 WeasyPrint PDF。"""
    try:
        filename = REPORTS_PATH / f"report_{date_str}.html"
        with open(filename, "w", encoding="utf-8") as f:
            f.write(html_content)
        logger.info(f"Report saved to {filename}")
        print(f"📁 报告已保存到 {filename}")
        try:
            _try_save_report_pdf(date_str, html_content, solar_data)
        except Exception as pdf_ex:
            logger.warning("PDF 导出异常（已忽略，不影响 HTML）：%s", pdf_ex, exc_info=True)
    except Exception as e:
        logger.error(f"Failed to save report: {e}")

def load_report(date_str: str) -> str:
    """加载指定日期的报告"""
    try:
        filename = REPORTS_PATH / f"report_{date_str}.html"
        if filename.exists():
            with open(filename, "r", encoding="utf-8") as f:
                return f.read()
        else:
            return f"<div class='error'>未找到 {date_str} 的报告</div>"
    except Exception as e:
        logger.error(f"Failed to load report: {e}")
        return f"<div class='error'>加载报告失败: {e}</div>"

def get_all_report_dates() -> list:
    """获取所有已保存的报告日期"""
    try:
        files = os.listdir(REPORTS_PATH)
        dates = []
        for f in files:
            if f.startswith("report_") and f.endswith(".html"):
                date_str = f.replace("report_", "").replace(".html", "")
                dates.append(date_str)
        return sorted(dates, reverse=True)  # 最新的在前
    except Exception as e:
        logger.error(f"Failed to get report dates: {e}")
        return []


def load_latest_report_from_disk() -> str:
    """从磁盘加载最新一份已保存日报（按日期文件名倒序）。"""
    dates = get_all_report_dates()
    if not dates:
        return (
            "<div class='jwdsar-placeholder'>"
            "暂无已保存日报。请先在终端执行 <code>python app_scheduled.py --generate-once</code> 生成。"
            "</div>"
        )
    return load_report(dates[0])


latest_report = load_latest_report_from_disk()


def refresh_latest_from_disk():
    """从磁盘重新载入最新日报到界面。"""
    global latest_report
    latest_report = load_latest_report_from_disk()
    return latest_report


# 自定义 CSS 样式
custom_css = """
.gradio-container {
    font-family: "Segoe UI", "Microsoft YaHei UI", "PingFang SC", "Noto Sans SC", sans-serif !important;
    background: linear-gradient(165deg, #f4f7fb 0%, #eef2f7 45%, #e8edf4 100%) !important;
    min-height: 100vh;
}
.jwdsar-shell {
    max-width: min(100%, 960px);
    margin: 0 auto !important;
    padding: 0.4rem 0.5rem 1.5rem !important;
}
.jwdsar-page-title-wrap {
    text-align: center;
    margin: 0.5rem 0 1.5rem 0;
    padding: 0.5rem 0.75rem 1rem;
}
.jwdsar-page-title-wrap .jwdsar-page-title {
    display: inline-block;
    margin: 0;
    padding: 0;
    font-size: 1.65rem;
    font-weight: 700;
    line-height: 1.4;
    letter-spacing: 0.03em;
    color: #0f172a;
    font-family: "Segoe UI", "Microsoft YaHei UI", "PingFang SC", "Noto Sans SC", "Source Han Sans SC", sans-serif;
}
@media (min-width: 640px) {
    .jwdsar-page-title-wrap .jwdsar-page-title {
        font-size: 1.9rem;
    }
}
.jwdsar-tabs {
    margin-top: 0.25rem;
}
.jwdsar-tabs .tab-nav button {
    border-radius: 10px 10px 0 0 !important;
    font-weight: 600 !important;
}
.jwdsar-tabs [class*="tabitem"] {
    border-radius: 0 0 12px 12px !important;
}
.report-container {
    font-family: "Noto Serif SC", "Source Han Serif SC", "SimSun", "Georgia", serif !important;
    font-size: 16px !important;
    line-height: 1.75 !important;
    font-variant-numeric: lining-nums !important;
    font-feature-settings: "lnum" 1 !important;
    padding: 0.65rem 0.75rem 1rem !important;
    background: #ffffff !important;
    border: 1px solid #e2e8f0 !important;
    border-radius: 8px !important;
    box-shadow: none !important;
    color: #1e293b !important;
}
.report-container table {
    border-collapse: collapse;
    width: 100%;
    margin: 1.25rem 0;
    font-size: 0.95em;
    border-radius: 8px;
    overflow: hidden;
    box-shadow: 0 1px 0 #e2e8f0;
}
.report-container th, .report-container td {
    border: 1px solid #e2e8f0;
    padding: 10px 12px;
    text-align: left;
}
.report-container th {
    background: linear-gradient(180deg, #f8fafc 0%, #f1f5f9 100%);
    font-weight: 600;
    color: #334155;
}
.report-container tr:nth-child(even) td {
    background: #fafbfc;
}
.jwdsar-report-math mjx-container {
    display: inline-block;
    margin: 0 0.1em;
    vertical-align: -0.15em;
}
.report-container h1 {
    color: #0f172a;
    border-bottom: none;
    padding-bottom: 0;
    margin-top: 0;
    font-size: 1.35em;
    font-weight: 700;
}
.report-container h2 {
    color: #1e293b;
    margin-top: 1.75rem;
    margin-bottom: 0.65rem;
    font-size: 1.12em;
    font-weight: 600;
    padding-bottom: 0.35rem;
    border-bottom: 1px solid #e2e8f0;
}
.report-container h3 {
    color: #1e293b;
    margin-top: 1rem;
    margin-bottom: 0.4rem;
    font-size: 1.05em;
    font-weight: 600;
    padding-bottom: 0;
    border-bottom: none;
}
.report-container p {
    margin: 0.65em 0;
}
.report-container ul, .report-container ol {
    margin: 0.5em 0 0.5em 1.25em;
}
.jwdsar-placeholder {
    text-align: center;
    padding: 3rem 1.5rem;
    color: #64748b;
    font-size: 0.95rem;
    line-height: 1.65;
}
.error {
    color: #b91c1c;
    padding: 1rem 1.25rem;
    background: #fef2f2;
    border: 1px solid #fecaca;
    border-radius: 10px;
}
"""

def _build_gradio_demo() -> Any:
    """仅在 Web 模式下构建 Gradio 界面，避免 generate-once 路径初始化 Gradio Blocks。"""
    theme = gr.themes.Soft()
    with gr.Blocks(css=custom_css, theme=theme) as demo:
        with gr.Column(elem_classes="jwdsar-shell"):
            gr.HTML(
                '<div class="jwdsar-page-title-wrap"><h1 class="jwdsar-page-title">'
                "JW-DSAR（金乌-每日太阳活动报告智能体）"
                "</h1></div>"
            )

            with gr.Tabs(elem_classes="jwdsar-tabs"):
                with gr.Tab("最新报告"):
                    report_output = gr.HTML(
                        value=latest_report,
                        elem_classes="report-container",
                    )

                    refresh_btn = gr.Button("刷新最新日报", variant="primary")
                    refresh_btn.click(fn=refresh_latest_from_disk, outputs=report_output)

                with gr.Tab("生成指定日期"):
                    gr.Markdown(
                        "输入 **UTC 日期**（格式 `YYYY-MM-DD`）生成并写入 `reports/`。若该日期超出 NOAA 接口可回溯范围，会给出错误提示。\n\n"
                        "**提示**：请耐心等待约 **5 分钟**。生成期间按钮会自动禁用，避免重复触发。"
                    )
                    with gr.Row():
                        date_input = gr.Textbox(
                            label="报告日（UTC）",
                            placeholder="例如 2026-04-10",
                            value="",
                        )
                        gen_btn = gr.Button("生成日报", variant="primary")
                    gen_output = gr.HTML(
                        value="<div class='jwdsar-placeholder'>请输入日期后点击「生成日报」</div>",
                        elem_classes="report-container",
                    )

                    def generate_for_date(date_str: str):
                        s = (date_str or "").strip()
                        if not s:
                            return "<div class='error'>请输入 UTC 日期（YYYY-MM-DD）</div>"
                        html = generate_report(report_utc_date=s)
                        return html

                    (
                        gen_btn.click(fn=lambda: gr.update(interactive=False), inputs=None, outputs=gen_btn)
                        .then(fn=generate_for_date, inputs=date_input, outputs=gen_output)
                        .then(fn=lambda: gr.update(interactive=True), inputs=None, outputs=gen_btn)
                    )

                with gr.Tab("历史报告"):
                    with gr.Row():
                        date_dropdown = gr.Dropdown(
                            choices=get_all_report_dates(),
                            label="选择日期",
                            value=get_all_report_dates()[0] if get_all_report_dates() else None
                        )
                        load_btn = gr.Button("加载报告", variant="primary")

                    history_output = gr.HTML(
                        value="<div class='jwdsar-placeholder'>请选择日期后点击「加载报告」</div>",
                        elem_classes="report-container",
                    )

                    def load_and_display(date_str):
                        if date_str:
                            return load_report(date_str)
                        return "<div class='error'>请选择日期</div>"

                    load_btn.click(fn=load_and_display, inputs=date_dropdown, outputs=history_output)

                    refresh_dates_btn = gr.Button("刷新日期列表", variant="secondary")

                    def refresh_dates():
                        return gr.Dropdown(choices=get_all_report_dates())

                    refresh_dates_btn.click(fn=refresh_dates, outputs=date_dropdown)
    return demo


def main() -> None:
    global REPORTS_DIR, REPORTS_PATH, latest_report
    parser = argparse.ArgumentParser(description="JW-DSAR（金乌-每日太阳活动报告智能体）")
    parser.add_argument(
        "--generate-once",
        action="store_true",
        help="仅生成一次日报并写入 reports/，不启动 Web（供 cron 调用）",
    )
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="指定报告 UTC 日期（YYYY-MM-DD）。与 --generate-once 搭配可用于补生成历史日报。",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=None,
        help="输出目录（保存 report_YYYY-MM-DD.html/.pdf）。也可用环境变量 JWDSAR_REPORTS_DIR 指定。",
    )
    parser.add_argument(
        "--continuum-image",
        type=str,
        default=None,
        help="本地全日面连续谱图像路径（JPEG/PNG），用于替换图库中的连续谱图；若未指定 --date，会从文件名提取 YYYYMMDD 作为报告日。",
    )
    # backward-compatible alias
    parser.add_argument(
        "--hmi-continuum-image",
        dest="continuum_image",
        type=str,
        default=None,
        help="（兼容旧参数）同 --continuum-image",
    )
    parser.add_argument(
        "--continuum-rotate-deg",
        type=float,
        default=None,
        help="本地连续谱图相对 HMI/AIA 的旋转角度（度，顺时针为正）。也可用环境变量 JWDSAR_LOCAL_ROTATE_DEG。",
    )
    parser.add_argument(
        "--hmi-continuum-rotate-deg",
        dest="continuum_rotate_deg",
        type=float,
        default=None,
        help="（兼容旧参数）同 --continuum-rotate-deg",
    )
    parser.add_argument(
        "--resolution",
        type=int,
        default=None,
        help="（可选）SDO browse/latest 图像分辨率（256/512/1024/2048/3072/4096）。默认 1024。一般不需要设置。",
    )
    parser.add_argument(
        "--sdo-resolution",
        dest="resolution",
        type=int,
        default=None,
        help="（兼容旧参数）同 --resolution",
    )
    args = parser.parse_args()

    if args.output_dir:
        REPORTS_PATH = _resolve_reports_dir(args.output_dir)
        REPORTS_DIR = str(REPORTS_PATH)
        os.environ["JWDSAR_REPORTS_DIR"] = REPORTS_DIR
        REPORTS_PATH.mkdir(parents=True, exist_ok=True)

    if args.generate_once:
        if args.continuum_rotate_deg is not None:
            os.environ["JWDSAR_LOCAL_ROTATE_DEG"] = str(args.continuum_rotate_deg)
        html = generate_report(
            report_utc_date=args.date,
            hmi_continuum_image_path=args.continuum_image,
            sdo_resolution=args.resolution,
        )
        latest_report = html  # 模块顶层赋值，供同进程内引用
        logger.info("generate-once finished.")
        failed = bool(html.strip().startswith("<div class='error'>"))
        raise SystemExit(1 if failed else 0)

    port = int(os.getenv("PORT", "7860"))
    latest_report = load_latest_report_from_disk()
    demo = _build_gradio_demo()
    logger.info("Starting Gradio (load latest from disk at startup; use cron for daily --generate-once)")
    demo.launch(server_name="0.0.0.0", server_port=port)


if __name__ == "__main__":
    main()
