"""NOAA 辅助：活动区号规范化与 SDO 全日面图 URL（原 SRS 文本解析已移除，分类以 solar_regions.json 为准）。"""
from __future__ import annotations

import html
import os
import re
import base64
import logging
import json
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timezone

from helio_disk_overlay import overlay_positions_for_regions

logger = logging.getLogger(__name__)

# 可嵌入 <img> 的 JPEG（image/jpeg）。勿用 data/aiahmi/latest.php：该 URL 返回 HTML+JS，浏览器无法作为图片显示。
SDO_ASSETS_LATEST_BASE = "https://sdo.gsfc.nasa.gov/assets/img/latest"
# 历史 browse 归档目录（可回溯，按 YYYY/MM/DD 组织）
SDO_ASSETS_BROWSE_BASE = "https://sdo.gsfc.nasa.gov/assets/img/browse"
# 浏览页（非直链图）：https://sdo.gsfc.nasa.gov/data/aiahmi/latest.php?t=...
SDO_LATEST_PAGE_BASE = "https://sdo.gsfc.nasa.gov/data/aiahmi/latest.php"

# 标签, 内部产品 id（映射到 assets 文件名中的波段代码）
SDO_FULL_DISK_PRODUCTS: List[tuple[str, str]] = [
    ("HMI 连续谱", "hmi_igr"),
    ("HMI 磁图", "hmi_mag"),
    ("AIA 131Å", "aia_0131"),
    ("AIA 171Å", "aia_0171"),
    ("AIA 193Å", "aia_0193"),
    ("AIA 304Å", "aia_0304"),
]


def _product_to_latest_jpg_slug(product: str) -> str:
    """hmi_igr / hmi_mag / aia_0171 -> assets 使用的文件名片段（如 HMII、0171）。"""
    if product == "hmi_igr":
        return "HMII"
    if product == "hmi_mag":
        return "HMIB"
    if product.startswith("aia_"):
        return product.replace("aia_", "")
    return product.upper()


def _product_to_browse_code(product: str) -> str:
    """映射到 browse 文件名末尾的 code（如 0131、HMIB、HMIIC）。"""
    if product == "hmi_mag":
        return "HMIB"
    # HMI 连续谱：browse 中更常见的是 HMIIC/HMIIF；这里选 HMIIC（continuum）
    if product == "hmi_igr":
        return "HMIIC"
    if product.startswith("aia_"):
        return product.replace("aia_", "")
    return product.upper()


def _clamp_browse_resolution(r: int) -> int:
    # browse 目录常见分辨率：256/512/1024/2048/3072/4096
    # 这里按最接近值取整到上述集合
    candidates = [256, 512, 1024, 2048, 3072, 4096]
    r0 = max(min(candidates), min(max(candidates), int(r)))
    return min(candidates, key=lambda x: abs(x - r0))


def _fetch_text(url: str, timeout: int = 30) -> Optional[str]:
    """简单文本下载（不负责缓存）。"""
    try:
        import requests  # local import to keep module light

        resp = requests.get(url, timeout=timeout)
        if resp.status_code != 200:
            return None
        resp.encoding = resp.encoding or "utf-8"
        return resp.text
    except Exception:
        return None


def _read_json_if_exists(path: str) -> Optional[Dict[str, Any]]:
    try:
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            with open(path, "r", encoding="utf-8") as f:
                v = json.load(f)
            return v if isinstance(v, dict) else None
    except Exception:
        return None


