"""根据 NOAA SWPC 风格日面位置串，将活动区编号映射到全日面图上的近似百分比坐标（正射投影近似）。"""
from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional, Tuple

# GSFC latest JPEG 中日面圆半径约占图像宽度的一半宽度比例（可调）
DISK_RADIUS_FRAC = 0.46

# NOAA location 正则：N15E25、S06W66、N08W0* 等
_LOC_RE = re.compile(
    r"^\s*([NS])(\d+(?:\.\d+)?)([EW])(\d+(?:\.\d+)?)\*?\s*$",
    re.IGNORECASE,
)


def parse_swpc_location(loc: str) -> Optional[Tuple[float, float]]:
    """
    解析 solar_regions.json 的 location 字符串为 (纬度°, 经度°)。
    经度采用 Stonyhurst 约定：**西向为正**（与 CM 夹角，西半球为正）。
    - W20 → +20；E25 → -25
    """
    if not loc or not str(loc).strip():
        return None
    s = str(loc).strip().upper().rstrip("*").strip()
    m = _LOC_RE.match(s)
    if not m:
        return None
    hemi_lat, lat_s, hemi_lon, lon_s = m.group(1), m.group(2), m.group(3), m.group(4)
    lat = float(lat_s) * (1 if hemi_lat == "N" else -1)
    lon_mag = float(lon_s)
    if hemi_lon.upper() == "W":
        lon_deg = lon_mag
    else:
        lon_deg = -lon_mag
    return (lat, lon_deg)


def stonyhurst_to_disk_xy(lat_deg: float, lon_deg: float, *, b0_deg: float = 0.0) -> Tuple[float, float]:
    """
    地球观测正射投影下的归一化日面坐标 (x, y)，范围约 [-1, 1]。
    x：东负西正（与经度符号一致）；y：北正南负。
    """
    lat_r = math.radians(lat_deg)
    lon_r = math.radians(lon_deg)
    b0_r = math.radians(float(b0_deg or 0.0))
    # 正射投影 + 观察者纬度(B0)倾角修正（x 轴指向西，y 轴指向北）
    x = math.cos(lat_r) * math.sin(lon_r)
    y = math.sin(lat_r) * math.cos(b0_r) - math.cos(lat_r) * math.cos(lon_r) * math.sin(b0_r)
    return (x, y)


def _visible_on_disk(x: float, y: float) -> bool:
    return (x * x + y * y) <= 1.0001


def overlay_positions_for_regions(
    regions: List[Dict[str, Any]],
    radius_frac: float = DISK_RADIUS_FRAC,
    *,
    center_left_pct: float = 50.0,
    center_top_pct: float = 50.0,
    b0_deg: float = 0.0,
) -> List[Dict[str, Any]]:
    """
    输入 active_regions 行（需含 NOAA Number、Position 或 location）。
    返回 {noaa, left_pct, top_pct, ok, reason?}
    """
    out: List[Dict[str, Any]] = []
    for row in regions:
        noaa = str(row.get("NOAA Number") or row.get("region") or "").strip()
        pos = row.get("Position") or row.get("location") or ""
        if not noaa:
            continue
        parsed = parse_swpc_location(str(pos))
        if not parsed:
            out.append({"noaa": noaa, "left_pct": None, "top_pct": None, "ok": False, "reason": "unparsed"})
            continue
        lat_deg, lon_deg = parsed
        x, y = stonyhurst_to_disk_xy(lat_deg, lon_deg, b0_deg=float(b0_deg or 0.0))
        if not _visible_on_disk(x, y):
            out.append({"noaa": noaa, "left_pct": None, "top_pct": None, "ok": False, "reason": "far_side"})
            continue
        r = max(0.05, min(0.499, float(radius_frac)))
        cx = float(center_left_pct)
        cy = float(center_top_pct)
        left_pct = cx + 100.0 * r * x
        top_pct = cy - 100.0 * r * y
        left_pct = max(0.0, min(100.0, left_pct))
        top_pct = max(0.0, min(100.0, top_pct))
        out.append({"noaa": noaa, "left_pct": left_pct, "top_pct": top_pct, "ok": True})
    return out
