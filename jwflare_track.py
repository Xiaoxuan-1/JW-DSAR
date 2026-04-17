"""从全日面 HMI FITS 序列裁剪活动区 150×150 PNG（依赖 sunpy / astropy；可延后安装）。"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import imageio.v2 as imageio
import numpy as np

from helio_disk_overlay import parse_swpc_location

logger = logging.getLogger(__name__)

CROP_HALF_SIZE = 75
SEARCH_BASE_4K = 80
MAG_THRESHOLD = 200
# HMI 快照 FITS 在当前处理链路下仅需做上下翻转：
# - 该数据源不再按“180°倒置后再还原”处理；
# - FITS->PNG 的坐标语义会引入上下方向反向，需要在写 PNG 前做一次 flipud 抵消。
APPLY_HMI_UD_FLIP = True


def hmi_norm(image: np.ndarray, threshold: int = 200) -> np.ndarray:
    image = np.nan_to_num(image)
    image = np.clip(image, -threshold, threshold)
    min_val, max_val = -threshold, threshold
    normalized = (image - min_val) / (max_val - min_val) * 255
    return normalized.astype(np.uint8)


def _scaled_search(ny: int) -> int:
    return max(16, int(SEARCH_BASE_4K * ny / 4096))


def _get_centroid(fits_path: str, rough_lon: float, rough_lat: float):
    from scipy.ndimage import center_of_mass

    import astropy.units as u
    import sunpy.map
    from astropy.coordinates import SkyCoord
    from sunpy.coordinates import frames

    m = sunpy.map.Map(fits_path)
    target = SkyCoord(
        lon=rough_lon * u.deg,
        lat=rough_lat * u.deg,
        frame=frames.HeliographicCarrington(obstime=m.date, observer="earth"),
    )
    pix = m.world_to_pixel(target)
    cx, cy = int(pix.x.value), int(pix.y.value)
    ny, nx = m.data.shape
    ss = _scaled_search(ny)
    y0, y1 = max(0, cy - ss), min(ny, cy + ss)
    x0, x1 = max(0, cx - ss), min(nx, cx + ss)
    sub = np.nan_to_num(m.data[y0:y1, x0:x1])
    abs_sub = np.abs(sub)
    mask = abs_sub > 100
    if np.sum(mask) == 0:
        return target
    dy, dx = center_of_mass(abs_sub * mask)
    new_y, new_x = y0 + dy, x0 + dx
    dist = float(np.sqrt((new_x - cx) ** 2 + (new_y - cy) ** 2))
    if dist > 150 * ny / 4096:
        return target
    return m.pixel_to_world(new_x * u.pix, new_y * u.pix).transform_to(
        frames.HeliographicCarrington(obstime=m.date, observer="earth")
    )


def _crop_one_frame(
    fits_path: str,
    carr_lon: float,
    carr_lat: float,
    out_png: str,
) -> bool:
    import astropy.units as u
    import sunpy.map
    from astropy.coordinates import SkyCoord
    from sunpy.coordinates import frames

    try:
        hmi_map = sunpy.map.Map(fits_path, memmap=True)
        ny, nx = hmi_map.data.shape
        if CROP_HALF_SIZE > min(nx, ny) // 2:
            logger.warning("CROP_HALF_SIZE too large for image %s", fits_path)
            return False
        target_coord = SkyCoord(
            lon=carr_lon * u.deg,
            lat=carr_lat * u.deg,
            frame=frames.HeliographicCarrington(obstime=hmi_map.date, observer="earth"),
        )
        pix = hmi_map.world_to_pixel(target_coord)
        x, y = int(pix.x.value), int(pix.y.value)
        if not (
            CROP_HALF_SIZE <= x <= nx - CROP_HALF_SIZE
            and CROP_HALF_SIZE <= y <= ny - CROP_HALF_SIZE
        ):
            return False
        crop = hmi_map.data[
            y - CROP_HALF_SIZE : y + CROP_HALF_SIZE,
            x - CROP_HALF_SIZE : x + CROP_HALF_SIZE,
        ].copy()
        if APPLY_HMI_UD_FLIP:
            crop = np.flipud(crop)
        os.makedirs(os.path.dirname(out_png), exist_ok=True)
        imageio.imwrite(out_png, hmi_norm(crop, MAG_THRESHOLD))
        return True
    except Exception as e:
        logger.warning("crop failed %s: %s", fits_path, e)
        return False


def _parse_utc_dt(s: str) -> Optional[datetime]:
    raw = str(s or "").strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt
    except Exception:
        return None


def _propagate_stonyhurst_lon_rigid(lon_deg: float, *, t0_utc: datetime, t1_utc: datetime) -> float:
    """
    按近似刚体自转将 Stonyhurst 经度推进到目标时刻。
    约定：西经为正（与 parse_swpc_location 一致）。
    """
    dt_days = (t1_utc - t0_utc).total_seconds() / 86400.0
    omega = 360.0 / 27.2753
    lon = float(lon_deg) + omega * dt_days
    while lon > 180:
        lon -= 360
    while lon < -180:
        lon += 360
    return lon


def _time_token_from_fits_path(fits_path: str, idx: int) -> str:
    """从全日面 FITS 文件名提取时间标记（优先 YYYYMMDD_HHMMSS）。"""
    bn = os.path.basename(str(fits_path))
    m = re.search(r"(20\d{6}_\d{6})", bn)
    if m:
        return m.group(1)
    m2 = re.search(r"(20\d{6}\d{6})", bn)
    if m2:
        s = m2.group(1)
        return f"{s[:8]}_{s[8:14]}"
    return f"{idx:03d}"


def _ar_token(region_row: Dict[str, Any]) -> str:
    """活动区号 token（只保留数字）。"""
    rid = str(region_row.get("NOAA Number") or region_row.get("region") or "").strip()
    d = re.sub(r"\D+", "", rid)
    return d or "UNKNOWN"


def track_ar_to_png_sequence(
    region_row: Dict[str, Any],
    fits_paths: List[str],
    out_ar_dir: str,
) -> Tuple[List[str], Optional[str]]:
    import astropy.units as u
    import sunpy.map
    from astropy.coordinates import SkyCoord
    from sunpy.coordinates import frames

    pos = str(region_row.get("Position") or region_row.get("location") or "")
    parsed = parse_swpc_location(pos)
    if not parsed:
        return [], f"无法解析位置: {pos}"
    lat_deg, lon_deg = parsed
    fits_sorted = sorted(fits_paths)
    if len(fits_sorted) < 15:
        return [], f"FITS 数量不足 15（{len(fits_sorted)}）"

    m0 = sunpy.map.Map(fits_sorted[0], memmap=True)
    # latest(solar_regions) 链路：observed_date 常为前一日，按 position_valid_time_utc
    # 对经度做时间差推进，减少“直接用昨日位置裁今日图像”的偏移。
    t0 = _parse_utc_dt(str(region_row.get("position_valid_time_utc") or ""))
    if t0 is not None:
        try:
            t1 = m0.date.to_datetime(timezone=timezone.utc)
            lon_deg = _propagate_stonyhurst_lon_rigid(float(lon_deg), t0_utc=t0, t1_utc=t1)
        except Exception:
            pass
    p_stony = SkyCoord(
        lon=lon_deg * u.deg,
        lat=lat_deg * u.deg,
        frame=frames.HeliographicStonyhurst(obstime=m0.date),
    )
    p_carr = p_stony.transform_to(frames.HeliographicCarrington(obstime=m0.date, observer="earth"))
    carr_lon, carr_lat = float(p_carr.lon.deg), float(p_carr.lat.deg)

    center = _get_centroid(fits_sorted[0], carr_lon, carr_lat)
    final_lon, final_lat = float(center.lon.deg), float(center.lat.deg)

    pngs: List[str] = []
    ar = _ar_token(region_row)
    for i, fp in enumerate(fits_sorted[:15]):
        tstr = _time_token_from_fits_path(fp, i)
        out_png = os.path.join(out_ar_dir, f"AR{ar}_{tstr}.png")
        if _crop_one_frame(fp, final_lon, final_lat, out_png):
            pngs.append(out_png)
    if len(pngs) != 15:
        return pngs, f"仅成功裁剪 {len(pngs)}/15 帧"
    return pngs, None