def _write_json_atomic(path: str, data: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def _pick_browse_filenames_for_day(*, dir_url: str, ymd: str, res: int, day_dir: str) -> Optional[Dict[str, str]]:
    """
    只抓取 1 个 browse 目录索引页（HTML），解析出文件名：
      YYYYMMDD_HHMMSS_{res}_{code}.jpg
    然后为每个 code 直接取“出现的第一个”文件名（目录通常按文件名升序）。
    结果缓存到 data/YYYY-MM-DD/sdo_browse_match.json，避免重复抓取索引页。
    """
    mapping_path = os.path.join(day_dir, "sdo_browse_match.json")
    cached = _read_json_if_exists(mapping_path)
    if cached and cached.get("ymd") == ymd and int(cached.get("res", 0) or 0) == int(res):
        files0 = cached.get("files")
        if isinstance(files0, dict) and files0:
            normalized = {str(k): str(v) for k, v in files0.items() if v}
            # 指定日期的 browse 允许“部分波段可用”，直接使用缓存，避免反复抓目录页。
            return normalized

    idx = _fetch_text(dir_url, timeout=30)
    if not idx:
        return None

    files: Dict[str, str] = {}
    for _, prod in SDO_FULL_DISK_PRODUCTS:
        code = _product_to_browse_code(prod)
        pat = re.compile(rf"\b({ymd}_(\d{{6}})_{int(res)}_{re.escape(code)}\.jpg)\b")
        m = pat.search(idx)
        if not m:
            continue
        files[code] = m.group(1)

    if not files:
        return None
    _write_json_atomic(mapping_path, {"ymd": ymd, "res": int(res), "files": files})
    return files


def _download_bytes_if_missing(url: str, dest_path: str, *, timeout: int = 60) -> Optional[str]:
    """下载二进制文件到本地；若已存在且非空则直接复用。返回本地路径或 None。"""
    try:
        import requests  # local import
        import os

        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        if os.path.isfile(dest_path) and os.path.getsize(dest_path) > 0:
            return dest_path
        r = requests.get(url, timeout=timeout, stream=True)
        if r.status_code != 200:
            return None
        tmp = dest_path + ".part"
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                if chunk:
                    f.write(chunk)
        os.replace(tmp, dest_path)
        return dest_path
    except Exception:
        return None


def _jpg_file_to_data_uri(path: str) -> Optional[str]:
    try:
        import base64
        import os

        if not os.path.isfile(path) or os.path.getsize(path) <= 0:
            return None
        with open(path, "rb") as f:
            b = f.read()
        return "data:image/jpeg;base64," + base64.b64encode(b).decode("ascii")
    except Exception:
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
    except Exception:
        return None


def _normalize_assistant_content(content: Any) -> Optional[str]:
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


def _parse_ocr_time_to_iso_utc(text: str) -> Optional[str]:
    s = str(text or "").strip()
    if not s:
        return None
    if s.upper() in {"NA", "N/A", "NONE", "NULL"}:
        return None
    s = s.replace("\n", " ").replace("\r", " ")
    # 允许常见格式：2026-04-17 03:40:15 / 2026/04/17 03:40 / 2026-04-17T03:40:15Z
    m = re.search(
        r"(20\d{2})[-/](\d{2})[-/](\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?",
        s,
    )
    if not m:
        return None
    yy, mm, dd, hh, mi, ss = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5), (m.group(6) or "00")
    try:
        dt = datetime(int(yy), int(mm), int(dd), int(hh), int(mi), int(ss), tzinfo=timezone.utc)
    except Exception:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _ocr_latest_obs_time_with_qwen(image_url: str, page_url: str) -> Optional[str]:
    """用 DashScope 多模态 OCR 识别 latest 图上的 UTC 时间。"""
    try:
        from dashscope import MultiModalConversation  # type: ignore
    except Exception:
        logger.warning("latest OCR 跳过：dashscope 不可用")
        return None

    api_key = os.getenv("DASHSCOPE_API_KEY") or _read_secret_file(os.getenv("DASHSCOPE_API_KEY_FILE"))
    if not api_key:
        logger.warning("latest OCR 跳过：未配置 DASHSCOPE_API_KEY / DASHSCOPE_API_KEY_FILE")
        return None
    model = os.getenv("DASHSCOPE_VL_MODEL", "qwen3.6-plus")
    prompt = (
        "请读取这张 SDO latest 图片上叠印的观测时间（UTC）。"
        "只返回一个时间，格式严格为 YYYY-MM-DDTHH:MM:SSZ。"
        "若无法识别则只返回 NA，不要返回其他文字。"
    )
    messages = [
        {"role": "system", "content": [{"text": "你是严谨的时间OCR助手。"}]},
        {
            "role": "user",
            "content": [
                {"image": image_url},
                {"text": f"参考页面：{page_url}"},
                {"text": prompt},
            ],
        },
    ]
    try:
        resp = MultiModalConversation.call(
            api_key=api_key,
            model=model,
            messages=messages,
            result_format="message",
        )
        txt = _extract_dashscope_message_text(resp)
        iso = _parse_ocr_time_to_iso_utc(str(txt or ""))
        if iso:
            logger.info("latest OCR 识别时间成功：%s", iso)
        else:
            logger.warning("latest OCR 未识别到有效时间，原始返回=%s", str(txt or "").strip()[:160])
        return iso
    except Exception as ex:
        logger.warning("latest OCR 调用失败: %s", ex)
        return None


