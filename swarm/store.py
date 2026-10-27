"""
store.py - the staging bucket, behind one small interface.

There is deliberately no delete. No pipeline role may delete in the customer
VPC, and keeping the method off the interface means no code path can try.
A failed run's partial files are simply overwritten on retry.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol


class ObjectStore(Protocol):
    def put(self, key: str, data: bytes) -> None: ...
    def get(self, key: str) -> bytes | None: ...         # None when the key does not exist
    def list(self, prefix: str) -> list[str]: ...        # keys under prefix, sorted


class LocalStore:
    """A directory standing in for the bucket. Used by tests and dry runs."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise ValueError(f"key {key!r} escapes the store root")
        return path

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def get(self, key: str) -> bytes | None:
        path = self._path(key)
        return path.read_bytes() if path.is_file() else None

    def list(self, prefix: str) -> list[str]:
        if not self.root.exists():
            return []
        keys = (p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if p.is_file())
        return sorted(k for k in keys if k.startswith(prefix))


class S3Store:
    """S3, GCS's S3 interoperability API, or the local dev bucket.

    Configured from the environment so the same image runs in both columns of
    the architecture table: S3_ENDPOINT (unset on real AWS), S3_BUCKET, and the
    standard AWS credential variables.
    """

    def __init__(self, bucket: str | None = None, endpoint: str | None = None):
        import boto3
        from botocore.config import Config

        self.bucket = bucket or os.environ["S3_BUCKET"]
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint or os.environ.get("S3_ENDPOINT") or None,
            region_name=os.environ.get("AWS_REGION", "us-east-1"),
            config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 5, "mode": "standard"}),
        )

    def put(self, key: str, data: bytes) -> None:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data)

    def get(self, key: str) -> bytes | None:
        try:
            return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except self.client.exceptions.NoSuchKey:
            return None

    def list(self, prefix: str) -> list[str]:
        keys: list[str] = []
        for page in self.client.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix):
            keys.extend(obj["Key"] for obj in page.get("Contents", []))
        return sorted(keys)
