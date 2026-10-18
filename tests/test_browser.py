"""Real Chromium + HTTP tests; these never contact an actual grant provider."""
import asyncio
import os
import shutil
import socket

import httpx
import pytest
import pytest_asyncio
import uvicorn
from fastapi.responses import HTMLResponse
from playwright.async_api import async_playwright

from grantrunner.app import create_app
from grantrunner.config import Settings

TOKEN = "browser-test-token-longer-than-24-characters"


@pytest_asyncio.fixture
async def server(tmp_path):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    settings = Settings(tmp_path, TOKEN, demo=True, poll_seconds=.05, demo_port=port,
                        browser_executable=os.getenv("GRANTRUNNER_BROWSER_EXECUTABLE"))
    app = create_app(settings)
    submissions = []

    @app.get("/demo/uncertain", response_class=HTMLResponse)
    def uncertain():
        return '<form action="/demo/uncertain" method="post"><label>Team name<input name="team_name" required></label><button>Submit application</button></form>'

    @app.post("/demo/uncertain", response_class=HTMLResponse)
    def uncertain_post():
        submissions.append(1)
        return "<h1>Processing your request</h1>"

    @app.get("/demo/stale-receipt", response_class=HTMLResponse)
    def stale_receipt():
        return '<h1>Application received</h1><form action="/demo/stale-receipt" method="post"><label>Team name<input name="team_name" required></label><button>Submit application</button></form>'

    @app.post("/demo/stale-receipt", response_class=HTMLResponse)
    def stale_receipt_post():
        submissions.append(1)
        return stale_receipt()

    uv_server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    task = asyncio.create_task(uv_server.serve(sockets=[sock]))
    for _ in range(100):
        if uv_server.started:
            break
        await asyncio.sleep(.05)
    assert uv_server.started
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", headers={"Authorization": f"Bearer {TOKEN}"}) as client:
        yield app, client, port, submissions
    uv_server.should_exit = True
    await asyncio.wait_for(task, 15)


async def wait_question(client, kind=None, timeout=25):
    for _ in range(int(timeout * 10)):
        state = (await client.get("/api/state")).json()
        if state["worker"]["error"] and not state["worker"]["running"]:
            pytest.fail("Browser worker stopped: " + state["worker"]["error"])
        questions = state["questions"]
        if questions and (kind is None or questions[0]["kind"] == kind):
            return questions[0]
        await asyncio.sleep(.1)
    pytest.fail("Expected question did not arrive: " + str(state))


async def wait_completed(client, app_id):
    for _ in range(150):
        state = (await client.get("/api/state")).json()
        grant = next(a for a in state["applications"] if a["id"] == app_id)
        if grant["status"] == "completed":
            return grant
        await asyncio.sleep(.1)
    pytest.fail("Application did not complete: " + str(state))


async def answer(client, q, value, remember=False):
    result = await client.post(f"/api/questions/{q['id']}/answer", json={"value": value, "remember": remember})
    assert result.status_code == 200, result.text


async def profile(client, **extra):
    body = {"facts": {"team_name": "Test Robotics", "team_number": "12345", "contact_email": "test@example.org"}, **extra}
    assert (await client.put("/api/profile", json=body)).status_code == 200


async def test_pause_answer_upload_approve_and_receive_receipt(server):
    app, client, port, _ = server
    await profile(client)
    result = await client.post("/api/applications", json={"name": "Local Grant", "url": f"http://127.0.0.1:{port}/demo/grant", "request_amount": 1500})
    app_id = result.json()["id"]
    q = await wait_question(client, "approval")
    assert q["field_key"] == "eligibility-check"
    await answer(client, q, "eligible")
    q = await wait_question(client, "value")
    assert q["label"] == "How will this grant benefit your community?"
    await answer(client, q, "We will run free robotics workshops for local students.", remember=True)
    q = await wait_question(client, "file")
    uploaded = await client.post("/api/files", files={"file": ("sponsorship.txt", b"Example team packet", "text/plain")})
    assert uploaded.status_code == 201
    await answer(client, q, uploaded.json()["id"], remember=True)
    q = await wait_question(client, "approval")
    assert q["field_key"].startswith("final-")
    screenshot = await client.get(f"/api/applications/{app_id}/screenshot")
    assert screenshot.status_code == 200 and screenshot.content[:8] == b"\x89PNG\r\n\x1a\n"
    await answer(client, q, "submit")
    result = await wait_completed(client, app_id)
    assert "DEMO-" in result["receipt"]
    assert "Test Robotics" in result["receipt"]
    assert "free robotics workshops" in result["receipt"]
    assert "sponsorship.txt" in result["receipt"]
    assert "19 bytes" in result["receipt"]
    assert result["submit_attempted"] == 1
    assert not (await client.get("/api/state")).json()["questions"]


async def test_standing_authorization_completes_without_questions(server):
    app, client, port, _ = server
    saved = (await client.post("/api/files", files={"file": ("packet.txt", b"Team packet")})).json()
    await profile(client, approved_answers={"How will this grant benefit your community?": "Free engineering workshops."}, attachments={"Sponsorship packet": saved["id"]}, authorization={"auto_submit": True, "max_request_amount": 2000})
    result = await client.post("/api/applications", json={"name": "Automatic demo", "url": f"http://127.0.0.1:{port}/demo/grant", "request_amount": 1000, "eligibility_confirmed": True})
    result = await wait_completed(client, result.json()["id"])
    assert "1000" in result["receipt"]
    assert not (await client.get("/api/state")).json()["questions"]