def build_full_disk_image_list_for_date(date_utc: str, resolution: int = 1024) -> List[Dict[str, str]]:
    """按指定 UTC 日期从 browse 归档取全日面浏览 JPEG（直链，可用于历史日报）。"""
    if not date_utc:
        return []
    ds = str(date_utc).strip()[:10]
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", ds):
        return []
    y, m, d = ds.split("-")
    ymd = f"{y}{m}{d}"
    r = _clamp_browse_resolution(int(resolution))
    dir_url = f"{SDO_ASSETS_BROWSE_BASE}/{y}/{m}/{d}/"
    strict_browse_local = str(os.getenv("JWDSAR_STRICT_BROWSE_LOCAL", "1")).strip().lower() in (
        "1",
        "true",
        "yes",
        "y",
    )
    out: List[Dict[str, str]] = []
    # 历史 browse：统一落盘到 data/YYYY-MM-DD/ 下
    # - sdo_browse_match.json：仅缓存“00:00 UTC 附近”的文件名映射（不落盘整份 index.html）
    # - *.jpg：各波段 browse 图（存在则跳过下载）
    base = os.path.dirname(os.path.abspath(__file__))
    day_dir = os.path.join(base, "data", f"{y}-{m}-{d}")
    mapping = _pick_browse_filenames_for_day(dir_url=dir_url, ymd=ymd, res=r, day_dir=day_dir)
    if not mapping:
        return []
    for label, prod in SDO_FULL_DISK_PRODUCTS:
        code = _product_to_browse_code(prod)
        fname = mapping.get(code)
        if not fname:
            continue
        # 从文件名解析观测时刻：YYYYMMDD_HHMMSS_...
        obs_time = ""
        m_ts = re.match(r"^(\d{8})_(\d{6})_", fname)
        if m_ts:
            ymd8, hms = m_ts.group(1), m_ts.group(2)
            obs_time = f"{ymd8[0:4]}-{ymd8[4:6]}-{ymd8[6:8]}T{hms[0:2]}:{hms[2:4]}:{hms[4:6]}Z"
        url = dir_url + fname
        local_path = os.path.join(day_dir, fname)
        lp = _download_bytes_if_missing(url, local_path, timeout=60)
        data_uri = _jpg_file_to_data_uri(lp) if lp else None
        if strict_browse_local and not data_uri:
            logger.warning("browse 图像本地化失败，已跳过该波段: %s", url)
            continue
        out.append(
            {
                "label": label,
                "product": prod,
                "url": data_uri or url,
                "page_url": "",
                "resolution": str(r),
                **({"obs_time_utc": obs_time} if obs_time else {}),
            }
        )
    return out


def normalize_noaa_region_id(value: Any) -> str:
    """与 solar_regions.json 的 region 字段一致的字符串（通常为数字）。"""
    if value is None:
        return ""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return str(value).strip()
    return str(n)


