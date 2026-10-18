import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


class Conflict(Exception):
    pass


class Store:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        for directory in ("uploads", "screenshots", "sessions"):
            (root / directory).mkdir(exist_ok=True, mode=0o700)
        self.db = root / "applications.sqlite3"
        with self.connect() as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS applications (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, url TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'queued', detail TEXT NOT NULL DEFAULT '',
                request_amount REAL, eligibility_confirmed INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                screenshot TEXT, receipt TEXT, submit_attempted INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS questions (
                id TEXT PRIMARY KEY, application_id TEXT NOT NULL REFERENCES applications(id),
                kind TEXT NOT NULL, field_key TEXT NOT NULL, label TEXT NOT NULL,
                context TEXT NOT NULL, options TEXT NOT NULL, answer TEXT,
                created_at TEXT NOT NULL, answered_at TEXT);
            CREATE TABLE IF NOT EXISTS files (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, path TEXT NOT NULL,
                size INTEGER NOT NULL, sha256 TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, application_id TEXT,
                message TEXT NOT NULL, created_at TEXT NOT NULL);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def profile(self):
        with self.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key='profile'").fetchone()
        return json.loads(row[0]) if row else {"facts": {}, "approved_answers": {}, "attachments": {}, "authorization": {"auto_submit": False}}

    def save_profile(self, value):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES ('profile',?)", (json.dumps(value),))

    def add(self, name, url, amount=None, eligibility_confirmed=False):
        app_id = uuid.uuid4().hex
        with self.connect() as db:
            try:
                db.execute("INSERT INTO applications (id,name,url,request_amount,eligibility_confirmed,created_at,updated_at) VALUES (?,?,?,?,?,?,?)", (app_id, name, url, amount, int(eligibility_confirmed), now(), now()))
            except sqlite3.IntegrityError:
                raise Conflict("This URL is already in your application list")
            self._event(db, app_id, "Application queued")
        return app_id

    def get(self, app_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
        return dict(row) if row else None

    def snapshot(self):
        with self.connect() as db:
            apps = [dict(r) for r in db.execute("SELECT * FROM applications ORDER BY created_at DESC")]
            questions = [dict(r) for r in db.execute("SELECT * FROM questions WHERE answered_at IS NULL ORDER BY created_at")]
            events = [dict(r) for r in db.execute("SELECT * FROM events ORDER BY id DESC LIMIT 80")]
            files = [dict(r) for r in db.execute("SELECT id,name,size FROM files")]
        for question in questions:
            question["options"] = json.loads(question["options"])
        return {"applications": apps, "questions": questions, "events": events, "files": files}

    def set_status(self, app_id, status, detail="", **extra):
        allowed = {"screenshot", "receipt", "submit_attempted"}
        assert extra.keys() <= allowed
        updates = {"status": status, "detail": detail, "updated_at": now(), **extra}
        with self.connect() as db:
            db.execute("UPDATE applications SET " + ",".join(f"{k}=?" for k in updates) + " WHERE id=?", (*updates.values(), app_id))
            self._event(db, app_id, detail or status)

    def _event(self, db, app_id, message):
        db.execute("INSERT INTO events(application_id,message,created_at) VALUES (?,?,?)", (app_id, message, now()))

    def event(self, app_id, message):
        with self.connect() as db:
            self._event(db, app_id, message)

    def ask(self, app_id, kind, key, label, context="", options=None):
        question_id = uuid.uuid4().hex
        with self.connect() as db:
            existing = db.execute("SELECT id FROM questions WHERE application_id=? AND field_key=? AND answered_at IS NULL", (app_id, key)).fetchone()
            if existing:
                return existing[0]
            db.execute("INSERT INTO questions(id,application_id,kind,field_key,label,context,options,created_at) VALUES (?,?,?,?,?,?,?,?)", (question_id, app_id, kind, key, label, context, json.dumps(options or []), now()))
            db.execute("UPDATE applications SET status='waiting',detail=?,updated_at=? WHERE id=?", (label, now(), app_id))
            self._event(db, app_id, "Needs your input: " + label)
        return question_id

    def answer(self, question_id, value, remember=False):
        with self.connect() as db:
            q = db.execute("SELECT * FROM questions WHERE id=? AND answered_at IS NULL", (question_id,)).fetchone()
            if not q:
                raise Conflict("This question was already answered or is no longer active")
            app = db.execute("SELECT * FROM applications WHERE id=?", (q["application_id"],)).fetchone()
            if app["status"] != "waiting":
                raise Conflict("This application is no longer waiting for a response")
            if q["kind"] in ("approval", "manual") and value not in json.loads(q["options"]):
                raise ValueError("Choose one of the available actions")
            if q["kind"] == "file" and not db.execute("SELECT id FROM files WHERE id=?", (value,)).fetchone():
                raise ValueError("Upload or select a file first")
            if q["kind"] == "choice" and value not in json.loads(q["options"]):
                raise ValueError("Choose an available form option")
            if not str(value).strip():
                raise ValueError("A response is required")
            db.execute("UPDATE questions SET answer=?,answered_at=? WHERE id=?", (json.dumps(value), now(), question_id))
            db.execute("UPDATE applications SET status='queued',updated_at=? WHERE id=?", (now(), q["application_id"]))
            if remember and q["kind"] in ("value", "choice", "file"):
                row = db.execute("SELECT value FROM settings WHERE key='profile'").fetchone()
                profile = json.loads(row[0]) if row else {"facts": {}, "approved_answers": {}, "attachments": {}, "authorization": {"auto_submit": False}}
                bucket = "attachments" if q["kind"] == "file" else "approved_answers"
                profile.setdefault(bucket, {})[q["label"]] = value
                db.execute("INSERT OR REPLACE INTO settings VALUES ('profile',?)", (json.dumps(profile),))
            self._event(db, q["application_id"], "Response received; application queued to resume")

    def answers(self, app_id):
        with self.connect() as db:
            rows = db.execute("SELECT field_key,answer FROM questions WHERE application_id=? AND answered_at IS NOT NULL ORDER BY answered_at", (app_id,)).fetchall()
        return {r[0]: json.loads(r[1]) for r in rows}

    def file(self, file_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
        return dict(row) if row else None

    def add_file(self, file_id, name, path, size, digest):
        with self.connect() as db:
            db.execute("INSERT INTO files VALUES (?,?,?,?,?)", (file_id, name, path, size, digest))

    def skip(self, app_id):
        self.set_status(app_id, "skipped", "Skipped by user")
        with self.connect() as db:
            db.execute("DELETE FROM questions WHERE application_id=? AND answered_at IS NULL", (app_id,))

    def recover(self):
        # Never replay a final submission after a crash or ambiguous network response.
        with self.connect() as db:
            rows = db.execute("SELECT id,submit_attempted FROM applications WHERE status='running'").fetchall()
        for app in rows:
            if app["submit_attempted"]:
                self.ask(app["id"], "manual", "submission-recovery", "Was this application submitted?", "The worker stopped after a submission attempt. Check the portal or confirmation email before choosing an action.", ["mark_submitted", "skip"])
            else:
                self.set_status(app["id"], "queued", "Worker restarted; resuming saved answers")
