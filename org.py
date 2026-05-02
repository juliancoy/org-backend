from fastapi import FastAPI, HTTPException, Depends, status, BackgroundTasks, Request, Query, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.encoders import jsonable_encoder
from fastapi.responses import Response, RedirectResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field, ConfigDict, field_validator
from typing import List, Optional, Dict, Any, Set, Annotated, Callable
from enum import Enum
import uuid
from datetime import datetime, timezone, date, timedelta
from decimal import Decimal, ROUND_DOWN
import hashlib
import hmac
import ipaddress
import random
import json
import os
import sys
import asyncio
import re
import ast
import secrets
import smtplib
import socket
import time
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import quote, urlparse, urljoin, urlencode
from domain.auth import extract_bearer_token
from domain.economy import EconomicEngine as DomainEconomicEngine
from domain.governance import is_transition_allowed
from domain.ingest import (
    city_from_feed_url,
    clean_ingest_tags,
    derive_org_name,
    normalize_ingest_url,
    normalize_org_source_urls,
)
from domain.network import slugify

# Database imports
import asyncpg
from sqlalchemy import Column, String, Integer, Numeric, DateTime, Date, Boolean, JSON, Text, ForeignKey, Enum as SQLEnum, CheckConstraint, Index, func
from sqlalchemy.orm import Session, relationship, declared_attr, declarative_base
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.sql import text, select, update, delete
from contextlib import asynccontextmanager
import logging

# Redis for caching and queueing
import redis

# JWT for authentication
import jwt
from jwt import InvalidTokenError
import requests
import httpx
from bs4 import BeautifulSoup
try:
    from mcp_server import mcp as org_mcp
except Exception:
    org_mcp = None
from config.settings import *
from config.settings import _parse_content_types_csv as _settings_parse_content_types_csv
from config.logging import configure_logging
from db.base import Database
from services.scan_ai import (
    extract_text_content_from_openai_message_content as _svc_extract_text_content_from_openai_message_content,
    ocr_business_card_with_openai as _svc_ocr_business_card_with_openai,
    summarize_scan_targets_with_openai as _svc_summarize_scan_targets_with_openai,
)
from api.routers.health import router as health_router

# FastAPI app setup
app = FastAPI(
    title="Democratic Economic System API",
    description="A complete democratic economic system with UBI, stock market, insurance, and fiscal policy",
    version="2.0.0"
)


if ALLOWED_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=ALLOWED_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# Security
security = HTTPBearer(auto_error=False)

# System Constants
SYSTEM_CURRENCY = "DEM"
INITIAL_UBI_AMOUNT = Decimal('1000.00')
UBI_PAYMENT_CYCLE = 30
TAX_RATE_BASE = Decimal('0.15')
MINIMUM_WAGE = Decimal('15.00')
STOCK_MARKET_OPEN_HOUR = 9
STOCK_MARKET_CLOSE_HOUR = 17

# Setup logging
configure_logging(logging.INFO)
logger = logging.getLogger(__name__)

_ORG_CHAT_FEED_CACHE: dict[str, tuple[float, Any]] = {}

from db.models_runtime import *
from schemas_runtime import *
# ============= DATABASE DEPENDENCY =============

db = Database(
    database_url=DATABASE_URL,
    async_db_url=ASYNC_DB_URL,
    redis_host=REDIS_HOST,
    redis_port=REDIS_PORT,
    redis_password=REDIS_PASSWORD,
    logger=logger,
)

DEFAULT_DENA_ANNUAL = Decimal(DEFAULT_DENA_ANNUAL_RAW)

TREASURY_ACCOUNT_EMAIL = "treasury@arkavo.org"
TREASURY_ACCOUNT_CODE = "central-treasury"
DEPARTMENT_SEEDS = [
    {
        "code": "peacekeeping-force",
        "name": "Peace",
        "domain": "Security",
        "mandate": "Maintains defensive readiness and civil peacekeeping capacity under democratic fiscal direction.",
    },
    {
        "code": "law-enforcement",
        "name": "Law Enforcement",
        "domain": "Security",
        "mandate": "Protects public safety, due process, and community order while remaining accountable to civic oversight.",
    },
    {
        "code": "faith",
        "name": "Faith",
        "domain": "Meaning",
        "mandate": "Supports pluralistic spiritual, ethical, and chaplaincy services without establishing a single creed.",
    },
    {
        "code": "communications",
        "name": "Communications",
        "domain": "Coordination",
        "mandate": "Maintains public communications, emergency messaging, civic media, and reliable information channels.",
    },
    {
        "code": "culture",
        "name": "Culture",
        "domain": "Civic Life",
        "mandate": "Funds arts, heritage, public memory, education-adjacent culture, and shared civic rituals.",
    },
    {
        "code": "housing",
        "name": "Housing",
        "domain": "Shelter",
        "mandate": "Coordinates shelter policy, housing supply, tenant stability, and homelessness prevention.",
    },
    {
        "code": "dept-of-housing",
        "name": "Housing",
        "domain": "Shelter",
        "mandate": "Operates as the explicit department account target for housing budgets, wage schedules, and housing programs.",
    },
    {
        "code": "energy",
        "name": "Energy",
        "domain": "Infrastructure",
        "mandate": "Plans energy resilience, utility access, generation, distribution, and public-interest infrastructure.",
    },
    {
        "code": "department-of-industry",
        "name": "Industry",
        "domain": "Production",
        "mandate": "Coordinates industrial capacity, productive infrastructure, supply chains, and public-interest enterprise development.",
    },
]