def build_full_disk_image_list(
    resolution: int = 1024,
    date_utc: Optional[str] = None,
    source: str = "NOAA_JSON",
    persist_latest: Optional[bool] = None,
) -> List[Dict[str, str]]:
    """
    NASA SDO GSFC 浏览 JPEG（直链）。

    强绑定规则（全局统一）：
    - source == "SRS"       -> 使用 date_utc 对应 browse 归档（历史图）
    - source != "SRS"（如 NOAA_JSON）-> 一律使用 latest（忽略 date_utc）
    """
    if str(source or "").upper() == "SRS":
        if date_utc:
            # SRS 模式：指定日期严格使用 browse，不回退 latest。
            return build_full_disk_image_list_for_date(date_utc, resolution=resolution)
        return []
    if persist_latest is None:
        persist_latest = str(os.getenv("JWDSAR_PERSIST_LATEST", "1")).strip().lower() in (
            "1",
            "true",
            "yes",
            "y",
        )
    # 为了让已生成 HTML 不受远端 latest 实时变化影响，默认开启 strict：
    # 当要求落盘(persist_latest=True)时，只允许使用本地 data-uri；下载失败则该波段不写入 HTML。
    strict_latest_local = str(os.getenv("JWDSAR_STRICT_LATEST_LOCAL", "1")).strip().lower() in (
        "1",
        "true",
        "yes",
        "y",
    )
    r = max(512, min(2048, int(resolution)))
    day = str(date_utc or "").strip()[:10]
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", day):
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    day_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", day)
    latest_obs_cache_path = os.path.join(day_dir, "sdo_latest_obs_time.json")
    latest_obs_cache = _read_json_if_exists(latest_obs_cache_path) or {}
    if not isinstance(latest_obs_cache, dict):
        latest_obs_cache = {}
    latest_obs_changed = False
    # latest 统一 OCR 一次时间（各波段同一批次）
    # 用户指定：固定识别 AIA 171 图像（时间标注更易读）
    ocr_key = f"latest_{r}_common_obs_time_utc_from_aia_0171"
    common_obs_time = str(latest_obs_cache.get(ocr_key) or "").strip()
    if not common_obs_time:
        ocr_prod = "aia_0171"
        ocr_slug = _product_to_latest_jpg_slug(ocr_prod)
        ocr_url = f"{SDO_ASSETS_LATEST_BASE}/latest_{r}_{ocr_slug}.jpg"
        ocr_page_url = f"{SDO_LATEST_PAGE_BASE}?t={ocr_prod}&r={r}"
        common_obs_time = str(_ocr_latest_obs_time_with_qwen(ocr_url, ocr_page_url) or "").strip()
        if common_obs_time:
            latest_obs_cache[ocr_key] = common_obs_time
            latest_obs_changed = True
    out: List[Dict[str, str]] = []
    for label, prod in SDO_FULL_DISK_PRODUCTS:
        slug = _product_to_latest_jpg_slug(prod)
        url = f"{SDO_ASSETS_LATEST_BASE}/latest_{r}_{slug}.jpg"
        page_url = f"{SDO_LATEST_PAGE_BASE}?t={prod}&r={r}"
        use_url = url
        obs_time = common_obs_time
        if persist_latest:
            fname = f"{day.replace('-', '')}_latest_{r}_{slug}.jpg"
            local_path = os.path.join(day_dir, fname)
            lp = _download_bytes_if_missing(url, local_path, timeout=60)
            data_uri = _jpg_file_to_data_uri(lp) if lp else None
            if data_uri:
                use_url = data_uri
            elif strict_latest_local:
                logger.warning("latest 图像本地化失败，已跳过该波段: %s", url)
                continue
        out.append(
            {
                "label": label,
                "product": prod,
                "url": use_url,
                "page_url": page_url,
                "resolution": str(r),
                **({"obs_time_utc": obs_time} if obs_time else {}),
            }
        )
    if latest_obs_changed:
        try:
            _write_json_atomic(latest_obs_cache_path, latest_obs_cache)
        except Exception as ex:
            logger.warning("保存 latest 观测时刻缓存失败: %s", ex)
    return out


