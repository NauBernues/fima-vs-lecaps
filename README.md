# Fima Premium vs Ahorro Plus / Lecaps

Dashboard estático (HTML + `data.json`) que compara el rendimiento diario de:

1. **Primario:** Fima Premium Clase A (money market, **T+0**) vs Fima Ahorro Plus Clase A (**T+1**), mismo gerente Galicia.
2. **Secundario:** Balanz Lecaps Clase A (categoría CAFCI Renta Fija LECAP), como proxy más cercano a Lecaps con dato público.
3. **Lecaps ticker-a-ticker** (planilla): *sin fuente pública Byma* → filas vacías, no se inventan tasas.

## Ver online

- Repo: https://github.com/NauBernues/fima-vs-lecaps
- GitHub Pages (activar en Settings → Pages → Deploy from `main` / `/`):  
  `https://naubernues.github.io/fima-vs-lecaps/`

## Qué muestra

- Última generación (America/Buenos_Aires)
- Por fondo: último paso de VCP, TNA/TEA (días calendario), MTD, 30d, YTD, $ por día sobre $100M
- Spread vs Premium (MTD / 30d / YTD) en %, bps y $ / 100M
- Observaciones negativas y max drawdown (relevante en el lado Lecaps/RF)
- Notas de liquidez T+0 vs T+1 y de composición (% Lecaps verificado en CNV)

## Fuentes (sin inventar)

| Fuente | Rol | Estado |
|--------|-----|--------|
| [ArgentinaDatos FCI](https://api.argentinadatos.com/v1/finanzas/fci/{categoria}/{YYYY}/{MM}/{DD}) | Serie diaria VCP (republica cierre CAFCI) | **En uso** |
| [CAFCI API](https://api.cafci.org.ar/) | Oficial | **401 Unauthorized** desde este entorno |
| CNV carteras | % Lecaps | Manual / verificado al armar |

### IDs CAFCI bloqueados

| Fondo | fondo_id | clase_id (A) |
|-------|----------|--------------|
| Fima Premium | 445 | 575 |
| Fima Ahorro Plus | 641 | 1008 |
| Balanz Lecaps | (pendiente) | (pendiente) |

### Composición (gate de Ray: >70% Lecaps)

- **Fima Ahorro Plus:** ≈ **23,9%** Lecaps (CNV 2026-10-02). **No cumple >70%.** Se mantiene como comparación same-manager.
- **Balanz Lecaps:** ≈ **62,5%** letras/Lecaps (CNV 2026-09-18). Mejor proxy dedicado encontrado; **tampoco >70%.**
- No se inventó un fondo “puro Lecaps”; si aparece uno con cartera CNV >70%, se agrega en `config.json`.

### Fees

La cuotaparte CAFCI **ya es neta** de honorarios del fondo. No se descuenta fee de asesor (Quaestus) ni costos Byma.

## Update diario

```bash
python scripts/fetch_data.py --out data.json
```

GitHub Action: `.github/workflows/daily-update.yml`  
Crons: `30 0 * * 1-5` y `30 11 * * 1-5` (UTC). Commitea `data.json` a `main`.

**Para Nerdo:** activar Pages + revisar que el token del workflow tenga `contents: write` (ya declarado en el YAML).

## Tickers Lecaps (configurables)

En `config.json` → `lecap_tickers`. Hoy: `S16A5, S28A5, S16Y5, S30Y5, S18J5, S30J5` (pueden estar vencidos/rolleados). Falta endpoint Byma público.

## No deploy Cloudflare de Fer

Solo GitHub Pages bajo `NauBernues`.
