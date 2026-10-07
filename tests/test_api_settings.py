"""Starting the service from environment settings (#43, R1)."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi", reason="the API needs the optional `api` extra")

from fastapi.testclient import TestClient  # noqa: E402

from s2e_validator.api.main import ServiceSettings, SettingsError, build_app, build_service  # noqa: E402

from fake_chain import wallet  # noqa: E402
from helpers import CASTLE, big_photo  # noqa: E402

API_KEY = "test-key-" + "k" * 32


def submit(client):
    return client.post(
        "/submissions",
        data={"description": CASTLE, "wallet": wallet(1)},
        files={"photo": ("photo.jpg", big_photo(), "image/jpeg")},
        headers={"Authorization": f"Bearer {API_KEY}"},
    )


class TestStartingFromTheEnvironment:
    @pytest.fixture
    def env(self, tmp_path):
        config = tmp_path / "campaign.json"
        config.write_text(json.dumps({"default_mode": "enforce", "filters": {"face_detection": "off"}}))
        return {
            "S2E_CHAIN": "dry-run",
            "S2E_PIPELINE_CONFIG": str(config),
            "S2E_BATCH_LOG": str(tmp_path / "batches.sqlite"),
            "S2E_API_KEY": API_KEY,
            "S2E_MAX_UNITS_PER_SUBMISSION": "50",
        }

    def test_a_dry_run_service_starts_and_pays_nothing_real(self, env, monkeypatch):
        for name, value in env.items():
            monkeypatch.setenv(name, value)

        with TestClient(build_app()) as client:
            body = submit(client).json()

        assert body["state"] == "paid"
        assert body["settled_on"] == "dry-run"
        assert body["transaction"].startswith("dryrun-")
        assert body["reward_units"] == 50  # 75 earned, clamped to the configured maximum

    def test_settings_are_read(self, env):
        settings = ServiceSettings.from_env({**env, "S2E_UNITS_PER_POINT": "10000"})

        assert settings.units_per_point == 10_000
        assert settings.max_units_per_submission == 50

    def test_the_key_never_shows_when_settings_are_printed(self, env):
        assert API_KEY not in repr(ServiceSettings.from_env(env))

    @pytest.mark.parametrize(
        "change, message",
        [
            ({"S2E_CHAIN": ""}, "set S2E_CHAIN to start"),
            ({"S2E_BATCH_LOG": "", "S2E_API_KEY": ""}, "set S2E_BATCH_LOG, S2E_API_KEY"),
            ({"S2E_MAX_UNITS_PER_SUBMISSION": ""}, "set S2E_MAX_UNITS_PER_SUBMISSION"),
            ({"S2E_CHAIN": "mainnet"}, "must be dry-run or solana"),
            ({"S2E_UNITS_PER_POINT": "lots"}, "must be whole numbers"),
            ({"S2E_API_KEY": "short"}, "at least 32 characters"),
        ],
    )
    def test_settings_that_cannot_start_a_service_say_why(self, env, change, message):
        with pytest.raises(SettingsError, match=message):
            ServiceSettings.from_env({**env, **change})

    def test_there_is_no_default_chain_and_no_default_maximum(self, env):
        """A deployment must say whether it pays, and how much at most."""
        for name in ("S2E_CHAIN", "S2E_MAX_UNITS_PER_SUBMISSION"):
            with pytest.raises(SettingsError, match=name):
                ServiceSettings.from_env({k: v for k, v in env.items() if k != name})

    def test_solana_names_the_settings_it_still_needs(self, env):
        env = {**env, "S2E_CHAIN": "solana", "S2E_RPC_URL": "https://rpc.example"}

        with pytest.raises(SettingsError, match="also needs S2E_PROGRAM_ID, S2E_SURVEY_ID, S2E_RUN_ID"):
            build_service(ServiceSettings.from_env(env), env)