async def ensure_ubi_runtime_settings_table() -> None:
    entity_csv = ",".join(DEFAULT_UBI_ENTITY_TYPES or ["individual"])
    async with db.async_pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ubi_runtime_settings (
                id INT PRIMARY KEY CHECK (id = 1),
                interval_seconds INT NOT NULL,
                dena_annual DECIMAL(20, 6) NOT NULL,
                dena_precision INT NOT NULL,
                entity_types TEXT NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_by TEXT
            )
            """
        )
        await conn.execute(
            """
            INSERT INTO ubi_runtime_settings (id, interval_seconds, dena_annual, dena_precision, entity_types, updated_by)
            VALUES (1, $1, $2, $3, $4, 'org-backend-bootstrap')
            ON CONFLICT (id) DO NOTHING
            """,
            DEFAULT_UBI_INTERVAL_SECONDS,
            float(DEFAULT_DENA_ANNUAL),
            DEFAULT_DENA_PRECISION,
            entity_csv,
        )


async def ensure_departments_and_treasury_schema() -> None:
    async with db.async_pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS treasury_accounts (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                code VARCHAR(64) NOT NULL UNIQUE,
                name VARCHAR(255) NOT NULL,
                account_id UUID NOT NULL UNIQUE REFERENCES accounts(id) ON DELETE RESTRICT,
                purpose TEXT,
                active BOOL NOT NULL DEFAULT true,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_treasury_accounts_code ON treasury_accounts (code)")
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS departments (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                code VARCHAR(64) NOT NULL UNIQUE,
                name VARCHAR(255) NOT NULL,
                domain VARCHAR(100) NOT NULL,
                mandate TEXT NOT NULL,
                account_id UUID NOT NULL UNIQUE REFERENCES accounts(id) ON DELETE RESTRICT,
                active BOOL NOT NULL DEFAULT true,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_departments_code ON departments (code)")
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS department_programs (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                department_id UUID NOT NULL REFERENCES departments(id) ON DELETE CASCADE,
                code VARCHAR(96) NOT NULL,
                name VARCHAR(255) NOT NULL,
                mandate TEXT,
                account_id UUID REFERENCES accounts(id) ON DELETE SET NULL,
                active BOOL NOT NULL DEFAULT true,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (department_id, code)
            )
            """
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_department_programs_department_code ON department_programs (department_id, code)"
        )
        await conn.execute(
            """
            ALTER TABLE budget_allocations
            ADD COLUMN IF NOT EXISTS department_id UUID REFERENCES departments(id) ON DELETE SET NULL
            """
        )
        await conn.execute(
            """
            ALTER TABLE budget_allocations
            ADD COLUMN IF NOT EXISTS program_id UUID REFERENCES department_programs(id) ON DELETE SET NULL
            """
        )
        await conn.execute(
            """
            ALTER TABLE budget_allocations
            ADD COLUMN IF NOT EXISTS treasury_transaction_id UUID
            """
        )

        treasury_account_id = await conn.fetchval(
            """
            INSERT INTO accounts
                (id, entity_type, name, email, balance, credit_score, created_at, updated_at, is_verified)
            VALUES
                (gen_random_uuid(), 'GOVERNMENT', 'Treasury', $1, 0, 850, NOW(), NOW(), true)
            ON CONFLICT (email) DO UPDATE SET
                entity_type = 'GOVERNMENT',
                name = EXCLUDED.name,
                is_verified = true,
                updated_at = NOW()
            RETURNING id
            """,
            TREASURY_ACCOUNT_EMAIL,
        )
        await conn.execute(
            """
            INSERT INTO treasury_accounts (id, code, name, account_id, purpose, active, created_at, updated_at)
            VALUES (
                gen_random_uuid(), $1, 'Treasury', $2,
                'Receives taxes and funds department allocations.',
                true, NOW(), NOW()
            )
            ON CONFLICT (code) DO UPDATE SET
                name = EXCLUDED.name,
                account_id = EXCLUDED.account_id,
                purpose = EXCLUDED.purpose,
                active = true,
                updated_at = NOW()
            """,
            TREASURY_ACCOUNT_CODE,
            treasury_account_id,
        )

        for seed in DEPARTMENT_SEEDS:
            account_id = await conn.fetchval(
                """
                INSERT INTO accounts
                    (id, entity_type, name, email, balance, credit_score, created_at, updated_at,
                     mission_statement, is_verified)
                VALUES
                    (gen_random_uuid(), 'GOVERNMENT', $1, $2, 0, 850, NOW(), NOW(), $3, true)
                ON CONFLICT (email) DO UPDATE SET
                    entity_type = 'GOVERNMENT',
                    name = EXCLUDED.name,
                    mission_statement = EXCLUDED.mission_statement,
                    is_verified = true,
                    updated_at = NOW()
                RETURNING id
                """,
                seed["name"],
                f"{seed['code']}@departments.arkavo.org",
                seed["mandate"],
            )
            await conn.execute(
                """
                INSERT INTO departments (id, code, name, domain, mandate, account_id, active, created_at, updated_at)
                VALUES (gen_random_uuid(), $1, $2, $3, $4, $5, true, NOW(), NOW())
                ON CONFLICT (code) DO UPDATE SET
                    name = EXCLUDED.name,
                    domain = EXCLUDED.domain,
                    mandate = EXCLUDED.mandate,
                    account_id = EXCLUDED.account_id,
                    active = true,
                    updated_at = NOW()
                """,
                seed["code"],
                seed["name"],
                seed["domain"],
                seed["mandate"],
                account_id,
            )


def _default_ubi_runtime_settings() -> dict:
    return {
        "interval_seconds": DEFAULT_UBI_INTERVAL_SECONDS,
        "dena_annual": DEFAULT_DENA_ANNUAL,
        "dena_precision": DEFAULT_DENA_PRECISION,
        "entity_types": DEFAULT_UBI_ENTITY_TYPES or ["individual"],
        "updated_at": datetime.now(timezone.utc),
        "updated_by": None,
    }


def _parse_entity_types_csv(value: str) -> list[str]:
    parsed = [item.strip() for item in (value or "").split(",") if item.strip()]
    return parsed or ["individual"]


def _default_business_card_runtime_settings() -> dict:
    return {
        "enabled": ORG_BUSINESS_CARD_DEFAULT_ENABLED,
        "per_user_limit_per_hour": ORG_BUSINESS_CARD_DEFAULT_USER_LIMIT_PER_HOUR,
        "per_ip_limit_per_hour": ORG_BUSINESS_CARD_DEFAULT_IP_LIMIT_PER_HOUR,
        "global_limit_per_hour": ORG_BUSINESS_CARD_DEFAULT_GLOBAL_LIMIT_PER_HOUR,
        "duplicate_hash_limit": ORG_BUSINESS_CARD_DEFAULT_DUPLICATE_HASH_LIMIT,
        "duplicate_hash_window_seconds": ORG_BUSINESS_CARD_DEFAULT_DUPLICATE_WINDOW_SECONDS,
        "max_bytes": ORG_BUSINESS_CARD_DEFAULT_MAX_BYTES,
        "allowed_content_types": sorted(ORG_BUSINESS_CARD_DEFAULT_ALLOWED_CONTENT_TYPES),
        "event_link_enrichment_enabled": ORG_SCAN_EVENT_LINK_ENRICHMENT_ENABLED,
        "auto_clarification_enabled": ORG_SCAN_AUTO_CLARIFICATION_ENABLED,
        "auto_min_confidence": ORG_SCAN_AUTO_MIN_CONFIDENCE,
        "auto_min_margin": ORG_SCAN_AUTO_MIN_MARGIN,
        "updated_at": datetime.now(timezone.utc),
        "updated_by": None,
    }


def _parse_content_types_runtime_csv(value: str) -> list[str]:
    parsed = sorted(_settings_parse_content_types_csv(value))
    return parsed or sorted(ORG_BUSINESS_CARD_DEFAULT_ALLOWED_CONTENT_TYPES)


