"""Tests for scripts/inspect_bundle.py.

Imported by path rather than as a package: the script is meant to be a
standalone, copy-pasteable file with zero dependencies, including on this
integration's own package layout -- the same reasoning as
tests/test_check_infeasibility.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import importlib.util
from pathlib import Path
import sys

_SCRIPT_PATH = Path(__file__).parent.parent / "scripts" / "inspect_bundle.py"
_spec = importlib.util.spec_from_file_location("inspect_bundle", _SCRIPT_PATH)
inspect_bundle = importlib.util.module_from_spec(_spec)
sys.modules["inspect_bundle"] = inspect_bundle
_spec.loader.exec_module(inspect_bundle)

Severity = inspect_bundle.Severity
run_checks = inspect_bundle.run_checks


def _findings(report, severity):
    return [f for f in report.findings if f.severity is severity]


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _clean_bundle(**overrides) -> dict:
    now = datetime.now(UTC)
    bundle = {
        "environment": {"integration_version": "0.9.1", "home_assistant_version": "2026.8.0"},
        "backend": {
            "url": "http://localhost:5000",
            "source": "add-on discovery",
            "version": "0.17.9",
        },
        "entry": {"options": {"mpc_interval_minutes": 15, "battery": {"use_battery": True}}},
        "subentries": [],
        "entities": [],
        "custom_profiles": [],
        "profiles": {"errors": {}},
        "last_run": {"status": "ok", "infeasible": False},
        "plan": {"rows": 12, "generated_at": _iso(now)},
        "series": {
            "pv_forecast": {"points": 48, "end": _iso(now + timedelta(hours=24))},
            "load_forecast": {"points": 48, "end": _iso(now + timedelta(hours=24))},
        },
    }
    bundle.update(overrides)
    return bundle


def test_a_clean_bundle_produces_no_findings_and_would_exit_zero():
    report = run_checks(_clean_bundle())
    assert report.findings == []


def test_a_missing_bundle_section_produces_no_findings_for_that_check():
    """An older bundle without the new sections must not crash the script."""
    report = run_checks({})
    # check_plan and check_last_run still have something to say about an
    # entirely empty bundle -- what must not happen is an exception.
    assert isinstance(report.findings, list)


# --- backend -------------------------------------------------------------


def test_backend_error_is_critical():
    bundle = _clean_bundle()
    bundle["backend"] = {"version_error": "Timeout calling http://localhost:5000/healthz"}
    report = run_checks(bundle)
    assert any("unreachable" in f.title for f in _findings(report, Severity.CRITICAL))


def test_backend_version_below_minimum_is_critical():
    bundle = _clean_bundle()
    bundle["backend"]["version"] = "0.17.0"
    report = run_checks(bundle)
    assert any("below the minimum" in f.title for f in _findings(report, Severity.CRITICAL))


def test_backend_version_at_minimum_is_not_flagged():
    bundle = _clean_bundle()
    bundle["backend"]["version"] = "0.17.9"
    report = run_checks(bundle)
    assert _findings(report, Severity.CRITICAL) == []


# --- entities -------------------------------------------------------------


def test_missing_entity_is_critical():
    bundle = _clean_bundle()
    bundle["entities"] = [
        {
            "entity_id": "sensor.does_not_exist",
            "referenced_by": ["options.soc_entity"],
            "exists": False,
            "state": None,
        }
    ]
    report = run_checks(bundle)
    assert any("does not exist" in f.title for f in _findings(report, Severity.CRITICAL))


def test_unavailable_entity_is_a_warning():
    bundle = _clean_bundle()
    bundle["entities"] = [
        {
            "entity_id": "sensor.soc",
            "referenced_by": ["options.soc_entity"],
            "exists": True,
            "state": "unavailable",
        }
    ]
    report = run_checks(bundle)
    assert any("unavailable" in f.title for f in _findings(report, Severity.WARNING))


def test_non_numeric_state_on_a_numeric_field_is_critical():
    bundle = _clean_bundle()
    bundle["entities"] = [
        {
            "entity_id": "sensor.soc",
            "referenced_by": ["options.soc_entity"],
            "exists": True,
            "state": "not_a_number",
        }
    ]
    report = run_checks(bundle)
    assert any("non-numeric" in f.title for f in _findings(report, Severity.CRITICAL))


def test_non_numeric_state_on_power_sensor_is_not_flagged():
    """power_sensor also accepts a plain on/off binary_sensor."""
    bundle = _clean_bundle()
    bundle["entities"] = [
        {
            "entity_id": "binary_sensor.dishwasher_running",
            "referenced_by": ["subentries[deferrable_load:Dishwasher].data.power_sensor"],
            "exists": True,
            "state": "on",
        }
    ]
    report = run_checks(bundle)
    assert _findings(report, Severity.CRITICAL) == []


def test_kw_where_watts_are_expected_is_a_warning():
    bundle = _clean_bundle()
    bundle["entities"] = [
        {
            "entity_id": "sensor.pv_power",
            "referenced_by": ["options.pv_entity"],
            "exists": True,
            "state": "3.2",
            "unit_of_measurement": "kW",
        }
    ]
    report = run_checks(bundle)
    assert any("kW where EMHASS expects W" in f.title for f in _findings(report, Severity.WARNING))


def test_healthy_entity_is_not_flagged():
    bundle = _clean_bundle()
    bundle["entities"] = [
        {
            "entity_id": "sensor.pv_power",
            "referenced_by": ["options.pv_entity"],
            "exists": True,
            "state": "3200",
            "unit_of_measurement": "W",
        }
    ]
    report = run_checks(bundle)
    assert report.findings == []


def test_an_error_entity_record_is_skipped_not_crashed_on():
    bundle = _clean_bundle()
    bundle["entities"] = [{"entity_id": "sensor.whatever", "error": "boom"}]
    report = run_checks(bundle)
    assert report.findings == []


# --- last run --------------------------------------------------------------


def test_no_run_ever_is_a_warning():
    bundle = _clean_bundle()
    bundle["last_run"] = {"status": "no-run"}
    report = run_checks(bundle)
    assert any("ever completed" in f.title for f in _findings(report, Severity.WARNING))


def test_failed_run_is_critical():
    bundle = _clean_bundle()
    bundle["last_run"] = {"status": "error", "error_message": "solver crashed"}
    report = run_checks(bundle)
    assert any("did not succeed" in f.title for f in _findings(report, Severity.CRITICAL))
    assert any("solver crashed" in f.detail for f in _findings(report, Severity.CRITICAL))


def test_infeasible_run_is_a_warning_pointing_at_the_other_script():
    bundle = _clean_bundle()
    bundle["last_run"] = {"status": "ok", "infeasible": True}
    report = run_checks(bundle)
    findings = _findings(report, Severity.WARNING)
    assert any("infeasible" in f.title for f in findings)
    assert any("check_infeasibility.py" in f.detail for f in findings)


# --- plan -------------------------------------------------------------


def test_zero_row_plan_is_critical():
    bundle = _clean_bundle()
    bundle["plan"] = {"rows": 0, "generated_at": _iso(datetime.now(UTC))}
    report = run_checks(bundle)
    assert any("zero rows" in f.title for f in _findings(report, Severity.CRITICAL))


def test_stale_plan_is_a_warning():
    bundle = _clean_bundle()
    old = datetime.now(UTC) - timedelta(hours=6)
    bundle["plan"] = {"rows": 12, "generated_at": _iso(old)}
    bundle["entry"]["options"]["mpc_interval_minutes"] = 15
    report = run_checks(bundle)
    assert any("old" in f.title for f in _findings(report, Severity.WARNING))


def test_fresh_plan_is_not_flagged_as_stale():
    report = run_checks(_clean_bundle())
    assert not any("old" in f.title for f in _findings(report, Severity.WARNING))


# --- forecast coverage -------------------------------------------------------


def test_forecast_source_that_stops_short_of_the_horizon_is_a_warning():
    bundle = _clean_bundle()
    now = datetime.now(UTC)
    bundle["series"] = {
        "pv_forecast": {"points": 4, "end": _iso(now + timedelta(hours=2))},
        "load_forecast": {"points": 48, "end": _iso(now + timedelta(hours=24))},
    }
    report = run_checks(bundle)
    assert any(
        "pv_forecast" in f.title and "short of the horizon" in f.title for f in report.findings
    )


def test_forecast_sources_covering_the_horizon_are_not_flagged():
    report = run_checks(_clean_bundle())
    assert not any("short of the horizon" in f.title for f in report.findings)


def test_sources_of_different_lengths_are_fine_while_all_cover_the_horizon():
    """The ordinary healthy install, which must stay silent.

    Solcast publishes four days of PV where a day-ahead market has published
    perhaps two of prices. Comparing sources against each other rather than
    against the horizon reported that as a fault on most installs.
    """
    bundle = _clean_bundle()
    now = datetime.now(UTC)
    bundle["series"] = {
        "buy_price": {"points": 192, "end": _iso(now + timedelta(days=2))},
        "sell_price": {"points": 192, "end": _iso(now + timedelta(days=2))},
        "pv_forecast": {"points": 192, "end": _iso(now + timedelta(days=4))},
        "load_forecast": {"points": 96, "end": _iso(now + timedelta(hours=24))},
    }
    report = run_checks(bundle)
    assert report.findings == []


def test_a_horizon_longer_than_a_source_reaches_is_still_flagged():
    bundle = _clean_bundle()
    now = datetime.now(UTC)
    bundle["entry"]["options"]["horizon_hours"] = 48
    bundle["series"] = {
        "pv_forecast": {"points": 96, "end": _iso(now + timedelta(hours=24))},
        "load_forecast": {"points": 96, "end": _iso(now + timedelta(hours=48))},
    }
    report = run_checks(bundle)
    assert any(
        "pv_forecast" in f.title and "short of the horizon" in f.title for f in report.findings
    )


def test_an_empty_series_is_critical_even_though_it_has_no_end_timestamp():
    """The failure that slipped through a purely timestamp-based check.

    A source returning nothing has no end to compare against anything, so
    the emptier the series the quieter it used to be.
    """
    bundle = _clean_bundle()
    bundle["series"]["load_forecast"] = {"points": 0, "end": None}
    report = run_checks(bundle)
    assert any("load_forecast is empty" in f.title for f in _findings(report, Severity.CRITICAL))


# --- forecast method ---------------------------------------------------------


def test_a_load_forecast_method_that_was_not_honoured_is_a_warning():
    bundle = _clean_bundle()
    bundle["entry"]["options"]["load"] = {"profile_options": {"method": "mlforecaster"}}
    bundle["last_payload"] = {"load_forecast_method": "naive"}
    report = run_checks(bundle)
    assert any("mlforecaster" in f.title and "naive" in f.title for f in report.findings)


def test_the_load_forecast_method_actually_used_is_not_flagged():
    bundle = _clean_bundle()
    bundle["entry"]["options"]["load"] = {"profile_options": {"method": "naive"}}
    bundle["last_payload"] = {"load_forecast_method": "naive"}
    report = run_checks(bundle)
    assert report.findings == []


# --- profile errors -------------------------------------------------------


def test_profile_error_is_reported_with_its_content_when_available():
    bundle = _clean_bundle()
    bundle["profiles"] = {
        "errors": {"/config/emhass_companion/profiles/price/broken.yaml": "bad yaml"}
    }
    bundle["custom_profiles"] = [
        {"path": "price/broken.yaml", "content": "kind: price\nname: [", "size": 20}
    ]
    report = run_checks(bundle)
    findings = _findings(report, Severity.CRITICAL)
    assert any("Profile failed to load" in f.title for f in findings)
    assert any("kind: price" in f.detail for f in findings)


def test_profile_error_with_no_matching_file_still_reports_the_reason():
    bundle = _clean_bundle()
    bundle["profiles"] = {
        "errors": {"/config/emhass_companion/profiles/price/gone.yaml": "missing"}
    }
    bundle["custom_profiles"] = []
    report = run_checks(bundle)
    assert any("Profile failed to load" in f.title for f in _findings(report, Severity.CRITICAL))


# --- subentries -------------------------------------------------------------


def test_zero_power_deferrable_load_is_critical():
    bundle = _clean_bundle()
    bundle["subentries"] = [
        {
            "subentry_id": "abc",
            "subentry_type": "deferrable_load",
            "title": "Dishwasher",
            "data": {"nominal_power_w": 0},
        }
    ]
    report = run_checks(bundle)
    assert any(
        "zero or negative nominal power" in f.title for f in _findings(report, Severity.CRITICAL)
    )


def test_window_narrower_than_run_time_is_critical():
    bundle = _clean_bundle()
    bundle["subentries"] = [
        {
            "subentry_id": "abc",
            "subentry_type": "deferrable_load",
            "title": "Dishwasher",
            "data": {
                "nominal_power_w": 2000,
                "use_time_window": True,
                "operating_hours": 3,
                "earliest_start": "22:00:00",
                "latest_end": "23:00:00",
            },
        }
    ]
    report = run_checks(bundle)
    assert any(
        "narrower than its own run time" in f.title for f in _findings(report, Severity.CRITICAL)
    )


def test_window_that_fits_is_not_flagged():
    bundle = _clean_bundle()
    bundle["subentries"] = [
        {
            "subentry_id": "abc",
            "subentry_type": "deferrable_load",
            "title": "Dishwasher",
            "data": {
                "nominal_power_w": 2000,
                "use_time_window": True,
                "operating_hours": 1,
                "earliest_start": "22:00:00",
                "latest_end": "23:00:00",
            },
        }
    ]
    report = run_checks(bundle)
    assert _findings(report, Severity.CRITICAL) == []


def test_window_crossing_midnight_is_handled():
    bundle = _clean_bundle()
    bundle["subentries"] = [
        {
            "subentry_id": "abc",
            "subentry_type": "deferrable_load",
            "title": "Pool pump",
            "data": {
                "nominal_power_w": 1000,
                "use_time_window": True,
                "operating_hours": 2,
                "earliest_start": "23:00:00",
                "latest_end": "02:00:00",
            },
        }
    ]
    report = run_checks(bundle)
    assert _findings(report, Severity.CRITICAL) == []


def test_missing_control_entity_is_a_warning():
    bundle = _clean_bundle()
    bundle["subentries"] = [
        {
            "subentry_id": "abc",
            "subentry_type": "deferrable_load",
            "title": "Dishwasher",
            "data": {"nominal_power_w": 2000, "control_entity": "switch.does_not_exist"},
        }
    ]
    bundle["entities"] = [
        {
            "entity_id": "switch.does_not_exist",
            "referenced_by": ["subentries[deferrable_load:Dishwasher].data.control_entity"],
            "exists": False,
            "state": None,
        }
    ]
    report = run_checks(bundle)
    assert any("control_entity" in f.title for f in _findings(report, Severity.WARNING))


def test_load_group_naming_a_gone_load_is_a_warning():
    bundle = _clean_bundle()
    bundle["subentries"] = [
        {
            "subentry_id": "group-1",
            "subentry_type": "load_group",
            "title": "Kitchen",
            "data": {"load_subentry_ids": ["gone-id"]},
        }
    ]
    report = run_checks(bundle)
    assert any(
        "names a load that no longer exists" in f.title for f in _findings(report, Severity.WARNING)
    )


def test_load_group_naming_a_live_load_is_not_flagged():
    bundle = _clean_bundle()
    bundle["subentries"] = [
        {
            "subentry_id": "load-1",
            "subentry_type": "deferrable_load",
            "title": "Dishwasher",
            "data": {"nominal_power_w": 2000},
        },
        {
            "subentry_id": "group-1",
            "subentry_type": "load_group",
            "title": "Kitchen",
            "data": {"load_subentry_ids": ["load-1"]},
        },
    ]
    report = run_checks(bundle)
    assert _findings(report, Severity.WARNING) == []


# --- nothing to optimise -----------------------------------------------------


def test_no_battery_and_no_loads_is_informational():
    bundle = _clean_bundle()
    bundle["entry"]["options"]["battery"] = {"use_battery": False}
    bundle["subentries"] = []
    report = run_checks(bundle)
    assert any("Nothing to optimise" in f.title for f in _findings(report, Severity.INFO))


def test_a_battery_alone_is_enough_to_avoid_the_nothing_to_optimise_finding():
    bundle = _clean_bundle()
    bundle["entry"]["options"]["battery"] = {"use_battery": True}
    bundle["subentries"] = []
    report = run_checks(bundle)
    assert not any("Nothing to optimise" in f.title for f in report.findings)
