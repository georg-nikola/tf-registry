from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase


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
