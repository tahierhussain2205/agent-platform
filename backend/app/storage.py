"""Object storage for large artifacts and diffs (S3 compatible: S3, R2; SeaweedFS locally).

Objects live under ``tenants/<org_id>/<kind>/<uuid>`` (tenant prefix on every key, spec 12.5).
Reads and deletes check that a reference belongs to the caller's org before touching the store,
so a forged or copied ``storage_ref`` cannot reach another tenant's objects.
"""

import asyncio
import re
import uuid
from typing import TYPE_CHECKING, Any
from uuid import UUID

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client

_KIND = re.compile(r"^[a-z][a-z0-9_]{0,40}$")


class StorageAccessError(PermissionError):
    """The reference does not belong to the caller's org (or is malformed)."""


class ObjectNotFoundError(LookupError):
    pass


class ObjectStore:
    def __init__(
        self,
        bucket: str,
        endpoint_url: str | None = None,
        region: str = "us-east-1",
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        client: "S3Client | None" = None,
    ) -> None:
        self.bucket = bucket
        self._client: S3Client = client or boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            region_name=region,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            config=Config(
                signature_version="s3v4", s3={"addressing_style": "path"}, retries={"max_attempts": 3}
            ),
        )

    def _prefix(self, org_id: UUID) -> str:
        if not isinstance(org_id, UUID):
            raise TypeError("org_id must be a UUID")
        return f"tenants/{org_id}/"

    def _key_from_ref(self, org_id: UUID, ref: str) -> str:
        expected = f"s3://{self.bucket}/{self._prefix(org_id)}"
        if not ref.startswith(expected) or ".." in ref or "//" in ref[len("s3://") :]:
            raise StorageAccessError("storage reference does not belong to this org")
        return ref[len(f"s3://{self.bucket}/") :]

    async def put(
        self, org_id: UUID, kind: str, data: bytes, content_type: str = "application/octet-stream"
    ) -> str:
        if not _KIND.match(kind):
            raise ValueError(f"bad object kind {kind!r}")
        key = f"{self._prefix(org_id)}{kind}/{uuid.uuid4()}"
        await asyncio.to_thread(
            self._client.put_object, Bucket=self.bucket, Key=key, Body=data, ContentType=content_type
        )
        return f"s3://{self.bucket}/{key}"

    async def get(self, org_id: UUID, ref: str) -> bytes:
        key = self._key_from_ref(org_id, ref)

        def read() -> bytes:
            try:
                response: Any = self._client.get_object(Bucket=self.bucket, Key=key)
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                    raise ObjectNotFoundError(ref) from exc
                raise
            body: bytes = response["Body"].read()
            return body

        return await asyncio.to_thread(read)

    async def delete(self, org_id: UUID, ref: str) -> None:
        key = self._key_from_ref(org_id, ref)
        await asyncio.to_thread(self._client.delete_object, Bucket=self.bucket, Key=key)

    async def delete_tenant(self, org_id: UUID) -> int:
        """Delete every object of an org (offboarding). Returns how many were deleted."""
        prefix = self._prefix(org_id)

        def run() -> int:
            deleted = 0
            paginator = self._client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
                keys = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
                if keys:
                    self._client.delete_objects(Bucket=self.bucket, Delete={"Objects": keys})  # type: ignore[typeddict-item]
                    deleted += len(keys)
            return deleted

        return await asyncio.to_thread(run)

    async def ping(self) -> bool:
        try:
            await asyncio.to_thread(self._client.head_bucket, Bucket=self.bucket)
            return True
        except (ClientError, BotoCoreError):
            return False


def store_from_settings(settings: Any) -> ObjectStore | None:
    """The configured store, or None when no bucket is set (large artifacts are then refused)."""
    if settings is None or not settings.s3_bucket:
        return None
    return ObjectStore(
        settings.s3_bucket,
        endpoint_url=settings.s3_endpoint_url,
        region=settings.s3_region,
        access_key_id=settings.s3_access_key_id.get_secret_value() if settings.s3_access_key_id else None,
        secret_access_key=(
            settings.s3_secret_access_key.get_secret_value() if settings.s3_secret_access_key else None
        ),
    )