async def ensure_business_card_runtime_settings_table() -> None:
    default_content_types_csv = ",".join(sorted(ORG_BUSINESS_CARD_DEFAULT_ALLOWED_CONTENT_TYPES))
    async with db.async_pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS business_card_runtime_settings (
                id INT PRIMARY KEY CHECK (id = 1),
                enabled BOOLEAN NOT NULL DEFAULT TRUE,
                per_user_limit_per_hour INT NOT NULL,
                per_ip_limit_per_hour INT NOT NULL,
                global_limit_per_hour INT NOT NULL,
                duplicate_hash_limit INT NOT NULL,
                duplicate_hash_window_seconds INT NOT NULL,
                max_bytes INT NOT NULL,
                allowed_content_types TEXT NOT NULL,
                event_link_enrichment_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                auto_clarification_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                auto_min_confidence DOUBLE PRECISION NOT NULL DEFAULT 0.75,
                auto_min_margin DOUBLE PRECISION NOT NULL DEFAULT 0.20,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_by TEXT
            )
            """
        )
        await conn.execute(
            """
            ALTER TABLE business_card_runtime_settings
            ADD COLUMN IF NOT EXISTS event_link_enrichment_enabled BOOLEAN NOT NULL DEFAULT TRUE
            """
        )
        await conn.execute(
            """
            ALTER TABLE business_card_runtime_settings
            ADD COLUMN IF NOT EXISTS auto_clarification_enabled BOOLEAN NOT NULL DEFAULT TRUE
            """
        )
        await conn.execute(
            """
            ALTER TABLE business_card_runtime_settings
            ADD COLUMN IF NOT EXISTS auto_min_confidence DOUBLE PRECISION NOT NULL DEFAULT 0.75
            """
        )
        await conn.execute(
            """
            ALTER TABLE business_card_runtime_settings
            ADD COLUMN IF NOT EXISTS auto_min_margin DOUBLE PRECISION NOT NULL DEFAULT 0.20
            """
        )
        await conn.execute(
            """
            INSERT INTO business_card_runtime_settings (
                id,
                enabled,
                per_user_limit_per_hour,
                per_ip_limit_per_hour,
                global_limit_per_hour,
                duplicate_hash_limit,
                duplicate_hash_window_seconds,
                max_bytes,
                allowed_content_types,
                event_link_enrichment_enabled,
                auto_clarification_enabled,
                auto_min_confidence,
                auto_min_margin,
                updated_by
            )
            VALUES (1, $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, 'org-backend-bootstrap')
            ON CONFLICT (id) DO NOTHING
            """,
            ORG_BUSINESS_CARD_DEFAULT_ENABLED,
            ORG_BUSINESS_CARD_DEFAULT_USER_LIMIT_PER_HOUR,
            ORG_BUSINESS_CARD_DEFAULT_IP_LIMIT_PER_HOUR,
            ORG_BUSINESS_CARD_DEFAULT_GLOBAL_LIMIT_PER_HOUR,
            ORG_BUSINESS_CARD_DEFAULT_DUPLICATE_HASH_LIMIT,
            ORG_BUSINESS_CARD_DEFAULT_DUPLICATE_WINDOW_SECONDS,
            ORG_BUSINESS_CARD_DEFAULT_MAX_BYTES,
            default_content_types_csv,
            ORG_SCAN_EVENT_LINK_ENRICHMENT_ENABLED,
            ORG_SCAN_AUTO_CLARIFICATION_ENABLED,
            ORG_SCAN_AUTO_MIN_CONFIDENCE,
            ORG_SCAN_AUTO_MIN_MARGIN,
        )


async def get_business_card_runtime_settings() -> dict:
    try:
        await ensure_business_card_runtime_settings_table()
        async with db.async_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    enabled,
                    per_user_limit_per_hour,
                    per_ip_limit_per_hour,
                    global_limit_per_hour,
                    duplicate_hash_limit,
                    duplicate_hash_window_seconds,
                    max_bytes,
                    allowed_content_types,
                    event_link_enrichment_enabled,
                    auto_clarification_enabled,
                    auto_min_confidence,
                    auto_min_margin,
                    updated_at,
                    updated_by
                FROM business_card_runtime_settings
                WHERE id = 1
                """
            )
    except Exception as exc:
        logger.warning(f"Business card runtime settings unavailable, using defaults: {exc}")
        return _default_business_card_runtime_settings()
    if not row:
        return _default_business_card_runtime_settings()
    return {
        "enabled": bool(row["enabled"]),
        "per_user_limit_per_hour": int(row["per_user_limit_per_hour"]),
        "per_ip_limit_per_hour": int(row["per_ip_limit_per_hour"]),
        "global_limit_per_hour": int(row["global_limit_per_hour"]),
        "duplicate_hash_limit": int(row["duplicate_hash_limit"]),
        "duplicate_hash_window_seconds": int(row["duplicate_hash_window_seconds"]),
        "max_bytes": int(row["max_bytes"]),
        "allowed_content_types": _parse_content_types_runtime_csv(str(row["allowed_content_types"] or "")),
        "event_link_enrichment_enabled": bool(row["event_link_enrichment_enabled"]),
        "auto_clarification_enabled": bool(row["auto_clarification_enabled"]),
        "auto_min_confidence": float(row["auto_min_confidence"]),
        "auto_min_margin": float(row["auto_min_margin"]),
        "updated_at": row["updated_at"],
        "updated_by": row["updated_by"],
    }


