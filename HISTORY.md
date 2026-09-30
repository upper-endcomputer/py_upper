# py_upper Git history

This repository preserves the complete release evolution from v0.9.1 through v0.16.2.

Every release has a dedicated commit and tag. The commit body records the principal architectural and build-system changes introduced by that release. Earlier tags are immutable historical snapshots; v0.16.2 is the current Build Tool compatibility release.


## v0.16.2

**Problem solved:** The Build Tool failed on Python 3.10 because `tomllib` only exists in Python 3.11+. The build tooling also needed a clean Python 3.8 floor for future legacy-runtime work.

**Implementation:** Added a TOML compatibility layer with vendored Tomli for Python 3.8–3.10, kept standard-library `tomllib` for 3.11+, normalized build-tool annotation evaluation, added an explicit Build Tool Python minimum, and added compatibility tests/documentation.

**Impact:** Developers can use Python 3.8–3.13+ to run the Build Tool while independently selecting the bundled Target Python, including custom Python 3.8.10-compatible runtimes for legacy targets.
