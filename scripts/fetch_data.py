#!/usr/bin/env python3
"""Fetch FCI VCP series from ArgentinaDatos (CAFCI republication) and write data.json.

Never invents numbers. If a source fails, records warnings and leaves fields null/empty.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ART = dt.timezone(dt.timedelta(hours=-3), name="ART")
UA = {"User-Agent": "NauBernues-fima-vs-lecaps/1.0 (+https://github.com/NauBernues/fima-vs-lecaps)"}


def http_json(url: str, timeout: int = 30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def load_config() -> dict:
    return json.loads((ROOT / "config.json").read_text(encoding="utf-8"))


def daterange(start: dt.date, end: dt.date):
    d = start
    while d <= end:
        yield d
        d += dt.timedelta(days=1)


def fetch_day(base: str, cat: str, day: dt.date):
    url = f"{base}/{cat}/{day.year}/{day.month:02d}/{day.day:02d}"
    try:
        data = http_json(url)
        return day.isoformat(), {row["fondo"]: row for row in data}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return day.isoformat(), None
        return day.isoformat(), {"__error__": f"HTTP {e.code}"}
    except Exception as e:
        return day.isoformat(), {"__error__": f"{type(e).__name__}: {e}"}


def build_points(series: dict[str, dict]) -> list[dict]:
    return [{"date": iso, "vcp": float(series[iso]["vcp"])} for iso in sorted(series)]


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "data.json"))
    ap.add_argument("--end", default=None, help="YYYY-MM-DD (default: today ART)")
    args = ap.parse_args()
    cfg = load_config()
    base = cfg["argentinadatos_base"]
    notional = cfg.get("notional_ars", 100_000_000)
    today = dt.datetime.now(ART).date()
    end = dt.date.fromisoformat(args.end) if args.end else today
    start = end - dt.timedelta(days=int(cfg.get("lookback_calendar_days", 320)))

    cats = sorted({f["argentinadatos_categoria"] for f in cfg["funds"]})
    by_cat: dict[str, dict] = {c: {} for c in cats}
    warnings = []
    errors = 0

    for cat in cats:
        days = list(daterange(start, end))
        print(f"Fetching {cat}: {len(days)} days…", file=sys.stderr)
        with ThreadPoolExecutor(max_workers=12) as ex:
            futs = [ex.submit(fetch_day, base, cat, day) for day in days]
            for fut in as_completed(futs):
                iso, payload = fut.result()
                if payload is None:
                    continue
                if "__error__" in payload:
                    errors += 1
                    if errors <= 5:
                        warnings.append(f"{cat} {iso}: {payload['__error__']}")
                    continue
                by_cat[cat][iso] = payload

    series_by_fund: dict[str, dict] = {}
    for f in cfg["funds"]:
        name = f["argentinadatos_nombre"]
        cat = f["argentinadatos_categoria"]
        series_by_fund[f["id"]] = {}
        for iso, rows in by_cat[cat].items():
            row = rows.get(name)
            if row and row.get("vcp") is not None:
                series_by_fund[f["id"]][iso] = row

    points_map = {fid: build_points(s) for fid, s in series_by_fund.items()}
    dates = [pts[-1]["date"] for pts in points_map.values() if pts]
    if not dates:
        print("ERROR: no series data", file=sys.stderr)
        sys.exit(1)
    as_of = max(dates)

    funds_out = {}
    for f in cfg["funds"]:
        fid = f["id"]
        pts = points_map[fid]
        m = metrics_for(pts, as_of) if len(pts) >= 2 else {"error": "serie insuficiente", "n_obs": len(pts)}
        if m.get("n_obs", 0) < 120:
            m["coverage_gap_warning"] = (
                f"Solo {m.get('n_obs')} observaciones entre {m.get('first_date')} y {m.get('last_date')}."
            )
            warnings.append(m["coverage_gap_warning"])
        dollar_daily = None
        if m.get("last_step"):
            dollar_daily = notional * m["last_step"]["return"] / m["last_step"]["calendar_days"]
        exp = f.get("lecaps_exposure") or {}
        funds_out[fid] = {
            **{k: f[k] for k in f},
            # flat aliases kept for backwards compatibility with older consumers
            "lecaps_pct_approx": exp.get("pct"),
            "composition_as_of": exp.get("as_of"),
            "composition_note": exp.get("note"),
            "meets_70pct_lecaps": (exp.get("pct") or 0) > 70,
            "metrics": m,
            "dollar_yield_per_100m_per_calendar_day_last_step": dollar_daily,
            "series": pts,
        }

    base_id = cfg.get("base_fund", "premium")
    prem_m = funds_out[base_id]["metrics"]
    spreads = {}
    for fid in [f["id"] for f in cfg["funds"] if f["id"] != base_id]:
        if fid not in funds_out:
            continue
        om = funds_out[fid]["metrics"]
        block = {}
        for key in ["mtd", "ytd", "rolling_30d"]:
            pr, or_ = prem_m.get(key), om.get(key)
            if pr and or_ and pr.get("return") is not None and or_.get("return") is not None:
                diff = or_["return"] - pr["return"]
                block[key] = {
                    "compare_return": or_["return"],
                    "premium_return": pr["return"],
                    "spread": diff,
                    "spread_bps": diff * 10000,
                    "compare_window": f"{or_['start']}→{or_['end']}",
                    "premium_window": f"{pr['start']}→{pr['end']}",
                    "dollar_spread_100m": notional * diff,
                }
            else:
                block[key] = None
        ps, os_ = prem_m.get("last_step"), om.get("last_step")
        if ps and os_:
            if ps["date"] == os_["date"] and ps["prev_date"] == os_["prev_date"]:
                diff = os_["return"] - ps["return"]
                block["last_aligned_step"] = {
                    "spread": diff,
                    "spread_bps": diff * 10000,
                    "window": f"{os_['prev_date']}→{os_['date']}",
                    "dollar_spread_100m": notional * diff,
                }
            else:
                block["last_aligned_step"] = {
                    "missing": f"ventanas no alinean (premium {ps['prev_date']}→{ps['date']} vs {os_['prev_date']}→{os_['date']})"
                }
        spreads[fid] = block

    # Probe CAFCI once (document status, do not invent)
    cafci_status = "unknown"
    try:
        http_json(f"{cfg['cafci_api_base']}/fondo/{cfg['funds'][0]['cafci_fondo_id']}")
        cafci_status = "ok"
    except urllib.error.HTTPError as e:
        cafci_status = f"HTTP {e.code}"
        warnings.append(f"CAFCI API {cafci_status} — usando solo ArgentinaDatos.")
    except Exception as e:
        cafci_status = f"{type(e).__name__}: {e}"
        warnings.append(f"CAFCI API error: {cafci_status}")

    payload = {
        "generated_at_art": dt.datetime.now(ART).isoformat(timespec="seconds"),
        "as_of": as_of,
        "timezone": "America/Buenos_Aires",
        "notional_ars": notional,
        "methodology": {
            "returns": "Variación de VCP entre fechas publicadas. Ya neto de honorarios del fondo.",
            "calendar_days": "TNA/TEA = (1+r)^(365/días_calendario) - 1.",
            "fees": "Cuotaparte CAFCI ya descuenta fees del fondo. No se aplican fees de asesor ni costos Byma.",
            "primary_comparison": next((c["description"] for c in cfg.get("comparisons", []) if c["id"] == "primary"), None),
            "composition_gate": cfg.get("notes", {}).get("composition"),
            "data_source": "ArgentinaDatos (republica CAFCI). Estado CAFCI directo: " + cafci_status,
            "lecap_tickers": "Sin fuente pública Byma sin API key.",
        },
        "sources": [
            {
                "name": "ArgentinaDatos FCI",
                "url": "https://api.argentinadatos.com/v1/finanzas/fci/{categoria}/{YYYY}/{MM}/{DD}",
                "role": "serie diaria VCP",
            },
            {
                "name": "CAFCI API",
                "url": "https://api.cafci.org.ar/",
                "role": f"estado: {cafci_status}",
            },
            {
                "name": "CAFCI Planilla Diaria",
                "url": "https://api.pub.cafci.org.ar/pb_get",
                "role": "verificación de IDs fondo/clase y plazo de liquidación",
            },
            {
                "name": "CNV carteras",
                "url": "https://www.cnv.gov.ar/SitioWeb/FondosComunesInversion/",
                "role": "composición / % Lecaps",
            },
        ],
        "base_fund": base_id,
        "comparisons": cfg.get("comparisons", []),
        "benchmarks": cfg.get("benchmarks", {}),
        "funds": funds_out,
        "spreads_vs_premium": spreads,
        "lecap_tickers": {
            "status": "sin_fuente_publica",
            "configured": cfg.get("lecap_tickers", []),
            "note": "Tickers de la planilla de referencia. Falta Byma público. No se inventan filas.",
            "rows": [],
        },
        "warnings": warnings,
    }

    out = Path(args.out)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {out} as_of={as_of} warnings={len(warnings)}", file=sys.stderr)


if __name__ == "__main__":
    main()
