"""Object storage against the local S3 compatible server (SeaweedFS)."""

import os
import socket
import uuid

import pytest

from app.storage import ObjectNotFoundError, ObjectStore, StorageAccessError

ENDPOINT = os.environ.get("TEST_S3_ENDPOINT", "http://localhost:8333")


@pytest.fixture(scope="module")
def store() -> ObjectStore:
    host, port = ENDPOINT.split("//")[1].split(":")
    try:
        socket.create_connection((host, int(port)), timeout=1).close()
    except OSError:
        pytest.skip(f"object store not reachable at {ENDPOINT} (run `make infra`)")
    return ObjectStore(
        "artifacts", ENDPOINT, access_key_id="local-access-key", secret_access_key="local-secret-key"
    )


async def test_round_trip_under_tenant_prefix(store: ObjectStore) -> None:
    org = uuid.uuid4()
    ref = await store.put(org, "diff", b"--- a\n+++ b\n", "text/x-diff")
    assert ref.startswith(f"s3://artifacts/tenants/{org}/diff/")
    assert await store.get(org, ref) == b"--- a\n+++ b\n"
    await store.delete(org, ref)
    with pytest.raises(ObjectNotFoundError):
        await store.get(org, ref)


async def test_another_orgs_reference_is_refused(store: ObjectStore) -> None:
    owner, intruder = uuid.uuid4(), uuid.uuid4()
    ref = await store.put(owner, "report", b"private")
    for op in (store.get, store.delete):
        with pytest.raises(StorageAccessError):
            await op(intruder, ref)
    assert await store.get(owner, ref) == b"private"


@pytest.mark.parametrize(
    "ref",
    [
        "s3://other-bucket/tenants/{org}/x",
        "s3://artifacts/tenants/{org}/../{other}/x",
        "s3://artifacts/tenants/{org}//x",
        "tenants/{org}/x",
        "",
    ],
)
async def test_malformed_references_are_refused(store: ObjectStore, ref: str) -> None:
    org, other = uuid.uuid4(), uuid.uuid4()
    with pytest.raises(StorageAccessError):
        await store.get(org, ref.format(org=org, other=other))


@pytest.mark.parametrize("kind", ["", "Diff", "../x", "a/b", "x" * 60])
async def test_bad_kinds_are_refused(store: ObjectStore, kind: str) -> None:
    with pytest.raises(ValueError, match="kind"):
        await store.put(uuid.uuid4(), kind, b"x")


async def test_delete_tenant_removes_only_that_tenant(store: ObjectStore) -> None:
    gone, kept = uuid.uuid4(), uuid.uuid4()
    refs = [await store.put(gone, "artifact", f"{i}".encode()) for i in range(3)]
    keep = await store.put(kept, "artifact", b"stay")
    assert await store.delete_tenant(gone) == 3
    for ref in refs:
        with pytest.raises(ObjectNotFoundError):
            await store.get(gone, ref)
    assert await store.get(kept, keep) == b"stay"


async def test_ping(store: ObjectStore) -> None:
    assert await store.ping()
    assert not await ObjectStore(
        "no-such-bucket-xyz", ENDPOINT, access_key_id="local-access-key", secret_access_key="local-secret-key"
    ).ping()
