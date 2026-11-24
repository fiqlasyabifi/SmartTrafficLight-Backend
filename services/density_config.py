"""Shared density thresholds stored at /analysis_config/density in Firebase."""

DEFAULT_DENSITY_CONFIG = {
    "enabled": True,
    "low_max": 5,
    "medium_max": 15,
}


def normalize_density_config(raw):
    """Validate Firebase values and fall back safely to defaults."""
    if not isinstance(raw, dict):
        return DEFAULT_DENSITY_CONFIG.copy()

    try:
        enabled = bool(raw.get("enabled", True))
        low_max = int(raw.get("low_max", DEFAULT_DENSITY_CONFIG["low_max"]))
        medium_max = int(raw.get("medium_max", DEFAULT_DENSITY_CONFIG["medium_max"]))
    except (TypeError, ValueError):
        return DEFAULT_DENSITY_CONFIG.copy()

    if low_max < 0 or medium_max <= low_max:
        return DEFAULT_DENSITY_CONFIG.copy()

    return {
        "enabled": enabled,
        "low_max": low_max,
        "medium_max": medium_max,
    }


def classify_density(vehicle_count, config):
    """Classify a representative vehicle count using shared global thresholds."""
    config = normalize_density_config(config)

    if not config["enabled"] or vehicle_count is None or int(vehicle_count) < 0:
        return "Belum diklasifikasikan"

    count = int(vehicle_count)
    if count <= config["low_max"]:
        return "Rendah"
    if count <= config["medium_max"]:
        return "Sedang"
    return "Tinggi"
