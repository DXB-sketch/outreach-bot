"""Settings, loaded from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def load_dotenv(path: str | Path = ".env") -> None:
    """Minimal .env loader. Existing environment variables win."""
    p = Path(path)
    if not p.exists():
        return
    for raw in p.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _list(name: str) -> list[str]:
    return [s.strip() for s in os.environ.get(name, "").split(",") if s.strip()]


@dataclass
class Settings:
    data_dir: Path
    # LLM (any OpenAI-compatible endpoint, e.g. freellmapi.co)
    llm_base_url: str
    llm_api_key: str
    llm_fast_models: list[str]
    llm_strong_models: list[str]
    llm_min_interval: float
    # Discovery
    google_places_api_key: str
    # Sender identity (used in drafts and the crawler User-Agent)
    sender_name: str
    sender_business: str
    sender_email: str
    sender_phone: str
    sender_website: str
    sender_location: str
    # Pipeline
    threshold: int

    @property
    def db_path(self) -> Path:
        return self.data_dir / "outreach.db"

    @property
    def drafts_dir(self) -> Path:
        return self.data_dir / "drafts"

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_base_url and self.llm_api_key and self.llm_fast_models)

    @property
    def user_agent(self) -> str:
        contact = self.sender_email or self.sender_website or "no-contact-set"
        return f"MonolithOutreachBot/0.1 (+{contact})"


def get_settings() -> Settings:
    load_dotenv()
    fast = _list("LLM_FAST_MODELS")
    strong = _list("LLM_STRONG_MODELS") or fast
    return Settings(
        data_dir=Path(os.environ.get("OUTREACH_DATA_DIR", "data")),
        llm_base_url=os.environ.get("LLM_BASE_URL", "").rstrip("/"),
        llm_api_key=os.environ.get("LLM_API_KEY", ""),
        llm_fast_models=fast,
        llm_strong_models=strong,
        llm_min_interval=float(os.environ.get("LLM_MIN_INTERVAL", "2")),
        google_places_api_key=os.environ.get("GOOGLE_PLACES_API_KEY", ""),
        sender_name=os.environ.get("SENDER_NAME", "[Your name]"),
        sender_business=os.environ.get("SENDER_BUSINESS", "Monolith Web Studio"),
        sender_email=os.environ.get("SENDER_EMAIL", ""),
        sender_phone=os.environ.get("SENDER_PHONE", ""),
        sender_website=os.environ.get("SENDER_WEBSITE", ""),
        sender_location=os.environ.get("SENDER_LOCATION", "Wamuran, QLD"),
        threshold=int(os.environ.get("SCORE_THRESHOLD", "60")),
    )
