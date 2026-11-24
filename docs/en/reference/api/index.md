---
title: "API Reference"
---

# Control Plane API Reference

The HTTP endpoints currently implemented by the control plane, including request parameters and response structures, are listed below with `curl` examples.

> **API first**: Every control-plane capability is exposed through the REST API as the primary interface. The Web administration interface (WebUI) is just one client of this API, on equal footing with third-party systems and automation scripts. Integrations always call the control-plane API; they never bypass the control plane to operate an Agent or the data plane directly.

The Control Plane is a persistent HTTP service on the control-plane host, responsible for:

- Device inventory and the registration window: reporting, challenge-response, TPM attestation, and binding
- Worker identity, system disks, and default boot disk inventory
- Agent registration, probing, draining, and storage placement
- Boot-chain projection (`/boot-vars`, `/boot-assets`)
- Management identity and PKI: sessions, tokens, human/component certificates, and CRLs
- Declaration status, dnsmasq-derived configuration, and operation auditing

It does not configure storage backends directly or host boot static files: `menu.ipxe` and the boot scripts live under `tftp/` and are served by nginx over HTTPS (TFTP only provides the first-stage firmware (`snponly.efi` / `ipxe.efi`) and the `boot.ipxe` pointer), while the menu's default item, timeout and OS label are projected through `/boot-vars`. The node Agent configures the storage backend on its host (nvmet configfs, or `tgtadm`/`targetcli` for iSCSI).

## Calling Conventions

- Authentication: management requests try the mTLS client certificate, management token and session cookie in order, stopping at the first success; anonymous endpoints are limited to health checks, the public CRL and boot endpoints.
- Errors: validation failures, missing resources and conflicts return 4xx with `{"detail": ...}`; authentication failures return 401, principal-class mismatches 403, rate-limited boot endpoints 429 with `Retry-After`, and audit or startup derivation failures 503.
- Data: requests and responses are JSON by default; `/boot-vars` returns an iPXE script by default and structured variables with `format=json`; keys, tokens and PSKs are never returned in plaintext.
- Integration: third-party systems call the control-plane API and never bypass it to operate Agents or the data plane directly.
- Authorization: write requests need the `admin` group; session-authenticated writes additionally require same-origin.

---

## Chapters

| Page | Contents |
| --- | --- |
| [Authentication and Authorization](./auth.md) | Credential chain, principal classes, authorization and audit |
| [Basics and State](./foundation.md) | Entry point, environment variables, state files and health checks |
| [Boot and Device Admission](./boot.md) | Boot-chain status, boot variables, registration window and challenge-attestation |
| [Device Pool](./devices.md) | Device ledger, binding, revocation and attestation baselines |
| [Workers](./workers.md) | Worker identity, queries, rebinding and batch operations |
| [System Disks and Credentials](./disks.md) | Disk creation, default disk, deletion and NVMe-oF credentials |
| [Agents and Storage](./agents.md) | Agent ledger, master tags, LUNs and the master image list |
| [Identity and PKI](./identity.md) | Tokens, one-command join, sessions and certificate revocation |
| [Operations and Verification](./operations.md) | Operation log and the typical call sequence |

## Endpoint Overview

