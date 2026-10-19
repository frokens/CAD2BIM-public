# Architecture

CAD2BIM separates model reasoning from deterministic data and geometry operations.

1. CAD source bytes are imported through the controlled source boundary. Extracted tables and derived outputs become immutable Data Packages.
2. Geometry and ProductFamily resources define parameters, nominal interfaces, constraint evaluation and construction. Exact resource references select actual published content.
3. Direct and Runner adapt their public inputs to the shared Application Core. PostgreSQL owns Task/Run, Catalog, Operation, Artifact and model revision persistence.
4. IFC writes use the common Writer and CURRENT/revision conflict protection. An operation report records what was attempted and what actually committed.
5. Agents investigate source relationships and current saved IFC, derive inputs or edits, submit explicit writes, and verify the saved result. The backend does not silently coordinate neighbouring pipework.

The Component workbench consumes the same backend and event authorities. Its browser replay snapshots are presentation data, not model or conversation authority; no private snapshots are distributed here.

Read the corresponding files under `docs/reference/` for exact contracts. Current code and migrations determine implemented behavior; a design or example is not proof of a complete network installation.
