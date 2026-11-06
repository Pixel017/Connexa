"""Connexa configuration."""
import os
import hashlib


def _env(name: str, default: str) -> str:
    return os.getenv(name, default)


class Settings:
    admin_token: str = _env("CONNEXA_ADMIN_TOKEN", "dev-admin")
    admin_cookie_value: str = hashlib.sha256(f"{admin_token}:connexa".encode()).hexdigest()[:32]

    slack_token: str = _env("CONNEXA_SLACK_TOKEN", "").strip()
    notion_token: str = _env("CONNEXA_NOTION_TOKEN", "").strip()
    github_token: str = _env("CONNEXA_GITHUB_TOKEN", "").strip()
    gmail_token: str = _env("CONNEXA_GMAIL_TOKEN", "").strip()

    openai_key: str = _env("OPENAI_API_KEY", "").strip()
    anthropic_key: str = _env("ANTHROPIC_API_KEY", "").strip()

    # OAuth "social login" lane — register free apps at each provider and paste ids here
    redirect_base: str = _env("CONNEXA_REDIRECT_BASE", "http://localhost:8000").rstrip("/")
    github_oauth: dict = {"client_id": _env("CONNEXA_GITHUB_OAUTH_CLIENT_ID", ""),
                          "client_secret": _env("CONNEXA_GITHUB_OAUTH_CLIENT_SECRET", "")}
    slack_oauth: dict = {"client_id": _env("CONNEXA_SLACK_CLIENT_ID", ""),
                         "client_secret": _env("CONNEXA_SLACK_CLIENT_SECRET", "")}
    notion_oauth: dict = {"client_id": _env("CONNEXA_NOTION_CLIENT_ID", ""),
                          "client_secret": _env("CONNEXA_NOTION_CLIENT_SECRET", "")}
    gmail_oauth: dict = {"client_id": _env("CONNEXA_GMAIL_CLIENT_ID", ""),
                         "client_secret": _env("CONNEXA_GMAIL_CLIENT_SECRET", "")}

    # Optional offline-override for the OAuth provider base URLs (testing)
    oauth_base_github: str = _env("CONNEXA_OAUTH_BASE_GITHUB", "").rstrip("/")
    github_api_base: str = _env("CONNEXA_GITHUB_API_BASE", "https://api.github.com").rstrip("/")

    db_path: str = _env("CONNEXA_DB", os.path.join(os.path.dirname(os.path.dirname(__file__)), "connexa.db"))

    # Demo defaults
    seed_org_name: str = "Acme Inc"
    seed_org_slug: str = "acme-inc"
    monthly_budget_credits: int = 10_000


settings = Settings()
