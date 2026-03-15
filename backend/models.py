from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    pass


class Module(Base):
    __tablename__ = "modules"

    id = Column(Integer, primary_key=True, autoincrement=True)
    namespace = Column(String(255), nullable=False, index=True)
    name = Column(String(255), nullable=False, index=True)
    provider = Column(String(255), nullable=False, index=True)
    version = Column(String(50), nullable=False)
    description = Column(Text, nullable=True)
    readme = Column(Text, nullable=True)
    source_url = Column(String(500), nullable=True)
    published_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    downloads = Column(Integer, default=0, nullable=False)
    archive_path = Column(String(500), nullable=False)

    __table_args__ = (
        UniqueConstraint("namespace", "name", "provider", "version", name="uq_module_version"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "namespace": self.namespace,
            "name": self.name,
            "provider": self.provider,
            "version": self.version,
            "description": self.description or "",
            "source_url": self.source_url or "",
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "downloads": self.downloads,
        }

    def to_version_dict(self) -> dict:
        return {
            "version": self.version,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "downloads": self.downloads,
        }


class ApiKey(Base):
    __tablename__ = "api_keys"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)          # human label e.g. "CI deploy"
    key_prefix = Column(String(8), nullable=False)      # first 8 chars, shown in list
    key_hash = Column(String(64), nullable=False, unique=True)  # sha256 of the full key
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    last_used_at = Column(DateTime(timezone=True), nullable=True)

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "key_prefix": self.key_prefix,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_used_at": self.last_used_at.isoformat() if self.last_used_at else None,
        }
