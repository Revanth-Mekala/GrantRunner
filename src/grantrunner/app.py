import asyncio
import csv
import hashlib
import hmac
import io
import logging
import os
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from .browser import Worker, validate_url
from .config import Settings
from .store import Conflict, Store


class Login(BaseModel):
    token: str


class Authorization(BaseModel):
    auto_submit: bool = False
    max_request_amount: float = Field(default=0, ge=0, le=1_000_000, allow_inf_nan=False)
    allow_ai_drafts: bool = False


class Profile(BaseModel):
    facts: dict[str, str] = Field(default_factory=dict)
    approved_answers: dict[str, str] = Field(default_factory=dict)
    attachments: dict[str, str] = Field(default_factory=dict)
    authorization: Authorization = Field(default_factory=Authorization)

    @field_validator("facts", "approved_answers", "attachments")
    @classmethod
    def bounded(cls, value):
        if len(value) > 200 or any(len(k) > 500 or len(v) > 20000 for k, v in value.items()):
            raise ValueError("Profile entries exceed the size limit")
        return value


class ApplicationIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    url: str = Field(max_length=4096)
    request_amount: float | None = Field(default=None, ge=0, le=1_000_000, allow_inf_nan=False)
    eligibility_confirmed: bool = False


class AnswerIn(BaseModel):
    value: str = Field(min_length=1, max_length=20000)
    remember: bool = False


class ImportIn(BaseModel):
    text: str = Field(min_length=1, max_length=100000)


