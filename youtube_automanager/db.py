#!/usr/bin/env python3
from __future__ import annotations
from collections.abc import Iterable
from datetime import datetime
from functools import cached_property
from pathlib import Path

import pendulum
from global_logger import Log
from sqlalchemy import Column, create_engine, String, update, DateTime
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session

from youtube_automanager import constants

LOG = Log.get_logger()

# https://docs.sqlalchemy.org/
# https://leportella.com/sqlalchemy-tutorial/
Base = declarative_base()


class YAMConfig(Base):
    __tablename__ = "config"
    INDEX_NAME = "username"

    username = Column(INDEX_NAME, String(50), primary_key=True)
    refresh_token = Column("refresh_token", String, nullable=True)
    last_update = Column("last_update", DateTime, default=datetime.now(tz=pendulum.local_timezone()))

    @classmethod
    def instantiate(cls, session, username):
        candidate = session.query(cls).filter_by(username=username)
        if candidate.count() != 0:
            return candidate.first()

        session.add(cls(username=username))
        if not session.dirty:
            session.commit()
        return cls.instantiate(session, username)

    @property
    def items(self):
        return {k: v for k, v in self.__dict__.items() if not k.startswith("_")}

    @property
    def updatable_items(self):
        return {k: v for k, v in self.items.items() if k != self.INDEX_NAME}

    def save(self, session):
        stmt = update(YAMConfig).where(YAMConfig.username == self.username).values(**self.updatable_items)
        commit = not session.dirty
        session.execute(stmt)
        if commit:
            session.commit()


class DatabaseController:
    def __init__(self, db_filepath: str | Path, username: str):
        self.db_filepath: Path = Path(db_filepath)
        self.username: str = username
        self._config = None

    @cached_property
    def db(self) -> Session:
        engine_str = rf"sqlite:///{self.db_filepath}"
        LOG.green(f"Opening database {engine_str}")
        engine = create_engine(engine_str)  # , echo=log.verbose)
        Base.metadata.create_all(engine)
        session = sessionmaker(bind=engine)()
        return session

    def commit(self):
        LOG.green("Saving database")
        self.db.commit()

    def close(self):
        LOG.green("Closing database")
        self.db.close()

    def add(self, obj):
        if not isinstance(obj, Iterable):
            obj = [obj]

        return self.db.add_all(obj)

    @property
    def config(self) -> YAMConfig:
        if self._config is None:
            self._config = YAMConfig.instantiate(self.db, self.username)
        return self._config

    def save_config(self):
        self._config.save(self.db)


def main():
    LOG.verbose = True
    db = DatabaseController(constants.DB_FILEPATH, constants.USERNAME)
    db.db.query()
    pass


if __name__ == "__main__":
    main()
    pass