async def get_ubi_runtime_settings() -> dict:
    try:
        await ensure_ubi_runtime_settings_table()
        async with db.async_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT interval_seconds, dena_annual, dena_precision, entity_types, updated_at, updated_by
                FROM ubi_runtime_settings
                WHERE id = 1
                """
            )
    except Exception as exc:
        logger.warning(f"UBI runtime settings unavailable, using defaults: {exc}")
        return _default_ubi_runtime_settings()
    if not row:
        return _default_ubi_runtime_settings()
    return {
        "interval_seconds": int(row["interval_seconds"]),
        "dena_annual": Decimal(str(row["dena_annual"])),
        "dena_precision": int(row["dena_precision"]),
        "entity_types": _parse_entity_types_csv(str(row["entity_types"])),
        "updated_at": row["updated_at"],
        "updated_by": row["updated_by"],
    }


def _spicedb_enabled() -> bool:
    return bool(SPICEDB_HTTP_URL and SPICEDB_PRESHARED_KEY)


def _spicedb_headers() -> dict:
    return {"Authorization": f"Bearer {SPICEDB_PRESHARED_KEY}"}


def _spicedb_relationship(
    resource_type: str,
    resource_id: str,
    relation: str,
    subject_type: str,
    subject_id: str,
    subject_relation: str | None = None,
) -> dict:
    relationship = {
        "resource": {"objectType": resource_type, "objectId": resource_id},
        "relation": relation,
        "subject": {
            "object": {"objectType": subject_type, "objectId": subject_id},
        },
    }
    if subject_relation:
        relationship["subject"]["optionalRelation"] = subject_relation
    return relationship


async def _spicedb_read_schema() -> str:
    if not _spicedb_enabled():
        return ""
    async with httpx.AsyncClient(timeout=5) as client:
        resp = await client.get(
            f"{SPICEDB_HTTP_URL}/v1/schema/read",
            headers=_spicedb_headers(),
        )
    if not resp.is_success:
        return ""
    data = resp.json()
    return data.get("schema_text", "") or ""


async def _spicedb_write_schema() -> None:
    if not _spicedb_enabled():
        return
    current_schema = await _spicedb_read_schema()
    parts: list[str] = []
    if "definition user" not in current_schema:
        parts.append("definition user {}")
    if "definition group" not in current_schema:
        parts.append("definition group { relation member: user }")
    if "definition org" not in current_schema:
        parts.append(
            "definition org { relation admin: user | group#member\n  permission db_admin = admin }"
        )
    if not parts:
        return
    next_schema = current_schema.rstrip()
    if next_schema:
        next_schema += "\n\n"
    next_schema += "\n\n".join(parts)
    async with httpx.AsyncClient(timeout=5) as client:
        await client.post(
            f"{SPICEDB_HTTP_URL}/v1/schema/write",
            headers=_spicedb_headers(),
            json={"schema": next_schema},
        )


async def _spicedb_write_relationships(relationships: list[dict]) -> None:
    if not _spicedb_enabled() or not relationships:
        return
    updates = [
        {
            "operation": "OPERATION_TOUCH",
            "relationship": relationship,
        }
        for relationship in relationships
    ]
    async with httpx.AsyncClient(timeout=5) as client:
        await client.post(
            f"{SPICEDB_HTTP_URL}/v1/relationships/write",
            headers=_spicedb_headers(),
            json={"updates": updates},
        )


async def _spicedb_check_sysadmin(user_id: str) -> bool:
    if not _spicedb_enabled():
        return False
    payload = {
        "resource": {"objectType": "org", "objectId": ORG_SYSADMIN_RESOURCE_ID},
        "permission": "db_admin",
        "subject": {"object": {"objectType": "user", "objectId": user_id}},
    }
    async with httpx.AsyncClient(timeout=5) as client:
        resp = await client.post(
            f"{SPICEDB_HTTP_URL}/v1/permissions/check",
            headers=_spicedb_headers(),
            json=payload,
        )
    if not resp.is_success:
        return False
    data = resp.json()
    return data.get("permissionship") == "PERMISSIONSHIP_HAS_PERMISSION"


def _ensure_network_ingest_schema() -> None:
    """Apply lightweight online schema fixes required for calendar ingest."""
    statements = [
        "ALTER TABLE IF EXISTS network_events ADD COLUMN IF NOT EXISTS ingest_key VARCHAR(255)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_network_events_ingest_key ON network_events (ingest_key)",
        "ALTER TABLE IF EXISTS organizations ADD COLUMN IF NOT EXISTS source_urls JSONB",
        "CREATE INDEX IF NOT EXISTS idx_organizations_source_urls_gin ON organizations USING GIN (source_urls)",
        "UPDATE organizations SET source_urls = jsonb_build_array(source_url) WHERE source_url IS NOT NULL AND (source_urls IS NULL OR jsonb_typeof(source_urls) <> 'array' OR jsonb_array_length(source_urls) = 0)",
        "ALTER TABLE IF EXISTS business_card_submissions ADD COLUMN IF NOT EXISTS image_storage_backend VARCHAR(32)",
        "ALTER TABLE IF EXISTS business_card_submissions ADD COLUMN IF NOT EXISTS image_storage_bucket VARCHAR(255)",
        "ALTER TABLE IF EXISTS business_card_submissions ADD COLUMN IF NOT EXISTS image_storage_path VARCHAR(1024)",
        "ALTER TABLE IF EXISTS business_card_submissions ADD COLUMN IF NOT EXISTS image_storage_error TEXT",
        "CREATE INDEX IF NOT EXISTS idx_business_card_submissions_sha_created ON business_card_submissions (image_sha256, created_at)",
        "ALTER TABLE IF EXISTS user_contact_pages ADD COLUMN IF NOT EXISTS github_url TEXT",
        "ALTER TABLE IF EXISTS user_contact_pages ADD COLUMN IF NOT EXISTS x_url TEXT",
        "ALTER TABLE IF EXISTS user_contact_pages ADD COLUMN IF NOT EXISTS source_profile_url TEXT",
        "ALTER TABLE IF EXISTS user_contact_pages ADD COLUMN IF NOT EXISTS source_profile_imported_at TIMESTAMPTZ",
    ]
    with db.engine.begin() as conn:
        for statement in statements:
            conn.execute(text(statement))


def _ensure_governance_dissolution_schema() -> None:
    """Apply online schema updates for dissolution governance flows."""
    statements = [
        "ALTER TABLE IF EXISTS governance_motions DROP CONSTRAINT IF EXISTS check_governance_motion_type",
        (
            "ALTER TABLE IF EXISTS governance_motions "
            "ADD CONSTRAINT check_governance_motion_type "
            "CHECK (type IN ('main','amendment','dissolution'))"
        ),
        (
            "CREATE TABLE IF NOT EXISTS governance_dissolution_plans ("
            "id UUID PRIMARY KEY, "
            "motion_id UUID NOT NULL UNIQUE REFERENCES governance_motions(id) ON DELETE CASCADE, "
            "asset_disposition TEXT NOT NULL, "
            "asset_recipient_name VARCHAR(255) NOT NULL, "
            "asset_recipient_type VARCHAR(32) NOT NULL DEFAULT 'other_legal_entity', "
            "legal_compliance_notes TEXT, "
            "executed_at TIMESTAMPTZ, "
            "executed_by_user_id VARCHAR(255), "
            "execution_notes TEXT, "
            "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), "
            "updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), "
            "CONSTRAINT check_dissolution_recipient_type "
            "CHECK (asset_recipient_type IN ('non_profit','other_legal_entity'))"
            ")"
        ),
        (
            "CREATE INDEX IF NOT EXISTS idx_governance_dissolution_plans_executed_by "
            "ON governance_dissolution_plans (executed_by_user_id)"
        ),
    ]
    with db.engine.begin() as conn:
        for statement in statements:
            conn.execute(text(statement))


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan manager for startup/shutdown"""
    await db.connect()

    # Create tables if they don't exist
    Base.metadata.create_all(bind=db.engine)
    logger.info("Database tables verified/created")
    try:
        _ensure_network_ingest_schema()
        logger.info("Network ingest schema verified/updated")
    except Exception as exc:
        logger.warning(f"Network ingest schema migration skipped: {exc}")
    try:
        _ensure_governance_dissolution_schema()
        logger.info("Governance dissolution schema verified/updated")
    except Exception as exc:
        logger.warning(f"Governance dissolution schema migration skipped: {exc}")
    await ensure_ubi_runtime_settings_table()
    await ensure_business_card_runtime_settings_table()
    await ensure_departments_and_treasury_schema()
    if ORG_BUSINESS_CARD_STORAGE_ENABLED:
        try:
            if _business_card_storage_backend() == "s3":
                _ensure_business_card_s3_bucket()
                logger.info(
                    "Business card S3 storage verified: bucket=%s endpoint=%s",
                    ORG_BUSINESS_CARD_S3_BUCKET,
                    ORG_BUSINESS_CARD_S3_ENDPOINT_URL or "aws-default",
                )
            else:
                _business_card_storage_root().mkdir(parents=True, exist_ok=True)
                logger.info("Business card storage directory verified: %s", _business_card_storage_root())
        except Exception as exc:
            logger.warning("Business card storage unavailable: %s", exc)
    session = None
    try:
        session = db.SessionLocal()
        seed_stats = _seed_organizations_from_event_sources(session, force_update=False)
        logger.info(
            "Organization seeds loaded=%s inserted=%s updated=%s",
            seed_stats.loaded,
            seed_stats.inserted,
            seed_stats.updated,
        )
    except Exception as exc:
        logger.warning(f"Organization seeding skipped: {exc}")
    finally:
        try:
            if session:
                session.close()
        except Exception:
            pass

    # SpiceDB schema + admin bootstrap
    try:
        await _spicedb_write_schema()
        relationships: list[dict] = [
            _spicedb_relationship(
                "org",
                ORG_SYSADMIN_RESOURCE_ID,
                "admin",
                "group",
                ORG_SYSADMIN_GROUP,
                "member",
            )
        ]
        for admin_id in ORG_SYSADMIN_USER_IDS:
            relationships.append(
                _spicedb_relationship("group", ORG_SYSADMIN_GROUP, "member", "user", admin_id)
            )
        await _spicedb_write_relationships(relationships)
    except Exception as exc:
        logger.warning(f"SpiceDB bootstrap skipped: {exc}")

    public_calendar_task: Optional[asyncio.Task] = None
    if ORG_PUBLIC_CALENDAR_PULL_ENABLED and ORG_PUBLIC_CALENDAR_FEEDS:
        public_calendar_task = asyncio.create_task(_public_calendar_pull_loop())
    worker_tasks = await _start_embedded_worker_tasks()

    yield

    if worker_tasks:
        for task in worker_tasks:
            task.cancel()
        await asyncio.gather(*worker_tasks, return_exceptions=True)

    if public_calendar_task:
        public_calendar_task.cancel()
        try:
            await public_calendar_task
        except asyncio.CancelledError:
            pass

    await db.disconnect()

