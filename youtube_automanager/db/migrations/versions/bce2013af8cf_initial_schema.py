"""initial schema

Revision ID: bce2013af8cf
Revises:
Create Date: 2026-10-11 22:50:41.949634

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "bce2013af8cf"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _string_enum(*values: str) -> sa.Enum:
    """A string column for an enumeration. Its CHECK constraint is a separate named constraint of the table."""
    return sa.Enum(*values, native_enum=False, create_constraint=False)


def upgrade() -> None:
    op.create_table(
        "account",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("channel_id", sa.String(), nullable=True),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("cookies", sa.LargeBinary(), nullable=True),
        sa.Column(
            "session_state",
            _string_enum("unknown", "ok", "expiring", "dead"),
            server_default="unknown",
            nullable=False,
        ),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("earliest_cookie_expiry", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "session_state IN ('unknown', 'ok', 'expiring', 'dead')", name=op.f("ck_account_session_state")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_account")),
    )
    op.create_table(
        "channel",
        sa.Column("channel_id", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("handle", sa.String(), nullable=True),
        sa.Column("thumbnail_url", sa.String(), nullable=True),
        sa.Column("is_subscribed", sa.Boolean(), nullable=False),
        sa.Column("mark_date", sa.Date(), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("channel_id", name=op.f("pk_channel")),
    )
    op.create_index(op.f("ix_channel_title"), "channel", ["title"], unique=False)
    op.create_table(
        "notification_target",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("min_level", sa.Integer(), nullable=False),
        sa.Column("events", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_target")),
        sa.UniqueConstraint("name", name=op.f("uq_notification_target_name")),
    )
    op.create_table(
        "playlist",
        sa.Column("playlist_id", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("is_own", sa.Boolean(), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("playlist_id", name=op.f("pk_playlist")),
    )
    op.create_table(
        "run",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", _string_enum("running", "succeeded", "failed"), nullable=False),
        sa.Column("trigger", _string_enum("schedule", "manual"), nullable=False),
        sa.Column("error_text", sa.Text(), nullable=True),
        sa.Column("added_count", sa.Integer(), nullable=False),
        sa.Column("seen_count", sa.Integer(), nullable=False),
        sa.CheckConstraint("status IN ('running', 'succeeded', 'failed')", name=op.f("ck_run_status")),
        sa.CheckConstraint("\"trigger\" IN ('schedule', 'manual')", name=op.f("ck_run_trigger")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_run")),
    )
    op.create_table(
        "schedule",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("kind", _string_enum("cron", "interval"), nullable=False),
        sa.Column("cron_expression", sa.String(), server_default="0 18 * * *", nullable=True),
        sa.Column("interval_s", sa.Integer(), nullable=True),
        sa.Column("timezone", sa.String(), nullable=False),
        sa.CheckConstraint("kind IN ('cron', 'interval')", name=op.f("ck_schedule_kind")),
        sa.CheckConstraint("id = 1", name=op.f("ck_schedule_single_row")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_schedule")),
    )
    op.create_table(
        "seen_video",
        sa.Column("channel_id", sa.String(), nullable=False),
        sa.Column("video_id", sa.String(), nullable=False),
        sa.Column("state", _string_enum("seen", "deferred"), nullable=False),
        sa.Column("upload_date", sa.Date(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("state IN ('seen', 'deferred')", name=op.f("ck_seen_video_state")),
        sa.PrimaryKeyConstraint("channel_id", "video_id", name=op.f("pk_seen_video")),
    )
    op.create_index(op.f("ix_seen_video_state"), "seen_video", ["state"], unique=False)
    op.create_table(
        "setting",
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_setting")),
    )
    op.create_table(
        "rule",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("condition", sa.JSON(), nullable=False),
        sa.Column("target_playlist_id", sa.String(), nullable=False),
        sa.Column("no_shorts", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["target_playlist_id"], ["playlist.playlist_id"], name=op.f("fk_rule_target_playlist_id_playlist")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rule")),
    )
    op.create_table(
        "run_action",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("action", _string_enum("added", "skipped", "deferred", "error"), nullable=False),
        sa.Column("video_id", sa.String(), nullable=True),
        sa.Column("channel_id", sa.String(), nullable=True),
        sa.Column("rule_id", sa.Integer(), nullable=True),
        sa.Column("playlist_id", sa.String(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.CheckConstraint("action IN ('added', 'skipped', 'deferred', 'error')", name=op.f("ck_run_action_action")),
        sa.ForeignKeyConstraint(["run_id"], ["run.id"], name=op.f("fk_run_action_run_id_run"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_run_action")),
    )
    op.create_index(op.f("ix_run_action_run_id"), "run_action", ["run_id"], unique=False)
    op.create_table(
        "added_video",
        sa.Column("playlist_id", sa.String(), nullable=False),
        sa.Column("video_id", sa.String(), nullable=False),
        sa.Column("rule_id", sa.Integer(), nullable=True),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["rule_id"], ["rule.id"], name=op.f("fk_added_video_rule_id_rule"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("playlist_id", "video_id", name=op.f("pk_added_video")),
    )
    op.create_index(op.f("ix_added_video_rule_id"), "added_video", ["rule_id"], unique=False)
    op.create_table(
        "rule_channel",
        sa.Column("rule_id", sa.Integer(), nullable=False),
        sa.Column("channel_id", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["channel_id"], ["channel.channel_id"], name=op.f("fk_rule_channel_channel_id_channel")
        ),
        sa.ForeignKeyConstraint(
            ["rule_id"], ["rule.id"], name=op.f("fk_rule_channel_rule_id_rule"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("rule_id", "channel_id", name=op.f("pk_rule_channel")),
    )
    op.create_index(op.f("ix_rule_channel_channel_id"), "rule_channel", ["channel_id"], unique=False)


def downgrade() -> None:
    op.drop_table("rule_channel")
    op.drop_table("added_video")
    op.drop_table("run_action")
    op.drop_table("rule")
    op.drop_table("setting")
    op.drop_table("seen_video")
    op.drop_table("schedule")
    op.drop_table("run")
    op.drop_table("playlist")
    op.drop_table("notification_target")
    op.drop_table("channel")
    op.drop_table("account")
