# Contributing

Kurrent is developed by a small group of maintainers working with extensive AI assistance. Contribution is assessed not by code volume but by understanding of the system's load-bearing structure.

## Repository layout

- **`control_plane/`** — FastAPI control plane: declaration validation, routers, PKI, ledgers, audit, and background tasks.
- **`storager/agent/`** — Storage-node host Agent: certificate bootstrap, master scanning, credential delivery, backend orchestration.
- **`storager/nvmeof/nvmet-host/`** — NVMe/TCP host management service: configfs, subsystems, ports, DH-HMAC-CHAP.
- **`cli/`** — Single-binary CLI written in Go.
- **`webui/`** — React management UI and nginx deployment.
- **`tftp/`, `dnsmasq/`** — Boot scripts, first-hop firmware, and DHCP configuration.
- **`docs/`** — Bilingual documentation site (VitePress).
- **`tests/`** — Python tests, deployment probes, and test tooling.
- **`api/`** — Bilingual control-plane API documentation.
- **`about/`** — Architecture, roadmap, and project notes.
- **`thirdparty/`** — Pinned upstream reference implementations and patches.

Firmware lives in the separate `dutyc/kurrent-firmware` repository, which maintains only iPXE difference patches and build assets.

## Build and test

| Target | Command |
| --- | --- |
| Docs | `npm ci && npm run docs:build` |
| Docs (Pages form) | `VITEPRESS_BASE=/kurrent/ VITEPRESS_NO_CLEAN=1 npm run docs:build` |
| CLI | `cd cli && go vet ./... && go test ./... && go build -o kurrent .` |
| Control plane and nodes | `.venv-linux/bin/python -m pytest tests/ -q` |
| WebUI | `cd webui/app && npm ci && npm test && npm run build` |
| Deployment probe | `bash tests/deploy/probe_edge.sh` against a running deployment |

Python tests must be limited to `tests/`; execution from the repository root also collects upstream tests under `thirdparty/`.

Changes touching the boot chain, credentials, or data plane need an end-to-end verification on real hardware or QEMU in addition to unit tests, with evidence in the PR.

## Documentation rules

- Chinese is the default site and `/en/` is its isomorphic English mirror; add or change pages in pairs and keep the page sets aligned.
- The site gives overviews of the API, CLI, and firmware contract with links to authority; full definitions stay in their repositories to avoid a drifting second reference.
- Internal risk identifiers, trust-level codes, and test counts must not appear; state facts with descriptive names.
- Unimplemented capabilities must not be described as implemented; the roadmap and current boundaries must match the code.
- Site-internal links use relative `.md` paths; manual links on the homepage must remain compatible with GitHub Pages `.html` output.
- Documentation uses a formal engineering register; manifestos, command-style instructions, and placeholders are excluded.

## AI assistance

AI is used in this project to generate implementation, tests, and refactoring. The division of responsibility:

- **Syntax and implementation can go to AI**; architectural judgment, boundary drawing, and state ownership stay with people.
- Whoever submits a PR must be able to explain the control-plane/data-plane boundary, boot ordering, and ownership of state and derived artifacts affected by the change.
- Implementation-level imperfections are acceptable and can be iterated; changes that break load-bearing structure are not.
- A change whose rationale cannot be explained beyond what was changed should begin as an Issue; that is more valuable than a direct PR.

## Issues and pull requests

Issues are for:

- architecture and boundary questions;
- requirements and usage scenarios;
- reproducible defects;
- documentation errors.

PRs are for:

- changes with a clear boundary and a verifiable outcome;
- implementation after direction has been agreed;
- tests and documentation updated together.

A PR description should include:

1. the problem the change solves;
2. the boundaries and state ownership involved;
3. how it was verified and the result;
4. impact on existing deployments or upgrades.

Keys, tokens, private keys, PSKs, and unredacted audit logs must not be pasted into an Issue or PR. Security-sensitive matters should be raised with the maintainers privately before the disclosure scope is determined.

## Commit conventions

- Conventional Commits with English subject and body;
- commits split by topic, with unrelated changes kept separate;
- docs, tests, and implementation in the same themed commit;
- runtime state, credentials, build outputs, and local configuration excluded from commits.

## License

The project uses the Apache License 2.0. Submitting a contribution means agreeing to publish it under the same license.

## Related pages

- [Project Overview](../guide/overview.md)
- [Architecture Overview](../architecture/index.md)
- [Roadmap](./roadmap.md)
- [Configuration Reference](../reference/configuration.md)
