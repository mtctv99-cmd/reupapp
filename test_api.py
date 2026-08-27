import os
import json
import pytest
from fastapi.testclient import TestClient
from app import app
from core.db import create_job, get_job, list_jobs, update_job, delete_job
from core.config import settings

client = TestClient(app)

def test_api_settings_get():
    res = client.get("/api/settings")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert "data" in data
    assert isinstance(data["data"], dict)

def test_api_settings_post():
    res = client.post("/api/settings", json={"target_lang": "vi", "test_param": "123"})
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert data["data"].get("test_param") == "123"

def test_api_jobs_flow():
    # 1. Create DB job
    job_id = create_job("https://example.com/video1", title="Test Title", source_lang="zh", target_lang="vi")
    assert job_id is not None
    job = get_job(job_id)
    assert job is not None
    assert job["title"] == "Test Title"

    # 2. Get detail
    res = client.get(f"/api/jobs/detail?id={job_id}")
    assert res.status_code == 200
    detail = res.json()["data"]
    assert detail["id"] == job_id
    assert detail["title"] == "Test Title"

    # 3. List jobs
    res = client.get("/api/jobs")
    assert res.status_code == 200
    jobs = res.json()["data"]
    assert any(j["id"] == job_id for j in jobs)

    # 4. Update status
    update_job(job_id, status="completed", progress=1.0)
    res = client.get(f"/api/jobs/detail?id={job_id}")
    assert res.json()["data"]["status"] == "completed"

    # 5. Delete job
    res = client.delete(f"/api/jobs?id={job_id}")
    assert res.status_code == 200
    res = client.get(f"/api/jobs/detail?id={job_id}")
    assert res.json()["data"] is None

def test_api_progress():
    res = client.get("/api/progress")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert "running" in data["data"]

def test_api_jobs_create_validation():
    # Empty url should fail
    res = client.post("/api/jobs/create", json={"url": ""})
    assert res.status_code == 400
    assert "Video URL is required" in res.json()["error"]

def test_api_tools_concat_validation():
    # Less than 2 files should fail
    res = client.post("/api/tools/concat", json={"files": ["one_file.mp4"]})
    assert res.status_code == 400

def test_api_media_security():
    # Empty path -> 400
    res = client.get("/media?path=")
    assert res.status_code == 400

    # Path traversal outside allowed -> 403 or 404
    res = client.get("/media?path=C:/Windows/System32/drivers/etc/hosts")
    assert res.status_code in (403, 404)


def test_api_queue_sequential():
    from core.db import get_next_pending_job, delete_job
    # Create 2 pending jobs
    res1 = client.post("/api/jobs/create", json={"url": "https://www.bilibili.com/video/BV111111", "title": "Job 1"})
    assert res1.status_code == 200
    id1 = res1.json()["job_id"]

    res2 = client.post("/api/jobs/create", json={"url": "https://www.bilibili.com/video/BV222222", "title": "Job 2"})
    assert res2.status_code == 200
    id2 = res2.json()["job_id"]

    assert id1 != id2
    # Clean up created test jobs
    delete_job(id1)
    delete_job(id2)


def test_api_version():
    res = client.get("/api/version")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert "version" in data


def test_api_check_update():
    res = client.get("/api/check-update")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert "has_update" in data
    assert "current_version" in data


def test_api_models_status_and_progress():
    res = client.get("/api/models/status")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert "ready" in data

    p_res = client.get("/api/models/progress")
    assert p_res.status_code == 200
    p_data = p_res.json()
    assert p_data["status"] == "ok"
    assert "progress" in p_data["data"]