def create_app(settings=None, *, start_worker=True):
    settings = settings or Settings.load()
    store = Store(settings.data_dir)
    worker = Worker(store, settings)

    async def supervise():
        try:
            await worker.run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            worker.last_error = type(exc).__name__
            logging.getLogger("grantrunner").error("Worker stopped (%s). Check browser installation and restart GrantRunner.", type(exc).__name__)

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(supervise()) if start_worker else None
        app.state.worker_task = task
        yield
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    app = FastAPI(title="GrantRunner", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    app.state.worker = worker
    app.state.settings = settings
    static = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.middleware("http")
    async def safety_headers(request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS") and request.url.path.startswith("/api/"):
            origin = request.headers.get("origin")
            if origin and origin != f"{request.url.scheme}://{request.url.netloc}":
                return Response("Cross-origin requests are not allowed", status_code=403)
            length = request.headers.get("content-length")
            try:
                if length and int(length) > settings.max_upload_bytes + 1024 * 1024:
                    return Response("Request is too large", status_code=413)
            except ValueError:
                return Response("Invalid Content-Length", status_code=400)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    def authenticated(request: Request):
        bearer = request.headers.get("authorization", "")
        candidate = bearer[7:] if bearer.startswith("Bearer ") else request.cookies.get("gr_session", "")
        if not hmac.compare_digest(candidate.encode(), settings.token.encode()):
            raise HTTPException(401, "Sign in with the PC's dashboard token")

    auth = [Depends(authenticated)]

    @app.exception_handler(Conflict)
    async def conflict_handler(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.get("/")
    def index():
        return FileResponse(static / "index.html")

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/api/login")
    def login(body: Login, request: Request, response: Response):
        if not hmac.compare_digest(body.token.encode(), settings.token.encode()):
            raise HTTPException(401, "Incorrect dashboard token")
        response.set_cookie("gr_session", settings.token, httponly=True, samesite="strict", secure=request.url.scheme == "https", max_age=60 * 60 * 24 * 7)
        return {"ok": True}

    @app.post("/api/logout", dependencies=auth)
    def logout(response: Response):
        response.delete_cookie("gr_session")
        return {"ok": True}

    @app.get("/api/state", dependencies=auth)
    def state(request: Request):
        task = getattr(app.state, "worker_task", None)
        return {**store.snapshot(), "worker": {"running": task is not None and not task.done(), "current": worker.current, "error": worker.last_error, "headless": settings.headless}, "demo_url": f"http://127.0.0.1:{settings.demo_port}/demo/grant" if settings.demo else None, "ai_available": bool(os.getenv("GRANTRUNNER_LLM_API_KEY"))}

    @app.get("/api/profile", dependencies=auth)
    def get_profile():
        return store.profile()

    @app.put("/api/profile", dependencies=auth)
    def put_profile(body: Profile):
        for file_id in body.attachments.values():
            if not store.file(file_id):
                raise HTTPException(422, "A profile attachment does not exist")
        store.save_profile(body.model_dump())
        return {"ok": True}

    async def add_application(body):
        try:
            await validate_url(body.url, settings.demo, settings.demo_port)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        parsed = urlsplit(body.url)
        # Fragments are navigation hints, not distinct applications.
        url = urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/", parsed.query, ""))
        return store.add(body.name, url, body.request_amount, body.eligibility_confirmed)

    @app.post("/api/applications", dependencies=auth, status_code=201)
    async def add(body: ApplicationIn):
        return {"id": await add_application(body)}

    @app.post("/api/import", dependencies=auth)
    async def import_urls(body: ImportIn):
        lines = [line.strip() for line in body.text.strip().splitlines() if line.strip()]
        rows = [{"url": line} for line in lines] if lines[0].startswith(("https://", "http://")) else list(csv.DictReader(io.StringIO(body.text.strip())))
        if len(rows) > 100:
            raise HTTPException(422, "Import at most 100 applications at a time")
        results = []
        for row in rows:
            try:
                url = row.get("url", "").strip()
                amount = row.get("request_amount", "").strip()
                body_in = ApplicationIn(name=row.get("name") or urlsplit(url).hostname or "Grant", url=url, request_amount=float(amount) if amount else None, eligibility_confirmed=str(row.get("eligibility_confirmed", "false")).lower() == "true")
                results.append({"url": url, "id": await add_application(body_in)})
            except (ValueError, HTTPException, Conflict) as exc:
                results.append({"url": row.get("url", ""), "error": exc.detail if isinstance(exc, HTTPException) else str(exc)})
        return {"results": results}

    @app.post("/api/applications/{app_id}/skip", dependencies=auth)
    async def skip(app_id: str):
        grant = store.get(app_id)
        if not grant:
            raise HTTPException(404, "Application not found")
        if grant["status"] == "running":
            raise HTTPException(409, "The worker is taking an action. Wait for a question or completion before skipping.")
        if grant["status"] == "completed":
            raise HTTPException(409, "This application has already completed")
        store.skip(app_id)
        return {"ok": True}

    @app.post("/api/questions/{question_id}/answer", dependencies=auth)
    def answer(question_id: str, body: AnswerIn):
        try:
            store.answer(question_id, body.value, body.remember)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        return {"ok": True}

    @app.post("/api/files", dependencies=auth, status_code=201)
    async def upload(file: Annotated[UploadFile, File()]):
        filename = (file.filename or "attachment").replace("\\", "/").split("/")[-1][:200]
        suffix = Path(filename).suffix.lower()
        if suffix not in {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".txt", ".csv", ".docx", ".xlsx", ".pptx"}:
            raise HTTPException(422, "Use a PDF, image, text, CSV, or Office document")
        file_id = uuid4().hex
        relative = f"uploads/{file_id}{suffix}"
        path = store.root / relative
        size, digest = 0, hashlib.sha256()
        try:
            with path.open("xb") as output:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > settings.max_upload_bytes:
                        raise HTTPException(413, "Files must be 20 MB or smaller")
                    digest.update(chunk)
                    output.write(chunk)
            if not size:
                raise HTTPException(422, "The file is empty")
            store.add_file(file_id, filename, relative, size, digest.hexdigest())
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        finally:
            await file.close()
        return {"id": file_id, "name": filename, "size": size}

    @app.get("/api/files/{file_id}", dependencies=auth)
    def download(file_id: str):
        saved = store.file(file_id)
        if not saved:
            raise HTTPException(404, "File not found")
        return FileResponse(store.root / saved["path"], filename=saved["name"], media_type="application/octet-stream")

    @app.get("/api/applications/{app_id}/screenshot", dependencies=auth)
    def screenshot(app_id: str):
        grant = store.get(app_id)
        if not grant or not grant["screenshot"]:
            raise HTTPException(404, "No screenshot is available yet")
        return FileResponse(store.root / "screenshots" / grant["screenshot"], media_type="image/png")

    if settings.demo:
        from .demo import router
        app.include_router(router)
    return app
