"""Terraform Module Registry API.

Implements the Terraform Registry Protocol for modules plus an authenticated
upload/delete API.
"""

import os
import re
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database import engine, get_db
from models import Base, Module
from storage import (
    delete_archive,
    extract_readme,
    read_archive,
    save_archive,
)

USERNAME = os.getenv("USERNAME", "admin")
PASSWORD = os.getenv("PASSWORD", "")
JWT_SECRET = os.getenv("JWT_SECRET", "")
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_DAYS = 7

BASE_URL = os.getenv("BASE_URL", "")

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[\w.]+)?(?:\+[\w.]+)?$")


def _create_token() -> str:
    payload = {
        "sub": USERNAME,
        "exp": datetime.now(timezone.utc) + timedelta(days=JWT_EXPIRY_DAYS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


async def _require_auth(authorization: str = Header(default="")) -> None:
    """Validate a JWT Bearer token."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    token = authorization[7:]
    try:
        jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


def _validate_semver(version: str) -> None:
    if not SEMVER_RE.match(version):
        raise HTTPException(status_code=400, detail=f"Invalid semver version: {version}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create database tables on startup."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield


app = FastAPI(
    title="Terraform Module Registry",
    version="1.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


@app.get("/api/health")
async def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


@app.post("/api/auth/login")
async def login(body: dict = Body(...)):
    """Authenticate with username + password; returns a JWT access token."""
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""

    if not PASSWORD or not JWT_SECRET:
        raise HTTPException(status_code=503, detail="Auth not configured")

    valid = secrets.compare_digest(username, USERNAME) and secrets.compare_digest(
        password, PASSWORD
    )
    if not valid:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    return {"access_token": _create_token(), "token_type": "bearer"}


# ---------------------------------------------------------------------------
# Terraform service discovery
# ---------------------------------------------------------------------------


@app.get("/.well-known/terraform.json")
async def terraform_discovery():
    return {"modules.v1": "/v1/modules/"}


# ---------------------------------------------------------------------------
# List all modules (with pagination, search, namespace filter)
# ---------------------------------------------------------------------------


@app.get("/v1/modules")
async def list_modules(
    q: str | None = Query(None, description="Search query"),
    namespace: str | None = Query(None, description="Filter by namespace"),
    provider: str | None = Query(None, description="Filter by provider"),
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """List modules, returning only the latest version of each."""
    # Subquery: latest version per (namespace, name, provider)
    latest_sq = (
        select(
            Module.namespace,
            Module.name,
            Module.provider,
            func.max(Module.published_at).label("max_published"),
        )
        .group_by(Module.namespace, Module.name, Module.provider)
        .subquery()
    )

    query = select(Module).join(
        latest_sq,
        (Module.namespace == latest_sq.c.namespace)
        & (Module.name == latest_sq.c.name)
        & (Module.provider == latest_sq.c.provider)
        & (Module.published_at == latest_sq.c.max_published),
    )

    if namespace:
        query = query.where(Module.namespace == namespace)
    if provider:
        query = query.where(Module.provider == provider)
    if q:
        pattern = f"%{q}%"
        query = query.where(
            Module.name.ilike(pattern)
            | Module.namespace.ilike(pattern)
            | Module.description.ilike(pattern)
        )

    # Count total before pagination
    count_query = select(func.count()).select_from(query.subquery())
    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    query = query.order_by(Module.namespace, Module.name, Module.provider)
    query = query.offset(offset).limit(limit)

    result = await db.execute(query)
    modules = result.scalars().all()

    return {
        "meta": {"limit": limit, "offset": offset, "total": total},
        "modules": [m.to_dict() for m in modules],
    }


# ---------------------------------------------------------------------------
# List versions for a module
# ---------------------------------------------------------------------------


@app.get("/v1/modules/{namespace}/{name}/{provider}/versions")
async def list_versions(
    namespace: str,
    name: str,
    provider: str,
    db: AsyncSession = Depends(get_db),
):
    query = (
        select(Module)
        .where(Module.namespace == namespace, Module.name == name, Module.provider == provider)
        .order_by(Module.published_at.desc())
    )
    result = await db.execute(query)
    modules = result.scalars().all()

    if not modules:
        raise HTTPException(status_code=404, detail="Module not found")

    return {
        "modules": [
            {
                "source": f"{namespace}/{name}/{provider}",
                "versions": [{"version": m.version} for m in modules],
            }
        ]
    }


# ---------------------------------------------------------------------------
# Get latest version info
# ---------------------------------------------------------------------------


@app.get("/v1/modules/{namespace}/{name}/{provider}")
async def get_latest(
    namespace: str,
    name: str,
    provider: str,
    db: AsyncSession = Depends(get_db),
):
    query = (
        select(Module)
        .where(Module.namespace == namespace, Module.name == name, Module.provider == provider)
        .order_by(Module.published_at.desc())
        .limit(1)
    )
    result = await db.execute(query)
    module = result.scalar_one_or_none()

    if not module:
        raise HTTPException(status_code=404, detail="Module not found")

    return {
        **module.to_dict(),
        "readme": module.readme or "",
        "root": {"path": "", "readme": module.readme or ""},
        "versions": [],
    }


# ---------------------------------------------------------------------------
# Get specific version info
# ---------------------------------------------------------------------------


@app.get("/v1/modules/{namespace}/{name}/{provider}/{version}")
async def get_version(
    namespace: str,
    name: str,
    provider: str,
    version: str,
    db: AsyncSession = Depends(get_db),
):
    _validate_semver(version)

    query = select(Module).where(
        Module.namespace == namespace,
        Module.name == name,
        Module.provider == provider,
        Module.version == version,
    )
    result = await db.execute(query)
    module = result.scalar_one_or_none()

    if not module:
        raise HTTPException(status_code=404, detail="Module version not found")

    return {
        **module.to_dict(),
        "readme": module.readme or "",
        "root": {"path": "", "readme": module.readme or ""},
    }


# ---------------------------------------------------------------------------
# Download endpoint (Terraform protocol: returns X-Terraform-Get header)
# ---------------------------------------------------------------------------


@app.get("/v1/modules/{namespace}/{name}/{provider}/{version}/download")
async def download_version(
    namespace: str,
    name: str,
    provider: str,
    version: str,
    db: AsyncSession = Depends(get_db),
):
    _validate_semver(version)

    query = select(Module).where(
        Module.namespace == namespace,
        Module.name == name,
        Module.provider == provider,
        Module.version == version,
    )
    result = await db.execute(query)
    module = result.scalar_one_or_none()

    if not module:
        raise HTTPException(status_code=404, detail="Module version not found")

    module.downloads = (module.downloads or 0) + 1
    await db.commit()

    base = BASE_URL.rstrip("/") if BASE_URL else ""
    download_url = f"{base}/v1/modules/{namespace}/{name}/{provider}/{version}/archive"

    return Response(
        status_code=204,
        headers={"X-Terraform-Get": download_url},
    )


# ---------------------------------------------------------------------------
# Actual file download
# ---------------------------------------------------------------------------


@app.get("/v1/modules/{namespace}/{name}/{provider}/{version}/archive")
async def download_archive(
    namespace: str,
    name: str,
    provider: str,
    version: str,
    db: AsyncSession = Depends(get_db),
):
    _validate_semver(version)

    query = select(Module).where(
        Module.namespace == namespace,
        Module.name == name,
        Module.provider == provider,
        Module.version == version,
    )
    result = await db.execute(query)
    module = result.scalar_one_or_none()

    if not module:
        raise HTTPException(status_code=404, detail="Module version not found")

    try:
        data = await read_archive(module.archive_path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Archive file not found on disk")

    return Response(
        content=data,
        media_type="application/gzip",
        headers={
            "Content-Disposition": f'attachment; filename="{name}-{version}.tar.gz"',
        },
    )


# ---------------------------------------------------------------------------
# Upload module version (authenticated)
# ---------------------------------------------------------------------------


@app.post("/v1/modules/{namespace}/{name}/{provider}/{version}")
async def upload_module(
    namespace: str,
    name: str,
    provider: str,
    version: str,
    file: UploadFile,
    description: str | None = Query(None),
    source_url: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    _auth: None = Depends(_require_auth),
):
    _validate_semver(version)

    existing = await db.execute(
        select(Module).where(
            Module.namespace == namespace,
            Module.name == name,
            Module.provider == provider,
            Module.version == version,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=409,
            detail=f"Version {version} already exists for {namespace}/{name}/{provider}",
        )

    data = await file.read()

    if not data:
        raise HTTPException(status_code=400, detail="Empty file")

    if data[:2] != b"\x1f\x8b":
        raise HTTPException(status_code=400, detail="File is not a valid gzip archive")

    readme = extract_readme(data)
    path = await save_archive(namespace, name, provider, version, data)

    module = Module(
        namespace=namespace,
        name=name,
        provider=provider,
        version=version,
        description=description,
        readme=readme,
        source_url=source_url,
        archive_path=path,
    )
    db.add(module)
    await db.commit()
    await db.refresh(module)

    return JSONResponse(status_code=201, content=module.to_dict())


# ---------------------------------------------------------------------------
# Delete module version (authenticated)
# ---------------------------------------------------------------------------


@app.delete("/v1/modules/{namespace}/{name}/{provider}/{version}")
async def delete_module(
    namespace: str,
    name: str,
    provider: str,
    version: str,
    db: AsyncSession = Depends(get_db),
    _auth: None = Depends(_require_auth),
):
    _validate_semver(version)

    query = select(Module).where(
        Module.namespace == namespace,
        Module.name == name,
        Module.provider == provider,
        Module.version == version,
    )
    result = await db.execute(query)
    module = result.scalar_one_or_none()

    if not module:
        raise HTTPException(status_code=404, detail="Module version not found")

    await delete_archive(module.archive_path)
    await db.delete(module)
    await db.commit()

    return {"detail": f"Deleted {namespace}/{name}/{provider} v{version}"}
