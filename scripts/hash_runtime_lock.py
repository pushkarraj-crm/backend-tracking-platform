"""Add published artifact SHA256 hashes to the complete, pinned runtime lock."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import httpx
from packaging.requirements import Requirement

root = Path(__file__).resolve().parents[1]
path = root / "requirements-runtime-lock.txt"
requirements = [
    Requirement(line.strip().rstrip("\\").strip())
    for line in path.read_text().splitlines()
    if line.strip() and not line.lstrip().startswith(("#", "--hash"))
]


def hashes(requirement):
    versions = list(requirement.specifier)
    if len(versions) != 1 or versions[0].operator != "==":
        raise RuntimeError("Runtime dependencies must be pinned before hashing")
    version = versions[0].version
    with httpx.Client(timeout=30, follow_redirects=False) as client:
        response = client.get(f"https://pypi.org/pypi/{requirement.name}/{version}/json")
        response.raise_for_status()
        values = sorted(
            {
                artifact["digests"]["sha256"]
                for artifact in response.json()["urls"]
                if not artifact.get("yanked")
            }
        )
    if not values:
        raise RuntimeError("No published artifacts for " + requirement.name)
    return (
        str(requirement) + " \\\n" + " \\\n".join("    --hash=sha256:" + value for value in values)
    )


with ThreadPoolExecutor(max_workers=6) as pool:
    result = list(pool.map(hashes, requirements))
path.write_text("\n\n".join(result) + "\n")
print(f"Added artifact hashes for {len(result)} runtime packages.")
