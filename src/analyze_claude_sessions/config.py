"""Runtime configuration, resolved from the environment (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PACKAGE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parents[1]

DEFAULT_REPORT_SCRIPT = REPO_ROOT / "scripts" / "cache_report_per_call_timing.sh"
DEFAULT_SQL_DIR = REPO_ROOT / "sql"


def _path_env(name: str, default: Path) -> Path:
    raw = os.getenv(name)
    return Path(raw).expanduser() if raw else default


@dataclass(frozen=True, slots=True)
class Settings:
    """Everything the pipeline needs to know about its surroundings."""

    projects_dir: Path
    data_dir: Path
    csv_dir: Path
    parquet_dir: Path
    parquet_dir_in_db: str
    report_script: Path
    sql_dir: Path
    database_url: str
    nlp_embeddings_cache_dir: Path
    # Read-only role for `acp ask`; from_env falls back to `database_url`.
    ask_database_url: str = ""
    # `acp ask --llm`: model alias and whether a key is set (the key itself is never stored).
    ask_llm_model: str = "claude-opus-5-5"
    # True only when ASK_DATABASE_URL is set in the environment (not the owner-URL fallback).
    ask_database_url_explicit: bool = False
    anthropic_api_key_set: bool = False

    @classmethod
    def from_env(cls) -> Settings:
        data_dir = _path_env("DATA_DIR", REPO_ROOT / "data")
        database_url = os.getenv("DATABASE_URL") or _database_url_from_parts()
        return cls(
            projects_dir=_path_env("CLAUDE_PROJECTS_DIR", Path.home() / ".claude" / "projects"),
            data_dir=data_dir,
            csv_dir=_path_env("CSV_DIR", data_dir / "csv"),
            parquet_dir=_path_env("PARQUET_DIR", data_dir / "parquet"),
            # Where the *server* sees the parquet bind mount; COPY ... TO runs
            # inside the postgres container, not next to this process.
            parquet_dir_in_db=os.getenv("PARQUET_DIR_IN_DB", "/parquet"),
            report_script=_path_env("REPORT_SCRIPT", DEFAULT_REPORT_SCRIPT),
            sql_dir=_path_env("SQL_DIR", DEFAULT_SQL_DIR),
            database_url=database_url,
            # Read-only role for `acp ask`; falls back to the owner role.
            ask_database_url=os.getenv("ASK_DATABASE_URL") or database_url,
            nlp_embeddings_cache_dir=_path_env("NLP_EMBEDDINGS_CACHE_DIR", data_dir / "nlp_cache"),
            ask_database_url_explicit=bool(os.getenv("ASK_DATABASE_URL")),
            ask_llm_model=os.getenv("ACP_ASK_LLM_MODEL") or "claude-opus-5-5",
            anthropic_api_key_set=bool(os.getenv("ANTHROPIC_API_KEY")),
        )

    def ensure_dirs(self) -> None:
        self.csv_dir.mkdir(parents=True, exist_ok=True)
        self.parquet_dir.mkdir(parents=True, exist_ok=True)


def _database_url_from_parts() -> str:
    user = os.getenv("POSTGRES_USER", "claude")
    password = os.getenv("POSTGRES_PASSWORD", "claude")
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "55432")
    database = os.getenv("POSTGRES_DB", "claude_analytics")
    return f"postgresql://{user}:{password}@{host}:{port}/{database}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()
