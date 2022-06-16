#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from functools import cached_property
from typing import Iterable

from pathlib import Path
from sqlalchemy import Column, Integer, create_engine, String, update, insert
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
import constants
from global_logger import Log

log = Log.get_logger()

Base = declarative_base()  # https://leportella.com/sqlalchemy-tutorial/


class Video(Base):
    __tablename__ = 'videos'

    id = Column(String(50), primary_key=True)

    def __repr__(self):
        return f'{self.__class__.__name__} {self.id}'

    @classmethod
    def find(cls, session, **kwargs):
        return session.query(cls).filter_by(**kwargs).all()


class YAMConfig(Base):
    __tablename__ = 'config'
    INDEX_NAME = 'username'

    username = Column(INDEX_NAME, String(50), primary_key=True)
    token = Column('token', String, nullable=True)

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
        return {k: v for k, v in self.__dict__.items() if not k.startswith('_')}

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
    def __init__(self, db_filepath=constants.DB_FILEPATH):
        self.db_filepath = Path(db_filepath)
        self._config = None

    @cached_property
    def db(self) -> Session:
        engine_str = fr'sqlite:///{self.db_filepath}'
        log.green(f"Opening database {engine_str}")
        engine = create_engine(engine_str)  # , echo=log.verbose)
        Base.metadata.create_all(engine)
        session = sessionmaker(bind=engine)()
        return session

    def commit(self):
        log.green("Saving database")
        self.db.commit()

    def close(self):
        log.green("Closing database")
        self.db.close()

    def add(self, obj):
        if not isinstance(obj, Iterable):
            obj = [obj]

        return self.db.add_all(obj)

    @property
    def config(self):
        if self._config is None:
            self._config = YAMConfig.instantiate(self.db, constants.USERNAME)
        return self._config

    def save_config(self):
        self._config.save(self.db)


def main():
    db = DatabaseController()
    print("")


if __name__ == '__main__':
    main()
    print("")
