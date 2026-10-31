# Contributing

Kurrent is developed by a small group of maintainers working with extensive AI assistance. The bar for contribution is not code volume; it is whether you understand the system's load-bearing structure.

## Repository layout

| Path | Content |
| --- | --- |
| `control_plane/` | FastAPI control plane: declaration validation, routers, PKI, ledgers, audit, and background tasks |
| `storager/agent/` | Storage-node host Agent: certificate bootstrap, master scanning, credential delivery, backend orchestration |
| `storager/nvmeof/nvmet-host/` | NVMe/TCP host management service: configfs, subsystems, ports, DH-HMAC-CHAP |
| `cli/` | Single-binary CLI written in Go |
| `webui/` | React management UI and nginx deployment |
| `tftp/`, `dnsmasq/` | Boot scripts, first-hop firmware, and DHCP configuration |
| `docs/` | Bilingual documentation site (VitePress) |
| `tests/` | Python tests, deployment probes, and test tooling |
| `api/` | Bilingual control-plane API documentation |
| `about/` | Architecture, roadmap, and project notes |
| `thirdparty/` | Pinned upstream reference implementations and patches |

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

Python tests must be limited to `tests/`; running from the repository root collects upstream tests under `thirdparty/` by mistake.

Changes touching the boot chain, credentials, or data plane need an end-to-end verification on real hardware or QEMU in addition to unit tests, with evidence in the PR.

## Documentation rules

- Chinese is the default site and `/en/` is its isomorphic English mirror; add or change pages in pairs and keep the page sets aligned.
- The site gives overviews of the API, CLI, and firmware contract with links to authority; full definitions stay in their repositories to avoid a drifting second reference.
- Do not write internal risk identifiers, trust-level codes, or test counts; state facts with descriptive names.
- Never describe unimplemented capabilities as implemented; the roadmap and current boundaries must match the code.
- Use relative `.md` links inside the site; manual links on the homepage must remain compatible with GitHub Pages `.html` output.
- Use a formal engineering register: no manifestos, command-style instructions, or placeholders.

## AI assistance

AI is used in this project to generate implementation, tests, and refactoring. The division of responsibility:

- **Syntax and implementation can go to AI**; architectural judgment, boundary drawing, and state ownership stay with people.
- Whoever submits a PR must be able to explain the control-plane/data-plane boundary, boot ordering, and ownership of state and derived artifacts affected by the change.
- Implementation-level imperfections are acceptable and can be iterated; changes that break load-bearing structure are not.
- If you can explain "what changed" but not "why this way", open an Issue first; that is more valuable than a PR.

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

Never paste keys, tokens, private keys, PSKs, or unredacted audit logs into an Issue or PR. For security-sensitive matters, contact the maintainers privately first and decide the disclosure scope afterwards.

## Commit conventions

- Use Conventional Commits with English subject and body;
- split commits by topic; do not mix unrelated changes;
- keep docs, tests, and implementation in the same themed commit;
- never commit runtime state, credentials, build outputs, or local configuration.

## License

The project uses the Apache License 2.0. Submitting a contribution means agreeing to publish it under the same license.

## Related pages

- [Project Overview](../guide/overview.md)
- [Architecture Overview](../architecture/index.md)
- [Roadmap](./roadmap.md)
- [Configuration Reference](../reference/configuration.md)
