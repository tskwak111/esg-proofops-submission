"""Goal primitive-to-rubric mapping, original v2 §4.5 / docs/28."""

ELEMENTS = {
    "G1": ("target_year",),
    "G2": ("target_metric",),
    "G3": ("baseline_year", "baseline_value"),
    "G4": ("scope", "org_boundary"),
    "G5": ("current_progress",),
    "G6": ("transition_plan",),
    "G7": ("offset_plan",),
    "G8": ("science_based_verification",),
}
