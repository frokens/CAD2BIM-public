# Deployment files

This directory currently contains deployment building blocks, not the final
CAD2BIM deployment topology.

- `compose.foundation.yaml` is the current PostgreSQL and migration foundation.
- `Dockerfile` and `cad2bim.env.example` support those building blocks.

Database preparation follows the capabilities being deployed:

- An independent SYSTEM library with an MCP reader runs the business migrations,
  then `cad2bim-db-runtime-roles --roles mcp system-curator`. Only the selected
  accounts' passwords are required and rotated. The publisher and reader retain
  separate database logins and privileges.
- A complete Runner deployment runs the business migrations,
  `cad2bim-checkpointer-setup`, then `cad2bim-db-runtime-roles`, as the foundation
  command already does. Checkpointer setup creates its tables and grants their
  required access to the Runner role. Runner startup checks the deployed schema.

The role command owns login accounts and role membership. Checkpointer setup owns
its schema and table grants. Both deployment commands use the maintenance database
connection; normal services use their corresponding runtime logins.

The Direct/Data release smoke is deferred, and no current release-smoke Compose
definition is shipped. The complete production service topology and final
Docker integration acceptance also remain unfinished. Do not treat the current
Compose file as a finished production deployment definition.
