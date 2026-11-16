#!/usr/bin/env python3
"""Arma data.json con el histórico de VCP de ArgentinaDatos.

El listado diario por categoría (`/fci/{categoria}/{YYYY}/{MM}/{DD}`) en muchos
días trae solo un puñado de fondos. Leer eso y saltear el resto dejaba huecos
falsos (p. ej. renta fija del 28/10 al 12/11/2026). La serie completa está en
`/fci/fondos/{slug}/historico`: 3 llamadas en vez de ~640, y con los días que
el listado diario no incluye.

No inventa números. Si el gate no pasa, no pisa el data.json publicado.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
import unicodedata
import re
import urllib.error
import urllib.request
from pathlib import Path

from fci_quality import (
    MAX_ABS_RETURN_PER_CALENDAR_DAY,
    MIN_COVERAGE,
    evaluate_gate,
    fmt_dmy,
    metrics_for,
    spreads_vs_base,
)

ROOT = Path(__file__).resolve().parents[1]
ART = dt.timezone(dt.timedelta(hours=-3), name="ART")
UA = {"User-Agent": "NauBernues-fima-vs-lecaps/1.0 (+https://github.com/NauBernues/fima-vs-lecaps)"}
# 1 intento + 3 reintentos. Solo errores transitorios.
MAX_ATTEMPTS = 4
TRANSIENT_HTTP = {408, 425, 429, 500, 502, 503, 504}
STATS = {"http_calls": 0}


def slugify(name: str) -> str:
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def _urlopen_json(url: str, timeout: int):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def http_json(url: str, timeout: int = 60, attempts: int = MAX_ATTEMPTS, sleep=time.sleep, opener=None):
    """GET JSON. Reintenta como máximo 3 veces, y solo si el error es transitorio.

    404/401/403 no se reintentan: son respuesta definitiva de la fuente.
    """
    opener = opener or _urlopen_json
    last_exc: Exception | None = None
    for i in range(attempts):
        STATS["http_calls"] += 1
        try:
            return opener(url, timeout)
        except urllib.error.HTTPError as e:
            last_exc = e
            if e.code not in TRANSIENT_HTTP or i == attempts - 1:
                raise
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ConnectionError) as e:
            last_exc = e
            if i == attempts - 1:
                raise
        sleep(min(0.4 * (2**i), 4))
    raise last_exc if last_exc else RuntimeError(url)


def load_config() -> dict:
    return json.loads((ROOT / "config.json").read_text(encoding="utf-8"))


def points_from_historico(payload: dict) -> list[dict]:
    by_date: dict[str, float] = {}
    for row in payload.get("historico") or []:
        fecha = str(row.get("fecha") or "")[:10]
        vcp = row.get("valorCuotaparte")
        if len(fecha) != 10 or vcp is None:
            continue
        try:
            dt.date.fromisoformat(fecha)
            v = float(vcp)
        except (TypeError, ValueError):
            continue
        by_date[fecha] = v
    return [{"date": d, "vcp": by_date[d]} for d in sorted(by_date)]


def fetch_fund_historico(base: str, fund: dict) -> tuple[list[dict], str | None]:
    """Devuelve (puntos, error). `error` es None si salió bien y el nombre coincide."""
    slug = fund.get("argentinadatos_slug") or slugify(fund["argentinadatos_nombre"])
    url = f"{base}/fondos/{slug}/historico"
    try:
        payload = http_json(url)
    except urllib.error.HTTPError as e:
        return [], f"{fund['id']}: histórico {slug} HTTP {e.code}"
    except Exception as e:
        return [], f"{fund['id']}: histórico {slug} {type(e).__name__}: {e}"
    if not isinstance(payload, dict):
        return [], f"{fund['id']}: histórico {slug} no es un objeto"
    got = payload.get("nombre")
    expected = fund["argentinadatos_nombre"]
    if got != expected:
        return [], f"{fund['id']}: slug {slug} devolvió {got!r}, se esperaba {expected!r}"
    return points_from_historico(payload), None


def trim_series(series: dict[str, list[dict]], lookback_calendar_days: int, end: dt.date) -> dict[str, list[dict]]:
    """Recorta al lookback, pero deja el último punto de cada fondo (aunque no coincidan)."""
    capped = {}
    lasts = []
    for fid, pts in series.items():
        kept = [p for p in pts if p["date"] <= end.isoformat()]
        capped[fid] = kept
        if kept:
            lasts.append(dt.date.fromisoformat(kept[-1]["date"]))
    if not lasts:
        return capped
    start = min(lasts) - dt.timedelta(days=lookback_calendar_days)
    start_iso = start.isoformat()
    return {fid: [p for p in pts if p["date"] >= start_iso] for fid, pts in capped.items()}


def fetch_all(cfg: dict, end: dt.date) -> tuple[dict[str, list[dict]], list[str], str]:
    base = cfg["argentinadatos_base"].rstrip("/")
    # El base histórico cuelga de /v1/finanzas/fci, igual que el listado diario.
    warnings: list[str] = []
    series: dict[str, list[dict]] = {}
    for fund in cfg["funds"]:
        pts, err = fetch_fund_historico(base, fund)
        if err:
            warnings.append(err)
            series[fund["id"]] = []
            continue
        series[fund["id"]] = pts
    series = trim_series(series, int(cfg.get("lookback_calendar_days", 320)), end)

    cafci_status = "unknown"
    try:
        http_json(f"{cfg['cafci_api_base'].rstrip('/')}/fondo/{cfg['funds'][0]['cafci_fondo_id']}")
        cafci_status = "ok"
    except urllib.error.HTTPError as e:
        cafci_status = f"HTTP {e.code}"
        warnings.append(f"CAFCI API {cafci_status} — usando solo ArgentinaDatos.")
    except Exception as e:
        cafci_status = f"{type(e).__name__}: {e}"
        warnings.append(f"CAFCI API error: {cafci_status}")
    return series, warnings, cafci_status


def _emit(level: str, msg: str) -> None:
    clean = " ".join(msg.split())
    print(f"::{level}::{clean}", file=sys.stderr)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary and level in {"error", "warning"}:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(f"- **{level}:** {clean}\n")


def _print_report(series, gate, spreads, cafci_status: str) -> None:
    print(f"api_calls={STATS['http_calls']} cafci={cafci_status}", file=sys.stderr)
    for fid, cov in gate["coverage"].items():
        ratio = "s/d" if cov["ratio"] is None else f"{cov['ratio']:.1%}"
        print(
            f"coverage {fid}: {cov['present']}/{cov['business_days']} ({ratio}) "
            f"{cov['start']}→{cov['end']} missing={len(cov['missing'])}",
            file=sys.stderr,
        )
    for fid, block in spreads.items():
        for key in ("mtd", "rolling_30d", "ytd", "last_aligned_step"):
            b = block.get(key)
            if not b:
                print(f"spread {fid} {key}: sin ventana común", file=sys.stderr)
                continue
            print(
                f"spread {fid} {key}: {b['window']} "
                f"spread={b['spread']:.6f} bps={b['spread_bps']:.1f}",
                file=sys.stderr,
            )
    for w in gate["warnings"]:
        print(f"warn {w}", file=sys.stderr)
    for r in gate["reasons"]:
        print(f"gate {r}", file=sys.stderr)


def build_payload(cfg: dict, series: dict[str, list[dict]], gate: dict, fetch_warnings: list[str], cafci_status: str) -> dict:
    notional = cfg.get("notional_ars", 100_000_000)
    as_of = gate["as_of"]
    base_id = cfg.get("base_fund", "premium")
    # Métricas por fondo contra su propio último día. El spread, aparte, usa fechas comunes.
    funds_out = {}
    for f in cfg["funds"]:
        fid = f["id"]
        pts = series.get(fid) or []
        fund_as_of = pts[-1]["date"] if pts else as_of
        m = metrics_for(pts, fund_as_of) if fund_as_of and len(pts) >= 2 else {"error": "serie insuficiente", "n_obs": len(pts)}
        dollar_daily = None
        if m.get("last_step"):
            dollar_daily = notional * m["last_step"]["return"] / m["last_step"]["calendar_days"]
        exp = f.get("lecaps_exposure") or {}
        cov = gate["coverage"].get(fid)
        if cov:
            m = {
                **m,
                "coverage": {
                    "business_days": cov["business_days"],
                    "present": cov["present"],
                    "ratio": cov["ratio"],
                    "missing_business_days": cov["missing"],
                },
            }
        funds_out[fid] = {
            **{k: f[k] for k in f},
            "lecaps_pct_approx": exp.get("pct"),
            "composition_as_of": exp.get("as_of"),
            "composition_note": exp.get("note"),
            "meets_70pct_lecaps": (exp.get("pct") or 0) > 70,
            "metrics": m,
            "dollar_yield_per_100m_per_calendar_day_last_step": dollar_daily,
            "series": pts,
        }

    others = {fid: series[fid] for fid in series if fid != base_id and series.get(fid)}
    as_of_d = dt.date.fromisoformat(as_of) if as_of else None
    if as_of_d is None:
        # Para el reporte de una corrida rechazada: ventana común hasta el menor último día.
        ends = [pts[-1]["date"] for pts in series.values() if pts]
        as_of_d = dt.date.fromisoformat(min(ends)) if ends else dt.date.today()
    spreads = spreads_vs_base(series.get(base_id) or [], others, as_of_d, notional) if series.get(base_id) else {}

    warnings = list(fetch_warnings)
    warnings.extend(gate["warnings"])

    payload = {
        "generated_at_art": dt.datetime.now(ART).isoformat(timespec="seconds"),
        "as_of": as_of,
        "timezone": "America/Buenos_Aires",
        "notional_ars": notional,
        "methodology": {
            "returns": "Variación de VCP entre fechas publicadas. Ya neto de honorarios del fondo.",
            "calendar_days": "TNA/TEA = (1+r)^(365/días_calendario) - 1.",
            "spreads": "Solo fechas presentes en las dos series. La ventana del spread es la misma para las dos.",
            "fees": "Cuotaparte CAFCI ya descuenta fees del fondo. No se aplican fees de asesor ni costos Byma.",
            "primary_comparison": next((c["description"] for c in cfg.get("comparisons", []) if c["id"] == "primary"), None),
            "composition_gate": cfg.get("notes", {}).get("composition"),
            "data_source": (
                "ArgentinaDatos histórico por fondo (/fondos/{slug}/historico). "
                "El listado diario por categoría queda afuera: muchos días traen solo un subconjunto de fondos. "
                "Estado CAFCI directo: " + cafci_status
            ),
            "business_days": (
                "Días hábiles = lunes a viernes menos feriados nacionales (Ley 27.399), "
                "puentes turísticos 2026 (Res. 164/2025) y cierres BYMA del 6, 9 y 10/11/2026."
            ),
            "lecap_tickers": "Sin fuente pública Byma sin API key.",
        },
        "quality": {
            "gate": "pass" if gate["ok"] else "fail",
            "min_coverage": gate["min_coverage"],
            "max_abs_return_per_calendar_day": MAX_ABS_RETURN_PER_CALENDAR_DAY,
            "acceptable_as_of": gate["acceptable_as_of"],
            "coverage": gate["coverage"],
            "reasons": gate["reasons"],
        },
        "sources": [
            {
                "name": "ArgentinaDatos FCI histórico",
                "url": "https://api.argentinadatos.com/v1/finanzas/fci/fondos/{slug}/historico",
                "role": "serie diaria de VCP por fondo",
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
        "api_calls": STATS["http_calls"],
    }
    return payload


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "data.json"))
    ap.add_argument("--end", default=None, help="YYYY-MM-DD (default: hoy ART)")
    ap.add_argument("--dry-run", action="store_true", help="No escribe el archivo")
    args = ap.parse_args(argv)

    STATS["http_calls"] = 0
    cfg = load_config()
    today = dt.datetime.now(ART).date()
    end = dt.date.fromisoformat(args.end) if args.end else today

    series, fetch_warnings, cafci_status = fetch_all(cfg, end)
    gate = evaluate_gate(
        series,
        end=end,
        lookback_calendar_days=int(cfg.get("lookback_calendar_days", 320)),
        min_coverage=MIN_COVERAGE,
    )
    payload = build_payload(cfg, series, gate, fetch_warnings, cafci_status)
    _print_report(series, gate, payload["spreads_vs_premium"], cafci_status)

    out = Path(args.out)
    if not gate["ok"]:
        for reason in gate["reasons"]:
            _emit("error", reason)
        for w in gate["warnings"]:
            _emit("warning", w)
        label = fmt_dmy(gate["as_of"]) if gate["as_of"] else "s/d"
        _emit(
            "error",
            f"No se publica data.json (gate). Queda el archivo anterior. as_of candidato: {label}.",
        )
        print(f"REFUSED write {out}", file=sys.stderr)
        return 2

    for w in gate["warnings"]:
        _emit("warning", w)
    if args.dry_run:
        print(f"dry-run ok as_of={payload['as_of']} (no escribo {out})", file=sys.stderr)
        return 0

    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {out} datos al {fmt_dmy(payload['as_of'])} api_calls={STATS['http_calls']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
