from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


SessionFactory = Callable[[], Session]


def create_engine_for_url(database_url: str, *, echo: bool = False) -> Engine:
    connect_args = (
        {"check_same_thread": False, "timeout": 30} if database_url.startswith("sqlite") else {}
    )
    return create_engine(
        database_url,
        echo=echo,
        pool_pre_ping=True,
        connect_args=connect_args,
    )


def create_session_factory(database_url: str, *, echo: bool = False) -> sessionmaker[Session]:
    engine = create_engine_for_url(database_url, echo=echo)
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=True)
