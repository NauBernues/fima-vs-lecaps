"""Gate de publicación, spreads en fechas comunes y reintentos HTTP."""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
import urllib.error
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fci_quality as q  # noqa: E402
import fetch_data as fd  # noqa: E402


def dense(start: date, end: date, v0: float = 100.0) -> list[dict]:
    pts = []
    v = v0
    d = start
    while d <= end:
        if q.is_business_day(d):
            pts.append({"date": d.isoformat(), "vcp": v})
            v *= 1.0004
        d += timedelta(days=1)
    return pts


class CalendarTests(unittest.TestCase):
    def test_easter_and_moved_holidays_2026(self):
        self.assertEqual(q.easter_sunday(2026), date(2026, 4, 5))
        # Güemes 17/06 cae miércoles → lunes 15/06. El 17 es hábil.
        self.assertFalse(q.is_business_day(date(2026, 6, 15)))
        self.assertTrue(q.is_business_day(date(2026, 6, 17)))
        # Visita papal y puentes: no son hábiles. El 11, 12 y 13/11 sí.
        for d in (date(2026, 11, 6), date(2026, 11, 9), date(2026, 11, 10), date(2026, 3, 23), date(2026, 7, 10)):
            self.assertFalse(q.is_business_day(d), d)
        for d in (date(2026, 11, 11), date(2026, 11, 12), date(2026, 11, 13)):
            self.assertTrue(q.is_business_day(d), d)
        self.assertFalse(q.is_business_day(date(2025, 12, 31)))
        self.assertFalse(q.is_business_day(date(2026, 12, 31)))

    def test_premium_only_holes_count_as_business_days(self):
        for iso in ("2026-01-08", "2026-03-05", "2026-08-04", "2026-08-20", "2026-10-26"):
            self.assertTrue(q.is_business_day(date.fromisoformat(iso)), iso)

    def test_papal_days_do_not_hurt_coverage(self):
        start, end = date(2026, 11, 2), date(2026, 11, 13)
        cov = q.coverage_of(dense(start, end), start, end)
        self.assertEqual(cov["missing"], [])
        self.assertGreater(cov["ratio"], 0.95)


class GateTests(unittest.TestCase):
    def test_passes_when_all_end_on_previous_business_day(self):
        pts = dense(date(2026, 10, 1), date(2026, 11, 13))
        series = {k: list(pts) for k in ("premium", "balanz_lecaps", "ahorro_plus")}
        gate = q.evaluate_gate(series, date(2026, 11, 16), lookback_calendar_days=40)
        self.assertTrue(gate["ok"], gate["reasons"])
        self.assertEqual(gate["as_of"], "2026-11-13")
        self.assertEqual(gate["warnings"], [])

    def test_rejects_misaligned_last_dates(self):
        full = dense(date(2026, 10, 1), date(2026, 11, 13))
        short = [p for p in full if p["date"] <= "2026-11-12"]
        series = {"premium": short, "balanz_lecaps": full, "ahorro_plus": list(full)}
        gate = q.evaluate_gate(series, date(2026, 11, 16), lookback_calendar_days=40)
        self.assertFalse(gate["ok"])
        self.assertTrue(any("desalineadas" in r for r in gate["reasons"]))
        self.assertIsNone(gate["as_of"])

    def test_rejects_aligned_but_stale_as_of(self):
        pts = dense(date(2026, 10, 1), date(2026, 11, 12))
        series = {k: list(pts) for k in ("premium", "balanz_lecaps", "ahorro_plus")}
        gate = q.evaluate_gate(series, date(2026, 11, 16), lookback_calendar_days=40)
        self.assertFalse(gate["ok"])
        self.assertTrue(any("no es un día hábil esperable" in r for r in gate["reasons"]))

    def test_rejects_low_coverage_and_names_missing_days(self):
        pts = dense(date(2026, 10, 20), date(2026, 11, 13))
        kept = [p for i, p in enumerate(pts) if i % 2 == 0]
        if kept[-1]["date"] != pts[-1]["date"]:
            kept.append(pts[-1])
        series = {"premium": pts, "balanz_lecaps": kept, "ahorro_plus": list(pts)}
        gate = q.evaluate_gate(series, date(2026, 11, 16), lookback_calendar_days=20)
        self.assertFalse(gate["ok"])
        self.assertTrue(any("cobertura" in r and "balanz_lecaps" in r for r in gate["reasons"]))
        missing = gate["coverage"]["balanz_lecaps"]["missing"]
        self.assertGreater(len(missing), 0)
        self.assertTrue(any("balanz_lecaps" in w and missing[0][8:10] in w for w in gate["warnings"]))

    def test_rejects_insane_step(self):
        pts = dense(date(2026, 10, 1), date(2026, 11, 13))
        bad = [dict(p) for p in pts]
        bad[-1] = {"date": bad[-1]["date"], "vcp": bad[-2]["vcp"] * 1.1}
        series = {"premium": bad, "balanz_lecaps": pts, "ahorro_plus": list(pts)}
        gate = q.evaluate_gate(series, date(2026, 11, 16), lookback_calendar_days=40)
        self.assertFalse(gate["ok"])
        self.assertTrue(any("fuera de rango" in r or "retorno" in r for r in gate["reasons"]))

    def test_rejects_non_positive_vcp(self):
        pts = dense(date(2026, 11, 11), date(2026, 11, 13), v0=50)
        bad = [dict(p) for p in pts]
        bad[0] = {"date": bad[0]["date"], "vcp": 0}
        self.assertTrue(any("fuera de rango" in m for m in q.range_violations(bad)))


