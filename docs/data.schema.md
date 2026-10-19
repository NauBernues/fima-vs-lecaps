# data.json (schema resumido)

```json
{
  "generated_at_art": "ISO-8601 con offset -03:00",
  "as_of": "YYYY-MM-DD",
  "timezone": "America/Buenos_Aires",
  "notional_ars": 100000000,
  "methodology": { "...": "..." },
  "sources": [{ "name": "", "url": "", "role": "" }],
  "base_fund": "premium",
  "comparisons": [{ "id": "primary", "label": "", "base": "premium", "compare": "balanz_lecaps", "description": "" }],
  "benchmarks": { "pure_lecaps_portfolio": { "status": "pendiente", "composition": [], "rules": null } },
  "funds": {
    "premium": {
      "name": "",
      "liquidity": "T+0",
      "cafci_fondo_id": 445,
      "cafci_clase_id": 575,
      "lecaps_exposure": { "pct": null, "as_of": null, "source": null, "note": "" },
      "lecaps_pct_approx": null,
      "meets_70pct_lecaps": false,
      "metrics": {
        "last_step": { "date": "", "prev_date": "", "calendar_days": 1, "return": 0.0 },
        "last_tna": 0.0,
        "mtd": { "start": "", "end": "", "calendar_days": 0, "return": 0.0, "tna": 0.0 },
        "ytd": {},
        "rolling_30d": {},
        "negative_obs": 0,
        "max_drawdown": { "max_drawdown": 0.0, "peak_date": "", "trough_date": "" }
      },
      "series": [{ "date": "YYYY-MM-DD", "vcp": 0.0 }]
    }
  },
  "spreads_vs_premium": {
    "balanz_lecaps": {
      "mtd": { "spread": 0.0, "spread_bps": 0.0, "dollar_spread_100m": 0.0 }
    }
  },
  "lecap_tickers": { "status": "sin_fuente_publica", "configured": [], "rows": [] },
  "warnings": []
}
```

Campos faltantes → `null` / omitidos / `warnings[]`. Nunca rellenar con estimaciones.
