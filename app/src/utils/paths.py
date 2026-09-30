from pathlib import Path


def resource_root() -> Path:
    # In development: app/src/utils -> app/resources.
    # In a package: site-packages/utils -> bundled-app/resources.
    return Path(__file__).resolve().parents[2] / "resources"
