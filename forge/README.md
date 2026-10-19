# forge/

Integration Forge data, not code: host allowlists, registered provider specs, conformance and retrieval eval sets, and the demo seed manifest. The code lives in `tesseract/forge/`; runtime state (credentials, registry, ledger, runs, replay recordings) lives in the git-ignored `var/forge/`. See [docs/FORGE.md](../docs/FORGE.md).

```
policy/hosts.yaml            provider host allowlists
specs/<provider>/v<N>.yaml   registered specs (+ transforms/)
evals/contract/              generated conformance fixtures
evals/retrieval/             questions + gold refs
demo/seed/manifest.yaml      planted assets and links
```
