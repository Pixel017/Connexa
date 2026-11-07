"""Connector registry: slugs -> connectors; data-plane providers per category."""
from __future__ import annotations

from ..config import settings
from .base import Connector
from .free_live import WeatherConnector, WikipediaConnector
from .real_slack import SlackConnector
from .real_notion import NotionConnector
from .real_gmail import GmailConnector
from .real_github import GitHubConnector
from .simulated import (JiraToolsConnector, HubSpotLikeConnector, HelpdeskLikeConnector,
                        SalespodLikeConnector, PeopleOsConnector, LedgerlyConnector)
from ..common_models import DATA_CATEGORIES


class ConnectorRegistry:
    def __init__(self) -> None:
        self._by_slug: dict[str, Connector] = {}
        for connector in (
            WeatherConnector(),
            WikipediaConnector(),
            JiraToolsConnector(),
            HubSpotLikeConnector(),
            HelpdeskLikeConnector(),
            SalespodLikeConnector(),
            PeopleOsConnector(),
            LedgerlyConnector(),
            SlackConnector(settings.slack_token, settings.slack_oauth),
            NotionConnector(settings.notion_token, settings.notion_oauth),
            GmailConnector(settings.gmail_token, settings.gmail_oauth),
            GitHubConnector(settings.github_token, settings.github_oauth,
                            api_base=settings.github_api_base,
                            authorize_url=(f"{settings.oauth_base_github}/login/oauth/authorize"
                                           if settings.oauth_base_github else ""),
                            token_url=(f"{settings.oauth_base_github}/login/oauth/access_token"
                                       if settings.oauth_base_github else "")),
        ):
            self._by_slug[connector.slug] = connector

    # --- lookups ---------------------------------------------------------------
    def get(self, slug: str) -> Connector | None:
        return self._by_slug.get(slug)

    def require(self, slug: str) -> Connector:
        connector = self._by_slug.get(slug)
        if not connector:
            raise KeyError(f"unknown connector {slug}")
        return connector

    def list_connectors(self) -> list[Connector]:
        return list(self._by_slug.values())

    # --- data-plane providers (connectors that can back a LinkedAccount) --------
    def data_plane_providers(self, category: str) -> list[Connector]:
        return [c for c in self._by_slug.values()
                if category in DATA_CATEGORIES and c.generate_sample(category)]


registry = ConnectorRegistry()