async def test_uncertain_submission_is_not_retried(server):
    app, client, port, submissions = server
    await profile(client, authorization={"auto_submit": True, "max_request_amount": 2000})
    result = await client.post("/api/applications", json={"name": "Ambiguous portal", "url": f"http://127.0.0.1:{port}/demo/uncertain", "request_amount": 1000, "eligibility_confirmed": True})
    app_id = result.json()["id"]
    q = await wait_question(client, "manual")
    assert q["options"] == ["mark_submitted", "skip"]
    assert submissions == [1]
    assert (await client.post(f"/api/questions/{q['id']}/answer", json={"value": "retry"})).status_code == 422
    await answer(client, q, "mark_submitted")
    await wait_completed(client, app_id)
    assert submissions == [1]


async def test_amount_outside_standing_permission_requires_approval(server):
    app, client, port, _ = server
    saved = (await client.post("/api/files", files={"file": ("packet.txt", b"Team packet")})).json()
    await profile(client, approved_answers={"How will this grant benefit your community?": "Engineering workshops."}, attachments={"Sponsorship packet": saved["id"]}, authorization={"auto_submit": True, "max_request_amount": 500})
    result = await client.post("/api/applications", json={"name": "Over limit", "url": f"http://127.0.0.1:{port}/demo/grant", "request_amount": 1000, "eligibility_confirmed": True})
    app_id = result.json()["id"]
    q = await wait_question(client, "approval")
    assert q["field_key"].startswith("final-")
    assert app.state.store.get(app_id)["submit_attempted"] == 0
    await answer(client, q, "skip")
    for _ in range(30):
        if app.state.store.get(app_id)["status"] == "skipped":
            break
        await asyncio.sleep(.1)
    assert app.state.store.get(app_id)["status"] == "skipped"


async def test_preexisting_receipt_text_does_not_confirm_a_submission(server):
    app, client, port, submissions = server
    await profile(client, authorization={"auto_submit": True, "max_request_amount": 2000})
    result = await client.post("/api/applications", json={"name": "Stale confirmation", "url": f"http://127.0.0.1:{port}/demo/stale-receipt", "request_amount": 1000, "eligibility_confirmed": True})
    q = await wait_question(client, "manual")
    assert q["field_key"] == "submission-stale-receipt"
    assert app.state.store.get(result.json()["id"])["status"] == "waiting"
    assert submissions == [1]


async def test_dashboard_login_profile_and_add_dialog(server):
    app, client, port, _ = server
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path=app.state.settings.browser_executable)
        page = await browser.new_page(viewport={"width": 1440, "height": 1100})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.goto(f"http://127.0.0.1:{port}")
        await page.locator("#token").fill(TOKEN)
        await page.get_by_role("button", name="Connect to your worker").click()
        await page.locator("#shell").wait_for(state="visible")
        await page.get_by_role("button", name="Team profile", exact=False).click()
        await page.get_by_role("button", name="Load example facts").click()
        await page.get_by_role("button", name="Save team profile").click()
        await page.get_by_role("button", name="Overview", exact=False).click()
        await page.get_by_role("button", name="Try the demo").click()
        assert await page.locator("#grant-url").input_value() == f"http://127.0.0.1:{port}/demo/grant"
        await page.locator("#grant-amount").fill("")
        await page.get_by_role("button", name="Add to the queue").click()
        await page.get_by_role("button", name="Our team is eligible").wait_for()
        assert app.state.store.profile()["facts"]["team_number"] == "12345"
        await page.screenshot(path=str(app.state.store.root / "dashboard-desktop.png"), full_page=True)
        await page.set_viewport_size({"width": 390, "height": 844})
        assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        await page.screenshot(path=str(app.state.store.root / "dashboard-mobile.png"), full_page=True)
        # Finish through the actual laptop UI, including a missing numeric fact
        # and a document transferred to the PC before browser upload.
        await page.get_by_role("button", name="Our team is eligible").click()
        await page.get_by_role("textbox", name="Amount requested", exact=True).fill("850")
        await page.get_by_role("button", name="Send & resume").click()
        await page.get_by_role("textbox", name="How will this grant benefit your community?", exact=True).fill("We will offer free robotics workshops.")
        await page.get_by_role("button", name="Send & resume").click()
        await page.get_by_label("Upload a new document").set_input_files({"name": "team-packet.pdf", "mimeType": "application/pdf", "buffer": b"%PDF-1.4\nFictional test packet"})
        await page.locator('.question select option[value]:not([value=""])').wait_for(state="attached")
        await page.get_by_role("button", name="Send & resume").click()
        await page.get_by_role("button", name="Approve & submit").click()
        grant = app.state.store.snapshot()["applications"][0]
        result = await wait_completed(client, grant["id"])
        assert "amount: 850" in result["receipt"]
        assert "team-packet.pdf" in result["receipt"]
        assert not errors
        await browser.close()
