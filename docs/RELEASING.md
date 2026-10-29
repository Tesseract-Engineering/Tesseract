# Releasing

`this-is-not-prod` is where development happens. `main` is the released branch. A release promotes `this-is-not-prod` into `main` through a PR and tags the result `vX.Y.Z`. The version is **one SemVer for the whole repository**, not one per component.

## What gets published

`.github/workflows/release.yml` builds every image and pushes it to GitHub Container Registry as `ghcr.io/tesseract-engineering/<name>`. The names are the same as in CI's `images` job: `swarm-github`, `swarm-slack`, `swarm-drive`, `swarm-jira`, `swarm-identity`, `swarm-sample` and `swarm-graph`, with `forge` to follow.

| When | Tags pushed |
|---|---|
| Every push to `this-is-not-prod` | `dev`, and the short commit SHA |
| Every push to `main` | `main`, and the short commit SHA |
| A tag `vX.Y.Z` | `X.Y.Z` and `X.Y`, and the short commit SHA |

Each image carries build provenance and an SBOM, and a signed attestation is pushed next to it.

**The images are private.** Pulling one needs a GitHub token with `read:packages`:

```bash
echo "$TOKEN" | docker login ghcr.io -u <github user> --password-stdin
```

A customer VPC gets its own token, from a machine account, scoped to `read:packages` only.

## Running released images

`swarm/compose.images.yaml` replaces every job's `build:` with its released image, so the stack runs without building anything. It needs Docker Compose 2.24.4 or newer.

```bash
docker compose -f swarm/compose.yaml -f swarm/compose.images.yaml run --rm sample                 # the dev tag
TESSERACT_TAG=0.1.0 docker compose -f swarm/compose.yaml -f swarm/compose.images.yaml run --rm sample
```

## Making a release

1. **Open the release PR** from `this-is-not-prod` into `main`, titled `Release vX.Y.Z`. List the PRs merged since the last release:
   ```bash
   gh pr list --base this-is-not-prod --state merged --search "merged:>YYYY-MM-DD" --json number,title \
     --jq '.[] | "- #\(.number) \(.title)"'
   ```
   Choose the version by SemVer. While we are at `0.x`, use a minor bump for anything a deployment or a consumer of forge must change for, and a patch bump otherwise.
2. **Merge it with a merge commit**, never a squash, once the required checks pass and it has one approval. `main`'s history then shows one merge per release.
3. **Tag the merge commit.** Pushing the tag publishes the `X.Y.Z` and `X.Y` images:
   ```bash
   git fetch origin && git tag -a vX.Y.Z -m "vX.Y.Z" origin/main && git push origin vX.Y.Z
   ```
4. **Create the GitHub release** from the tag: `gh release create vX.Y.Z --generate-notes`.
5. **Check that the images published.** The Release run for the tag must be green for every image. Then pull one by its new tag:
   ```bash
   gh api orgs/tesseract-engineering/packages?package_type=container --jq '.[].name'
   docker pull ghcr.io/tesseract-engineering/swarm-sample:X.Y.Z
   ```

## The first publish (admin)

- **Packages permission.** The organisation must allow workflows' `GITHUB_TOKEN` to create packages (Organisation settings → Packages). If the first Release run fails with `denied`, that setting is the cause.
- **Visibility.** New GHCR packages start private. After the first publish, open each package's settings and check that it is **private** and linked to this repository, so the repository's access rules apply.

## Not covered here

Deploying into a customer VPC (SWARM.md Phase 5: Terraform, Fargate, EventBridge). That work consumes the images and tags this process publishes.
