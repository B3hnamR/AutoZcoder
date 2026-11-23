"""Environment isolation for system tools launched by the frozen gateway."""
import os
import sys
from collections.abc import Mapping


def external_process_env(overrides: Mapping[str, str] | None = None) -> dict[str, str]:
    """Restore the pre-bootloader loader path for external Linux executables.

    PyInstaller prepends its extraction directory to LD_LIBRARY_PATH so its
    own Python can load bundled libraries. System Python/Node/Xvfb must not
    inherit that directory: their extensions may require newer OpenSSL etc.
    Do not mutate the parent's environment or change unfrozen/Windows launches.
    """
    env = os.environ.copy()
    if overrides is not None:
        env.update(overrides)
    if getattr(sys, "frozen", False) and sys.platform.startswith("linux"):
        original = env.get("LD_LIBRARY_PATH_ORIG")
        if original:
            env["LD_LIBRARY_PATH"] = original
        else:
            env.pop("LD_LIBRARY_PATH", None)
        # Keep ORIG so sanitizing a prepared environment twice is idempotent.
        # It is not a loader search variable and does not affect system tools.
    return env