class SpreadTests(unittest.TestCase):
    def test_common_dates_ignore_the_extra_tail(self):
        """El caso del 16/11: una serie llega al 13/11 y la otra al 12/11.

        El spread no puede usar el 13/11 ni un ancla distinta para cada fondo.
        """
        base = [
            {"date": "2026-10-27", "vcp": 100.0},
            {"date": "2026-11-11", "vcp": 100.5},
            {"date": "2026-11-12", "vcp": 101.0},
        ]
        other = [
            {"date": "2026-10-27", "vcp": 200.0},
            {"date": "2026-11-11", "vcp": 202.0},
            {"date": "2026-11-12", "vcp": 204.0},
            {"date": "2026-11-13", "vcp": 210.0},
        ]
        spreads = q.spreads_vs_base(base, {"balanz_lecaps": other}, date(2026, 11, 13), 100_000_000)
        block = spreads["balanz_lecaps"]
        mtd = block["mtd"]
        self.assertIsNotNone(mtd)
        self.assertEqual(mtd["compare_window"], mtd["premium_window"])
        self.assertEqual(mtd["window"], "2026-10-27→2026-11-12")
        self.assertTrue(mtd["windows_equal"])
        self.assertAlmostEqual(mtd["premium_return"], 0.01)
        self.assertAlmostEqual(mtd["compare_return"], 0.02)
        self.assertAlmostEqual(mtd["spread"], 0.01)
        step = block["last_aligned_step"]
        self.assertEqual(step["window"], "2026-11-11→2026-11-12")
        self.assertEqual(step["compare_window"], step["premium_window"])
        self.assertAlmostEqual(step["premium_return"], 101 / 100.5 - 1)
        self.assertAlmostEqual(step["compare_return"], 204 / 202 - 1)

    def test_no_window_when_there_is_no_shared_anchor(self):
        base = [{"date": "2026-11-12", "vcp": 101.0}, {"date": "2026-11-13", "vcp": 102.0}]
        other = [{"date": "2026-10-01", "vcp": 200.0}, {"date": "2026-10-02", "vcp": 201.0}]
        spreads = q.spreads_vs_base(base, {"ahorro_plus": other}, date(2026, 11, 13), 1)
        self.assertIsNone(spreads["ahorro_plus"]["mtd"])
        self.assertIsNone(spreads["ahorro_plus"]["ytd"])
        self.assertIsNone(spreads["ahorro_plus"]["rolling_30d"])


