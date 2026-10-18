"""A deliberately local grant portal for exercising the whole workflow."""
from html import escape
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from starlette.datastructures import UploadFile

router = APIRouter(prefix="/demo")


@router.get("/grant", response_class=HTMLResponse)
def grant():
    return '''<!doctype html><html lang="en"><meta charset="utf-8"><title>Example Community Robotics Grant</title>
    <h1>Example Community Robotics Grant</h1>
    <p>This is a fictional local demonstration. Nothing is sent to a sponsor.</p>
    <p>Eligibility: FTC teams requesting at most $5,000 for community robotics education.</p>
    <form method="post" enctype="multipart/form-data" action="/demo/receipt">
    <p><label>Team name <input name="team_name" required></label></p>
    <p><label>FTC team number <input name="team_number" required></label></p>
    <p><label>Contact email <input name="email" type="email" required></label></p>
    <p><label>Amount requested <input name="amount" type="number" min="1" max="5000" required></label></p>
    <p><label>How will this grant benefit your community? <textarea name="impact" maxlength="1000" required></textarea></label></p>
    <p><label>Sponsorship packet <input name="packet" type="file" accept=".pdf,.png,.jpg,.txt" required></label></p>
    <button type="submit">Submit application</button></form></html>'''


@router.post("/receipt", response_class=HTMLResponse)
async def receipt(request: Request):
    form = await request.form()
    fields = []
    for key, value in form.multi_items():
        if isinstance(value, UploadFile):
            content = await value.read()
            fields.append(f"{escape(key)}: {escape(value.filename or '')} ({len(content)} bytes)")
            await value.close()
        else:
            fields.append(f"{escape(key)}: {escape(str(value))}")
    return f"<!doctype html><html><title>Application received</title><h1>Thank you for applying!</h1><p>Application received. Confirmation: DEMO-{uuid4().hex[:10]}</p><p>Fictional local submission only.</p><pre>{chr(10).join(fields)}</pre></html>"
