"""TASK-028 catalog entry: local durability is explicit; no AWS client is configured.

The local adapter implements JobRepository using real SQLite transactions.
DynamoDB/S3/SQS deployment and integration are not_run, never inferred from local tests.
"""

from proofops.adapters.local.job_store import LocalSQLiteJobStore

__all__ = ["LocalSQLiteJobStore"]