app = FastAPI(lifespan=lifespan)
app.include_router(health_router)

if org_mcp is not None:
    try:
        if hasattr(org_mcp, "streamable_http_app"):
            app.mount("/mcp", org_mcp.streamable_http_app())
            logger.info("Mounted Org MCP server at /mcp (streamable HTTP)")
        elif hasattr(org_mcp, "sse_app"):
            app.mount("/mcp", org_mcp.sse_app())
            logger.info("Mounted Org MCP server at /mcp (SSE)")
        else:
            logger.warning("Org MCP server loaded but no compatible ASGI app factory was found")
    except Exception as exc:
        logger.warning(f"Failed to mount Org MCP server: {exc}")

# Dependency for database session
def get_db():
    session = db.SessionLocal()
    try:
        yield session
    finally:
        session.close()

# Dependency for async database connection
async def get_async_db():
    async with db.async_pool.acquire() as conn:
        yield conn

import helpers.auth_and_scan as _helpers_auth_and_scan
import helpers.matrix_and_mapping as _helpers_matrix_and_mapping
import helpers.network_utils as _helpers_network_utils

# Re-export helper symbols into this module, including underscore-prefixed
# names, so existing router imports from `org` remain backward-compatible.
for _helpers_module in (
    _helpers_auth_and_scan,
    _helpers_matrix_and_mapping,
    _helpers_network_utils,
):
    for _name, _value in vars(_helpers_module).items():
        if not _name.startswith("__"):
            globals()[_name] = _value
# ============= ECONOMIC ENGINE =============

class EconomicEngine(DomainEconomicEngine):
    """Compatibility shim; implementation now lives under domain.economy."""

    pass


# ============= ORG NETWORK ENDPOINTS =============

def _ingest_calendar_payload(
    session: Session,
    payload: CalendarIngestPayload,
) -> CalendarIngestResponse:
    org_inserted = 0
    org_updated = 0
    event_inserted = 0
    event_updated = 0
    event_skipped = 0

    host_org_by_source: Dict[str, Organization] = {}
    for item in payload.organizations:
        org, created = _upsert_ingested_organization(session, item)
        for source_url in _org_source_urls(org):
            host_org_by_source[source_url] = org
        if created:
            org_inserted += 1
        else:
            org_updated += 1

    for item in payload.events:
        source_url = _normalize_ingest_url(item.host_org_source_url)
        if source_url and source_url not in host_org_by_source:
            fallback_org, created = _upsert_ingested_organization(
                session,
                CalendarIngestOrganization(
                    source_url=source_url,
                    name=(item.host_org_name or "").strip() or None,
                    image_url=item.host_org_image_url,
                    city=item.city,
                    tags=item.tags or [],
                ),
            )
            for mapped_url in _org_source_urls(fallback_org):
                host_org_by_source[mapped_url] = fallback_org
            if created:
                org_inserted += 1
            else:
                org_updated += 1

        event, status_label = _upsert_ingested_event(session, item, host_org_by_source)
        if event is None and status_label == "skipped":
            event_skipped += 1
            continue
        if status_label == "created":
            event_inserted += 1
        elif status_label == "updated":
            event_updated += 1

    session.commit()
    return CalendarIngestResponse(
        organizations_inserted=org_inserted,
        organizations_updated=org_updated,
        events_inserted=event_inserted,
        events_updated=event_updated,
        events_skipped=event_skipped,
    )


