#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from typing import Iterable

from pathlib import Path
from sqlalchemy import Column, Integer, create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import constants
from global_logger import Log

log = Log.get_logger()

Base = declarative_base()  # https://leportella.com/sqlalchemy-tutorial/


class Video(Base):
    __tablename__ = 'videos'

    id = Column(Integer, primary_key=True)

    def __repr__(self):
        return f'{self.__class__.__name__} {self.id}'

    @classmethod
    def find(cls, session, **kwargs):
        return session.query(cls).filter_by(**kwargs).all()


class DatabaseController:
    def __init__(self, db_filepath=constants.DB_FILEPATH):
        self.db_filepath = Path(db_filepath)
        self.need_save = False

    @property
    def db(self):
        engine_str = fr'sqlite:///{self.db_filepath}'
        log.green(f"Opening database {engine_str}")
        engine = create_engine(engine_str, echo=log.verbose)
        Base.metadata.create_all(engine)
        session = sessionmaker(bind=engine)()
        return session

    def save(self):
        log.green("Saving database")
        self.db.commit()

    def close(self):
        log.green("Closing database")
        self.db.close()

    def add(self, obj):
        if not isinstance(obj, Iterable):
            obj = [obj]

        self.need_save = True
        return self.db.add_all(obj)


def main():
    db = DatabaseController()
    print("")


if __name__ == '__main__':
    main()
    print("")
