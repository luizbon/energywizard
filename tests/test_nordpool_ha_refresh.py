"""Prissensorer i HA med dynamiska priser (t.ex. Amber): prognosen för kommande intervall revideras var 5:e
minut, så lagrade rader måste ersättas i stället för att ligga kvar som första gången de sågs. Testerna kör
mot en riktig Postgres och ett syntetiskt HA-svar, aldrig nätverket. Nord Pool-källor (priserna är låsta när
de publiceras) ska fortsatt vara insert-once."""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

import nordpool

UTC = timezone.utc
ENTITY = "sensor.amber_general_forecast"
NOW = datetime(2026, 11, 9, 1, 37, 0, tzinfo=UTC)


@pytest.fixture()
def priser(smartbox, db):
    """elpriser med time_end som i drift (conftest skapar tabellen utan kolumnen)."""
    db.execute("ALTER TABLE elpriser ADD COLUMN IF NOT EXISTS time_end TIMESTAMPTZ")
    db.execute("TRUNCATE elpriser")
    return db


def _amber(rows):
    """HA-svar för Amber-sensorn: forecasts med start_time/end_time/per_kwh."""
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = {
        "entity_id": ENTITY,
        "state": "0.1",
        "attributes": {
            "unit_of_measurement": "$/kWh",
            "forecasts": [{"start_time": s, "end_time": e, "per_kwh": v} for s, e, v in rows],
        },
    }
    return r


def _nordpool(rows):
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = {
        "entity_id": "sensor.nordpool",
        "state": "0.5",
        "attributes": {"raw_today": [{"start": s, "end": e, "value": v} for s, e, v in rows], "raw_tomorrow": []},
    }
    return r


def _refresh(response, entity=ENTITY, now=NOW):
    with patch("nordpool.requests.get", return_value=response), patch("nordpool._price_entities", return_value=[entity]):
        return nordpool.refresh_ha_prices(now)


def _rows(db):
    return [(r[0], r[1], float(r[2])) for r in db.execute("SELECT time_start, time_end, sek_per_kwh FROM elpriser ORDER BY time_start")]


def _covering(db, t):
    return [float(r[0]) for r in db.execute("SELECT sek_per_kwh FROM elpriser WHERE time_start <= %s AND time_end > %s", (t, t))]


def test_ersatter_framtida_rader_nar_prognosen_andras(priser):
    forst = [("2026-11-09T01:40:01+00:00", "2026-11-09T01:45:00+00:00", 0.10),
             ("2026-11-09T01:45:01+00:00", "2026-11-09T01:50:00+00:00", 0.11)]
    andra = [("2026-11-09T01:40:01+00:00", "2026-11-09T01:45:00+00:00", 0.25),
             ("2026-11-09T01:45:01+00:00", "2026-11-09T01:50:00+00:00", 0.08)]
    _refresh(_amber(forst))
    _refresh(_amber(andra))
    assert [v for _, _, v in _rows(priser)] == [0.25, 0.08]


def test_ett_enda_pris_per_tidpunkt_nar_upplosningen_gar_fran_30_till_5_minuter(priser):
    grov = [("2026-11-09T01:40:01+00:00", "2026-11-09T02:10:00+00:00", 0.20)]
    fin = [(f"2026-11-09T01:{m:02d}:01+00:00", f"2026-11-09T01:{m + 5:02d}:00+00:00", 0.30) for m in range(40, 55, 5)]
    _refresh(_amber(grov))
    _refresh(_amber(fin))
    for minut in (41, 46, 51):
        assert _covering(priser, datetime(2026, 11, 9, 1, minut, 30, tzinfo=UTC)) == [0.30]
    assert len(_rows(priser)) == 3


def test_rader_som_redan_passerat_lamnas_ororda(priser):
    priser.execute("INSERT INTO elpriser (time_start, time_end, sek_per_kwh) VALUES ('2026-11-09T01:00:01+00:00', '2026-11-09T01:05:00+00:00', 0.77)")
    _refresh(_amber([("2026-11-09T01:40:01+00:00", "2026-11-09T01:45:00+00:00", 0.10)]))
    assert _rows(priser)[0][2] == 0.77
    assert len(_rows(priser)) == 2


def test_tom_hamtning_ror_inte_lagrade_priser(priser):
    _refresh(_amber([("2026-11-09T01:40:01+00:00", "2026-11-09T01:45:00+00:00", 0.10)]))
    otillganglig = MagicMock()
    otillganglig.status_code = 404
    assert _refresh(otillganglig) is False
    assert _refresh(_amber([])) is False
    assert [v for _, _, v in _rows(priser)] == [0.10]


