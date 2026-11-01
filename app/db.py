from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.config import settings

engine = create_engine(settings.database_url, pool_pre_ping=True, pool_size=6, max_overflow=4,
                       pool_timeout=5, connect_args={"connect_timeout": 3, "tcp_user_timeout": 3000,
                       "keepalives": 1, "keepalives_idle": 3, "keepalives_interval": 1, "keepalives_count": 2,
                       "options": "-c statement_timeout=5000 -c lock_timeout=3000"})
Session = sessionmaker(engine, expire_on_commit=False)


def db_now(session):
    # clock_timestamp, unlike now(), advances during a transaction.
    return session.scalar(text("SELECT clock_timestamp()"))
