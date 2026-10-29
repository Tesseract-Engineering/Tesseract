"""Reads `docker compose ... config --format json` on stdin and fails unless swarm/compose.images.yaml
makes every job service run a released image: no build: section, and a ghcr.io/tesseract-engineering image."""
import json
import sys

JOBS = {"init", "sample", "github", "slack", "drive", "jira", "identity", "graph"}

services = json.load(sys.stdin)["services"]
problems = [f"{name}: missing" for name in sorted(JOBS - services.keys())]
for name in sorted(JOBS & services.keys()):
    svc = services[name]
    if "build" in svc:
        problems.append(f"{name}: still has a build: section")
    if not svc.get("image", "").startswith("ghcr.io/tesseract-engineering/"):
        problems.append(f"{name}: image is {svc.get('image')!r}")
print("\n".join(problems) or f"all {len(JOBS)} job services run released images")
sys.exit(1 if problems else 0)
