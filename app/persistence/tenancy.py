"""Control-plane directory models; tenant credentials remain encrypted."""

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Company(Base):
    __tablename__ = "company_directory"
    slug: Mapped[str] = mapped_column(String(63), primary_key=True)
    database_name: Mapped[str] = mapped_column(String(63), unique=True)
    database_secret: Mapped[str] = mapped_column(Text)
    workspace_id: Mapped[str | None] = mapped_column(String(32), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="provisioning", index=True)


class CompanyHostname(Base):
    __tablename__ = "company_hostnames"
    hostname: Mapped[str] = mapped_column(String(253), primary_key=True)
    slug: Mapped[str] = mapped_column(String(63), index=True)
