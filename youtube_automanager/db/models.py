"""The tables of the service database as SQLAlchemy models."""

import enum
from datetime import UTC, date, datetime
from typing import Any, ClassVar

from sqlalchemy import JSON, CheckConstraint, DateTime, Dialect, Enum, ForeignKey, LargeBinary, MetaData, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

DEFAULT_CRON_EXPRESSION = "0 18 * * *"


def utc_now() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """A timezone-aware UTC datetime on every database.

    A naive datetime is refused on write. SQLite keeps no UTC offset, so a value read from it gets UTC attached.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"A naive datetime cannot go into the database: {value!r}")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


def _string_enum(enum_class: type[enum.Enum], name: str) -> Enum:
    """Store the values of an enumeration as plain strings with a CHECK constraint, so PostgreSQL gets no enum type."""
    return Enum(
        enum_class,
        name=name,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
        values_callable=lambda members: [member.value for member in members],
    )


class SessionState(enum.StrEnum):
    UNKNOWN = "unknown"
    OK = "ok"
    EXPIRING = "expiring"
    DEAD = "dead"


class SeenVideoState(enum.StrEnum):
    SEEN = "seen"
    DEFERRED = "deferred"


class ScheduleKind(enum.StrEnum):
    CRON = "cron"
    INTERVAL = "interval"


class RunStatus(enum.StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class RunTrigger(enum.StrEnum):
    SCHEDULE = "schedule"
    MANUAL = "manual"


class RunActionKind(enum.StrEnum):
    ADDED = "added"
    SKIPPED = "skipped"
    DEFERRED = "deferred"
    ERROR = "error"


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map: ClassVar[dict[type, Any]] = {datetime: UTCDateTime}


class Account(Base):
    __tablename__ = "account"

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[str | None]
    title: Mapped[str | None]
    cookies: Mapped[bytes | None] = mapped_column(LargeBinary)
    session_state: Mapped[SessionState] = mapped_column(
        _string_enum(SessionState, "session_state"),
        default=SessionState.UNKNOWN,
        server_default=SessionState.UNKNOWN.value,
    )
    last_checked_at: Mapped[datetime | None]
    earliest_cookie_expiry: Mapped[datetime | None]


class Channel(Base):
    """A YouTube channel, known only by its `UC...` id.

    The row stays after the channel disappears from YouTube, so the UI still shows the title of a rule channel.
    The title and the handle are for display only. The service never looks for a channel by them.
    """

    __tablename__ = "channel"

    channel_id: Mapped[str] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(index=True)
    handle: Mapped[str | None]
    thumbnail_url: Mapped[str | None]
    is_subscribed: Mapped[bool]
    mark_date: Mapped[date | None]
    last_checked_at: Mapped[datetime | None]


class Playlist(Base):
    """A playlist of the account. Watch Later is a normal row with the id `WL`."""

    __tablename__ = "playlist"

    playlist_id: Mapped[str] = mapped_column(primary_key=True)
    title: Mapped[str]
    is_own: Mapped[bool]
    last_synced_at: Mapped[datetime | None]


class Rule(Base):
    __tablename__ = "rule"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    enabled: Mapped[bool]
    position: Mapped[int]
    condition: Mapped[Any] = mapped_column(JSON)
    target_playlist_id: Mapped[str] = mapped_column(ForeignKey("playlist.playlist_id"))
    no_shorts: Mapped[bool]
    created_at: Mapped[datetime] = mapped_column(default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(default=utc_now, onupdate=utc_now)


class RuleChannel(Base):
    __tablename__ = "rule_channel"

    rule_id: Mapped[int] = mapped_column(ForeignKey("rule.id", ondelete="CASCADE"), primary_key=True)
    channel_id: Mapped[str] = mapped_column(ForeignKey("channel.channel_id"), primary_key=True, index=True)


class SeenVideo(Base):
    """A video that the service saw.

    A rule could not decide a deferred video, because a field had no value. The next runs check the video again
    while it stays in the processing window.
    """

    __tablename__ = "seen_video"

    channel_id: Mapped[str] = mapped_column(primary_key=True)
    video_id: Mapped[str] = mapped_column(primary_key=True)
    state: Mapped[SeenVideoState] = mapped_column(_string_enum(SeenVideoState, "state"), index=True)
    upload_date: Mapped[date | None]
    first_seen_at: Mapped[datetime] = mapped_column(default=utc_now)
    last_checked_at: Mapped[datetime] = mapped_column(default=utc_now)


class AddedVideo(Base):
    """A video that the service added to a playlist once.

    The service never adds the video again, also after the user removed it.
    """

    __tablename__ = "added_video"

    playlist_id: Mapped[str] = mapped_column(primary_key=True)
    video_id: Mapped[str] = mapped_column(primary_key=True)
    rule_id: Mapped[int | None] = mapped_column(ForeignKey("rule.id", ondelete="SET NULL"), index=True)
    added_at: Mapped[datetime] = mapped_column(default=utc_now)


class Schedule(Base):
    """The schedule of the runs. The table holds one row, and its id is always 1."""

    __tablename__ = "schedule"
    __table_args__ = (CheckConstraint("id = 1", name="single_row"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False, default=1)
    enabled: Mapped[bool]
    kind: Mapped[ScheduleKind] = mapped_column(_string_enum(ScheduleKind, "kind"))
    cron_expression: Mapped[str | None] = mapped_column(
        default=DEFAULT_CRON_EXPRESSION,
        server_default=DEFAULT_CRON_EXPRESSION,
    )
    interval_s: Mapped[int | None]
    timezone: Mapped[str]


class Run(Base):
    __tablename__ = "run"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(default=utc_now)
    finished_at: Mapped[datetime | None]
    status: Mapped[RunStatus] = mapped_column(_string_enum(RunStatus, "status"))
    trigger: Mapped[RunTrigger] = mapped_column(_string_enum(RunTrigger, "trigger"))
    error_text: Mapped[str | None] = mapped_column(Text)
    added_count: Mapped[int] = mapped_column(default=0)
    seen_count: Mapped[int] = mapped_column(default=0)


class RunAction(Base):
    """One action of a run. `rule_id` has no foreign key, so the history stays after the rule is deleted."""

    __tablename__ = "run_action"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("run.id", ondelete="CASCADE"), index=True)
    at: Mapped[datetime] = mapped_column(default=utc_now)
    action: Mapped[RunActionKind] = mapped_column(_string_enum(RunActionKind, "action"))
    video_id: Mapped[str | None]
    channel_id: Mapped[str | None]
    rule_id: Mapped[int | None]
    playlist_id: Mapped[str | None]
    detail: Mapped[str | None] = mapped_column(Text)


class NotificationTarget(Base):
    __tablename__ = "notification_target"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(unique=True)
    url: Mapped[str] = mapped_column(Text)
    min_level: Mapped[int]
    events: Mapped[list[str]] = mapped_column(JSON)
    enabled: Mapped[bool]


class Setting(Base):
    __tablename__ = "setting"

    key: Mapped[str] = mapped_column(primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)
