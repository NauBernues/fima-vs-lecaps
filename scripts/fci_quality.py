"""Calendario hábil, cobertura, spreads en fechas comunes y gate de publicación.

No habla con la red. Lo usa scripts/fetch_data.py y los tests.
"""
from __future__ import annotations

import datetime as dt
import math
from functools import lru_cache

# Variación absoluta máxima entre dos VCP consecutivos, por día calendario.
# En 2026 estas series se movieron <0,7% por día. 2% deja margen y corta saltos basura.
MAX_ABS_RETURN_PER_CALENDAR_DAY = 0.02
MIN_COVERAGE = 0.95

# Días extra sin cuotaparte, además de los feriados de la Ley 27.399.
# 2026: puentes turísticos (Resolución 164/2025), visita papal (Decreto 1103/26,
# calendario BYMA: 6, 9 y 10/11 sin negociación) y 31/12 sin negociación (BYMA).
# El 10/07 es día no laborable: ninguno de los tres fondos publicó VCP.
EXTRA_CLOSURES: dict[int, frozenset[dt.date]] = {
    2026: frozenset(
        {
            dt.date(2026, 3, 23),
            dt.date(2026, 7, 10),
            dt.date(2026, 11, 6),
            dt.date(2026, 11, 9),
            dt.date(2026, 11, 10),
            dt.date(2026, 12, 7),
        }
    )
}


