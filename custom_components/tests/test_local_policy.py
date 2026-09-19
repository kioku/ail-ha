"""Regression tests for Kioku's explicit local deployment policy."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ail import async_migrate_entry
from custom_components.ail.api_client import AILEnergyClient, ConsumptionResponse
from custom_components.ail.const import (
    CONSUMPTION_DATA_DAYS_TO_FETCH,
    DOMAIN,
    INITIAL_HISTORY_DAYS,
)
from custom_components.ail.coordinator import (
    ConsumptionData,
    EnergyDataUpdateCoordinator,
)


class _DummyRecorder:
    async def async_add_executor_job(self, func, *args):
        return func(*args)


def _coordinator(hass) -> EnergyDataUpdateCoordinator:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"username": "user@example.com", "password": "secret"},
    )
    entry.add_to_hass(hass)
    return EnergyDataUpdateCoordinator(hass, entry, AILEnergyClient("u", "p"))


def test_history_windows_match_local_policy():
    """Keep API load bounded while retaining a correction overlap."""
    assert CONSUMPTION_DATA_DAYS_TO_FETCH == 3
    assert INITIAL_HISTORY_DAYS == 90


def test_pending_records_are_not_persisted():
    """Only finalized AIL readings may enter long-term statistics."""
    response = ConsumptionResponse.model_validate(
        {
            "response": [
                {
                    "from": "2026-01-01T00:00:00+00:00",
                    "to": "2026-01-01T01:00:00+00:00",
                    "day": 1.0,
                    "night": 0.0,
                    "isPending": True,
                    "readingsCount": 4,
                },
                {
                    "from": "2026-01-01T01:00:00+00:00",
                    "to": "2026-01-01T02:00:00+00:00",
                    "day": 2.0,
                    "night": 0.0,
                    "isPending": False,
                    "readingsCount": 4,
                },
            ]
        }
    )

    records = ConsumptionData.from_api_response(response)

    assert [record.day for record in records] == [2.0]


async def test_legacy_entry_migrates_to_single_account_identity(hass):
    """Preserve the version-two identity already stored by the live instance."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"username": "User@Example.com", "password": "secret"},
        version=1,
    )
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry)
    assert entry.version == 2
    assert entry.unique_id == "user@example.com"


async def test_revised_hours_recalculate_cumulative_sum(hass):
    """Re-emitted hours continue from the sum immediately before the overlap."""
    coordinator = _coordinator(hass)
    first_hour = datetime(2026, 1, 1, tzinfo=timezone.utc)
    consumptions = {
        first_hour: ConsumptionData(
            from_date=first_hour,
            to_date=first_hour + timedelta(hours=1),
            day=1.0,
        ),
        first_hour + timedelta(hours=1): ConsumptionData(
            from_date=first_hour + timedelta(hours=1),
            to_date=first_hour + timedelta(hours=2),
            day=2.0,
        ),
    }
    captured = {}

    def capture(_hass, _metadata, statistics):
        captured["statistics"] = statistics

    with (
        patch(
            "custom_components.ail.coordinator.statistics_during_period",
            return_value={"test:stat": [{"sum": 5.0}]},
        ),
        patch(
            "custom_components.ail.coordinator.get_instance",
            return_value=_DummyRecorder(),
        ),
        patch(
            "custom_components.ail.coordinator.async_add_external_statistics",
            new=capture,
        ),
    ):
        await coordinator._insert_statistic_type(
            consumptions,
            "day",
            "test:stat",
            "Test statistic",
            metadata={"unit_of_measurement": None},
        )

    assert [row["sum"] for row in captured["statistics"]] == [6.0, 8.0]
