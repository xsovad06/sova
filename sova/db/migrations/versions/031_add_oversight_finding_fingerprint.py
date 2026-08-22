"""Add fingerprint column to oversight_findings for content-based dedup.

Revision ID: 031
Revises: 030
Create Date: 2026-08-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "031"
down_revision = "030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("oversight_findings", sa.Column("fingerprint", sa.String(16), nullable=True))
    op.create_index("ix_oversight_findings_fingerprint", "oversight_findings", ["fingerprint"])


def downgrade() -> None:
    op.drop_index("ix_oversight_findings_fingerprint", table_name="oversight_findings")
    op.drop_column("oversight_findings", "fingerprint")
