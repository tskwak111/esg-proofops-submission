"""TASK-034 fault injection uses the real durable local job repository."""

from uuid import UUID


def test_expired_worker_cannot_overwrite_recovered_checkpoint(tmp_path) -> None:
    from proofops.adapters.local.job_store import LocalSQLiteJobStore
    from proofops.application.ports.jobs import JobMessage

    tenant_id, run_id, document_version_id, job_id = (str(UUID(int=value)) for value in range(1, 5))
    store = LocalSQLiteJobStore(tmp_path / "jobs.sqlite")
    store.create_run(tenant_id, run_id, document_version_id)
    message = JobMessage(
        tenant_id,
        run_id,
        document_version_id,
        job_id,
        "parse",
        "full",
        "a" * 64,
    )
    store.enqueue(message, now=0)

    abandoned = store.claim_job(message, owner="local-synthetic-abandoned", now=0, lease_seconds=1)
    recovered = store.claim_job(message, owner="local-synthetic-recovery", now=1, lease_seconds=10)

    assert abandoned is not None and recovered is not None
    assert not store.commit_job(abandoned, payload=b"stale", now=1)
    assert store.commit_job(recovered, payload=b"recovered", now=1)
    assert store.read_checkpoint(message) == b"recovered"
    assert store.get_job(message)["fencing_token"] == 2
