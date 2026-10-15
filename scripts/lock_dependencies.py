"""Capture installed runtime and development versions; evaluate runtime markers for Linux."""

from importlib.metadata import distribution
from pathlib import Path
import subprocess
import sys
from packaging.requirements import Requirement
from packaging.markers import default_environment
from packaging.utils import canonicalize_name

root = Path(__file__).resolve().parents[1]
environment = default_environment()
environment.update(sys_platform="linux", platform_system="Linux", os_name="posix")
selected = {}
visited = set()


def include(requirement):
    name = canonicalize_name(requirement.name)
    extras = requirement.extras or {""}
    item = distribution(requirement.name)
    selected[name] = f"{item.metadata['Name']}=={item.version}"
    for extra in extras:
        key = (name, extra)
        if key in visited:
            continue
        visited.add(key)
        for raw in item.requires or []:
            child = Requirement(raw)
            if child.marker is None or child.marker.evaluate({**environment, "extra": extra}):
                include(child)


for line in (root / "requirements.txt").read_text().splitlines():
    if line.strip() and not line.startswith("#"):
        include(Requirement(line))
(root / "requirements-runtime-lock.txt").write_text(
    "\n".join(selected[key] for key in sorted(selected)) + "\n"
)
result = subprocess.run(
    [sys.executable, "-m", "pip", "freeze"], capture_output=True, text=True, check=True
)
(root / "requirements-lock.txt").write_text(result.stdout)
print("Runtime and development dependency versions locked.")
