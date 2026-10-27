"""Shared pipeline stages. Only enumerate and fetch are source-specific; every
source runs redact, ACL, write and manifest from here."""
