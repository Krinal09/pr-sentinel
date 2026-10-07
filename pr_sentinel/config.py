"""Runtime settings, read from environment variables (or a .env file)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

DEFAULT_IGNORE = [
    "*.lock", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
    "*.min.js", "*.min.css", "*.map", "*.svg", "*.png", "*.jpg", "*.jpeg", "*.gif",
    "*.ico", "*.pdf", "*.zip", "dist/*", "build/*", "node_modules/*", "vendor/*",
]

SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def _load_dotenv(path: str = ".env") -> None:
    """Tiny .env loader so we don't need python-dotenv."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    # LLM
    llm_provider: str = "groq"
    llm_model: str = "llama-3.3-70b-versatile"
    llm_temperature: float = 0.1

    # GitHub App (webhook / Lambda mode)
    github_app_id: str = ""
    github_private_key: str = ""
    github_webhook_secret: str = ""

    # Plain token (GitHub Actions / local CLI mode)
    github_token: str = ""
    github_api_url: str = "https://api.github.com"

    # Review behaviour
    max_files: int = 25
    max_patch_chars: int = 12_000
    min_severity: str = "low"
    ignore_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_IGNORE))

    # RAG
    enable_rag: bool = True
    rag_top_k: int = 4
    rag_max_repo_files: int = 400
    embedding_model: str = "BAAI/bge-small-en-v1.5"

    @classmethod
    def from_env(cls) -> "Settings":
        _load_dotenv()
        private_key = os.getenv("GITHUB_PRIVATE_KEY", "")
        key_path = os.getenv("GITHUB_PRIVATE_KEY_PATH")
        if not private_key and key_path and os.path.exists(key_path):
            with open(key_path, encoding="utf-8") as fh:
                private_key = fh.read()
        extra_ignore = [p.strip() for p in os.getenv("IGNORE_PATTERNS", "").split(",") if p.strip()]
        return cls(
            llm_provider=os.getenv("LLM_PROVIDER", cls.llm_provider),
            llm_model=os.getenv("LLM_MODEL", cls.llm_model),
            llm_temperature=float(os.getenv("LLM_TEMPERATURE", cls.llm_temperature)),
            github_app_id=os.getenv("GITHUB_APP_ID", ""),
            github_private_key=private_key.replace("\\n", "\n"),
            github_webhook_secret=os.getenv("GITHUB_WEBHOOK_SECRET", ""),
            github_token=os.getenv("GITHUB_TOKEN", ""),
            github_api_url=os.getenv("GITHUB_API_URL", cls.github_api_url).rstrip("/"),
            max_files=int(os.getenv("MAX_FILES", cls.max_files)),
            max_patch_chars=int(os.getenv("MAX_PATCH_CHARS", cls.max_patch_chars)),
            min_severity=os.getenv("MIN_SEVERITY", cls.min_severity).lower(),
            ignore_patterns=DEFAULT_IGNORE + extra_ignore,
            enable_rag=_bool("ENABLE_RAG", True),
            rag_top_k=int(os.getenv("RAG_TOP_K", cls.rag_top_k)),
            rag_max_repo_files=int(os.getenv("RAG_MAX_REPO_FILES", cls.rag_max_repo_files)),
            embedding_model=os.getenv("EMBEDDING_MODEL", cls.embedding_model),
        )