def test_intervallet_som_pagar_raderas_inte_utan_beskars(priser):
    priser.execute("INSERT INTO elpriser (time_start, time_end, sek_per_kwh) VALUES ('2026-11-09T01:30:01+00:00', '2026-11-09T02:00:00+00:00', 0.20)")
    _refresh(_amber([("2026-11-09T01:40:01+00:00", "2026-11-09T01:45:00+00:00", 0.10)]))
    assert _covering(priser, NOW) == [0.20]
    assert _covering(priser, datetime(2026, 11, 9, 1, 42, 0, tzinfo=UTC)) == [0.10]


def test_raderna_hamtas_oavsett_stockholmsdygn(priser):
    """Amber ger ett dygn framåt, vilket korsar midnatt i SWE_TZ."""
    rader = [("2026-11-09T22:30:01+00:00", "2026-11-09T23:00:00+00:00", 0.12),
             ("2026-11-09T23:00:01+00:00", "2026-11-09T23:30:00+00:00", 0.13)]
    _refresh(_amber(rader), now=datetime(2026, 11, 9, 22, 0, 0, tzinfo=UTC))
    assert len(_rows(priser)) == 2


def test_nord_pool_uppdateras_inte_som_standard(priser):
    rader = [("2026-11-09T01:40:00+00:00", "2026-11-09T01:55:00+00:00", 0.45)]
    assert _refresh(_nordpool(rader), entity="sensor.nordpool") is False
    assert _rows(priser) == []


def test_installning_kan_tvinga_pa_eller_av_uppdatering(priser, monkeypatch):
    rader = [("2026-11-09T01:40:00+00:00", "2026-11-09T01:55:00+00:00", 0.45)]
    monkeypatch.setenv("PRICE_HA_REFRESH", "true")
    assert _refresh(_nordpool(rader), entity="sensor.nordpool") is True
    monkeypatch.setenv("PRICE_HA_REFRESH", "false")
    assert _refresh(_amber([("2026-11-09T02:00:01+00:00", "2026-11-09T02:05:00+00:00", 0.1)])) is False


def test_hamtningen_stryps_mellan_uppdateringar(priser, monkeypatch):
    monkeypatch.setattr(nordpool, "_last_ha_refresh", None)
    monkeypatch.setattr(nordpool, "price_source", lambda: "ha_nordpool")
    with patch("nordpool.requests.get", return_value=_amber([("2026-11-09T01:40:01+00:00", "2026-11-09T01:45:00+00:00", 0.1)])) as get, \
         patch("nordpool._price_entities", return_value=[ENTITY]):
        nordpool.refresh_ha_prices_if_due(monotonic=1000.0, now=NOW)
        nordpool.refresh_ha_prices_if_due(monotonic=1030.0, now=NOW)
        assert get.call_count == 1
        nordpool.refresh_ha_prices_if_due(monotonic=1000.0 + nordpool.HA_REFRESH_SECONDS + 1, now=NOW)
        assert get.call_count == 2


def test_inga_uppdateringar_for_andra_kalla_an_ha_sensor(priser, monkeypatch):
    monkeypatch.setattr(nordpool, "_last_ha_refresh", None)
    monkeypatch.setattr(nordpool, "price_source", lambda: "elprisetjustnu")
    with patch("nordpool.requests.get") as get:
        nordpool.refresh_ha_prices_if_due(monotonic=1000.0, now=NOW)
        get.assert_not_called()


def test_pris_for_nu_hittas_i_startluckan_och_i_sekundluckan(priser):
    """Första raden börjar efter 'nu' direkt efter en omstart, och Ambers rader har en sekunds glapp (:00 -> :01)."""
    priser.execute("INSERT INTO elpriser (time_start, time_end, sek_per_kwh) VALUES ('2026-11-09T01:40:01+00:00', '2026-11-09T01:45:00+00:00', 0.31)")
    priser.execute("INSERT INTO elpriser (time_start, time_end, sek_per_kwh) VALUES ('2026-11-09T01:45:01+00:00', '2026-11-09T01:50:00+00:00', 0.32)")
    assert nordpool.get_price_at(datetime(2026, 11, 9, 1, 38, 0, tzinfo=UTC)) == 0.31      # före första raden
    assert nordpool.get_price_at(datetime(2026, 11, 9, 1, 45, 0, 500000, tzinfo=UTC)) in (0.31, 0.32)  # glappet
    assert nordpool.get_price_at(datetime(2026, 11, 9, 1, 42, 0, tzinfo=UTC)) == 0.31      # vanligt fall


def test_pris_for_nu_gissas_inte_langt_utanfor_lagrade_rader(priser):
    priser.execute("INSERT INTO elpriser (time_start, time_end, sek_per_kwh) VALUES ('2026-11-09T01:40:01+00:00', '2026-11-09T01:45:00+00:00', 0.31)")
    assert nordpool.get_price_at(NOW - timedelta(hours=3)) is None
    assert nordpool.get_price_at(NOW + timedelta(hours=3)) is None
