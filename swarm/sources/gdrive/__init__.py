"""Google Drive: a read-only service account over the Drive API."""

from .connector import DriveConnector
from .normalize import DriveNormalizer

__all__ = ["DriveConnector", "DriveNormalizer"]
