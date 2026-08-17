def resume_offset(state: dict[str, int]) -> int:
    """Return the saved offset for a resumable run."""

    return state.get("offset", 0)