class PublishTests(unittest.TestCase):
    def setUp(self):
        self._fetch_all = fd.fetch_all

    def tearDown(self):
        fd.fetch_all = self._fetch_all

    def test_gate_failure_does_not_overwrite(self):
        full = dense(date(2026, 10, 1), date(2026, 11, 13))
        short = [p for p in full if p["date"] <= "2026-11-12"]

        def fake_fetch(cfg, end):
            return (
                {"premium": short, "balanz_lecaps": full, "ahorro_plus": list(full)},
                ["CAFCI API HTTP 403 — usando solo ArgentinaDatos."],
                "HTTP 403",
            )

        fd.fetch_all = fake_fetch
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "data.json"
            sentinel = '{"as_of":"keep-me"}\n'
            out.write_text(sentinel, encoding="utf-8")
            rc = fd.main(["--out", str(out), "--end", "2026-11-16"])
            self.assertEqual(rc, 2)
            self.assertEqual(out.read_text(encoding="utf-8"), sentinel)

    def test_gate_pass_writes_as_of_and_equal_windows(self):
        pts = dense(date(2025, 12, 1), date(2026, 11, 13))

        def fake_fetch(cfg, end):
            return (
                {k: [dict(p) for p in pts] for k in ("premium", "balanz_lecaps", "ahorro_plus")},
                [],
                "HTTP 403",
            )

        fd.fetch_all = fake_fetch
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "data.json"
            rc = fd.main(["--out", str(out), "--end", "2026-11-16"])
            self.assertEqual(rc, 0, "el gate no debería rechazar una serie alineada")
            data = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(data["as_of"], "2026-11-13")
        for fid, block in data["spreads_vs_premium"].items():
            for key in ("mtd", "rolling_30d", "ytd"):
                b = block[key]
                self.assertIsNotNone(b, f"{fid} {key}")
                self.assertEqual(b["compare_window"], b["premium_window"])
                self.assertTrue(b["windows_equal"])


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://test", code, "x", hdrs=None, fp=io.BytesIO(b""))


class RetryTests(unittest.TestCase):
    def test_retries_transient_then_succeeds(self):
        calls = {"n": 0}

        def opener(url, timeout):
            calls["n"] += 1
            if calls["n"] < 3:
                raise _http_error(503)
            return {"ok": True}

        got = fd.http_json("http://test", attempts=4, sleep=lambda _s: None, opener=opener)
        self.assertEqual(got, {"ok": True})
        self.assertEqual(calls["n"], 3)

    def test_does_not_retry_404_or_403(self):
        for code in (404, 403, 401):
            calls = {"n": 0}

            def opener(url, timeout, code=code):
                calls["n"] += 1
                raise _http_error(code)

            with self.assertRaises(urllib.error.HTTPError):
                fd.http_json("http://test", attempts=4, sleep=lambda _s: None, opener=opener)
            self.assertEqual(calls["n"], 1, code)

    def test_retries_urlerror_at_most_three_times(self):
        calls = {"n": 0}

        def opener(url, timeout):
            calls["n"] += 1
            raise urllib.error.URLError("timed out")

        with self.assertRaises(urllib.error.URLError):
            fd.http_json("http://test", attempts=4, sleep=lambda _s: None, opener=opener)
        self.assertEqual(calls["n"], 4)

    def test_does_not_retry_forever_on_503(self):
        calls = {"n": 0}

        def opener(url, timeout):
            calls["n"] += 1
            raise _http_error(503)

        with self.assertRaises(urllib.error.HTTPError):
            fd.http_json("http://test", attempts=4, sleep=lambda _s: None, opener=opener)
        self.assertEqual(calls["n"], 4)


if __name__ == "__main__":
    unittest.main()
