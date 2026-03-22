"""
Tests for the FastAPI backend (api.py).

Uses FastAPI TestClient for synchronous request testing.
Tests cover all main endpoint categories: health, status, scripts,
HITL, pipeline triggers, costs, backups, news, and jobs.
"""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Add code directory to path
sys.path.insert(0, str(Path(__file__).parent.parent / "code"))


@pytest.fixture
def mock_config():
    """Minimal config for API tests."""
    return {
        "version": "2.2-test",
        "apis": {},
        "platforms": {"youtube": {"enabled": False}, "tiktok": {"enabled": False}, "instagram": {"enabled": False}},
        "automation": {"auto_publish_threshold": 9.0},
        "quality_gate": {"auto_approve_above": 9.0, "auto_reject_below": 6.0},
        "cost_tracking": {
            "monthly_budget_hard_stop": 20, "daily_budget_hard_stop": 5,
            "per_video_budget_hard_stop": 1, "monthly_budget_alert": 10,
            "daily_budget_alert": 3, "per_video_budget_alert": 0.5,
        },
        "logging": {"file_path": "/tmp/bolt_test_logs", "level": "WARNING"},
    }


@pytest.fixture
def client(mock_config, tmp_path):
    """Create a TestClient with mocked DB and config."""
    db_path = tmp_path / "test_api.db"

    with patch.dict("os.environ", {"BOLT_API_KEY": ""}, clear=False):
        # Patch config loading before importing api
        with patch("secrets_manager.load_all_secrets", side_effect=lambda x: x):
            # Patch the config file read
            import json
            config_path = tmp_path / "config.json"
            config_path.write_text(json.dumps(mock_config))

            with patch("api._load_config", return_value=mock_config):
                with patch("api.CONFIG", mock_config):
                    with patch("database.DEFAULT_DB_PATH", db_path):
                        from fastapi.testclient import TestClient
                        from api import app
                        yield TestClient(app)


class TestHealthEndpoint:
    def test_health_returns_ok(self, client):
        resp = client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "version" in data
        assert "timestamp" in data

    def test_health_includes_db_status(self, client):
        resp = client.get("/api/health")
        data = resp.json()
        assert data["database"] == "connected"


class TestStatusEndpoint:
    def test_status_returns_system_health(self, client):
        resp = client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "systemHealth" in data
        assert "pipeline" in data
        assert "costs" in data
        assert "providers" in data


class TestScriptsEndpoint:
    def test_scripts_returns_list(self, client):
        resp = client.get("/api/scripts")
        assert resp.status_code == 200
        data = resp.json()
        assert "scripts" in data
        assert "total" in data
        assert isinstance(data["scripts"], list)

    def test_scripts_filter_by_status(self, client):
        resp = client.get("/api/scripts?status=approved")
        assert resp.status_code == 200

    def test_script_not_found(self, client):
        resp = client.get("/api/scripts/nonexistent_id")
        assert resp.status_code == 404


class TestHITLEndpoints:
    def test_pending_returns_list(self, client):
        resp = client.get("/api/hitl/pending")
        assert resp.status_code == 200
        data = resp.json()
        assert "pending" in data

    def test_approve_nonexistent(self, client, tmp_path):
        # Ensure queue directory exists for flag file creation
        import os
        os.makedirs("data/queue", exist_ok=True)
        resp = client.post("/api/hitl/approve/nonexistent_id")
        assert resp.status_code == 200

    def test_reject_nonexistent(self, client, tmp_path):
        import os
        os.makedirs("data/queue", exist_ok=True)
        resp = client.post("/api/hitl/reject/nonexistent_id", json={"reason": "test"})
        assert resp.status_code == 200


class TestPipelineEndpoints:
    def test_trigger_pipeline_creates_job(self, client):
        resp = client.post("/api/pipeline/run")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "queued"

    def test_trigger_step_valid(self, client):
        resp = client.post("/api/pipeline/news")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "queued"
        assert data["step"] == "news"

    def test_trigger_step_invalid(self, client):
        resp = client.post("/api/pipeline/invalid_step")
        assert resp.status_code == 400

    def test_pipeline_status(self, client):
        resp = client.get("/api/pipeline/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "running" in data


class TestCostsEndpoint:
    def test_costs_returns_summary(self, client):
        resp = client.get("/api/costs")
        assert resp.status_code == 200
        data = resp.json()
        assert "month" in data
        assert "total_usd" in data

    def test_costs_with_month_filter(self, client):
        resp = client.get("/api/costs?month=2026-03")
        assert resp.status_code == 200


class TestNewsEndpoint:
    def test_news_returns_articles(self, client):
        resp = client.get("/api/news")
        assert resp.status_code == 200
        data = resp.json()
        assert "articles" in data
        assert "total" in data


class TestJobsEndpoint:
    def test_jobs_returns_queue_status(self, client):
        resp = client.get("/api/jobs")
        assert resp.status_code == 200
        data = resp.json()
        assert "pending" in data
        assert "by_status" in data


class TestBackupsEndpoint:
    def test_list_backups(self, client):
        resp = client.get("/api/backups")
        assert resp.status_code == 200
        data = resp.json()
        assert "backups" in data
