from dataclasses import dataclass
from pathlib import Path
import os
import secrets


@dataclass
class Settings:
    data_dir: Path
    token: str
    demo: bool = False
    headless: bool = True
    poll_seconds: float = 2
    max_upload_bytes: int = 20 * 1024 * 1024
    demo_port: int = 8765
    browser_executable: str | None = None

    @classmethod
    def load(cls):
        root = Path(os.environ.get("GRANTRUNNER_DATA_DIR", ".grantrunner")).resolve()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        token_file = root / "dashboard-token"
        token = os.environ.get("GRANTRUNNER_TOKEN")
        if not token:
            if not token_file.exists():
                fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as stream:
                    stream.write(secrets.token_urlsafe(32))
            token = token_file.read_text().strip()
        if len(token) < 24:
            raise ValueError("Dashboard token must contain at least 24 characters")
        return cls(root, token, os.getenv("GRANTRUNNER_DEMO") == "1", os.getenv("GRANTRUNNER_HEADLESS", "1") != "0", demo_port=int(os.getenv("GRANTRUNNER_PORT", "8765")), browser_executable=os.getenv("GRANTRUNNER_BROWSER_EXECUTABLE"))