async def _pull_public_calendar_feed_once(feed_url: str) -> Optional[CalendarIngestResponse]:
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            resp = await client.get(feed_url, headers={"accept": "application/json"})
        if not resp.is_success:
            logger.warning("Public calendar pull failed (%s): %s", resp.status_code, feed_url)
            return None
        payload_json = resp.json()
        if not isinstance(payload_json, list):
            logger.warning("Public calendar payload is not a list: %s", feed_url)
            return None
        payload = _build_ingest_payload_from_public_feed(
            feed_url,
            [item for item in payload_json if isinstance(item, dict)],
        )
        if not payload.events:
            logger.info("Public calendar feed had zero events: %s", feed_url)
            return CalendarIngestResponse(
                organizations_inserted=0,
                organizations_updated=0,
                events_inserted=0,
                events_updated=0,
                events_skipped=0,
            )
        session = db.SessionLocal()
        try:
            result = _ingest_calendar_payload(session, payload)
        finally:
            session.close()
        logger.info(
            "Public calendar ingest complete feed=%s org_inserted=%s org_updated=%s event_inserted=%s event_updated=%s skipped=%s",
            feed_url,
            result.organizations_inserted,
            result.organizations_updated,
            result.events_inserted,
            result.events_updated,
            result.events_skipped,
        )
        return result
    except Exception as exc:
        logger.warning("Public calendar pull failed for %s: %s", feed_url, exc)
        return None


async def _public_calendar_pull_loop() -> None:
    if not ORG_PUBLIC_CALENDAR_PULL_ENABLED:
        logger.info("Public calendar pull disabled (ORG_PUBLIC_CALENDAR_PULL_ENABLED=false)")
        return
    if not ORG_PUBLIC_CALENDAR_FEEDS:
        logger.info("Public calendar pull disabled (no ORG_PUBLIC_CALENDAR_FEEDS configured)")
        return
    logger.info(
        "Public calendar pull enabled feeds=%s interval_seconds=%s",
        ORG_PUBLIC_CALENDAR_FEEDS,
        ORG_PUBLIC_CALENDAR_PULL_INTERVAL_SECONDS,
    )
    while True:
        for feed_url in ORG_PUBLIC_CALENDAR_FEEDS:
            await _pull_public_calendar_feed_once(feed_url)
        await asyncio.sleep(ORG_PUBLIC_CALENDAR_PULL_INTERVAL_SECONDS)



# ============= ACCOUNT ENDPOINTS =============

@app.post("/api/accounts", response_model=AccountResponse)
async def create_account(
    account_data: AccountCreate,
    session: Session = Depends(get_db)
):
    """Create a new financial account"""
    # Check if email already exists
    existing = session.query(Account).filter_by(email=account_data.email).first()
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")

    # Generate tax ID for businesses/nonprofits
    tax_id = None
    if account_data.entity_type in [EntityType.BUSINESS, EntityType.NONPROFIT]:
        tax_id = f"TX{secrets.token_hex(5).upper()}"

    # Create account
    account = Account(
        id=uuid.uuid4(),
        entity_type=account_data.entity_type,
        name=account_data.name,
        email=account_data.email,
        address=account_data.address,
        balance=account_data.initial_deposit,
        business_type=account_data.business_type,
        mission_statement=account_data.mission_statement,
        tax_id=tax_id
    )

    session.add(account)

    # Create UBI eligibility if individual
    if account_data.entity_type == EntityType.INDIVIDUAL:
        ubi = UBIEligibility(
            id=uuid.uuid4(),
            account_id=account.id,
            next_payment_date=date.today() + timedelta(days=UBI_PAYMENT_CYCLE),
            is_eligible=True
        )
        session.add(ubi)

    # Record initial deposit transaction
    if account_data.initial_deposit > 0:
        transaction = Transaction(
            id=uuid.uuid4(),
            to_account_id=account.id,
            amount=account_data.initial_deposit,
            transaction_type=TransactionType.PURCHASE,
            description="Initial account deposit"
        )
        session.add(transaction)

    session.commit()
    session.refresh(account)

    return account










async def process_ubi_payment(account_id: uuid.UUID, amount: Decimal):
    """Process UBI payment asynchronously"""
    async with db.async_pool.acquire() as conn:
        try:
            async with conn.transaction():
                # Update account balance
                await conn.execute("""
                    UPDATE accounts
                    SET balance = balance + $1, updated_at = NOW()
                    WHERE id = $2
                """, float(amount), account_id)

                # Create transaction
                transaction_id = uuid.uuid4()
                await conn.execute("""
                    INSERT INTO transactions
                    (id, to_account_id, amount, transaction_type, description, timestamp)
                    VALUES ($1, $2, $3, $4, $5, NOW())
                """, transaction_id, account_id, float(amount),
                   TransactionType.UBI_PAYMENT.value, "Universal Basic Income payment")

                # Update UBI eligibility
                next_payment = date.today() + timedelta(days=UBI_PAYMENT_CYCLE)
                await conn.execute("""
                    UPDATE ubi_eligibility
                    SET last_payment_date = $1,
                        last_payment_amount = $2,
                        next_payment_date = $3,
                        total_payments_received = total_payments_received + $2,
                        updated_at = NOW()
                    WHERE account_id = $4
                """, date.today(), float(amount), next_payment, account_id)

                logger.info(f"Processed UBI payment of {amount} to account {account_id}")

        except Exception as e:
            logger.error(f"Failed to process UBI payment: {e}")

def calculate_ubi_amount(
    account_balance: Decimal,
    system_average_balance: Decimal
) -> Decimal:
    """Calculate UBI amount with means testing"""
    base_amount = INITIAL_UBI_AMOUNT

    # Adjust based on relative wealth
    if account_balance < system_average_balance * Decimal('0.5'):
        # Boost for poorer individuals
        base_amount *= Decimal('1.3')
    elif account_balance > system_average_balance * Decimal('2.0'):
        # Reduce for wealthier individuals
        base_amount *= Decimal('0.7')

    return base_amount.quantize(Decimal('0.01'), rounding=ROUND_DOWN)


# ============= SYSTEM METRICS =============

async def get_system_metrics():
    """Get comprehensive system metrics"""
    async with db.async_pool.acquire() as conn:
        # Try to get from cache first
        cache_key = "system_metrics"
        cached = db.redis_client.get(cache_key)

        if cached:
            return json.loads(cached)

        # Calculate metrics
        metrics = {}

        # Account statistics
        result = await conn.fetchrow("""
            SELECT
                COUNT(*) as total_accounts,
                AVG(balance) as average_balance,
                SUM(balance) as total_money_supply,
                COUNT(CASE WHEN LOWER(entity_type::text) = 'individual' THEN 1 END) as individual_accounts,
                COUNT(CASE WHEN LOWER(entity_type::text) = 'business' THEN 1 END) as business_accounts,
                COUNT(CASE WHEN LOWER(entity_type::text) = 'nonprofit' THEN 1 END) as nonprofit_accounts
            FROM accounts
        """)

        metrics.update(dict(result))

        # Transaction statistics
        result = await conn.fetchrow("""
            SELECT
                COUNT(*) as total_transactions,
                SUM(amount) as total_transaction_volume,
                COUNT(CASE WHEN LOWER(transaction_type::text) = 'ubi_payment' THEN 1 END) as ubi_payments,
                COUNT(CASE WHEN LOWER(transaction_type::text) = 'tax_payment' THEN 1 END) as tax_payments
            FROM transactions
            WHERE timestamp > NOW() - INTERVAL '30 days'
        """)

        metrics.update(dict(result))

        # Market statistics
        result = await conn.fetchrow("""
            SELECT
                COUNT(*) as total_stocks,
                SUM(market_cap) as total_market_cap,
                AVG(current_price) as average_stock_price
            FROM stocks
            WHERE is_active = true
        """)

        metrics.update(dict(result))

        # Normalize DB numerics (e.g., Decimal) for cache/storage compatibility.
        encoded_metrics = jsonable_encoder(metrics)

        # Cache for 5 minutes
        db.redis_client.setex(cache_key, 300, json.dumps(encoded_metrics))

        return encoded_metrics

