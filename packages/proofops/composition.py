"""Explicit composition root (TASK-000 baseline).

Local model/storage/queue/search adapters are local-only. Non-local
composition fails closed until real adapters and configuration gates are
ready (TASK-029 and cloud tasks): a MODEL_ADAPTER string alone never
certifies readiness, so staging/production reject every TASK-000 adapter,
not just the synthetic one.

Cloud wiring (S3/DynamoDB/SQS/Bedrock) arrives in later tasks; this root
only assembles what TASK-000 owns.

Environment values are parameters here, never read from os.environ inside
this package. Entry points pass them in from the process environment.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from proofops.adapters.local.models import (
    AdapterRejectedError,
    BedrockTagger,
    SyntheticTagger,
)
from proofops.application.ports.models import ModelBinding

AppEnv = Literal["local", "staging", "production"]
ModelAdapterName = Literal["synthetic", "bedrock"]

__all__ = [
    "AdapterRejectedError",
    "AppEnv",
    "LocalComposition",
    "ModelAdapterName",
    "build_composition",
    "build_local_composition",
]

type AnyAdapter = SyntheticTagger | BedrockTagger


@dataclass(frozen=True, slots=True)
class LocalComposition:
    profile: str
    app_env: AppEnv
    model_adapter: AnyAdapter
    binding: ModelBinding


def _make_adapter(name: ModelAdapterName) -> AnyAdapter:
    if name == "synthetic":
        return SyntheticTagger()
    if name == "bedrock":
        return BedrockTagger()
    raise AdapterRejectedError(f"unknown model adapter: {name}")


def build_local_composition(
    *, app_env: AppEnv, model_adapter: ModelAdapterName
) -> LocalComposition:
    """Build the local-only composition. Fails closed outside local."""
    if app_env not in ("local", "staging", "production"):
        raise AdapterRejectedError(f"unknown APP_ENV: {app_env}")
    if model_adapter not in ("synthetic", "bedrock"):
        raise AdapterRejectedError(f"unknown MODEL_ADAPTER: {model_adapter}")
    if app_env != "local":
        raise AdapterRejectedError(
            f"local composition is local-only; {app_env} refuses all local "
            "adapters (synthetic included) until real adapters and "
            "configuration gates are ready"
        )
    adapter = _make_adapter(model_adapter)
    binding = ModelBinding(
        binding_id="local-synthetic-binding" if model_adapter == "synthetic" else "unbound",
        role="tagger",
        synthetic=model_adapter == "synthetic",
    )
    return LocalComposition(
        profile=f"{app_env}-{model_adapter}",
        app_env=app_env,
        model_adapter=adapter,
        binding=binding,
    )


def build_composition(*, app_env: str, model_adapter: str) -> LocalComposition:
    """Environment dispatcher for entry points. Non-local fails closed."""
    if app_env not in ("local", "staging", "production"):
        raise AdapterRejectedError(f"unknown APP_ENV: {app_env}")
    if model_adapter not in ("synthetic", "bedrock"):
        raise AdapterRejectedError(f"unknown MODEL_ADAPTER: {model_adapter}")
    if app_env != "local":
        raise AdapterRejectedError(
            f"{app_env} composition is not_run in TASK-000: no approved live "
            "binding is wired yet, and string selection alone does not "
            "certify readiness"
        )
    return build_local_composition(
        app_env=app_env,  # type: ignore[arg-type]
        model_adapter=model_adapter,  # type: ignore[arg-type]
    )
