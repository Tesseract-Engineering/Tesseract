"""GitHub: a read-only GitHub App (or token) over the REST API."""

from .connector import GitHubConnector
from .normalize import GitHubNormalizer

__all__ = ["GitHubConnector", "GitHubNormalizer"]
