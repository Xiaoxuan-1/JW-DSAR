"""JSOC 目录浏览：列出并下载 HMI FITS（按日缓存到 full_disk）。"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from typing import List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests

logger = logging.getLogger(__name__)

JSOC_HMI_FITS_BASE = "https://jsoc1.stanford.edu/data/hmi/fits"


def jsoc_day_url(report_date: str) -> str:
    """report_date YYYY-MM-DD -> JSOC URL path YYYY/MM/DD"""
    d = datetime.strptime(report_date[:10], "%Y-%m-%d")
    return f"{JSOC_HMI_FITS_BASE}/{d.year:04d}/{d.month:02d}/{d.day:02d}/"


def list_fits_hrefs(page_url: str, timeout: int = 60) -> List[str]:
    r = requests.get(page_url, timeout=timeout)
    r.raise_for_status()
    text = r.text
    hrefs = re.findall(r'href=["\']([^"\']+\.fits)["\']', text, re.I)
    out: List[str] = []
    for h in hrefs:
        full = urljoin(page_url, h)
        out.append(full)
    # 去重保序
    seen = set()
    uniq: List[str] = []
    for u in out:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


def _parse_fits_time(path: str) -> Tuple[int, str]:
    """返回 (sort_key, display)；无法解析则 (0, basename)。"""
    base = os.path.basename(urlparse(path).path)
    m = re.search(r"(\d{8})_(\d{6})", base)
    if not m:
        return (0, base)
    ds, ts = m.group(1), m.group(2)
    key = int(ds) * 1_000_000 + int(ts)
    return (key, f"{ds}_{ts}")


def pick_fits_urls_hourly(urls: List[str], n: int = 15) -> List[str]:
    """按文件名时间排序，尽量均匀选取 n 个。"""
    if not urls:
        return []
    mag = [u for u in urls if "magnetogram" in u.lower()]
    if len(mag) >= n:
        urls = mag
    decorated = sorted((( _parse_fits_time(u)[0], u) for u in urls), key=lambda x: x[0])
    sorted_urls = [u for _, u in decorated]
    if len(sorted_urls) <= n:
        return sorted_urls
    # 均匀索引
    idxs = [int(round(i * (len(sorted_urls) - 1) / (n - 1))) for i in range(n)]
    seen = set()
    picked: List[str] = []
    for i in idxs:
        if i not in seen:
            seen.add(i)
            picked.append(sorted_urls[i])
    while len(picked) < n:
        for u in sorted_urls:
            if u not in picked:
                picked.append(u)
                if len(picked) >= n:
                    break
    return picked[:n]


def download_if_missing(url: str, dest_dir: str, timeout: int = 120) -> str:
    os.makedirs(dest_dir, exist_ok=True)
    name = os.path.basename(urlparse(url).path) or "file.fits"
    path = os.path.join(dest_dir, name)
    if os.path.isfile(path) and os.path.getsize(path) > 0:
        return path
    logger.info("Downloading %s", url)
    r = requests.get(url, timeout=timeout, stream=True)
    r.raise_for_status()
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        for chunk in r.iter_content(chunk_size=1 << 20):
            if chunk:
                f.write(chunk)
    os.replace(tmp, path)
    return path


def ensure_fits_for_report_day(report_date: str, full_disk_dir: str) -> Tuple[List[str], Optional[str]]:
    """
    返回 (本地 fits 路径列表, 错误信息)。
    """
    try:
        page = jsoc_day_url(report_date)
        hrefs = list_fits_hrefs(page)
        if not hrefs:
            return [], f"JSOC 目录无 .fits 链接: {page}"
        picked = pick_fits_urls_hourly(hrefs, 15)
        local: List[str] = []
        for u in picked:
            try:
                local.append(download_if_missing(u, full_disk_dir))
            except Exception as e:
                logger.warning("download failed %s: %s", u, e)
        if len(local) < 15:
            return local, f"仅下载 {len(local)}/15 个 FITS（部分失败或目录不足）"
        return local, None
    except Exception as e:
        return [], str(e)
