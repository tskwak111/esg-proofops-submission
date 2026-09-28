"""Developer B reconciliation domain (contract 1.1).

Pure decision code for the disclosure-linkage items C1-C4. It is a separate axis
from the existing G/P/M evidence ladder and never produces a grade or a label.
"""

from .engine import ENGINE_VERSION, canonical_json, canonical_sha256, evaluate

__all__ = ["ENGINE_VERSION", "canonical_json", "canonical_sha256", "evaluate"]
