from contextlib import contextmanager
from threading import Lock

from sqlalchemy import Boolean, Column, ForeignKey, Integer, String, create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship, sessionmaker

Base = declarative_base()
_Session = sessionmaker()
_lock = Lock()


class Account(Base):
    __tablename__ = "account"
    id = Column(Integer, primary_key=True)
    user = Column(String(1000), nullable=False)
    url = Column(String(1000), nullable=False)
    token = Column(String(1000), nullable=False)
    home = Column(Integer, nullable=False)
    notifications = Column(Integer, nullable=False)
    last_home = Column(String(1000))
    last_notif = Column(String(1000))
    muted_home = Column(Boolean)
    muted_notif = Column(Boolean)

    dm_chats = relationship("DmChat", backref="account", cascade="all, delete, delete-orphan")


class DmChat(Base):
    __tablename__ = "dmchat"
    chat_id = Column(Integer, primary_key=True)
    contactid = Column(Integer, ForeignKey("account.id"), nullable=False)
    contact = Column(String(1000), nullable=False)


class OAuth(Base):
    __tablename__ = "oauth"
    id = Column(Integer, primary_key=True)
    url = Column(String(1000), nullable=False)
    user = Column(String(1000))
    client_id = Column(String(1000), nullable=False)
    client_secret = Column(String(1000), nullable=False)


class Client(Base):
    __tablename__ = "client"
    url = Column(String(1000), primary_key=True)
    id = Column(String(1000))
    secret = Column(String(1000))


@contextmanager
def session_scope():
    """Provide a transactional scope around a series of operations."""
    with _lock:
        session = _Session()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


def initdb(path: str, debug: bool = False) -> None:
    """Initialize engine."""
    engine = create_engine(path, echo=debug)
    Base.metadata.create_all(engine)
    _Session.configure(bind=engine)
