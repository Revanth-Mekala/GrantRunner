import pytest
from fastapi.testclient import TestClient

from grantrunner.app import create_app
from grantrunner.browser import validate_url
from grantrunner.config import Settings
from grantrunner.store import Conflict

TOKEN = "test-token-with-more-than-24-characters"


@pytest.fixture
def client(tmp_path):
    app = create_app(Settings(tmp_path, TOKEN, demo=True, max_upload_bytes=100), start_worker=False)
    with TestClient(app) as client:
        yield client


def sign_in(client):
    assert client.post("/api/login", json={"token": TOKEN}).status_code == 200


def test_authentication_and_origin_checks(client):
    assert client.get("/api/state").status_code == 401
    assert client.post("/api/login", json={"token": "wrong"}).status_code == 401
    sign_in(client)
    assert client.get("/api/state").status_code == 200
    assert client.post("/api/logout", headers={"Origin": "https://attacker.invalid"}).status_code == 403
    client.post("/api/logout")
    assert client.get("/api/profile").status_code == 401


def test_uploads_are_bounded_and_keep_user_filename_out_of_path(client):
    sign_in(client)
    upload = client.post("/api/files", files={"file": ("../../team.pdf", b"team document", "application/pdf")})
    assert upload.status_code == 201
    saved = client.app.state.store.file(upload.json()["id"])
    assert saved["name"] == "team.pdf"
    assert saved["path"].startswith("uploads/") and ".." not in saved["path"]
    assert client.get("/api/files/" + saved["id"]).content == b"team document"
    assert client.post("/api/files", files={"file": ("large.pdf", b"x" * 101)}).status_code == 413
    assert client.post("/api/files", files={"file": ("empty.pdf", b"")}).status_code == 422
    assert client.post("/api/files", files={"file": ("script.html", b"<script>")}).status_code == 422
    assert len(list((client.app.state.store.root / "uploads").iterdir())) == 1


def test_duplicate_urls_and_bulk_import_results(client):
    sign_in(client)
    body = {"name": "Demo", "url": "http://127.0.0.1:8765/demo/grant#first"}
    assert client.post("/api/applications", json=body).status_code == 201
    body["url"] = "http://127.0.0.1:8765/demo/grant#second"
    assert client.post("/api/applications", json=body).status_code == 409
    imported = client.post("/api/import", json={"text": "name,url,request_amount,eligibility_confirmed\nDemo,http://127.0.0.1:8765/demo/grant,100,true\nOther,http://127.0.0.1:8765/demo/other,250,true\nBad,file:///etc/passwd,1,true"}).json()["results"]
    assert "error" in imported[0] and "id" in imported[1] and "error" in imported[2]
    other = client.app.state.store.get(imported[1]["id"])
    assert other["request_amount"] == 250 and other["eligibility_confirmed"] == 1


def test_question_answers_are_single_use_and_remembered(client):
    sign_in(client)
    store = client.app.state.store
    app_id = store.add("Demo", "http://127.0.0.1:8765/demo/grant")
    question = store.ask(app_id, "value", "budget", "Annual budget")
    result = client.post(f"/api/questions/{question}/answer", json={"value": "8500", "remember": True})
    assert result.status_code == 200
    assert store.profile()["approved_answers"]["Annual budget"] == "8500"
    assert store.get(app_id)["status"] == "queued"
    assert client.post(f"/api/questions/{question}/answer", json={"value": "9000"}).status_code == 409
    question = store.ask(app_id, "file", "letter", "Sponsor letter")
    assert client.post(f"/api/questions/{question}/answer", json={"value": "nonexistent"}).status_code == 422
    assert store.get(app_id)["status"] == "waiting"


def test_profile_and_amount_validation(client):
    sign_in(client)
    assert client.put("/api/profile", json={"attachments": {"Letter": "missing"}}).status_code == 422
    assert client.post("/api/applications", json={"name": "Bad", "url": "http://127.0.0.1:8765/demo/grant", "request_amount": -1}).status_code == 422


def test_crash_recovery_does_not_repeat_submissions(client):
    store = client.app.state.store
    before = store.add("Before", "https://example.org/before")
    after = store.add("After", "https://example.org/after")
    store.set_status(before, "running")
    store.set_status(after, "running", submit_attempted=1)
    store.recover()
    assert store.get(before)["status"] == "queued"
    assert store.get(after)["status"] == "waiting"
    question = store.snapshot()["questions"][0]
    assert question["options"] == ["mark_submitted", "skip"]
    with pytest.raises(ValueError):
        store.answer(question["id"], "retry")


@pytest.mark.asyncio
async def test_private_network_and_demo_boundaries():
    for url in ("file:///etc/passwd", "http://127.0.0.1/", "http://169.254.169.254/", "http://user:pass@example.org", "http://[::1]/"):
        with pytest.raises(ValueError):
            await validate_url(url)
    await validate_url("http://127.0.0.1:8765/demo/grant", demo=True)
    with pytest.raises(ValueError):
        await validate_url("http://127.0.0.1:8766/demo/grant", demo=True)
