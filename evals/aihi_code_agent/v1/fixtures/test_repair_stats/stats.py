def median(values: list[float]) -> float:
    """Return the median of a non-empty sequence."""

    ordered = sorted(values)
    return ordered[len(ordered) // 2]
