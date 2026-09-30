"""Compatibility facade for third-party wheel handling."""
from .third_party import dependency_specs, install_wheels, installed_import_names, resolve_wheels, wheel_sha256

__all__ = ["dependency_specs", "install_wheels", "installed_import_names", "resolve_wheels", "wheel_sha256"]