@app.get("/api/system/metrics")
async def get_system_metrics_endpoint():
    """Get system-wide economic metrics"""
    metrics = await get_system_metrics()

    # Add real-time data
    metrics["timestamp"] = datetime.now(timezone.utc).isoformat()
    metrics["market_open"] = is_market_open()
    metrics["currency"] = SYSTEM_CURRENCY

    return metrics

@app.get("/api/system/money-supply/history", response_model=MoneySupplyHistoryResponse)
async def get_money_supply_history(
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
    days: int = 365,
    bucket: str = "day",
):
    """Get total Dena in circulation as a time series."""
    if current_user.get("is_anonymous"):
        raise HTTPException(status_code=401, detail="Authentication required")

    safe_days = max(1, min(days, 3650))
    if bucket not in {"hour", "day", "week"}:
        raise HTTPException(status_code=400, detail="bucket must be one of: hour, day, week")
    now = datetime.now(timezone.utc)
    start_time = now - timedelta(days=safe_days)

    def floor_bucket(dt: datetime) -> datetime:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        if bucket == "hour":
            return dt.replace(minute=0, second=0, microsecond=0)
        if bucket == "week":
            monday = dt - timedelta(days=dt.weekday())
            return monday.replace(hour=0, minute=0, second=0, microsecond=0)
        return dt.replace(hour=0, minute=0, second=0, microsecond=0)

    step = timedelta(hours=1) if bucket == "hour" else timedelta(days=7) if bucket == "week" else timedelta(days=1)

    current_total_supply = Decimal("0")
    tx_rows: list[tuple[datetime, Any, Any, Decimal]] = []
    try:
        current_total_supply = Decimal(
            str(session.query(func.coalesce(func.sum(Account.balance), 0)).scalar() or 0)
        )
        tx_rows = (
            session.query(Transaction.timestamp, Transaction.from_account_id, Transaction.to_account_id, Transaction.amount)
            .filter(Transaction.timestamp >= start_time)
            .order_by(Transaction.timestamp.asc())
            .all()
        )
    except Exception as exc:
        logger.error(f"Money supply history query failed, serving fallback series: {exc}")

    delta_by_bucket: dict[datetime, Decimal] = {}
    for timestamp, from_account_id, to_account_id, amount in tx_rows:
        b = floor_bucket(timestamp)
        delta = Decimal("0")
        if from_account_id is None and to_account_id is not None:
            delta = Decimal(str(amount or 0))
        elif from_account_id is not None and to_account_id is None:
            delta = -Decimal(str(amount or 0))
        if delta:
            delta_by_bucket[b] = delta_by_bucket.get(b, Decimal("0")) + delta

    start_bucket = floor_bucket(start_time)
    end_bucket = floor_bucket(now)
    buckets: list[datetime] = []
    cursor = start_bucket
    while cursor <= end_bucket:
        buckets.append(cursor)
        cursor += step

    window_delta = sum((delta_by_bucket.get(b, Decimal("0")) for b in buckets), Decimal("0"))
    running_total = current_total_supply - window_delta

    points: list[dict[str, Any]] = []
    for b in buckets:
        running_total += delta_by_bucket.get(b, Decimal("0"))
        points.append(
            {
                "timestamp": b,
                "total_supply": running_total,
            }
        )

    return {
        "points": points,
        "current_total_supply": current_total_supply,
        "currency": SYSTEM_CURRENCY,
    }

# ============= UTILITY FUNCTIONS =============

def is_market_open() -> bool:
    """Check if stock market is open"""
    now = datetime.now(timezone.utc)

    # Check if it's a weekend
    if now.weekday() >= 5:
        return False

    # Check time (9 AM to 5 PM UTC)
    market_open = now.replace(hour=STOCK_MARKET_OPEN_HOUR, minute=0, second=0, microsecond=0)
    market_close = now.replace(hour=STOCK_MARKET_CLOSE_HOUR, minute=0, second=0, microsecond=0)

    return market_open <= now <= market_close

# ============= BACKGROUND TASKS =============

async def update_stock_prices():
    """Background task to update stock prices"""
    while True:
        if is_market_open():
            async with db.async_pool.acquire() as conn:
                try:
                    # Get all active stocks
                    stocks = await conn.fetch("SELECT * FROM stocks WHERE is_active = true")

                    for stock in stocks:
                        # Calculate price variation
                        market_sentiment = Decimal(str(random.uniform(0.4, 0.6)))  # Simulated sentiment
                        new_price = EconomicEngine.calculate_stock_price_variation(
                            Decimal(str(stock['current_price'])),
                            stock['volume'],
                            market_sentiment
                        )

                        # Update stock price
                        await conn.execute("""
                            UPDATE stocks
                            SET current_price = $1,
                                day_high = GREATEST(day_high, $1),
                                day_low = LEAST(day_low, $1),
                                last_updated = NOW()
                            WHERE id = $2
                        """, float(new_price), stock['id'])

                    logger.info(f"Updated prices for {len(stocks)} stocks")

                except Exception as e:
                    logger.error(f"Failed to update stock prices: {e}")

        await asyncio.sleep(60)  # Update every minute