| Method | Path | Description |
|---|---|---|
| `GET` | `/healthz` | Health check (see [Basics and State](./foundation.md)) |
| `GET` | `/boot-assets` | Boot-chain status: whether the UEFI firmware, script chain, and menu are usable (see [Boot chain](./boot.md)) |
| `GET` | `/whoami` | Current request identity (authenticator chain result, see [Authentication](./auth.md)) |
| `POST` | `/session` | Exchange credentials for a session cookie (registry-backed sign-in, see [Identity and PKI](./identity.md)) |
| `DELETE` | `/session` | Sign out (delete the session registry entry and clear the cookie, see [Identity and PKI](./identity.md)) |
| `GET` | `/sessions` | List active sessions (admin, see [Identity and PKI](./identity.md)) |
| `DELETE` | `/sessions/{session_id}` | Revoke one session (admin, see [Identity and PKI](./identity.md)) |
| `DELETE` | `/sessions` | Sign out all: clear the registry and bump epoch (admin, see [Identity and PKI](./identity.md)) |
| `GET` | `/boot/phase` | Platform enrollment phase script: serves the key enrollment flow while the window is open, no auth (see [Boot chain](./boot.md)) |
| `GET` | `/boot-vars` | Dynamic iPXE boot variable injection, no auth required (see [Boot and Device Admission](./boot.md)) |
| `GET` | `/devices/report` | iPXE device info reporting (no auth, 11 fingerprint fields + TPM AK, see [Devices](./devices.md)) |
| `GET` | `/devices/enroll-report` | Platform key enrollment result report (no auth, rate-limited, see [Boot chain](./boot.md)) |
| `GET` | `/devices/challenge` | One-time nonce issuance (no auth, challenge-response, see [Boot chain](./boot.md)) |
| `POST` | `/devices/{mac}/approve-baseline` | Approve the device's most recently reported PCR baseline (admin, see [Devices](./devices.md)) |
| `GET` | `/devices` | Device pool list (state filter, see [Devices](./devices.md)) |
| `GET` | `/devices/{mac}` | Single device detail (see [Devices](./devices.md)) |
| `POST` | `/devices` | Manually register a device into the pool (see [Devices](./devices.md)) |
| `POST` | `/devices/import` | Bulk import device manifest (see [Devices](./devices.md)) |
| `POST` | `/devices/{mac}/bind` | Bind a device to a Worker (`force` performs an atomic rebind, see [Devices](./devices.md)) |
| `DELETE` | `/devices/{mac}/bind` | Unbind a device and return it to the pool (see [Devices](./devices.md)) |
| `POST` | `/devices/bind/batch/preview` | Batch-bind preview: pairing table and conflict classification (see [Devices](./devices.md)) |
| `POST` | `/devices/bind/batch` | Batch-bind execution (per-item independent, see [Devices](./devices.md)) |
| `DELETE` | `/devices/{mac}` | Revoke a device (see [Devices](./devices.md)) |
| `DELETE` | `/devices/revoked` | Purge all revoked devices (optional `dry_run` preview, see [Devices](./devices.md)) |
| `GET` | `/settings/registration-window` | Get registration window status (see [Boot chain](./boot.md)) |
| `POST` | `/settings/registration-window` | Open the registration window (TTL hard cap 1-60 min; optional admission quota `max_devices` 1-1000, see [Boot chain](./boot.md)) |
| `DELETE` | `/settings/registration-window` | Close the registration window early (see [Boot chain](./boot.md)) |
| `GET` | `/settings/enforcement` | Get the TPM attestation enforcement switch (see [Boot chain](./boot.md)) |
| `PUT` | `/settings/enforcement` | Toggle the TPM attestation enforcement switch (see [Boot chain](./boot.md)) |
| `GET` | `/settings/platform-enrollment` | Get the platform enrollment window and enrolled devices (see [Boot chain](./boot.md)) |
| `POST` | `/settings/platform-enrollment` | Open the platform enrollment window (see [Boot chain](./boot.md)) |
| `DELETE` | `/settings/platform-enrollment` | Close the platform enrollment window (see [Boot chain](./boot.md)) |
| `GET` | `/config` | Runtime declaration configuration and derived-artifact consistency (see [Basics](./foundation.md)) |
| `GET` | `/agents` | List Agents and their capabilities (see [Agents and Storage](./agents.md)) |
| `POST` | `/agents` | Register a new Agent (writes agents.yml; 409 if id exists, see [Agents and Storage](./agents.md)) |
| `POST` | `/agents/probe` | Probe an Agent and auto-derive registration parameters (preview, no file writes, see [Agents and Storage](./agents.md)) |
| `PUT` | `/agents/{agent_id}` | Update an Agent’s configuration (id cannot be changed, see [Agents and Storage](./agents.md)) |
| `POST` | `/pki/tokens` | Issue a node-bound bootstrap token (default single-use, 60 minutes) or a management token (scope/TTL, see [Identity and PKI](./identity.md)) |
| `GET` | `/pki/tokens` | List the unexpired token ledger (see [Identity and PKI](./identity.md)) |
| `DELETE` | `/pki/tokens/{token_id}` | Revoke a token (deletion takes effect immediately, see [Identity and PKI](./identity.md)) |
| `POST` | `/pki/users/{cn}` | Issue a certificate for a registered user from a CSR (the private key never leaves the requester, see [Identity and PKI](./identity.md)) |
| `GET` | `/pki/users` | List certificates issued by the users CA (revoked/expired included, see [Identity and PKI](./identity.md)) |
| `DELETE` | `/pki/users/{cn}` | Revoke certificates (optional `serial` for a single one): re-sign CRL + reload nginx + kick sessions (see [Identity and PKI](./identity.md)) |
| `GET` | `/pki/components` | List nodes-domain component certificates (revoked/expired included, see [Identity and PKI](./identity.md)) |
| `DELETE` | `/pki/components/{agent_id}` | Revoke component certificates (optional `serial`): write the nodes CRL + reload nginx + delete bootstrap tokens (see [Identity and PKI](./identity.md)) |
| `GET` | `/pki/control` | List control-domain certificates (control-plane clients, revoked/expired included, see [Identity and PKI](./identity.md)) |
| `DELETE` | `/pki/control/{cn}` | Revoke a control-domain certificate (optional `serial`): write the control CRL (see [Identity and PKI](./identity.md)) |
| `GET` | `/enroll/crl` | Fetch the nodes + control CRLs as a component (component mTLS, see [Identity and PKI](./identity.md)) |
| `POST` | `/enroll` | First-time component enrollment: exchange a bootstrap token for certificates and trust anchors (see [Identity and PKI](./identity.md)) |
| `POST` | `/enroll/renew` | Renew component certificates (`component` principal only, see [Identity and PKI](./identity.md)) |
| `GET` | `/crl/nodes.crl` | Public read-only nodes CRL (unauthenticated, GET/HEAD only, for revoked-component startup self-check; see [Identity and PKI](./identity.md)) |
| `DELETE` | `/agents/{agent_id}` | Remove an Agent’s registry entry (incl. master-tag cleanup, see [Agents and storage](./agents.md)) |
| `GET` | `/agents/{agent_id}/luns` | List targets/LUNs on a given Agent (NVMe-oF primary path and iSCSI compatibility backends, see [Agents and Storage](./agents.md)) |
| `GET` | `/masters` | Aggregate master image inventory from all storage nodes (merged with registered tags, for clone selection, see [Agents and Storage](./agents.md)) |
| `PUT` | `/agents/{agent_id}/masters/{master_name}/tag` | Register a master tag (os / os_version annotations, see [Agents and storage](./agents.md)) |
| `DELETE` | `/agents/{agent_id}/masters/{master_name}/tag` | Clear a master tag (see [Agents and storage](./agents.md)) |
| `POST` | `/agents/{agent_id}/luns/disk` | Create a disk LUN on a given Agent (master clone / empty disk, see [Agents and Storage](./agents.md)) |
| `POST` | `/agents/{agent_id}/luns/cd` | Create a CD (ISO virtual drive) LUN on a given Agent (see [Agents and Storage](./agents.md)) |
| `DELETE` | `/agents/{agent_id}/luns` | Delete a LUN/target on a given Agent (see [Agents and Storage](./agents.md)) |
| `POST` | `/agents/{agent_id}/luns/scan` | Trigger an Agent to scan its image directory and rebuild targets (see [Agents and Storage](./agents.md)) |
| `POST` | `/workers` | Register a Worker identity (hostname binding; `mac` optional — providing it binds the device directly, see [Workers](./workers.md)) |
| `POST` | `/workers/batch` | Bulk-create Workers (count + naming rule, per-item independent, optional `macs` direct bind, see [Workers](./workers.md)) |
| `POST` | `/workers/{worker_id}/luns/disk` | Create a system disk LUN for a given Worker (see [System Disks and Credentials](./disks.md)) |
| `POST` | `/workers/luns/disk/batch` | Batch create system disks for multiple Workers (per-item node naming, or `workers` placed by the control plane; `dry_run` returns a plan, see [System Disks and Credentials](./disks.md)) |
| `DELETE` | `/workers/{worker_id}/luns/disk/{os_tag}` | Delete a single system disk of a Worker (with option to keep/delete .img file, see [System Disks and Credentials](./disks.md)) |
| `PUT` | `/workers/{worker_id}/default-disk` | Set the Worker’s default boot configuration (default disk os_tag / menu item / timeout, see [System Disks and Credentials](./disks.md)) |
| `PUT` | `/workers/{worker_id}/mac` | Rebind the Worker’s MAC (device ledger is authoritative; missing dnsmasq projection is rewritten; old/new MAC audited, see [Workers](./workers.md)) |
| `GET` | `/workers` | List Workers (see [Workers](./workers.md)) |
| `GET` | `/workers/{worker_id}` | Query a single Worker (see [Workers](./workers.md)) |
| `GET` | `/workers/{worker_id}/status` | Query Worker inventory and real-time status (see [Workers](./workers.md)) |
| `DELETE` | `/workers/{worker_id}` | Delete a Worker (see [Workers](./workers.md)) |
| `POST` | `/workers/delete/batch` | Batch delete Workers (each item processed independently, with success/failure summary, see [Workers](./workers.md)) |
| `PUT` | `/workers/{worker_id}/credential` | Set/rotate the NVMe-oF authentication key (DHHC-1 validation; nvmet disk creation already issues one, see [System disks](./disks.md)) |
| `DELETE` | `/workers/{worker_id}/credential` | Delete the NVMe-oF authentication key (revoke authentication, see [System disks](./disks.md)) |
| `GET` | `/workers/{worker_id}/credential` | Query credential metadata (never returns plaintext, see [System disks](./disks.md)) |
| `GET` | `/operations` | Read operation audit log (see [Operations and Verification](./operations.md)) |

---

