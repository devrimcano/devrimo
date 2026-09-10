"""Remember a conversation as it was last read, so opening it is one query."""

import sqlalchemy as sa

from alembic import op

revision = "0037_chat_transcript_cache"
down_revision = "0036_assistant_headroom"
branch_labels = None
depends_on = None

# Opening a chat measured about half a second of server time, and the bulk of
# it was Agno's own read: 203ms for a one-message conversation, which at 40ms
# per round trip to Frankfurt is roughly five sequential queries for a single
# line of text.
#
# This table holds what that read returned. Nothing writes to it on the
# turn-completion path - it fills in lazily the first time a conversation is
# opened - so the path that must never break is untouched.
#
# `source_updated_at` is what keeps it honest: it records the chat_sessions
# row's updated_at at the moment the copy was taken, and that column moves on
# every turn. A copy is only ever served for a conversation that has not
# changed since, so there is no invalidation step to forget.


def upgrade() -> None:
    op.create_table(
        "chat_transcript_cache",
        sa.Column("session_id", sa.String(length=64), primary_key=True),
        sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("messages", sa.JSON(), nullable=False),
        sa.Column("cached_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        # Deleting a conversation takes its copy with it, rather than leaving a
        # transcript behind for a session the student asked to be rid of.
        sa.ForeignKeyConstraint(["session_id"], ["chat_sessions.id"], ondelete="CASCADE"),
    )


def downgrade() -> None:
    op.drop_table("chat_transcript_cache")