def html_sdo_gallery_section(
    images: List[Dict[str, str]],
    active_regions: Optional[List[Dict[str, str]]] = None,
    *,
    compact_for_pdf: bool = False,
) -> str:
    def _b0_deg_for_image(im: Dict[str, str]) -> float:
        """优先用 sunpy 按观测时刻计算 B0；无依赖/无时刻则回退 0。"""
        t1 = str(im.get("obs_time_utc") or "")
        if not t1:
            return 0.0
        try:
            from astropy.time import Time
            from sunpy.coordinates.sun import B0

            return float(B0(Time(t1)).degree)
        except Exception:
            return 0.0

    """生成可嵌入日报 HTML 的 SDO 图库片段；可选根据 NOAA 位置叠字活动区编号（近似）。
    compact_for_pdf=True：三列 flex、限高缩略图、无 lightbox，供 WeasyPrint 与论文排版。"""
    if not images:
        return ""
    # 叠标参数：完全按旧版报告（如 report_2026-04-14.html）一致的常量投影
    # - 圆心固定在图像中心（50%,50%）
    # - 半径使用 helio_disk_overlay.DISk_RADIUS_FRAC（当前为 0.46）


    def _parse_noaa_lonlat_from_position(pos: str) -> Optional[Tuple[float, float]]:
        """N13E58 / S16W65 / N01W0* -> (lon_deg_west_positive, lat_deg)."""
        s = str(pos or "").strip().upper().replace("*", "")
        m = re.match(r"^([NS])(\\d{1,2})([EW])(\\d{1,3})$", s)
        if not m:
            return None
        lat = float(m.group(2))
        if m.group(1) == "S":
            lat = -lat
        lon = float(m.group(4))
        # Stonyhurst: West positive, East negative
        if m.group(3) == "E":
            lon = -lon
        return (lon, lat)


    def _format_position_from_lonlat(lon: float, lat: float) -> str:
        """(lon_west_positive, lat) -> NxxEyy/Wyy string."""
        ns = "N" if lat >= 0 else "S"
        ew = "W" if lon >= 0 else "E"
        return f"{ns}{int(round(abs(lat))):02d}{ew}{int(round(abs(lon))):02d}"


    def _propagate_position_rigid(pos: str, *, t0_utc: str, t1_utc: str) -> str:
        """
        刚体自转传播（近似）：以西经为正的 Stonyhurst 经度按固定角速度推进。
        omega = 360 / 27.2753 deg/day (synodic, 近似地球视角日面漂移)。
        """
        ll = _parse_noaa_lonlat_from_position(pos)
        if not ll:
            return pos
        try:
            t0 = datetime.fromisoformat(t0_utc.replace("Z", "+00:00")).astimezone(timezone.utc)
            t1 = datetime.fromisoformat(t1_utc.replace("Z", "+00:00")).astimezone(timezone.utc)
        except Exception:
            return pos
        dt_days = (t1 - t0).total_seconds() / 86400.0
        omega = 360.0 / 27.2753
        lon0, lat0 = ll
        lon1 = lon0 + omega * dt_days
        # wrap to [-180,180]
        while lon1 > 180:
            lon1 -= 360
        while lon1 < -180:
            lon1 += 360
        return _format_position_from_lonlat(lon1, lat0)


    def _labels_for_image(im: Dict[str, str]) -> tuple[str, str, str, str]:
        """按旧版常量投影计算叠标位置。"""
        if not active_regions:
            return ("", "", "", "")
        regs = active_regions or []
        # AIA 波段：按用户要求使用更小的日面半径比例
        prod = str(im.get("product") or "")
        # SFMM 连续谱：单独设定半径比例
        radius_frac = 0.48 if str(im.get("label") or "") == "SFMM 连续谱" else (0.40 if prod.startswith("aia_") else None)
        b0_deg = _b0_deg_for_image(im)
        # 所有图像统一半透明标号，尽量减少对底图细节遮挡
        is_sfmm = str(im.get("label") or "") == "SFMM 连续谱"
        # 若提供了 SRS 位置有效时刻与图像观测时刻，则按每张图进行位置传播后再叠标
        t0 = str(im.get("srs_valid_time_utc") or "")
        t1 = str(im.get("obs_time_utc") or "")
        if t0 and t1:
            regs2: List[Dict[str, str]] = []
            for r in regs:
                rr = dict(r)
                if rr.get("Position"):
                    rr["Position"] = _propagate_position_rigid(rr["Position"], t0_utc=t0, t1_utc=t1)
                regs2.append(rr)
            overlay = overlay_positions_for_regions(
                regs2,
                **({"radius_frac": radius_frac} if radius_frac is not None else {}),
                b0_deg=b0_deg,
            )
        else:
            overlay = overlay_positions_for_regions(
                regs,
                **({"radius_frac": radius_frac} if radius_frac is not None else {}),
                b0_deg=b0_deg,
            )
        labels_html = ""
        labels_html_lb = ""
        labels_html_pdf = ""
        labels_html_lb_pdf = ""
        for o in overlay:
            if not o.get("ok"):
                continue
            noaa = html.escape(str(o.get("noaa", "")))
            lp = float(o["left_pct"])
            tp = float(o["top_pct"])
            base = (
                f'<span class="jwdsar-ar-label" role="img" aria-label="NOAA {noaa}" '
                f'style="position:absolute;left:{lp:.2f}%;top:{tp:.2f}%;transform:translate(-50%,-50%);'
                f"font-weight:{'500' if is_sfmm else '600'};color:{'rgba(37,99,235,0.28)' if is_sfmm else 'rgba(255,255,255,0.58)'};pointer-events:none;white-space:nowrap;line-height:1;"
            )
            shadow_small = (
                "text-shadow:0 0 1px rgba(255,255,255,0.65);" if is_sfmm else "text-shadow:0 0 1px rgba(0,0,0,0.35);"
            )
            shadow_big = (
                "text-shadow:0 0 1px rgba(255,255,255,0.65);" if is_sfmm else "text-shadow:0 0 2px rgba(0,0,0,0.45),0 0 4px rgba(0,0,0,0.35);"
            )
            labels_html += base + f"font-size:7px;letter-spacing:0;{shadow_small}" + f'">{noaa}</span>'
            labels_html_lb += base + f"font-size:16px;letter-spacing:0;{shadow_big}" + f'">{noaa}</span>'
            labels_html_pdf += (
                base
                + "font-size:1.7mm;font-family:Arial,Helvetica,sans-serif;"
                + "font-weight:700;letter-spacing:-0.05mm;"
                + f'">{noaa}</span>'
            )
            labels_html_lb_pdf += (
                base
                + "font-size:2.8mm;font-family:Arial,Helvetica,sans-serif;"
                + "font-weight:700;letter-spacing:-0.08mm;"
                + f'">{noaa}</span>'
            )
        return (labels_html, labels_html_lb, labels_html_pdf, labels_html_lb_pdf)
    items: List[str] = []
    lightboxes: List[str] = []
    for idx, im in enumerate(images):
        label = im.get("label", "")
        url = im.get("url", "")
        esc = url.replace("&", "&amp;")
        esc_alt = html.escape(label, quote=True)
        lb_id = f"jwdsar-sdo-lb-{idx}"
        lb_hash = f"#{lb_id}"
        cap_html = (
            f'<a href="{lb_hash}" '
            'style="color:#64748b;text-decoration:underline;cursor:pointer;">'
            f"{html.escape(label)}</a>"
        )
        labels_html, labels_html_lb, labels_html_pdf, labels_html_lb_pdf = _labels_for_image(im)
        overlay_block = labels_html if labels_html else ""
        overlay_block_pdf = labels_html_pdf if labels_html_pdf else ""
        brand = (
            '<span class="jwdsar-sdo-brand" aria-hidden="true" '
            'style="position:absolute;right:2%;bottom:2%;'
            "font-size:8px;font-weight:600;letter-spacing:0.06em;"
            "color:rgba(255,255,255,0.88);text-shadow:0 0 3px #000,0 0 5px #000;"
            'pointer-events:none;">JW-DSAR</span>'
        )
        brand_pdf = (
            '<span class="jwdsar-sdo-brand" aria-hidden="true" '
            'style="position:absolute;right:2%;bottom:2%;'
            "font-size:1.8mm;font-family:Arial,Helvetica,sans-serif;"
            "font-weight:700;letter-spacing:0;"
            "color:rgba(255,255,255,0.88);"
            'pointer-events:none;">JW-DSAR</span>'
        )
        brand_lb = (
            '<span class="jwdsar-sdo-brand jwdsar-sdo-brand--lb" aria-hidden="true" '
            'style="position:absolute;right:2%;bottom:2%;'
            "font-size:16px;font-weight:600;letter-spacing:0.06em;"
            "color:rgba(255,255,255,0.9);text-shadow:0 0 4px #000,0 0 10px #000;"
            'pointer-events:none;">JW-DSAR</span>'
        )
        overlay_lb = labels_html_lb if labels_html_lb else ""
        overlay_lb_pdf = labels_html_lb_pdf if labels_html_lb_pdf else ""

        if compact_for_pdf:
            cap_plain = f'<span style="color:#64748b;">{html.escape(label)}</span>'
            items.append(
                '<figure class="jwdsar-sdo-fig-pdf" style="margin:0 0 0.5rem 0;'
                'width:33.333%;box-sizing:border-box;vertical-align:top;page-break-inside:avoid;'
                'padding:0 0.5% 0 0;">'
                '<div style="text-align:center;">'
                '<div style="position:relative;display:inline-block;line-height:0;max-width:100%;">'
                f'<img src="{esc}" alt="{esc_alt}" '
                'style="max-width:100%;max-height:45mm;width:auto;height:auto;border-radius:6px;'
                'border:1px solid #e2e8f0;display:block;" '
                'draggable="false" />'
                f"{overlay_block_pdf}"
                f"{brand_pdf}"
                "</div>"
                "</div>"
                f'<figcaption style="margin-top:0.35rem;font-size:0.75rem;text-align:center;">{cap_plain}</figcaption>'
                "</figure>"
            )
            continue

        items.append(
            f'<figure style="margin:0;text-align:center;">'
            f'<a href="{lb_hash}" aria-label="{html.escape("点击放大：" + label, quote=True)}" '
            'style="position:relative;display:inline-block;width:100%;max-width:100%;'
            'cursor:pointer;border-radius:8px;text-decoration:none;color:inherit;">'
            f'<img src="{esc}" alt="{esc_alt}" '
            f'style="max-width:100%;height:auto;border-radius:8px;border:1px solid #e2e8f0;display:block;" '
            f'loading="lazy" draggable="false" />'
            f"{overlay_block}"
            f"{brand}"
            f"</a>"
            f'<figcaption style="margin-top:0.4rem;font-size:0.85rem;color:#64748b;">{cap_html}</figcaption>'
            f"</figure>"
        )
        lightboxes.append(
            f'<div id="{lb_id}" class="jwdsar-sdo-lb-pop" role="dialog" aria-modal="true" '
            f'aria-label="{html.escape("放大：" + label, quote=True)}">'
            '<div style="position:absolute;top:0;left:0;right:0;bottom:0;display:flex;align-items:center;justify-content:center;'
            'padding:0.5rem;box-sizing:border-box;">'
            f'<a href="#jwdsar-sdo-gallery" class="jwdsar-sdo-lb-scrim" aria-label="关闭" '
            'style="position:absolute;top:0;left:0;right:0;bottom:0;background:rgba(15,23,42,0.85);"></a>'
            '<div class="jwdsar-sdo-lb-panel" style="position:relative;z-index:1;max-width:1400px;width:96%;'
            'background:#0f172a;border-radius:12px;overflow:hidden;'
            'box-shadow:0 25px 50px -12px rgba(0,0,0,0.5);">'
            f'<a href="#jwdsar-sdo-gallery" class="jwdsar-sdo-lb-x" aria-label="关闭" '
            'style="position:absolute;top:0.5rem;right:0.5rem;z-index:3;width:2.25rem;height:2.25rem;'
            'display:flex;align-items:center;justify-content:center;border-radius:9999px;'
            'background:rgba(15,23,42,0.75);color:#f8fafc;font-size:1.35rem;line-height:1;'
            'text-decoration:none;">&times;</a>'
            '<div style="position:relative;display:inline-block;line-height:0;max-width:100%;">'
            f'<img src="{esc}" alt="{esc_alt}" '
            'style="max-width:100%;max-height:800px;width:auto;height:auto;display:block;margin:0 auto;" />'
            f"{overlay_lb}"
            f"{brand_lb}"
            "</div>"
            f'<div style="text-align:center;padding:0.45rem 0.75rem 0.65rem;font-size:0.88rem;color:#e2e8f0;">'
            f"{html.escape(label)}</div>"
            "</div></div></div>"
        )

    if compact_for_pdf:
        grid = (
            '<div class="jwdsar-sdo-grid-pdf" style="display:flex;flex-wrap:wrap;align-items:flex-start;'
            'justify-content:flex-start;margin-top:0.75rem;">'
            + "".join(items)
            + "</div>"
        )
        lb_html = ""
        intro2 = (
            "<p style=\"color:#64748b;font-size:0.9rem;margin:0 0 0.75rem 0;\">"
            "图像为 NASA SDO 官方 latest 浏览图（约近实时），与上文耀斑事件时刻未必逐点对齐。"
            "</p>"
        )
    else:
        grid = (
            '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));'
            'gap:1rem;margin-top:0.75rem;">'
            + "".join(items)
            + "</div>"
        )
        lb_css = (
            "<style>"
            "#jwdsar-sdo-gallery .jwdsar-sdo-lb-pop{display:none;position:fixed;top:0;left:0;right:0;bottom:0;z-index:2147483000;"
            "align-items:center;justify-content:center;padding:0.5rem;box-sizing:border-box;}"
            "#jwdsar-sdo-gallery .jwdsar-sdo-lb-pop:target{display:flex;}"
            "#jwdsar-sdo-gallery .jwdsar-sdo-lb-x:hover{background:rgba(30,41,59,0.95)!important;}"
            "</style>"
        )
        lb_html = lb_css + "".join(lightboxes)
        intro2 = (
            "<p style=\"color:#64748b;font-size:0.9rem;margin:0 0 0.75rem 0;\">"
            "图像为 NASA SDO 官方 latest 浏览图（约近实时），与上文耀斑事件时刻未必逐点对齐。"
            "点击下方缩略图或波段名称可在本页放大（无需脚本）；点遮罩或 × 关闭。"
            "</p>"
        )

    sec_class = "jwdsar-sdo-gallery jwdsar-sdo-gallery--pdf" if compact_for_pdf else "jwdsar-sdo-gallery"
    return (
        f'<section id="jwdsar-sdo-gallery" class="{sec_class}" style="margin-top:2rem;padding-top:1.25rem;'
        'border-top:1px solid #e2e8f0;">'
        "<h2 style=\"color:#0f172a;font-size:1.15em;margin:0 0 0.5rem 0;\">全日面影像（SDO / HMI · AIA）</h2>"
        f"{intro2}"
        "<p style=\"color:#94a3b8;font-size:0.82rem;margin:0 0 0.75rem 0;\">"
        "图上的活动区编号由 NOAA 表格中的「位置」经日面几何近似推算，仅作示意，非精确定位。"
        "若「位置」末尾带星号（如 N08W0*），为 SWPC 发布数据中的业务标记（多见于中央子午线 W0° 附近等）；"
        "叠标解析时会忽略 *。"
        "</p>"
        f"{grid}"
        f"{lb_html}"
        '<p style="margin-top:1rem;font-size:0.8rem;color:#94a3b8;">'
        "数据源：NASA GSFC SDO · <a href=\"https://sdo.gsfc.nasa.gov/\" target=\"_blank\" rel=\"noopener\">sdo.gsfc.nasa.gov</a>"
        "</p></section>"
    )
