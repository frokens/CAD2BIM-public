"""Converge current MCP Apply and Component authoring histories.

Both branches retain their historical revision identities. The overlapping
checkpoint topology removal is handled at its two original migration owners;
the merged runtime does not retain an old contract or qualification column.
"""

revision = "20261014_0110"
down_revision = ("20261013_0108", "20261002_0109")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
