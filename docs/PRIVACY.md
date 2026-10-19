# Public distribution and data handling

This repository is a fresh, allowlisted snapshot of the current development source. The private
repository's Git history, branches, remotes, author metadata, staging area and runtime state were
not imported. The public initial commit uses the GitHub account's no-reply identity.

## Deliberately excluded

- Original CAD drawings, full project IFCs, Data Packages, PostgreSQL/storage snapshots and model checkpoints.
- Native agent calls, complete conversations, feedback, terminal logs, approvals, operation records and experiment output directories.
- Historical browser replay datasets, recorded scenarios, saved project meshes, preview images and deployment project identifiers.
- Real-drawing golden tables and acceptance samples that depend on a private project.
- `.env` files, credentials, authentication sessions, machine configuration, local permissions, caches, dependencies and build output.
- Internal process journals and historical acceptance evidence.

## Retained test material

Tests use synthetic identifiers and explicit test-only signing keys. A minimal DXF contains a single
synthetic line. Standalone local-coordinate geometry fixtures contain no original project identity
or global location. The finite-miter regression cases use independent local origins and anonymous
case keys while preserving relative geometry and the intended numerical conditions.

Synthetic privacy-rejection tests intentionally contain strings such as `/Users/private/…`, fake
passwords or invalid URLs. These are test inputs, not local machine paths or active credentials.
Named preview cases and idempotency values are metadata, not API keys; secret-scanner findings are
reviewed in context rather than suppressed for an entire source or test directory.

The public browser starts without recorded projects or static resource instances. Connect it to
your own locally prepared backend. Do not commit the runtime directory, login state, model outputs
or drawings used in your own work.

This release was checked for secrets, machine-specific identities, source project identifiers and
accidental artifacts before publication. This describes the reviewed snapshot, not a guarantee
that future additions cannot introduce sensitive material. Review each future commit and its Git history.
