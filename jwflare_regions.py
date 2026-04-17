"""重点活动区选取（确定性规则）。"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set


def _parse_int(s: str) -> Optional[int]:
    try:
        return int(str(s).strip())
    except (TypeError, ValueError):
        return None


def _area_score(area_s: str) -> int:
    m = re.search(r"(\d+)", str(area_s or ""))
    return int(m.group(1)) if m else 0


def _spots_score(spots_s: str) -> int:
    m = re.search(r"(\d+)", str(spots_s or ""))
    return int(m.group(1)) if m else 0


def _hale_complexity(hale: str) -> int:
    h = (hale or "").upper()
    score = 0
    if "DELTA" in h or "δ" in h or "D" in h.replace(" ", ""):
        score += 50
    if "GAMMA" in h or "Γ" in h or "G" in h:
        score += 30
    if "BETA" in h or "Β" in h or "B" in h:
        score += 10
    return score


def _flare_today_score(flares_cell: str) -> int:
    s = str(flares_cell or "")
    if not s or s in ("无", "N/A", ""):
        return 0
    if re.search(r"[XMC]\d", s, re.I):
        return 80
    return 0


def _noaa_in_flares(noaa: str, flares: List[Dict[str, Any]]) -> bool:
    for f in flares:
        reg = str(f.get("NOAA Region") or f.get("region") or "").strip()
        match = reg == noaa or (noaa.isdigit() and reg == str(int(noaa)))
        if not match:
            continue
        cls = str(f.get("Class") or "")
        if re.search(r"[XMC]\d", cls, re.I):
            return True
    return False


def select_key_regions(
    active_regions: List[Dict[str, Any]],
    flares: Optional[List[Dict[str, Any]]],
    max_regions: int,
) -> List[str]:
    """
    返回 NOAA 编号字符串列表（去重、有序），数量不超过 max_regions。
    优先级：当日合并耀斑列表命中 C+ > 当日表格耀斑列含 C+ > Hale 复杂度高 > 面积 > 黑子数。
    """
    flares = flares or []
    scored: List[tuple[float, str]] = []
    seen: Set[str] = set()

    for row in active_regions:
        noaa = str(row.get("NOAA Number") or row.get("region") or "").strip()
        if not noaa or noaa in seen:
            continue
        seen.add(noaa)

        fl_cell = str(row.get("Flares") or row.get("Today Flares") or "无")
        hale = str(row.get("Hale Class") or "")
        area_s = str(row.get("Area") or "")
        spots_s = str(row.get("Spots") or "")

        score = 0.0
        if _noaa_in_flares(noaa, flares):
            score += 200
        score += _flare_today_score(fl_cell)
        score += _hale_complexity(hale)
        score += _area_score(area_s) * 0.5
        score += _spots_score(spots_s) * 0.3

        scored.append((score, noaa))

    scored.sort(key=lambda x: (-x[0], x[1]))
    out = [n for _, n in scored[:max_regions]]
    return out