async def check_and_process_proposals():
    """Background task to check and process completed proposals"""
    while True:
        async with db.async_pool.acquire() as conn:
            try:
                # Find proposals where voting has ended
                proposals = await conn.fetch("""
                    SELECT * FROM fiscal_proposals
                    WHERE status = 'voting' AND voting_end < NOW()
                """)

                for proposal in proposals:
                    # Determine if proposal passed (simple majority)
                    yes_votes = proposal['yes_votes']
                    no_votes = proposal['no_votes']

                    if yes_votes > no_votes:
                        new_status = "passed"
                        # Implement budget allocation (simplified)
                        await conn.execute("""
                            INSERT INTO budget_allocations
                            (id, fiscal_year, policy_area, allocated_amount, percentage, created_at)
                            VALUES ($1, $2, $3, $4, $5, NOW())
                            ON CONFLICT (fiscal_year, policy_area)
                            DO UPDATE SET allocated_amount = allocated_amount + $4
                        """, uuid.uuid4(), date.today().year, proposal['policy_area'],
                           proposal['proposed_budget'], Decimal('0.0'))
                    else:
                        new_status = "rejected"

                    # Update proposal status
                    await conn.execute("""
                        UPDATE fiscal_proposals
                        SET status = $1, updated_at = NOW()
                        WHERE id = $2
                    """, new_status, proposal['id'])

                    logger.info(f"Proposal {proposal['id']} {new_status}")

            except Exception as e:
                logger.error(f"Failed to process proposals: {e}")

        await asyncio.sleep(300)  # Check every 5 minutes


# ============= STARTUP TASKS =============

def _is_worker_role() -> bool:
    return ORG_RUNTIME_ROLE in {"worker", "all"}


def _worker_lock_key() -> str:
    return f"org:worker:lock:{ORG_SYSADMIN_RESOURCE_ID}"


def _try_acquire_worker_lock() -> bool:
    if not ORG_WORKER_LOCK_ENABLED:
        return True
    try:
        if not db.redis_client:
            logger.warning("Worker lock requested but Redis is unavailable; skipping worker tasks")
            return False
        acquired = bool(
            db.redis_client.set(
                _worker_lock_key(),
                str(uuid.uuid4()),
                nx=True,
                ex=ORG_WORKER_LOCK_SECONDS,
            )
        )
        if not acquired:
            logger.info("Worker lock held by another instance; skipping embedded worker tasks")
        return acquired
    except Exception as exc:
        logger.warning(f"Worker lock check failed; skipping embedded worker tasks: {exc}")
        return False


async def _start_embedded_worker_tasks() -> list[asyncio.Task]:
    if not _is_worker_role():
        logger.info("Skipping background jobs for ORG_RUNTIME_ROLE=%s", ORG_RUNTIME_ROLE)
        return []

    if ORG_ENABLE_SAMPLE_DATA:
        await create_sample_data()

    if not ORG_ENABLE_BACKGROUND_JOBS:
        logger.info("Background jobs disabled via ORG_ENABLE_BACKGROUND_JOBS=false")
        return []

    if not _try_acquire_worker_lock():
        return []

    logger.info("Starting embedded worker tasks")
    return [
        asyncio.create_task(update_stock_prices()),
        asyncio.create_task(check_and_process_proposals()),
    ]

async def create_sample_data():
    """Create sample data for demonstration"""
    async with db.async_pool.acquire() as conn:
        # Check if sample data already exists
        count = await conn.fetchval("SELECT COUNT(*) FROM accounts")

        if count > 0:
            return

        logger.info("Creating sample data...")

        # Create sample accounts
        sample_accounts = [
            ("John Doe", "john@example.com", EntityType.INDIVIDUAL, Decimal('50000.00')),
            ("Jane Smith", "jane@example.com", EntityType.INDIVIDUAL, Decimal('75000.00')),
            ("Acme Corp", "acme@example.com", EntityType.BUSINESS, Decimal('1000000.00')),
            ("Green Energy Inc", "green@example.com", EntityType.BUSINESS, Decimal('500000.00')),
            ("Community Nonprofit", "nonprofit@example.com", EntityType.NONPROFIT, Decimal('100000.00')),
        ]

        for name, email, entity_type, balance in sample_accounts:
            account_id = uuid.uuid4()

            await conn.execute("""
                INSERT INTO accounts
                (id, entity_type, name, email, balance, credit_score, is_verified, created_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, 750, true, NOW(), NOW())
            """, account_id, entity_type.value, name, email, float(balance))

            # Create UBI eligibility for individuals
            if entity_type == EntityType.INDIVIDUAL:
                await conn.execute("""
                    INSERT INTO ubi_eligibility
                    (id, account_id, is_eligible, next_payment_date, created_at, updated_at)
                    VALUES ($1, $2, true, $3, NOW(), NOW())
                """, uuid.uuid4(), account_id, date.today() + timedelta(days=7))

        # Create sample stocks
        sample_stocks = [
            ("Democratic Energy Corp", "DEC", Decimal('50.00'), 1000000, "Energy"),
            ("People's Healthcare", "PHC", Decimal('75.00'), 500000, "Healthcare"),
            ("Sustainable Agriculture", "SAC", Decimal('30.00'), 750000, "Agriculture"),
        ]

        for name, ticker, price, shares, sector in sample_stocks:
            stock_id = uuid.uuid4()

            await conn.execute("""
                INSERT INTO stocks
                (id, company_name, ticker_symbol, current_price, day_open, day_high, day_low,
                 volume, total_shares, shares_outstanding, market_cap, sector, is_active, created_at, last_updated)
                VALUES ($1, $2, $3, $4, $4, $4, $4, 0, $5, $5, $6, $7, true, NOW(), NOW())
            """, stock_id, name, ticker, float(price), shares,
               float(price * shares), sector)

        logger.info("Sample data created successfully")


# Router registration (deferred to avoid circular-import initialization issues)
from api.routers.network_orgs import router as network_orgs_router
from api.routers.network_events import router as network_events_router
from api.routers.network_scans import router as network_scans_router
from api.routers.network_chat import router as network_chat_router
from api.routers.contact import router as contact_router
from api.routers.accounts_read import router as accounts_read_router
from api.routers.authz_admin import router as authz_admin_router
from api.routers.accounts_write import router as accounts_write_router
from api.routers.transactions import router as transactions_router
from api.routers.ubi_eligibility import router as ubi_eligibility_router
from api.routers.ubi_settings import router as ubi_settings_router
from api.routers.admin_settings import router as admin_settings_router
from api.routers.admin_scans import router as admin_scans_router
from api.routers.stocks import router as stocks_router
from api.routers.portfolio import router as portfolio_router
from api.routers.insurance import router as insurance_router
from api.routers.fiscal import router as fiscal_router
from api.routers.governance import router as governance_router
from api.routers.departments import router as departments_router

app.include_router(network_orgs_router)
app.include_router(network_events_router)
app.include_router(network_scans_router)
app.include_router(network_chat_router)
app.include_router(contact_router)
app.include_router(accounts_read_router)
app.include_router(authz_admin_router)
app.include_router(accounts_write_router)
app.include_router(transactions_router)
app.include_router(ubi_eligibility_router)
app.include_router(ubi_settings_router)
app.include_router(admin_settings_router)
app.include_router(admin_scans_router)
app.include_router(stocks_router)
app.include_router(portfolio_router)
app.include_router(insurance_router)
app.include_router(fiscal_router)
app.include_router(governance_router)
app.include_router(departments_router)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        reload=True
    )
