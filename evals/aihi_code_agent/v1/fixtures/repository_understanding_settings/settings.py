DEFAULTS = {"timeout": 30, "retries": 2}


def get_setting(config: dict[str, int], name: str) -> int | None:
    """Read a setting and fall back to the known defaults."""

    return config.get(name, DEFAULTS.get(name))
