"""Build the E2B sandbox template (B-008): what repositories need to be read, tested and changed
with no network in the sandbox: git, Python 3.12 with pytest, Node with npm.

Run from backend/ (only the E2B API key is needed, no Docker):

    E2B_API_KEY=... uv run python ../scripts/e2b_template.py [name]

The name defaults to the platform's ``sandbox_template`` setting; building again replaces it, and
new sandboxes use the new build (running ones keep theirs).
"""

import os
import sys

from e2b import Template, default_build_logger

from app.settings import Settings

TEMPLATE = (
    Template()
    .from_python_image("3.12")  # Debian with git, curl and the build tools
    .apt_install(["nodejs", "npm"], no_install_recommends=True)
    .pip_install(["pytest"])
)


def main() -> None:
    key = os.environ.get("E2B_API_KEY") or os.environ.get("SANDBOX_API_KEY")
    if not key:
        sys.exit("set E2B_API_KEY (or SANDBOX_API_KEY) to the E2B API key")
    name = sys.argv[1] if len(sys.argv) > 1 else Settings().sandbox_template
    built = Template.build(
        TEMPLATE, name, cpu_count=2, memory_mb=2048, on_build_logs=default_build_logger(), api_key=key
    )
    print(f"built {name}: {built}")


if __name__ == "__main__":
    main()