def easter_sunday(year: int) -> dt.date:
    """Domingo de Pascua (algoritmo gregoriano anónimo)."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return dt.date(year, month, day)


def _observe_monday(d: dt.date) -> dt.date:
    """Feriado trasladable: mar/mié → lunes anterior; jue/vie → lunes siguiente; sáb/dom → lunes siguiente."""
    wd = d.weekday()
    if wd == 0:
        return d
    if wd in (1, 2):
        return d - dt.timedelta(days=wd)
    if wd in (3, 4):
        return d + dt.timedelta(days=7 - wd)
    if wd == 5:
        return d + dt.timedelta(days=2)
    return d + dt.timedelta(days=1)


@lru_cache(maxsize=16)
def closures_for(year: int) -> frozenset[dt.date]:
    """Días sin cuotaparte esperable: feriados nacionales + cierres extra del año."""
    easter = easter_sunday(year)
    fixed = [
        dt.date(year, 1, 1),
        dt.date(year, 3, 24),
        dt.date(year, 4, 2),
        dt.date(year, 5, 1),
        dt.date(year, 5, 25),
        dt.date(year, 6, 20),
        dt.date(year, 7, 9),
        dt.date(year, 12, 8),
        dt.date(year, 12, 25),
        # BYMA no negocia el 31/12 (calendario 2026; el 31/12/2025 tampoco tiene archivo).
        dt.date(year, 12, 31),
    ]
    carnival_mon = easter - dt.timedelta(days=48)
    carnival_tue = easter - dt.timedelta(days=47)
    good_friday = easter - dt.timedelta(days=2)
    trasladables = [
        _observe_monday(dt.date(year, 6, 17)),
        _observe_monday(dt.date(year, 8, 17)),
        _observe_monday(dt.date(year, 10, 12)),
        _observe_monday(dt.date(year, 11, 20)),
    ]
    days = set(fixed)
    days.update([carnival_mon, carnival_tue, good_friday])
    days.update(trasladables)
    days.update(EXTRA_CLOSURES.get(year, ()))
    return frozenset(days)


def is_business_day(d: dt.date) -> bool:
    if d.weekday() >= 5:
        return False
    return d not in closures_for(d.year)


def business_days(start: dt.date, end: dt.date) -> list[dt.date]:
    if end < start:
        return []
    out = []
    d = start
    while d <= end:
        if is_business_day(d):
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def previous_business_day(d: dt.date) -> dt.date:
    d = d - dt.timedelta(days=1)
    while not is_business_day(d):
        d -= dt.timedelta(days=1)
    return d


def acceptable_as_of(end: dt.date) -> set[dt.date]:
    """Días que podemos publicar si la corrida corre en `end`.

    El hábil anterior ya debería estar. Si `end` es hábil (corrida de la tarde),
    también sirve la cuotaparte de esa sesión.
    """
    ok = {previous_business_day(end)}
    if is_business_day(end):
        ok.add(end)
    return ok


def fmt_dmy(iso: str) -> str:
    y, m, d = iso.split("-")
    return f"{d}/{m}/{y}"


def index_points(points: list[dict]) -> dict[str, float]:
    return {p["date"]: float(p["vcp"]) for p in points}


def coverage_of(points: list[dict], start: dt.date, end: dt.date) -> dict:
    have = {p["date"] for p in points}
    days = business_days(start, end)
    missing = [d.isoformat() for d in days if d.isoformat() not in have]
    n = len(days)
    present = n - len(missing)
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "business_days": n,
        "present": present,
        "ratio": (present / n) if n else None,
        "missing": missing,
    }


def range_violations(points: list[dict], limit: float = MAX_ABS_RETURN_PER_CALENDAR_DAY) -> list[str]:
    """VCP no positivo, no finito, o salto mayor a `limit` por día calendario."""
    bad = []
    cleaned = []
    for p in points:
        v = p.get("vcp")
        try:
            v = float(v)
        except (TypeError, ValueError):
            bad.append(f"{p.get('date')}: VCP no numérico ({p.get('vcp')!r})")
            continue
        if not math.isfinite(v) or v <= 0:
            bad.append(f"{p.get('date')}: VCP fuera de rango ({v})")
            continue
        cleaned.append((p["date"], v))
    for (d0, v0), (d1, v1) in zip(cleaned, cleaned[1:]):
        days = (dt.date.fromisoformat(d1) - dt.date.fromisoformat(d0)).days
        if days <= 0:
            bad.append(f"{d0}→{d1}: fechas no crecen")
            continue
        r = v1 / v0 - 1.0
        per_day = abs(r) / days
        if per_day > limit:
            bad.append(
                f"{d0}→{d1}: retorno {r:.2%} en {days} días "
                f"({per_day:.2%}/día, límite {limit:.0%}/día)"
            )
    return bad


def _window_for(last: dt.date, lookback_calendar_days: int) -> tuple[dt.date, dt.date]:
    return last - dt.timedelta(days=lookback_calendar_days), last


def evaluate_gate(
    series: dict[str, list[dict]],
    end: dt.date,
    lookback_calendar_days: int,
    min_coverage: float = MIN_COVERAGE,
) -> dict:
    """Decide si se puede pisar data.json.

    Rechaza si los fondos no terminan el mismo día hábil esperable, si la
    cobertura de esa ventana está bajo `min_coverage`, o si hay un VCP fuera de rango.
    Igual arma `warnings` con los hábiles faltantes.
    """
    reasons: list[str] = []
    warnings: list[str] = []
    coverage: dict[str, dict] = {}
    lasts: dict[str, str] = {}

    for fid, pts in series.items():
        if len(pts) < 2:
            reasons.append(f"{fid}: serie insuficiente ({len(pts)} puntos)")
            continue
        lasts[fid] = pts[-1]["date"]
        last = dt.date.fromisoformat(pts[-1]["date"])
        start, _ = _window_for(last, lookback_calendar_days)
        cov = coverage_of(pts, start, last)
        coverage[fid] = cov
        if cov["missing"]:
            head = cov["missing"][:25]
            shown = ", ".join(fmt_dmy(d) for d in head)
            if len(cov["missing"]) > len(head):
                shown += f" … y {len(cov['missing']) - len(head)} más"
            n_miss = len(cov["missing"])
            unidad = "día hábil" if n_miss == 1 else "días hábiles"
            verbo = "falta" if n_miss == 1 else "faltan"
            warnings.append(
                f"{fid}: {verbo} {n_miss} {unidad} "
                f"entre {fmt_dmy(cov['start'])} y {fmt_dmy(cov['end'])} "
                f"(cobertura {cov['ratio']:.1%}): {shown}"
            )
        if cov["ratio"] is None or cov["ratio"] < min_coverage:
            ratio = "s/d" if cov["ratio"] is None else f"{cov['ratio']:.1%}"
            reasons.append(
                f"{fid}: cobertura {ratio} < {min_coverage:.0%} "
                f"({cov['present']}/{cov['business_days']} días hábiles)"
            )
        window_pts = [p for p in pts if start.isoformat() <= p["date"] <= last.isoformat()]
        for msg in range_violations(window_pts):
            reasons.append(f"{fid}: {msg}")

    as_of = None
    unique = set(lasts.values())
    acceptable = acceptable_as_of(end)
    acceptable_txt = ", ".join(sorted(fmt_dmy(d.isoformat()) for d in acceptable))
    if len(series) < 2:
        reasons.append("faltan fondos para comparar")
    if len(unique) != 1:
        detail = ", ".join(f"{fid} al {fmt_dmy(iso)}" for fid, iso in sorted(lasts.items()))
        reasons.append(
            f"series desalineadas (no cierran el mismo día): {detail}. "
            f"Se espera uno de: {acceptable_txt}"
        )
    elif unique:
        as_of = next(iter(unique))
        if dt.date.fromisoformat(as_of) not in acceptable:
            reasons.append(
                f"as_of {fmt_dmy(as_of)} no es un día hábil esperable para una corrida al "
                f"{fmt_dmy(end.isoformat())} (válidos: {acceptable_txt})"
            )

    # uniques reasons, keep order
    deduped = list(dict.fromkeys(reasons))
    return {
        "ok": not deduped,
        "as_of": as_of,
        "reasons": deduped,
        "warnings": warnings,
        "coverage": coverage,
        "acceptable_as_of": sorted(d.isoformat() for d in acceptable),
        "min_coverage": min_coverage,
    }


def daily_returns(points: list[dict]) -> list[dict]:
    out = []
    for i in range(1, len(points)):
        d0 = dt.date.fromisoformat(points[i - 1]["date"])
        d1 = dt.date.fromisoformat(points[i]["date"])
        days = (d1 - d0).days
        if days <= 0:
            continue
        r = points[i]["vcp"] / points[i - 1]["vcp"] - 1.0
        out.append(
            {
                "date": points[i]["date"],
                "prev_date": points[i - 1]["date"],
                "calendar_days": days,
                "return": r,
            }
        )
    return out


def period_return(points: list[dict], start_date: dt.date, end_date: dt.date):
    """Retorno de una sola serie, anclas = último punto propio en o antes de cada tope."""
    pts = [p for p in points if dt.date.fromisoformat(p["date"]) <= end_date]
    if not pts:
        return None
    end_p = pts[-1]
    start_pts = [p for p in points if dt.date.fromisoformat(p["date"]) <= start_date]
    start_p = start_pts[-1] if start_pts else points[0]
    if start_p["date"] == end_p["date"]:
        return None
    days = (dt.date.fromisoformat(end_p["date"]) - dt.date.fromisoformat(start_p["date"])).days
    if days <= 0:
        return None
    r = end_p["vcp"] / start_p["vcp"] - 1.0
    tna = (1 + r) ** (365 / days) - 1
    return {
        "start": start_p["date"],
        "end": end_p["date"],
        "calendar_days": days,
        "return": r,
        "tna": tna,
        "tea": tna,
    }


def max_drawdown(points: list[dict]):
    peak = points[0]["vcp"]
    peak_date = points[0]["date"]
    max_dd = 0.0
    best = {"max_drawdown": 0.0, "peak_date": peak_date, "trough_date": peak_date}
    for p in points:
        if p["vcp"] > peak:
            peak = p["vcp"]
            peak_date = p["date"]
        dd = p["vcp"] / peak - 1.0
        if dd < max_dd:
            max_dd = dd
            best = {"max_drawdown": max_dd, "peak_date": peak_date, "trough_date": p["date"]}
    return best


def metrics_for(points: list[dict], as_of: str):
    if len(points) < 2:
        return {"error": "serie insuficiente", "n_obs": len(points)}
    rets = daily_returns(points)
    last = points[-1]
    last_ret = rets[-1] if rets else None
    last_tna = None
    if last_ret:
        last_tna = (1 + last_ret["return"]) ** (365 / last_ret["calendar_days"]) - 1
    as_of_d = dt.date.fromisoformat(as_of)
    month_start = as_of_d.replace(day=1)
    ytd_start = dt.date(as_of_d.year, 1, 1)
    d30 = as_of_d - dt.timedelta(days=30)
    return {
        "n_obs": len(points),
        "first_date": points[0]["date"],
        "last_date": last["date"],
        "last_vcp": last["vcp"],
        "last_step": last_ret,
        "last_tna": last_tna,
        "last_tea": last_tna,
        "mtd": period_return(points, month_start - dt.timedelta(days=1), as_of_d),
        "ytd": period_return(points, ytd_start - dt.timedelta(days=1), as_of_d),
        "rolling_30d": period_return(points, d30, as_of_d),
        "negative_obs": sum(1 for r in rets if r["return"] < 0),
        "negative_obs_note": "Observaciones con retorno < 0 entre fechas con VCP publicado.",
        "max_drawdown": max_drawdown(points),
    }


def _latest_common(indexes: list[dict[str, float]], bound_iso: str) -> str | None:
    common: set[str] | None = None
    for idx in indexes:
        dates = {d for d in idx if d <= bound_iso}
        common = dates if common is None else (common & dates)
    if not common:
        return None
    return max(common)


def _return_exact(idx: dict[str, float], start: str, end: str) -> dict | None:
    if start not in idx or end not in idx or start >= end:
        return None
    days = (dt.date.fromisoformat(end) - dt.date.fromisoformat(start)).days
    if days <= 0:
        return None
    r = idx[end] / idx[start] - 1.0
    tna = (1 + r) ** (365 / days) - 1
    return {
        "start": start,
        "end": end,
        "calendar_days": days,
        "return": r,
        "tna": tna,
        "tea": tna,
    }


def aligned_window(base_idx: dict[str, float], other_idx: dict[str, float], start_bound: str, end_bound: str):
    """Última fecha común ≤ cada tope. Las dos series usan esas mismas fechas."""
    indexes = [base_idx, other_idx]
    end = _latest_common(indexes, end_bound)
    start = _latest_common(indexes, start_bound)
    if start is None or end is None or start >= end:
        return None
    return start, end


def _spread_block(base_idx, other_idx, start: str, end: str, notional: float) -> dict | None:
    base = _return_exact(base_idx, start, end)
    other = _return_exact(other_idx, start, end)
    if not base or not other:
        return None
    diff = other["return"] - base["return"]
    window = f"{start}→{end}"
    return {
        "compare_return": other["return"],
        "premium_return": base["return"],
        "spread": diff,
        "spread_bps": diff * 10000,
        "compare_window": window,
        "premium_window": window,
        "window": window,
        "windows_equal": True,
        "calendar_days": base["calendar_days"],
        "dollar_spread_100m": notional * diff,
    }


def spreads_vs_base(
    base_points: list[dict],
    others: dict[str, list[dict]],
    as_of: dt.date,
    notional: float,
) -> dict:
    """Spreads MTD / 30d / YTD / último paso, solo sobre fechas presentes en las dos series."""
    base_idx = index_points(base_points)
    as_of_iso = as_of.isoformat()
    month_anchor = (as_of.replace(day=1) - dt.timedelta(days=1)).isoformat()
    ytd_anchor = (dt.date(as_of.year, 1, 1) - dt.timedelta(days=1)).isoformat()
    d30_anchor = (as_of - dt.timedelta(days=30)).isoformat()
    bounds = {
        "mtd": month_anchor,
        "ytd": ytd_anchor,
        "rolling_30d": d30_anchor,
    }
    out = {}
    for fid, pts in others.items():
        other_idx = index_points(pts)
        block: dict = {}
        for key, start_bound in bounds.items():
            window = aligned_window(base_idx, other_idx, start_bound, as_of_iso)
            if window is None:
                block[key] = None
                continue
            block[key] = _spread_block(base_idx, other_idx, window[0], window[1], notional)
        common = sorted(d for d in (set(base_idx) & set(other_idx)) if d <= as_of_iso)
        if len(common) >= 2:
            block["last_aligned_step"] = _spread_block(
                base_idx, other_idx, common[-2], common[-1], notional
            )
        else:
            block["last_aligned_step"] = None
        out[fid] = block
    return out
