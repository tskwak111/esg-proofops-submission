"""AgentCore entrypoint stub (TASK-000 baseline). No live model calls."""

from __future__ import annotations

from proofops_agent.tagger import validate_tags


def main() -> None:
    print("proofops-agent baseline: tagging boundary ready (no live model calls)")


__all__ = ["main", "validate_tags"]


if __name__ == "__main__":
    main()
