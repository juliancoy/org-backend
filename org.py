from fastapi import FastAPI, HTTPException, Depends, status, BackgroundTasks, Request, Query, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.encoders import jsonable_encoder
from fastapi.responses import Response
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
import base64
import smtplib
import socket
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import quote, urlparse, urljoin
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
from sqlalchemy import create_engine, Column, String, Integer, Numeric, DateTime, Date, Boolean, JSON, Text, ForeignKey, Enum as SQLEnum, CheckConstraint, Index, func
from sqlalchemy.orm import sessionmaker, Session, relationship, declared_attr, declarative_base
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

# FastAPI app setup
app = FastAPI(
    title="Democratic Economic System API",
    description="A complete democratic economic system with UBI, stock market, insurance, and fiscal policy",
    version="2.0.0"
)


def _normalize_origin_entry(raw: str) -> str | None:
    value = (raw or "").strip()
    if not value:
        return None
    if value.startswith(("http://", "https://", "capacitor://")):
        return value.rstrip("/")
    # Backward compatibility: bare hostnames in env become https origins.
    return f"https://{value}".rstrip("/")


def _parse_allowed_origins(value: str) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for item in (value or "").split(","):
        candidate = _normalize_origin_entry(item)
        if not candidate or candidate in seen:
            continue
        normalized.append(candidate)
        seen.add(candidate)
    return normalized


_default_native_origins = "capacitor://localhost,http://localhost,http://127.0.0.1,https://localhost"
_allowed_origins = _parse_allowed_origins(
    ",".join(
        [
            os.environ.get("ALLOWED_ORIGINS", ""),
            os.environ.get("ORG_NATIVE_ALLOWED_ORIGINS", _default_native_origins),
        ]
    )
)
if _allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# Security
security = HTTPBearer(auto_error=False)

# Database setup
DATABASE_URL = os.environ.get(
    "COCKROACH_DB_URL",
    "cockroachdb://root@cockroach:9000/defaultdb?sslmode=disable"
)

# For asyncpg direct connection (better for complex queries)
ASYNC_DB_URL = os.environ.get(
    "COCKROACH_ASYNC_URL",
    "postgresql://root@cockroach:9000/defaultdb?sslmode=disable"
)

# Redis for caching and queuing
REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
REDIS_PORT = int(os.environ.get("REDIS_PORT", 6379))
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD", "")

# JWT settings
PIDP_JWKS_URL = os.environ.get("PIDP_JWKS_URL", "http://pidp:8000/.well-known/jwks.json")
PIDP_BASE_URL = os.environ.get("PIDP_BASE_URL", "http://pidp:8000")
PIDP_JWT_ISSUER = os.environ.get("PIDP_JWT_ISSUER")
PIDP_JWT_AUDIENCE = os.environ.get("PIDP_JWT_AUDIENCE")
ORG_ALLOWED_PAT_SCOPES = {
    item.strip()
    for item in os.environ.get("ORG_ALLOWED_PAT_SCOPES", "org_portal,org_mcp,org_admin").split(",")
    if item.strip()
}


def _parse_content_types_csv(value: str) -> set[str]:
    parsed = {
        item.strip().lower()
        for item in (value or "").split(",")
        if item.strip()
    }
    return parsed or {"image/jpeg", "image/png", "image/webp"}


ORG_BUSINESS_CARD_DEFAULT_MAX_BYTES = max(
    1024 * 1024,
    int(os.environ.get("ORG_BUSINESS_CARD_MAX_BYTES", str(8 * 1024 * 1024))),
)
ORG_BUSINESS_CARD_DEFAULT_ALLOWED_CONTENT_TYPES = _parse_content_types_csv(
    os.environ.get(
        "ORG_BUSINESS_CARD_ALLOWED_CONTENT_TYPES",
        "image/jpeg,image/png,image/webp",
    )
)
ORG_BUSINESS_CARD_DEFAULT_ENABLED = os.environ.get(
    "ORG_BUSINESS_CARD_ABUSE_ENABLED",
    "true",
).strip().lower() in {"1", "true", "yes", "on"}
ORG_BUSINESS_CARD_DEFAULT_USER_LIMIT_PER_HOUR = max(
    1,
    int(os.environ.get("ORG_BUSINESS_CARD_SUBMIT_PER_USER_PER_HOUR", "60")),
)
ORG_BUSINESS_CARD_DEFAULT_IP_LIMIT_PER_HOUR = max(
    1,
    int(os.environ.get("ORG_BUSINESS_CARD_SUBMIT_PER_IP_PER_HOUR", "120")),
)
ORG_BUSINESS_CARD_DEFAULT_GLOBAL_LIMIT_PER_HOUR = max(
    1,
    int(os.environ.get("ORG_BUSINESS_CARD_SUBMIT_GLOBAL_PER_HOUR", "2000")),
)
ORG_BUSINESS_CARD_DEFAULT_DUPLICATE_HASH_LIMIT = max(
    1,
    int(os.environ.get("ORG_BUSINESS_CARD_DUPLICATE_HASH_LIMIT", "3")),
)
ORG_BUSINESS_CARD_DEFAULT_DUPLICATE_WINDOW_SECONDS = max(
    60,
    int(os.environ.get("ORG_BUSINESS_CARD_DUPLICATE_WINDOW_SECONDS", str(24 * 3600))),
)
ORG_BUSINESS_CARD_STORAGE_ENABLED = os.environ.get(
    "ORG_BUSINESS_CARD_STORAGE_ENABLED",
    "true",
).strip().lower() in {"1", "true", "yes", "on"}
ORG_BUSINESS_CARD_STORAGE_BACKEND = os.environ.get(
    "ORG_BUSINESS_CARD_STORAGE_BACKEND",
    "local",
).strip().lower() or "local"
ORG_BUSINESS_CARD_STORAGE_DIR = os.environ.get(
    "ORG_BUSINESS_CARD_STORAGE_DIR",
    "/var/lib/org/business-cards",
).strip() or "/var/lib/org/business-cards"
ORG_BUSINESS_CARD_S3_ENDPOINT_URL = os.environ.get("ORG_BUSINESS_CARD_S3_ENDPOINT_URL", "").strip()
ORG_BUSINESS_CARD_S3_BUCKET = os.environ.get("ORG_BUSINESS_CARD_S3_BUCKET", "org-business-cards").strip() or "org-business-cards"
ORG_BUSINESS_CARD_S3_REGION = os.environ.get("ORG_BUSINESS_CARD_S3_REGION", "us-east-1").strip() or "us-east-1"
ORG_BUSINESS_CARD_S3_ACCESS_KEY = os.environ.get("ORG_BUSINESS_CARD_S3_ACCESS_KEY", "").strip()
ORG_BUSINESS_CARD_S3_SECRET_KEY = os.environ.get("ORG_BUSINESS_CARD_S3_SECRET_KEY", "").strip()
ORG_BUSINESS_CARD_S3_USE_SSL = os.environ.get("ORG_BUSINESS_CARD_S3_USE_SSL", "false").strip().lower() in {"1", "true", "yes", "on"}
ORG_BUSINESS_CARD_S3_PREFIX = os.environ.get("ORG_BUSINESS_CARD_S3_PREFIX", "business-cards").strip().strip("/")
ORG_BUSINESS_CARD_OCR_PROVIDER = os.environ.get(
    "ORG_BUSINESS_CARD_OCR_PROVIDER",
    "openai",
).strip().lower()
ORG_OPENAI_API_KEY = os.environ.get("ORG_OPENAI_API_KEY", "").strip()
ORG_OPENAI_API_BASE_URL = os.environ.get("ORG_OPENAI_API_BASE_URL", "https://api.openai.com/v1").rstrip("/")
ORG_BUSINESS_CARD_OCR_MODEL = os.environ.get("ORG_BUSINESS_CARD_OCR_MODEL", "gpt-4.1-mini").strip()
ORG_SCAN_EVENT_LINK_ENRICHMENT_ENABLED = os.environ.get(
    "ORG_SCAN_EVENT_LINK_ENRICHMENT_ENABLED",
    "true",
).strip().lower() in {"1", "true", "yes", "on"}
ORG_SCAN_AI_SUMMARY_ENABLED = os.environ.get(
    "ORG_SCAN_AI_SUMMARY_ENABLED",
    "true",
).strip().lower() in {"1", "true", "yes", "on"}
ORG_SCAN_AUTO_CLARIFICATION_ENABLED = os.environ.get(
    "ORG_SCAN_AUTO_CLARIFICATION_ENABLED",
    "true",
).strip().lower() in {"1", "true", "yes", "on"}
ORG_SCAN_AUTO_MIN_CONFIDENCE = max(
    0.0,
    min(1.0, float(os.environ.get("ORG_SCAN_AUTO_MIN_CONFIDENCE", "0.75"))),
)
ORG_SCAN_AUTO_MIN_MARGIN = max(
    0.0,
    min(1.0, float(os.environ.get("ORG_SCAN_AUTO_MIN_MARGIN", "0.20"))),
)
ORG_SMTP_HOST = os.environ.get("ORG_SMTP_HOST", "").strip()
ORG_SMTP_PORT = int(os.environ.get("ORG_SMTP_PORT", "587"))
ORG_SMTP_USERNAME = os.environ.get("ORG_SMTP_USERNAME", "").strip()
ORG_SMTP_PASSWORD = os.environ.get("ORG_SMTP_PASSWORD", "").strip()
ORG_SMTP_FROM = os.environ.get("ORG_SMTP_FROM", "").strip() or "noreply@arkavo.org"
ORG_SMTP_STARTTLS = os.environ.get("ORG_SMTP_STARTTLS", "true").strip().lower() in {"1", "true", "yes", "on"}
ORG_PORTAL_BASE_URL = os.environ.get("ORG_PORTAL_BASE_URL", "").strip()
ORG_MATRIX_HOMESERVER_URL = os.environ.get("ORG_MATRIX_HOMESERVER_URL", "http://synapse:8008").rstrip("/")
ORG_MATRIX_SERVER_NAME = os.environ.get("ORG_MATRIX_SERVER_NAME", "matrix.arkavo.org").strip()
ORG_MATRIX_ADMIN_TOKEN = os.environ.get("ORG_MATRIX_ADMIN_TOKEN", "").strip()
ORG_MATRIX_PASSWORD_SECRET = os.environ.get("ORG_MATRIX_PASSWORD_SECRET", "").strip()
ORG_MATRIX_AUTO_PROVISION_PUBLIC_ORG_ROOMS = os.environ.get(
    "ORG_MATRIX_AUTO_PROVISION_PUBLIC_ORG_ROOMS",
    "true",
).strip().lower() in {"1", "true", "yes", "on"}

# SpiceDB (authorization)
SPICEDB_HTTP_URL = os.environ.get("SPICEDB_HTTP_URL", "http://spicedb:8443").rstrip("/")
SPICEDB_PRESHARED_KEY = os.environ.get("SPICEDB_PRESHARED_KEY", "")
ORG_SYSADMIN_GROUP = os.environ.get(
    "ORG_SYSADMIN_GROUP",
    "admins",
)
ORG_SYSADMIN_RESOURCE_ID = os.environ.get(
    "ORG_SYSADMIN_RESOURCE_ID",
    "portal",
)
ORG_SYSADMIN_USER_IDS = [
    item.strip()
    for item in os.environ.get("ORG_SYSADMIN_USER_IDS", "").split(",")
    if item.strip()
]
ORG_SYSADMIN_EMAILS = [
    item.strip().lower()
    for item in os.environ.get("ORG_SYSADMIN_EMAILS", "").split(",")
    if item.strip()
]
ORG_PUBLIC_CALENDAR_FEEDS = [
    item.strip()
    for item in os.environ.get(
        "ORG_PUBLIC_CALENDAR_FEEDS",
        "https://codecollective.us/baltimore/upcoming_events.json",
    ).split(",")
    if item.strip()
]
ORG_PUBLIC_CALENDAR_PULL_INTERVAL_SECONDS = max(
    60,
    int(os.environ.get("ORG_PUBLIC_CALENDAR_PULL_INTERVAL_SECONDS", "900")),
)
ORG_PUBLIC_CALENDAR_PULL_ENABLED = os.environ.get(
    "ORG_PUBLIC_CALENDAR_PULL_ENABLED",
    "true",
).strip().lower() in {"1", "true", "yes", "on"}


def _env_truthy(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


ORG_RUNTIME_ROLE = os.environ.get("ORG_RUNTIME_ROLE", "all").strip().lower() or "all"
ORG_ENABLE_BACKGROUND_JOBS = _env_truthy("ORG_ENABLE_BACKGROUND_JOBS", default=True)
ORG_ENABLE_SAMPLE_DATA = _env_truthy("ORG_ENABLE_SAMPLE_DATA", default=False)
ORG_WORKER_LOCK_ENABLED = _env_truthy("ORG_WORKER_LOCK_ENABLED", default=False)
ORG_WORKER_LOCK_SECONDS = max(
    30,
    int(os.environ.get("ORG_WORKER_LOCK_SECONDS", "300")),
)

# System Constants
SYSTEM_CURRENCY = "DEM"
INITIAL_UBI_AMOUNT = Decimal('1000.00')
UBI_PAYMENT_CYCLE = 30
TAX_RATE_BASE = Decimal('0.15')
MINIMUM_WAGE = Decimal('15.00')
STOCK_MARKET_OPEN_HOUR = 9
STOCK_MARKET_CLOSE_HOUR = 17

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Database Base
Base = declarative_base()

# ============= DATABASE MODELS =============

class EntityType(str, Enum):
    INDIVIDUAL = "individual"
    BUSINESS = "business"
    NONPROFIT = "nonprofit"
    GOVERNMENT = "government"

class InsuranceType(str, Enum):
    LIFE = "life"
    HEALTH = "health"
    FIRE = "fire"
    ACTS_OF_GOD = "acts_of_god"

class FiscalPolicyArea(str, Enum):
    EDUCATION = "education"
    HEALTHCARE = "healthcare"
    INFRASTRUCTURE = "infrastructure"
    DEFENSE = "defense"
    ENVIRONMENT = "environment"
    SOCIAL_WELFARE = "social_welfare"
    RESEARCH = "research"
    CULTURE = "culture"

class TransactionType(str, Enum):
    UBI_PAYMENT = "ubi_payment"
    TAX_PAYMENT = "tax_payment"
    SALARY = "salary"
    PURCHASE = "purchase"
    INVESTMENT = "investment"
    DIVIDEND = "dividend"
    INSURANCE_PREMIUM = "insurance_premium"
    INSURANCE_CLAIM = "insurance_claim"
    BUSINESS_REVENUE = "business_revenue"
    DONATION = "donation"
    GRANT = "grant"
    STOCK_PURCHASE = "stock_purchase"
    STOCK_SALE = "stock_sale"
    INTEREST = "interest"

class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"

class OrderStatus(str, Enum):
    PENDING = "pending"
    EXECUTED = "executed"
    CANCELLED = "cancelled"
    PARTIAL = "partial"

class VoteType(str, Enum):
    YES = "yes"
    NO = "no"
    ABSTAIN = "abstain"


class EventHostType(str, Enum):
    UNCLAIMED = "unclaimed"
    INDIVIDUAL = "individual"
    ORG = "org"


class GovernanceMotionType(str, Enum):
    MAIN = "main"
    AMENDMENT = "amendment"
    DISSOLUTION = "dissolution"


class GovernanceMotionStatus(str, Enum):
    PROPOSED = "proposed"
    SECONDED = "seconded"
    DISCUSSION = "discussion"
    VOTING = "voting"
    PASSED = "passed"
    FAILED = "failed"
    TABLED = "tabled"
    WITHDRAWN = "withdrawn"


class GovernanceProposerType(str, Enum):
    USER = "user"
    ORG = "org"


class GovernanceVoteChoice(str, Enum):
    YEA = "yea"
    NAY = "nay"
    ABSTAIN = "abstain"


class GovernanceReactionType(str, Enum):
    UP = "up"
    DOWN = "down"

class Account(Base):
    """Financial account for individuals, businesses, nonprofits, or government"""
    __tablename__ = "accounts"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    entity_type = Column(SQLEnum(EntityType), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    email = Column(String(255), nullable=False, unique=True, index=True)
    address = Column(Text)
    balance = Column(Numeric(20, 2), nullable=False, default=Decimal('0.00'))
    credit_score = Column(Integer, nullable=False, default=650)
    created_at = Column(DateTime(timezone=True), nullable=False, default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, default=func.now(), onupdate=func.now())
    
    # Business/nonprofit specific
    business_type = Column(String(100))
    mission_statement = Column(Text)
    tax_id = Column(String(50), unique=True)
    is_verified = Column(Boolean, nullable=False, default=False)
    
    # Indexes
    __table_args__ = (
        Index('idx_accounts_email', 'email'),
        Index('idx_accounts_entity_type', 'entity_type'),
        Index('idx_accounts_created_at', 'created_at'),
        CheckConstraint('balance >= 0', name='check_balance_non_negative'),
        CheckConstraint('credit_score >= 300 AND credit_score <= 850', name='check_credit_score_range'),
    )
    
    # Relationships
    transactions_from = relationship("Transaction", foreign_keys="Transaction.from_account_id", back_populates="from_account")
    transactions_to = relationship("Transaction", foreign_keys="Transaction.to_account_id", back_populates="to_account")
    portfolio = relationship("PortfolioHolding", back_populates="account")
    insurance_policies = relationship("InsurancePolicy", back_populates="account")
    fiscal_votes = relationship("FiscalVote", back_populates="account")
    edit_requests = relationship(
        "EditRequest",
        foreign_keys="EditRequest.account_id",
        back_populates="account",
    )

class Transaction(Base):
    """Financial transaction record with double-entry accounting"""
    __tablename__ = "transactions"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    from_account_id = Column(UUID(as_uuid=True), ForeignKey('accounts.id', ondelete='SET NULL'), index=True)
    to_account_id = Column(UUID(as_uuid=True), ForeignKey('accounts.id', ondelete='SET NULL'), index=True)
    amount = Column(Numeric(20, 2), nullable=False)
    currency = Column(String(3), nullable=False, default=SYSTEM_CURRENCY)
    transaction_type = Column(SQLEnum(TransactionType), nullable=False, index=True)
    description = Column(Text, nullable=False)
    timestamp = Column(DateTime(timezone=True), nullable=False, default=func.now(), index=True)
    reference_id = Column(String(100))  # For linking to other entities
    tx_metadata = Column("metadata", JSONB)  # Additional transaction data
    
    # Indexes
    __table_args__ = (
        Index('idx_transactions_timestamp', 'timestamp'),
        Index('idx_transactions_from_account', 'from_account_id', 'timestamp'),
        Index('idx_transactions_to_account', 'to_account_id', 'timestamp'),
        Index('idx_transactions_type', 'transaction_type', 'timestamp'),
        CheckConstraint('amount > 0', name='check_amount_positive'),
    )
    
    # Relationships
    from_account = relationship("Account", foreign_keys=[from_account_id], back_populates="transactions_from")
    to_account = relationship("Account", foreign_keys=[to_account_id], back_populates="transactions_to")

class UBIEligibility(Base):
    """Universal Basic Income eligibility and payment tracking"""
    __tablename__ = "ubi_eligibility"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id = Column(UUID(as_uuid=True), ForeignKey('accounts.id', ondelete='CASCADE'), unique=True, nullable=False)
    is_eligible = Column(Boolean, nullable=False, default=True)
    next_payment_date = Column(Date, nullable=False)
    last_payment_date = Column(Date)
    last_payment_amount = Column(Numeric(20, 2))
    total_payments_received = Column(Numeric(20, 2), default=Decimal('0.00'))
    created_at = Column(DateTime(timezone=True), nullable=False, default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, default=func.now(), onupdate=func.now())
    
    # Indexes
    __table_args__ = (
        Index('idx_ubi_eligibility_next_payment', 'next_payment_date', 'is_eligible'),
        Index('idx_ubi_eligibility_account', 'account_id'),
    )
    
    # Relationships
    account = relationship("Account")

class Stock(Base):
    """Publicly traded company stock"""
    __tablename__ = "stocks"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_name = Column(String(255), nullable=False)
    ticker_symbol = Column(String(10), nullable=False, unique=True, index=True)
    current_price = Column(Numeric(20, 2), nullable=False)
    day_open = Column(Numeric(20, 2), nullable=False)
    day_high = Column(Numeric(20, 2), nullable=False)
    day_low = Column(Numeric(20, 2), nullable=False)
    volume = Column(Integer, nullable=False, default=0)
    total_shares = Column(Integer, nullable=False)
    shares_outstanding = Column(Integer, nullable=False)
    market_cap = Column(Numeric(20, 2), nullable=False)
    sector = Column(String(100), nullable=False)
    description = Column(Text)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=func.now())
    last_updated = Column(DateTime(timezone=True), nullable=False, default=func.now(), onupdate=func.now())
    
    # Indexes
    __table_args__ = (
        Index('idx_stocks_ticker', 'ticker_symbol'),
        Index('idx_stocks_sector', 'sector'),
        Index('idx_stocks_active', 'is_active'),
        CheckConstraint('current_price > 0', name='check_price_positive'),
        CheckConstraint('shares_outstanding <= total_shares', name='check_shares_outstanding'),
    )
    
    # Relationships
    holdings = relationship("PortfolioHolding", back_populates="stock")
    orders = relationship("StockOrder", back_populates="stock")

class PortfolioHolding(Base):
    """Stock holdings in accounts"""
    __tablename__ = "portfolio_holdings"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id = Column(UUID(as_uuid=True), ForeignKey('accounts.id', ondelete='CASCADE'), nullable=False)
    stock_id = Column(UUID(as_uuid=True), ForeignKey('stocks.id', ondelete='CASCADE'), nullable=False)
    quantity = Column(Integer, nullable=False, default=0)
    average_purchase_price = Column(Numeric(20, 2))
    total_invested = Column(Numeric(20, 2), default=Decimal('0.00'))
    created_at = Column(DateTime(timezone=True), nullable=False, default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, default=func.now(), onupdate=func.now())
    
    # Composite unique constraint
    __table_args__ = (
        Index('idx_portfolio_account_stock', 'account_id', 'stock_id', unique=True),
        Index('idx_portfolio_account', 'account_id'),
        CheckConstraint('quantity >= 0', name='check_quantity_non_negative'),
    )
    
    # Relationships
    account = relationship("Account", back_populates="portfolio")
    stock = relationship("Stock", back_populates="holdings")

class StockOrder(Base):
    """Stock market orders"""
    __tablename__ = "stock_orders"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id = Column(UUID(as_uuid=True), ForeignKey('accounts.id', ondelete='CASCADE'), nullable=False)
    stock_id = Column(UUID(as_uuid=True), ForeignKey('stocks.id', ondelete='CASCADE'), nullable=False)
    order_type = Column(SQLEnum(OrderType), nullable=False)
    action = Column(String(4), nullable=False)  # 'buy' or 'sell'
    quantity = Column(Integer, nullable=False)
    limit_price = Column(Numeric(20, 2))
    status = Column(SQLEnum(OrderStatus), nullable=False, default=OrderStatus.PENDING)
    executed_price = Column(Numeric(20, 2))
    executed_quantity = Column(Integer, default=0)
    timestamp = Column(DateTime(timezone=True), nullable=False, default=func.now(), index=True)
    executed_at = Column(DateTime(timezone=True))
    
    # Indexes
    __table_args__ = (
        Index('idx_orders_account', 'account_id', 'timestamp'),
        Index('idx_orders_stock', 'stock_id', 'timestamp'),
        Index('idx_orders_status', 'status', 'timestamp'),
        CheckConstraint('quantity > 0', name='check_quantity_positive'),
        CheckConstraint("action IN ('buy', 'sell')", name='check_action_valid'),
    )
    
    # Relationships
    account = relationship("Account")
    stock = relationship("Stock", back_populates="orders")

class InsurancePolicy(Base):
    """Insurance policies"""
    __tablename__ = "insurance_policies"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id = Column(UUID(as_uuid=True), ForeignKey('accounts.id', ondelete='CASCADE'), nullable=False)
    insurance_type = Column(SQLEnum(InsuranceType), nullable=False)
    coverage_amount = Column(Numeric(20, 2), nullable=False)
    premium_amount = Column(Numeric(20, 2), nullable=False)
    duration_years = Column(Integer, nullable=False, default=1)
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    beneficiaries = Column(JSONB)  # List of beneficiary account IDs
    deductible = Column(Numeric(20, 2), default=Decimal('0.00'))
    claims_made = Column(Integer, nullable=False, default=0)
    total_claims_paid = Column(Numeric(20, 2), default=Decimal('0.00'))
    created_at = Column(DateTime(timezone=True), nullable=False, default=func.now())
    
    # Indexes
    __table_args__ = (
        Index('idx_insurance_account', 'account_id'),
        Index('idx_insurance_type', 'insurance_type'),
        Index('idx_insurance_active', 'is_active', 'end_date'),
        CheckConstraint('coverage_amount > 0', name='check_coverage_positive'),
        CheckConstraint('premium_amount > 0', name='check_premium_positive'),
    )
    
    # Relationships
    account = relationship("Account", back_populates="insurance_policies")
    claims = relationship("InsuranceClaim", back_populates="policy")

class InsuranceClaim(Base):
    """Insurance claims"""
    __tablename__ = "insurance_claims"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    policy_id = Column(UUID(as_uuid=True), ForeignKey('insurance_policies.id', ondelete='CASCADE'), nullable=False)
    claim_amount = Column(Numeric(20, 2), nullable=False)
    approved_amount = Column(Numeric(20, 2))
    description = Column(Text, nullable=False)
    incident_date = Column(Date, nullable=False)
    status = Column(String(20), nullable=False, default='pending')  # pending, approved, rejected, paid
    supporting_docs = Column(JSONB)  # List of document URLs
    reviewed_by = Column(UUID(as_uuid=True), ForeignKey('accounts.id'))
    reviewed_at = Column(DateTime(timezone=True))
    paid_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), nullable=False, default=func.now())
    
    # Indexes
    __table_args__ = (
        Index('idx_claims_policy', 'policy_id'),
        Index('idx_claims_status', 'status', 'created_at'),
        CheckConstraint('claim_amount > 0', name='check_claim_amount_positive'),
    )
    
    # Relationships
    policy = relationship("InsurancePolicy", back_populates="claims")
    reviewer = relationship("Account", foreign_keys=[reviewed_by])

class FiscalProposal(Base):
    """Fiscal policy proposals for democratic voting"""
    __tablename__ = "fiscal_proposals"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=False)
    policy_area = Column(SQLEnum(FiscalPolicyArea), nullable=False, index=True)
    proposed_budget = Column(Numeric(20, 2), nullable=False)
    duration_months = Column(Integer, nullable=False)
    expected_impact = Column(Text)
    created_by = Column(UUID(as_uuid=True), ForeignKey('accounts.id'), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=func.now())
    voting_start = Column(DateTime(timezone=True))
    voting_end = Column(DateTime(timezone=True))
    status = Column(String(20), nullable=False, default='draft')  # draft, voting, passed, rejected, implemented
    yes_votes = Column(Integer, default=0)
    no_votes = Column(Integer, default=0)
    abstain_votes = Column(Integer, default=0)
    total_votes = Column(Integer, default=0)
    
    # Indexes
    __table_args__ = (
        Index('idx_proposals_status', 'status', 'voting_end'),
        Index('idx_proposals_policy_area', 'policy_area'),
        Index('idx_proposals_created_at', 'created_at'),
        CheckConstraint('proposed_budget > 0', name='check_budget_positive'),
    )
    
    # Relationships
    creator = relationship("Account")
    votes = relationship("FiscalVote", back_populates="proposal")

class FiscalVote(Base):
    """Votes on fiscal proposals"""
    __tablename__ = "fiscal_votes"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    proposal_id = Column(UUID(as_uuid=True), ForeignKey('fiscal_proposals.id', ondelete='CASCADE'), nullable=False)
    account_id = Column(UUID(as_uuid=True), ForeignKey('accounts.id', ondelete='CASCADE'), nullable=False)
    vote = Column(SQLEnum(VoteType), nullable=False)
    timestamp = Column(DateTime(timezone=True), nullable=False, default=func.now())
    rationale = Column(Text)
    
    # Composite unique constraint - one vote per account per proposal
    __table_args__ = (
        Index('idx_votes_proposal_account', 'proposal_id', 'account_id', unique=True),
        Index('idx_votes_account', 'account_id'),
        Index('idx_votes_proposal', 'proposal_id'),
    )
    
    # Relationships
    proposal = relationship("FiscalProposal", back_populates="votes")
    account = relationship("Account", back_populates="fiscal_votes")

class BudgetAllocation(Base):
    """Government budget allocations"""
    __tablename__ = "budget_allocations"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    fiscal_year = Column(Integer, nullable=False)
    policy_area = Column(SQLEnum(FiscalPolicyArea), nullable=False)
    allocated_amount = Column(Numeric(20, 2), nullable=False)
    spent_amount = Column(Numeric(20, 2), default=Decimal('0.00'))
    percentage = Column(Numeric(5, 2))  # Percentage of total budget
    created_at = Column(DateTime(timezone=True), nullable=False, default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, default=func.now(), onupdate=func.now())
    
    # Composite unique constraint
    __table_args__ = (
        Index('idx_budget_fiscal_year', 'fiscal_year', 'policy_area', unique=True),
        Index('idx_budget_policy_area', 'policy_area'),
        CheckConstraint('allocated_amount >= 0', name='check_allocated_non_negative'),
        CheckConstraint('spent_amount >= 0', name='check_spent_non_negative'),
    )

class TaxRecord(Base):
    """Tax payment records"""
    __tablename__ = "tax_records"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id = Column(UUID(as_uuid=True), ForeignKey('accounts.id', ondelete='CASCADE'), nullable=False)
    tax_year = Column(Integer, nullable=False)
    taxable_income = Column(Numeric(20, 2), nullable=False)
    tax_amount = Column(Numeric(20, 2), nullable=False)
    paid_amount = Column(Numeric(20, 2), default=Decimal('0.00'))
    status = Column(String(20), nullable=False, default='unpaid')  # unpaid, partial, paid
    due_date = Column(Date, nullable=False)
    paid_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), nullable=False, default=func.now())
    
    # Indexes
    __table_args__ = (
        Index('idx_tax_account_year', 'account_id', 'tax_year', unique=True),
        Index('idx_tax_status_due', 'status', 'due_date'),
        CheckConstraint('taxable_income >= 0', name='check_income_non_negative'),
        CheckConstraint('tax_amount >= 0', name='check_tax_non_negative'),
    )
    
    # Relationships
    account = relationship("Account")

class EditRequest(Base):
    """Request to edit account information (for KYC/verification)"""
    __tablename__ = "edit_requests"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id = Column(UUID(as_uuid=True), ForeignKey('accounts.id', ondelete='CASCADE'), nullable=False)
    field_name = Column(String(100), nullable=False)
    old_value = Column(Text)
    new_value = Column(Text, nullable=False)
    status = Column(String(20), nullable=False, default='pending')  # pending, approved, rejected
    reviewed_by = Column(UUID(as_uuid=True), ForeignKey('accounts.id'))
    reviewed_at = Column(DateTime(timezone=True))
    message = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, default=func.now())
    
    # Indexes
    __table_args__ = (
        Index('idx_edit_requests_account', 'account_id', 'status'),
        Index('idx_edit_requests_status', 'status', 'created_at'),
    )
    
    # Relationships
    account = relationship(
        "Account",
        foreign_keys=[account_id],
        back_populates="edit_requests",
    )
    reviewer = relationship("Account", foreign_keys=[reviewed_by])


class Organization(Base):
    """LinkedIn-like organization profile that can be seeded or user-created."""
    __tablename__ = "organizations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(255), nullable=False, index=True)
    slug = Column(String(255), nullable=False, unique=True, index=True)
    description = Column(Text)
    source_url = Column(Text, unique=True, index=True)
    source_urls = Column(JSONB)
    image_url = Column(Text)
    tags = Column(JSONB)
    seeded_from_events = Column(Boolean, nullable=False, default=False)
    claimed_by_user_id = Column(String(255), index=True)
    created_by_user_id = Column(String(255), index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, default=func.now(), onupdate=func.now())

    memberships = relationship("OrganizationMembership", back_populates="organization", cascade="all, delete-orphan")
    # Let DB-level FK ondelete behavior handle parent deletes; avoid ORM nulling
    # host_org_id during merges, which can violate host_type/org binding checks.
    hosted_events = relationship("NetworkEvent", back_populates="host_org", passive_deletes=True)


class NetworkEvent(Base):
    """Network event with explicit host binding and optional ownership claim."""
    __tablename__ = "network_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title = Column(String(255), nullable=False, index=True)
    slug = Column(String(255), nullable=False, unique=True, index=True)
    description = Column(Text)
    starts_at = Column(DateTime(timezone=True), index=True)
    ends_at = Column(DateTime(timezone=True))
    location = Column(String(255), index=True)
    source_url = Column(Text, unique=True, index=True)
    ingest_key = Column(String(255), unique=True, index=True)
    image_url = Column(Text)
    tags = Column(JSONB)
    host_type = Column(String(20), nullable=False, default=EventHostType.UNCLAIMED.value, index=True)
    host_user_id = Column(String(255), index=True)
    host_org_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="SET NULL"), index=True)
    claimed_by_user_id = Column(String(255), index=True)
    created_by_user_id = Column(String(255), index=True)
    seeded_from_events = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint(
            "host_type IN ('unclaimed', 'individual', 'org')",
            name="check_network_event_host_type",
        ),
        CheckConstraint(
            "("
            "(host_type = 'unclaimed' AND host_user_id IS NULL AND host_org_id IS NULL) OR "
            "(host_type = 'individual' AND host_user_id IS NOT NULL AND host_org_id IS NULL) OR "
            "(host_type = 'org' AND host_org_id IS NOT NULL AND host_user_id IS NULL)"
            ")",
            name="check_network_event_host_binding",
        ),
        CheckConstraint(
            "(ends_at IS NULL OR starts_at IS NULL OR ends_at >= starts_at)",
            name="check_network_event_time_range",
        ),
    )

    host_org = relationship("Organization", back_populates="hosted_events")


class OrganizationMembership(Base):
    """Membership relation between users and organizations."""
    __tablename__ = "organization_memberships"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(String(255), nullable=False, index=True)
    user_email = Column(String(255))
    user_name = Column(String(255))
    role = Column(String(50), nullable=False, default="member")  # member|admin
    created_at = Column(DateTime(timezone=True), nullable=False, default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("idx_org_membership_org_user_unique", "organization_id", "user_id", unique=True),
    )

    organization = relationship("Organization", back_populates="memberships")


class Team(Base):
    """Constitution-aligned team entity."""
    __tablename__ = "teams"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(255), nullable=False, index=True)
    slug = Column(String(255), nullable=False, unique=True, index=True)
    description = Column(Text)
    status = Column(String(32), nullable=False, default="active", index=True)
    created_by_user_id = Column(String(255), index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint("status IN ('active','inactive','archived')", name="check_team_status"),
    )

    memberships = relationship("TeamMembership", back_populates="team", cascade="all, delete-orphan")


class TeamMembership(Base):
    """Membership relation between users and teams."""
    __tablename__ = "team_memberships"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    team_id = Column(UUID(as_uuid=True), ForeignKey("teams.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(String(255), nullable=False, index=True)
    user_email = Column(String(255))
    user_name = Column(String(255))
    role = Column(String(50), nullable=False, default="member")  # member|lead
    active = Column(Boolean, nullable=False, default=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("idx_team_membership_team_user_unique", "team_id", "user_id", unique=True),
        CheckConstraint("role IN ('member','lead')", name="check_team_membership_role"),
    )

    team = relationship("Team", back_populates="memberships")


class EventAttendance(Base):
    """Attendance records used to derive Attendee class eligibility."""
    __tablename__ = "event_attendance"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id = Column(UUID(as_uuid=True), ForeignKey("network_events.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(String(255), nullable=False, index=True)
    user_email = Column(String(255))
    user_name = Column(String(255))
    attended_at = Column(DateTime(timezone=True), nullable=False, default=func.now(), index=True)
    source = Column(String(64), nullable=False, default="self_checkin")
    verified_by_user_id = Column(String(255), index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=func.now())

    __table_args__ = (
        Index("idx_event_attendance_event_user_unique", "event_id", "user_id", unique=True),
    )


class BusinessCardSubmission(Base):
    """Captured business card submission and resulting onboarding state."""
    __tablename__ = "business_card_submissions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    submitted_by_user_id = Column(String(255), nullable=False, index=True)
    submitted_by_email = Column(String(255))
    submitted_by_name = Column(String(255))
    image_filename = Column(String(512))
    image_content_type = Column(String(100), nullable=False)
    image_size_bytes = Column(Integer, nullable=False)
    image_sha256 = Column(String(64), nullable=False, index=True)
    image_storage_backend = Column(String(32))
    image_storage_bucket = Column(String(255))
    image_storage_path = Column(String(1024))
    image_storage_error = Column(Text)
    ocr_provider = Column(String(64), nullable=False)
    ocr_text = Column(Text, nullable=False)
    extracted_name = Column(String(255))
    extracted_title = Column(String(255))
    extracted_company = Column(String(255))
    extracted_email = Column(String(255), index=True)
    extracted_phone = Column(String(80))
    extracted_website = Column(String(255))
    extracted_address = Column(Text)
    extracted_metadata = Column(JSONB, default=dict)
    notes = Column(Text)
    pidp_user_created = Column(Boolean, nullable=False, default=False)
    pidp_user_id = Column(String(255), index=True)
    notification_email_sent = Column(Boolean, nullable=False, default=False)
    notification_error = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, default=func.now(), onupdate=func.now())

    @property
    def image_stored(self) -> bool:
        return bool((self.image_storage_path or "").strip())

    @property
    def scan_kind_requested(self) -> str:
        metadata = self.extracted_metadata if isinstance(self.extracted_metadata, dict) else {}
        value = str(metadata.get("scan_kind_requested") or "").strip().lower()
        return value or "auto"

    @property
    def scan_kind(self) -> str:
        metadata = self.extracted_metadata if isinstance(self.extracted_metadata, dict) else {}
        value = str(metadata.get("scan_kind") or "").strip().lower()
        return value or "person"

    @property
    def created_target_type(self) -> Optional[str]:
        metadata = self.extracted_metadata if isinstance(self.extracted_metadata, dict) else {}
        value = str(metadata.get("created_target_type") or "").strip().lower()
        return value or None

    @property
    def created_target_id(self) -> Optional[str]:
        metadata = self.extracted_metadata if isinstance(self.extracted_metadata, dict) else {}
        value = str(metadata.get("created_target_id") or "").strip()
        return value or None

    @property
    def created_target_slug(self) -> Optional[str]:
        metadata = self.extracted_metadata if isinstance(self.extracted_metadata, dict) else {}
        value = str(metadata.get("created_target_slug") or "").strip()
        return value or None

    @property
    def created_target_name(self) -> Optional[str]:
        metadata = self.extracted_metadata if isinstance(self.extracted_metadata, dict) else {}
        value = str(metadata.get("created_target_name") or "").strip()
        return value or None

    @property
    def created_targets(self) -> List[Dict[str, Optional[str]]]:
        metadata = self.extracted_metadata if isinstance(self.extracted_metadata, dict) else {}
        raw_targets = metadata.get("created_targets")
        normalized: List[Dict[str, Optional[str]]] = []
        if isinstance(raw_targets, list):
            for item in raw_targets:
                if not isinstance(item, dict):
                    continue
                target_type = str(item.get("type") or "").strip().lower()
                target_id = str(item.get("id") or "").strip()
                target_slug = str(item.get("slug") or "").strip() or None
                target_name = str(item.get("name") or "").strip() or None
                target_url = str(item.get("url") or "").strip() or None
                target_image_url = str(item.get("image_url") or "").strip() or None
                target_summary = str(item.get("summary") or "").strip() or None
                if not target_type:
                    continue
                normalized.append(
                    {
                        "type": target_type,
                        "id": target_id or None,
                        "slug": target_slug,
                        "name": target_name,
                        "url": target_url,
                        "image_url": target_image_url,
                        "summary": target_summary,
                    }
                )
        if normalized:
            return normalized
        if self.created_target_type:
            fallback_url = None
            if self.created_target_type == "organization" and self.created_target_slug:
                fallback_url = f"/orgs/{self.created_target_slug}"
            elif self.created_target_type == "event" and self.created_target_slug:
                fallback_url = f"/events/{self.created_target_slug}"
            return [
                {
                    "type": self.created_target_type,
                    "id": self.created_target_id,
                    "slug": self.created_target_slug,
                    "name": self.created_target_name,
                    "url": fallback_url,
                }
            ]
        return []

    @property
    def clarification_required(self) -> bool:
        metadata = self.extracted_metadata if isinstance(self.extracted_metadata, dict) else {}
        return bool(metadata.get("clarification_required", False))

    @property
    def clarification_message(self) -> Optional[str]:
        metadata = self.extracted_metadata if isinstance(self.extracted_metadata, dict) else {}
        value = str(metadata.get("clarification_message") or "").strip()
        return value or None

    @property
    def processing_status(self) -> str:
        metadata = self.extracted_metadata if isinstance(self.extracted_metadata, dict) else {}
        value = str(metadata.get("processing_status") or "").strip().lower()
        return value or "processed"


class UserContactPage(Base):
    """Public optional contact page for a user."""
    __tablename__ = "user_contact_pages"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(String(255), nullable=False, unique=True, index=True)
    user_email = Column(String(255))
    user_name = Column(String(255))
    slug = Column(String(255), nullable=False, unique=True, index=True)
    enabled = Column(Boolean, nullable=False, default=False)
    headline = Column(String(255))
    bio = Column(Text)
    photo_url = Column(Text)
    email_public = Column(String(255))
    phone_public = Column(String(64))
    linkedin_url = Column(Text)
    github_url = Column(Text)
    x_url = Column(Text)
    website_url = Column(Text)
    source_profile_url = Column(Text)
    source_profile_imported_at = Column(DateTime(timezone=True))
    links = Column(JSONB)
    created_at = Column(DateTime(timezone=True), nullable=False, default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, default=func.now(), onupdate=func.now())


class OrganizationClaimRequest(Base):
    """Contested claim requests for already-claimed organizations."""
    __tablename__ = "organization_claim_requests"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    requested_by_user_id = Column(String(255), nullable=False, index=True)
    requested_by_email = Column(String(255))
    requested_by_name = Column(String(255))
    message = Column(Text)
    status = Column(String(50), nullable=False, default="pending")  # pending|approved|rejected
    reviewed_by_user_id = Column(String(255))
    reviewed_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), nullable=False, default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("idx_org_claim_request_unique_pending", "organization_id", "requested_by_user_id", "status"),
    )


class NetworkAuditEvent(Base):
    """Audit log for org-network actions."""
    __tablename__ = "network_audit_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    actor_user_id = Column(String(255), index=True)
    actor_email = Column(String(255))
    event_type = Column(String(100), nullable=False, index=True)
    target_type = Column(String(100), nullable=False)
    target_id = Column(String(255), nullable=False, index=True)
    metadata_json = Column("metadata", JSONB)
    created_at = Column(DateTime(timezone=True), nullable=False, default=func.now(), index=True)


class GovernanceMotion(Base):
    __tablename__ = "governance_motions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    type = Column(String(32), nullable=False, default=GovernanceMotionType.MAIN.value, index=True)
    parent_motion_id = Column(UUID(as_uuid=True), ForeignKey("governance_motions.id", ondelete="SET NULL"), index=True)
    title = Column(String(255), nullable=False)
    body = Column(Text, nullable=False)
    proposed_body_diff = Column(Text)
    status = Column(String(32), nullable=False, default=GovernanceMotionStatus.PROPOSED.value, index=True)
    proposer_type = Column(String(16), nullable=False, default=GovernanceProposerType.USER.value, index=True)
    proposer_user_id = Column(String(255), nullable=False, index=True)
    proposer_name = Column(String(255), nullable=False)
    proposer_user_name = Column(String(255))
    proposer_org_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="SET NULL"), index=True)
    proposer_org_name = Column(String(255))
    seconder_id = Column(String(255), index=True)
    seconder_name = Column(String(255))
    discussion_deadline = Column(DateTime(timezone=True))
    voting_deadline = Column(DateTime(timezone=True))
    quorum_required = Column(Integer, nullable=False, default=5)
    created_at = Column(DateTime(timezone=True), nullable=False, default=func.now(), index=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=func.now(), onupdate=func.now(), index=True)

    __table_args__ = (
        Index("idx_governance_motions_status_created", "status", "created_at"),
        CheckConstraint("quorum_required >= 1", name="check_governance_motion_quorum_positive"),
        CheckConstraint("type IN ('main','amendment','dissolution')", name="check_governance_motion_type"),
        CheckConstraint(
            "status IN ('proposed','seconded','discussion','voting','passed','failed','tabled','withdrawn')",
            name="check_governance_motion_status",
        ),
        CheckConstraint("proposer_type IN ('user','org')", name="check_governance_motion_proposer_type"),
    )

    votes = relationship("GovernanceVote", back_populates="motion", cascade="all, delete-orphan")
    comments = relationship("GovernanceComment", back_populates="motion", cascade="all, delete-orphan")
    reactions = relationship("GovernanceReaction", back_populates="motion", cascade="all, delete-orphan")
    dissolution_plan = relationship(
        "GovernanceDissolutionPlan",
        back_populates="motion",
        cascade="all, delete-orphan",
        uselist=False,
    )


class GovernanceDissolutionPlan(Base):
    __tablename__ = "governance_dissolution_plans"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    motion_id = Column(
        UUID(as_uuid=True),
        ForeignKey("governance_motions.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    asset_disposition = Column(Text, nullable=False)
    asset_recipient_name = Column(String(255), nullable=False)
    asset_recipient_type = Column(String(32), nullable=False, default="other_legal_entity")
    legal_compliance_notes = Column(Text)
    executed_at = Column(DateTime(timezone=True))
    executed_by_user_id = Column(String(255), index=True)
    execution_notes = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint(
            "asset_recipient_type IN ('non_profit','other_legal_entity')",
            name="check_dissolution_recipient_type",
        ),
    )

    motion = relationship("GovernanceMotion", back_populates="dissolution_plan")


class GovernanceVote(Base):
    __tablename__ = "governance_votes"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    motion_id = Column(UUID(as_uuid=True), ForeignKey("governance_motions.id", ondelete="CASCADE"), nullable=False, index=True)
    voter_user_id = Column(String(255), nullable=False, index=True)
    voter_name = Column(String(255), nullable=False)
    choice = Column(String(16), nullable=False, index=True)
    cast_at = Column(DateTime(timezone=True), nullable=False, default=func.now(), index=True)

    __table_args__ = (
        Index("idx_governance_votes_motion_user_unique", "motion_id", "voter_user_id", unique=True),
        CheckConstraint("choice IN ('yea','nay','abstain')", name="check_governance_vote_choice"),
    )

    motion = relationship("GovernanceMotion", back_populates="votes")


class GovernanceComment(Base):
    __tablename__ = "governance_comments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    motion_id = Column(UUID(as_uuid=True), ForeignKey("governance_motions.id", ondelete="CASCADE"), nullable=False, index=True)
    author_id = Column(String(255), nullable=False, index=True)
    author_name = Column(String(255), nullable=False)
    body = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=func.now(), index=True)

    motion = relationship("GovernanceMotion", back_populates="comments")


class GovernanceReaction(Base):
    __tablename__ = "governance_reactions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    motion_id = Column(UUID(as_uuid=True), ForeignKey("governance_motions.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(String(255), nullable=False, index=True)
    direction = Column(String(8), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("idx_governance_reactions_motion_user_unique", "motion_id", "user_id", unique=True),
        CheckConstraint("direction IN ('up','down')", name="check_governance_reaction_direction"),
    )

    motion = relationship("GovernanceMotion", back_populates="reactions")

# ============= PYDANTIC MODELS =============

class AccountCreate(BaseModel):
    entity_type: EntityType
    name: str = Field(..., min_length=1, max_length=255)
    email: str = Field(..., pattern=r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$')
    address: Optional[str] = None
    business_type: Optional[str] = None
    mission_statement: Optional[str] = None
    initial_deposit: Decimal = Field(Decimal('0.00'), ge=0)

class AccountUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    address: Optional[str] = None
    business_type: Optional[str] = None
    mission_statement: Optional[str] = None

class AccountResponse(BaseModel):
    id: uuid.UUID
    entity_type: EntityType
    name: str
    email: str
    address: Optional[str]
    balance: Decimal
    credit_score: int
    created_at: datetime
    business_type: Optional[str] = None
    mission_statement: Optional[str] = None
    tax_id: Optional[str] = None
    is_verified: bool
    
    model_config = ConfigDict(from_attributes=True)

class AccountListItemResponse(BaseModel):
    id: uuid.UUID
    entity_type: EntityType
    name: str
    email: str
    balance: Decimal
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

class MoneySupplyPointResponse(BaseModel):
    timestamp: datetime
    total_supply: Decimal

class MoneySupplyHistoryResponse(BaseModel):
    points: List[MoneySupplyPointResponse]
    current_total_supply: Decimal
    currency: str

class TransactionCreate(BaseModel):
    to_account_id: Optional[uuid.UUID] = None
    amount: Decimal = Field(..., gt=0)
    transaction_type: TransactionType
    description: str
    reference_id: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None

class TransactionResponse(BaseModel):
    id: uuid.UUID
    from_account_id: Optional[uuid.UUID]
    to_account_id: Optional[uuid.UUID]
    amount: Decimal
    currency: str
    transaction_type: TransactionType
    description: str
    timestamp: datetime
    reference_id: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = Field(None, alias="tx_metadata")
    
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

class RecentTransactionResponse(BaseModel):
    id: uuid.UUID
    timestamp: datetime
    transaction_type: TransactionType
    amount: Decimal
    currency: str
    description: str
    from_account_id: Optional[uuid.UUID] = None
    to_account_id: Optional[uuid.UUID] = None
    from_account_name: Optional[str] = None
    to_account_name: Optional[str] = None

class AccountAutomationResponse(BaseModel):
    account_id: uuid.UUID
    name: str
    email: str
    balance: Decimal
    currency: str
    account_endpoint: str
    incoming_transactions_endpoint: str
    all_transactions_endpoint: str
    send_payment_endpoint: str
    send_url_template: str
    updated_at: datetime

class StockCreate(BaseModel):
    company_name: str = Field(..., min_length=1, max_length=255)
    ticker_symbol: str = Field(..., min_length=1, max_length=10, pattern=r'^[A-Z]{1,10}$')
    total_shares: int = Field(..., gt=0)
    initial_price: Decimal = Field(..., gt=0)
    sector: str
    description: Optional[str] = None

class StockOrderCreate(BaseModel):
    stock_id: uuid.UUID
    quantity: int = Field(..., gt=0)
    order_type: OrderType
    limit_price: Optional[Decimal] = Field(None, gt=0)
    action: str = Field(..., pattern='^(buy|sell)$')

class InsurancePolicyCreate(BaseModel):
    insurance_type: InsuranceType
    coverage_amount: Decimal = Field(..., gt=0)
    duration_years: int = Field(1, ge=1, le=30)
    beneficiaries: Optional[List[uuid.UUID]] = None
    deductible: Optional[Decimal] = Field(None, ge=0)

class InsuranceClaimCreate(BaseModel):
    policy_id: uuid.UUID
    claim_amount: Decimal = Field(..., gt=0)
    description: str
    incident_date: date
    supporting_docs: Optional[List[str]] = None

class FiscalProposalCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)
    description: str
    policy_area: FiscalPolicyArea
    proposed_budget: Decimal = Field(..., gt=0)
    duration_months: int = Field(..., gt=0, le=120)
    expected_impact: str
    voting_days: int = Field(7, ge=1, le=30)

class FiscalVoteCreate(BaseModel):
    vote: VoteType
    rationale: Optional[str] = None

class TaxEstimate(BaseModel):
    taxable_income: Decimal = Field(..., ge=0)
    tax_year: int

class UBIRuntimeSettingsResponse(BaseModel):
    interval_seconds: int
    dena_annual: Decimal
    dena_precision: int
    entity_types: List[str]
    updated_at: datetime
    updated_by: Optional[str] = None

class UBIRuntimeSettingsUpdate(BaseModel):
    interval_seconds: Optional[int] = Field(None, ge=1, le=86400)
    dena_annual: Optional[Decimal] = Field(None, ge=Decimal("0"))
    dena_precision: Optional[int] = Field(None, ge=0, le=12)
    entity_types: Optional[List[str]] = None

    @field_validator("entity_types")
    @classmethod
    def validate_entity_types(cls, value):
        if value is None:
            return value
        cleaned = [item.strip() for item in value if item and item.strip()]
        if not cleaned:
            raise ValueError("entity_types must contain at least one value")
        return cleaned


class BusinessCardAbuseSettingsResponse(BaseModel):
    enabled: bool
    per_user_limit_per_hour: int
    per_ip_limit_per_hour: int
    global_limit_per_hour: int
    duplicate_hash_limit: int
    duplicate_hash_window_seconds: int
    max_bytes: int
    allowed_content_types: List[str]
    event_link_enrichment_enabled: bool = True
    auto_clarification_enabled: bool = True
    auto_min_confidence: float = 0.75
    auto_min_margin: float = 0.2
    updated_at: datetime
    updated_by: Optional[str] = None


class BusinessCardAbuseSettingsUpdate(BaseModel):
    enabled: Optional[bool] = None
    per_user_limit_per_hour: Optional[int] = Field(None, ge=1, le=2000)
    per_ip_limit_per_hour: Optional[int] = Field(None, ge=1, le=10000)
    global_limit_per_hour: Optional[int] = Field(None, ge=1, le=50000)
    duplicate_hash_limit: Optional[int] = Field(None, ge=1, le=100)
    duplicate_hash_window_seconds: Optional[int] = Field(None, ge=60, le=30 * 24 * 3600)
    max_bytes: Optional[int] = Field(None, ge=1024 * 100, le=25 * 1024 * 1024)
    allowed_content_types: Optional[List[str]] = None
    event_link_enrichment_enabled: Optional[bool] = None
    auto_clarification_enabled: Optional[bool] = None
    auto_min_confidence: Optional[float] = Field(None, ge=0.0, le=1.0)
    auto_min_margin: Optional[float] = Field(None, ge=0.0, le=1.0)

    @field_validator("allowed_content_types")
    @classmethod
    def validate_allowed_content_types(cls, value):
        if value is None:
            return value
        cleaned = sorted({item.strip().lower() for item in value if item and item.strip()})
        if not cleaned:
            raise ValueError("allowed_content_types must contain at least one value")
        invalid = [item for item in cleaned if "/" not in item or not item.startswith("image/")]
        if invalid:
            raise ValueError(f"Only image/* content types are allowed: {', '.join(invalid)}")
        return cleaned


class OrganizationCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    source_url: Optional[str] = None
    image_url: Optional[str] = None
    tags: Optional[List[str]] = None
    claim_on_create: bool = True


class OrganizationUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    image_url: Optional[str] = None
    tags: Optional[List[str]] = None


class OrganizationMembershipUpsert(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=255)
    user_email: Optional[str] = None
    user_name: Optional[str] = None
    role: str = Field("member", pattern="^(member|admin)$")


class OrganizationMembershipUpdate(BaseModel):
    role: str = Field(..., pattern="^(member|admin)$")


class OrganizationClaimRequestCreate(BaseModel):
    message: Optional[str] = Field(None, max_length=4000)


class OrganizationMergeRequest(BaseModel):
    source_organization_id: uuid.UUID


class OrganizationClaimRequestResponse(BaseModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    requested_by_user_id: str
    requested_by_email: Optional[str] = None
    requested_by_name: Optional[str] = None
    message: Optional[str] = None
    status: str
    reviewed_by_user_id: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class OrganizationClaimRequestQueueItemResponse(BaseModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    organization_name: str
    organization_slug: str
    organization_claimed_by_user_id: Optional[str] = None
    requested_by_user_id: str
    requested_by_email: Optional[str] = None
    requested_by_name: Optional[str] = None
    message: Optional[str] = None
    status: str
    reviewed_by_user_id: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    created_at: datetime


class OrganizationMembershipResponse(BaseModel):
    user_id: str
    user_email: Optional[str] = None
    user_name: Optional[str] = None
    role: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class TeamCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None


class TeamMembershipUpsert(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=255)
    user_email: Optional[str] = None
    user_name: Optional[str] = None
    role: str = Field("member", pattern="^(member|lead)$")
    active: bool = True


class TeamMembershipResponse(BaseModel):
    user_id: str
    user_email: Optional[str] = None
    user_name: Optional[str] = None
    role: str
    active: bool
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class TeamResponse(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    description: Optional[str] = None
    status: str
    created_by_user_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class EventAttendanceRecordResponse(BaseModel):
    id: uuid.UUID
    event_id: uuid.UUID
    user_id: str
    user_email: Optional[str] = None
    user_name: Optional[str] = None
    attended_at: datetime
    source: str
    verified_by_user_id: Optional[str] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class BusinessCardSubmissionResponse(BaseModel):
    id: uuid.UUID
    submitted_by_user_id: str
    image_filename: Optional[str] = None
    image_content_type: str
    image_size_bytes: int
    image_stored: bool = False
    ocr_provider: str
    extracted_name: Optional[str] = None
    extracted_title: Optional[str] = None
    extracted_company: Optional[str] = None
    extracted_email: Optional[str] = None
    extracted_phone: Optional[str] = None
    extracted_website: Optional[str] = None
    extracted_address: Optional[str] = None
    scan_kind_requested: str = "auto"
    scan_kind: str = "person"
    created_target_type: Optional[str] = None
    created_target_id: Optional[str] = None
    created_target_slug: Optional[str] = None
    created_target_name: Optional[str] = None
    created_targets: List[Dict[str, Optional[str]]] = Field(default_factory=list)
    clarification_required: bool = False
    clarification_message: Optional[str] = None
    processing_status: str = "processed"
    extracted_metadata: Dict[str, Any] = Field(default_factory=dict)
    pidp_user_created: bool
    pidp_user_id: Optional[str] = None
    notification_email_sent: bool
    notification_error: Optional[str] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class MatrixBootstrapSessionResponse(BaseModel):
    access_token: str
    user_id: str
    device_id: Optional[str] = None
    homeserver_url: str


class OrgChatRoomDirectoryItemResponse(BaseModel):
    organization_id: uuid.UUID
    organization_name: str
    organization_slug: str
    relationship_status: str = Field(..., pattern="^(attendee|member|admin)$")
    room_id: str
    room_alias: Optional[str] = None
    room_name: Optional[str] = None


class ChatLinkPreviewResponse(BaseModel):
    url: str
    canonical_url: str
    title: Optional[str] = None
    description: Optional[str] = None
    image_url: Optional[str] = None
    site_name: Optional[str] = None
    domain: Optional[str] = None


class AccessClassSnapshotResponse(BaseModel):
    is_public: bool
    is_attendee: bool
    is_member: bool
    is_org_admin: bool
    is_sysadmin: bool
    reasons: List[str] = Field(default_factory=list)


class OrganizationResponse(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    description: Optional[str] = None
    source_url: Optional[str] = None
    source_urls: List[str] = Field(default_factory=list)
    image_url: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    seeded_from_events: bool
    claimed_by_user_id: Optional[str] = None
    created_by_user_id: Optional[str] = None
    membership_count: int = 0
    my_role: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class PublicOrganizationResponse(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    description: Optional[str] = None
    source_url: Optional[str] = None
    source_urls: List[str] = Field(default_factory=list)
    image_url: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    seeded_from_events: bool
    upcoming_events_count: int = 0
    pending_claim_requests_count: int = 0
    is_contested: bool = False
    redirected_from_slug: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class PublicOrganizationListItemResponse(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    description: Optional[str] = None
    source_url: Optional[str] = None
    source_urls: List[str] = Field(default_factory=list)
    image_url: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    seeded_from_events: bool
    membership_count: int = 0
    upcoming_events_count: int = 0
    pending_claim_requests_count: int = 0
    is_contested: bool = False
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class PublicOrganizationAdminResponse(BaseModel):
    user_id: str
    user_name: Optional[str] = None
    user_email: Optional[str] = None
    role: str = "admin"


class NetworkEventCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    location: Optional[str] = Field(None, max_length=255)
    source_url: Optional[str] = None
    image_url: Optional[str] = None
    tags: Optional[List[str]] = None
    host_type: str = Field(EventHostType.UNCLAIMED.value, pattern="^(unclaimed|individual|org)$")
    host_user_id: Optional[str] = Field(None, min_length=1, max_length=255)
    host_org_id: Optional[uuid.UUID] = None
    claim_on_create: bool = False


class NetworkEventClaimRequest(BaseModel):
    host_type: str = Field(..., pattern="^(individual|org)$")
    host_user_id: Optional[str] = Field(None, min_length=1, max_length=255)
    host_org_id: Optional[uuid.UUID] = None


class NetworkEventResponse(BaseModel):
    id: uuid.UUID
    title: str
    slug: str
    description: Optional[str] = None
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    location: Optional[str] = None
    source_url: Optional[str] = None
    image_url: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    host_type: str
    host_user_id: Optional[str] = None
    host_org_id: Optional[uuid.UUID] = None
    host_org_name: Optional[str] = None
    claimed_by_user_id: Optional[str] = None
    created_by_user_id: Optional[str] = None
    seeded_from_events: bool
    represented_in_codecollective_source: bool
    is_unclaimed: bool
    my_host_role: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class GovernanceMotionCreate(BaseModel):
    type: str = Field(GovernanceMotionType.MAIN.value, pattern="^(main|amendment|dissolution)$")
    parent_motion_id: Optional[uuid.UUID] = None
    title: str = Field(..., min_length=1, max_length=255)
    body: str = Field(..., min_length=1)
    proposed_body_diff: Optional[str] = None
    proposer_type: str = Field(GovernanceProposerType.USER.value, pattern="^(user|org)$")
    proposer_org_id: Optional[uuid.UUID] = None
    quorum_required: int = Field(5, ge=1, le=100000)
    dissolution_asset_disposition: Optional[str] = None
    dissolution_asset_recipient_name: Optional[str] = None
    dissolution_asset_recipient_type: Optional[str] = Field(
        None,
        pattern="^(non_profit|other_legal_entity)$",
    )
    dissolution_legal_compliance_notes: Optional[str] = None


class NetworkEventPublicFeedResponse(BaseModel):
    generated_at: datetime
    total: int
    events: List[NetworkEventResponse] = Field(default_factory=list)


class GovernanceMotionResponse(BaseModel):
    id: uuid.UUID
    type: str
    parent_motion_id: Optional[uuid.UUID] = None
    title: str
    body: str
    proposed_body_diff: Optional[str] = None
    status: str
    proposer_type: str
    proposer_id: str
    proposer_name: str
    proposer_user_name: Optional[str] = None
    proposer_org_id: Optional[uuid.UUID] = None
    proposer_org_name: Optional[str] = None
    seconder_id: Optional[str] = None
    seconder_name: Optional[str] = None
    discussion_deadline: Optional[datetime] = None
    voting_deadline: Optional[datetime] = None
    quorum_required: int
    is_dissolution: bool = False
    created_at: datetime
    updated_at: datetime


class GovernanceMotionVoteCastRequest(BaseModel):
    choice: str = Field(..., pattern="^(yea|nay|abstain)$")


class GovernanceCommentCreate(BaseModel):
    body: str = Field(..., min_length=1, max_length=10000)


class GovernanceCommentResponse(BaseModel):
    id: uuid.UUID
    motion_id: uuid.UUID
    author_id: str
    author_name: str
    body: str
    created_at: datetime


class GovernanceReactionResponse(BaseModel):
    score: int
    user_vote: Optional[str] = None


class GovernanceVoteCountsResponse(BaseModel):
    up: int = 0
    down: int = 0
    score: int = 0


class GovernanceUserVoteResponse(BaseModel):
    user_vote: Optional[str] = None


class GovernanceVoteResultResponse(BaseModel):
    yea: int = 0
    nay: int = 0
    abstain: int = 0
    total_eligible: int = 0
    participating_voters: int = 0
    threshold_rule: str = "simple_majority"
    required_yea: int = 0
    quorum_met: bool = False
    passed: bool = False


class GovernanceDissolutionPlanResponse(BaseModel):
    motion_id: uuid.UUID
    asset_disposition: str
    asset_recipient_name: str
    asset_recipient_type: str
    legal_compliance_notes: Optional[str] = None
    executed_at: Optional[datetime] = None
    executed_by_user_id: Optional[str] = None
    execution_notes: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class GovernanceDissolutionExecuteRequest(BaseModel):
    execution_notes: Optional[str] = Field(None, max_length=5000)


class CalendarIngestOrganization(BaseModel):
    name: Optional[str] = Field(None, max_length=255)
    source_url: Optional[str] = None
    image_url: Optional[str] = None
    description: Optional[str] = None
    tags: Optional[List[str]] = None
    city: Optional[str] = Field(None, max_length=64)


class CalendarIngestEvent(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    location: Optional[str] = Field(None, max_length=255)
    source_url: Optional[str] = None
    host_org_source_url: Optional[str] = None
    host_org_name: Optional[str] = Field(None, max_length=255)
    host_org_image_url: Optional[str] = None
    image_url: Optional[str] = None
    tags: Optional[List[str]] = None
    city: Optional[str] = Field(None, max_length=64)
    ingest_key: Optional[str] = Field(None, max_length=255)


class CalendarIngestPayload(BaseModel):
    source: Optional[str] = Field("genCalendar", max_length=120)
    run_id: Optional[str] = Field(None, max_length=255)
    generated_at: Optional[datetime] = None
    organizations: List[CalendarIngestOrganization] = Field(default_factory=list)
    events: List[CalendarIngestEvent] = Field(default_factory=list)


class CalendarIngestResponse(BaseModel):
    organizations_inserted: int
    organizations_updated: int
    events_inserted: int
    events_updated: int
    events_skipped: int


class SeedOrganizationsResponse(BaseModel):
    loaded: int
    inserted: int
    updated: int


class ContactLink(BaseModel):
    label: str = Field(..., min_length=1, max_length=120)
    url: str = Field(..., min_length=1, max_length=1000)


class ContactPageUpdate(BaseModel):
    enabled: Optional[bool] = None
    slug: Optional[str] = Field(None, min_length=1, max_length=255)
    headline: Optional[str] = Field(None, max_length=255)
    bio: Optional[str] = None
    photo_url: Optional[str] = None
    email_public: Optional[str] = None
    phone_public: Optional[str] = Field(None, max_length=64)
    linkedin_url: Optional[str] = None
    github_url: Optional[str] = None
    x_url: Optional[str] = None
    website_url: Optional[str] = None
    links: Optional[List[ContactLink]] = None

    @field_validator("email_public")
    @classmethod
    def _validate_email_public(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        cleaned = value.strip().lower()
        if not cleaned:
            return None
        if not re.fullmatch(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[A-Za-z]{2,}$", cleaned):
            raise ValueError("email_public must be a valid email address")
        return cleaned

    @field_validator("phone_public")
    @classmethod
    def _validate_phone_public(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        normalized = re.sub(r"[\s().-]+", "", cleaned)
        if normalized.startswith("+"):
            digits = normalized[1:]
            candidate = f"+{digits}"
        else:
            digits = normalized
            candidate = digits
        if not digits.isdigit() or len(digits) < 7 or len(digits) > 15:
            raise ValueError("phone_public must contain 7-15 digits")
        return candidate


class ContactPageResponse(BaseModel):
    user_id: str
    user_name: str
    slug: str
    enabled: bool
    headline: Optional[str] = None
    bio: Optional[str] = None
    photo_url: Optional[str] = None
    email_public: Optional[str] = None
    phone_public: Optional[str] = None
    linkedin_url: Optional[str] = None
    github_url: Optional[str] = None
    x_url: Optional[str] = None
    website_url: Optional[str] = None
    source_profile_url: Optional[str] = None
    source_profile_imported_at: Optional[datetime] = None
    links: List[ContactLink] = Field(default_factory=list)
    public_url: Optional[str] = None
    updated_at: datetime


class PublicUserProfileResponse(BaseModel):
    user_id: str
    user_name: str
    slug: str
    headline: Optional[str] = None
    bio: Optional[str] = None
    photo_url: Optional[str] = None
    email_public: Optional[str] = None
    phone_public: Optional[str] = None
    linkedin_url: Optional[str] = None
    github_url: Optional[str] = None
    x_url: Optional[str] = None
    website_url: Optional[str] = None
    links: List[ContactLink] = Field(default_factory=list)
    public_url: Optional[str] = None
    upcoming_events_count: int = 0
    updated_at: datetime


class PublicUserListItemResponse(BaseModel):
    user_id: str
    user_name: str
    slug: str
    headline: Optional[str] = None
    photo_url: Optional[str] = None
    upcoming_events_count: int = 0
    updated_at: datetime


class NetworkUserListItemResponse(BaseModel):
    user_id: str
    user_name: str
    email: str
    created_at: datetime
    contact_slug: Optional[str] = None
    contact_enabled: bool = False
    headline: Optional[str] = None
    photo_url: Optional[str] = None


class ContactImportPayload(BaseModel):
    source_url: str = Field(..., min_length=1, max_length=1000)
    overwrite: bool = True


class ContactImportResponse(BaseModel):
    contact: ContactPageResponse
    imported_fields: List[str] = Field(default_factory=list)
    source_url: str


# ============= DATABASE DEPENDENCY =============

class Database:
    def __init__(self):
        self.engine = None
        self.SessionLocal = None
        self.redis_client = None
        self.async_pool = None
    
    async def connect(self):
        """Initialize database connections"""
        try:
            # SQLAlchemy engine for synchronous operations
            self.engine = create_engine(
                DATABASE_URL,
                pool_size=20,
                max_overflow=30,
                pool_pre_ping=True,
                echo=False
            )
            self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
            
            # Asyncpg pool for complex async operations
            self.async_pool = await asyncpg.create_pool(
                ASYNC_DB_URL,
                min_size=5,
                max_size=20,
                command_timeout=60
            )
            
            # Redis client for caching
            self.redis_client = redis.Redis(
                host=REDIS_HOST,
                port=REDIS_PORT,
                password=REDIS_PASSWORD if REDIS_PASSWORD else None,
                decode_responses=True
            )
            
            logger.info("Database connections established")
            
        except Exception as e:
            logger.error(f"Failed to connect to database: {e}")
            raise
    
    async def disconnect(self):
        """Close database connections"""
        if self.async_pool:
            await self.async_pool.close()
        if self.engine:
            self.engine.dispose()
        if self.redis_client:
            self.redis_client.close()
        logger.info("Database connections closed")
    
    def get_session(self):
        """Get database session for dependency injection"""
        session = self.SessionLocal()
        try:
            yield session
        finally:
            session.close()
    
    async def get_async_conn(self):
        """Get async database connection"""
        async with self.async_pool.acquire() as conn:
            yield conn

# Initialize database
db = Database()

DEFAULT_UBI_INTERVAL_SECONDS = int(os.environ.get("UBI_INTERVAL_SECONDS", "60"))
DEFAULT_DENA_ANNUAL = Decimal(os.environ.get("DENA_ANNUAL", "1"))
DEFAULT_DENA_PRECISION = int(os.environ.get("DENA_PRECISION", "6"))
DEFAULT_UBI_ENTITY_TYPES = [
    item.strip()
    for item in os.environ.get("UBI_ENTITY_TYPES", "individual").split(",")
    if item.strip()
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
    parsed = sorted(_parse_content_types_csv(value))
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

# ============= AUTHENTICATION =============

def _is_sysadmin(current_user: dict) -> bool:
    return bool(current_user.get("is_sysadmin"))


async def _fetch_pidp_identity(token: str) -> dict[str, Any]:
    timeout = httpx.Timeout(connect=10.0, read=10.0, write=10.0, pool=10.0)
    auth_headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=timeout) as client:
        auth_resp = await client.get(
            f"{PIDP_BASE_URL}/auth/me",
            headers=auth_headers,
        )

        if auth_resp.is_success:
            pidp_user = auth_resp.json()
            user_id = str(pidp_user.get("id") or "").strip()
            email = str(pidp_user.get("email") or "").strip()
            name = str(pidp_user.get("full_name") or email or "User").strip()
            pidp_is_sysadmin = bool(pidp_user.get("is_sysadmin"))
            if not user_id or not email:
                raise HTTPException(status_code=401, detail="Invalid credentials")
            return {
                "pidp_id": user_id,
                "email": email,
                "name": name,
                "pidp_is_sysadmin": pidp_is_sysadmin,
                "token_kind": "jwt",
                "token_scope": "session",
                "token_scope_grants": ["session:*"],
            }

        service_resp = await client.get(
            f"{PIDP_BASE_URL}/service/token-info",
            headers=auth_headers,
        )
        if not service_resp.is_success:
            raise HTTPException(status_code=401, detail="Invalid credentials")

        token_info = service_resp.json()
        token_kind = str(token_info.get("token_kind") or "").strip().lower()
        scope = str(token_info.get("scope") or "").strip()
        owner = token_info.get("owner") or {}
        user_id = str(owner.get("id") or "").strip()
        email = str(owner.get("email") or "").strip()
        name = str(owner.get("full_name") or email or "User").strip()
        pidp_is_sysadmin = bool(owner.get("is_sysadmin"))
        if not user_id or not email:
            raise HTTPException(status_code=401, detail="Invalid credentials")

        if token_kind == "pat":
            if not scope or scope not in ORG_ALLOWED_PAT_SCOPES:
                allowed = ", ".join(sorted(ORG_ALLOWED_PAT_SCOPES))
                raise HTTPException(
                    status_code=403,
                    detail=f"PAT scope '{scope or 'none'}' is not allowed for Org backend. Allowed scopes: {allowed}",
                )

        return {
            "pidp_id": user_id,
            "email": email,
            "name": name,
            "pidp_is_sysadmin": pidp_is_sysadmin,
            "token_kind": token_kind or "unknown",
            "token_scope": scope or "unknown",
            "token_scope_grants": token_info.get("scope_grants") or [],
        }


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    session: Session = Depends(get_db)
):
    if not credentials:
        raise HTTPException(status_code=401, detail="Authentication required")
    
    token = credentials.credentials
    
    try:
        identity = await _fetch_pidp_identity(token)
        user_id = identity["pidp_id"]
        email = identity["email"]
        name = identity["name"]

        # Find or create account
        account = session.query(Account).filter_by(email=email).first()
        if not account:
            account = Account(
                id=uuid.uuid4(),
                entity_type=EntityType.INDIVIDUAL,
                name=name,
                email=email,
                balance=Decimal('10000.00')
            )
            session.add(account)
            session.commit()

        # Prefer PIdP-provided platform admin claim as the source of truth.
        # Keep local checks as break-glass fallback for recovery/bootstrap.
        pidp_is_sysadmin = bool(identity.get("pidp_is_sysadmin"))
        break_glass_is_sysadmin = user_id in ORG_SYSADMIN_USER_IDS or await _spicedb_check_sysadmin(user_id)
        is_sysadmin = pidp_is_sysadmin or break_glass_is_sysadmin
        return {
            "id": str(account.id),
            "email": account.email,
            "name": account.name,
            "is_anonymous": False,
            "is_sysadmin": is_sysadmin,
            "pidp_id": user_id,
            "token_kind": identity.get("token_kind"),
            "token_scope": identity.get("token_scope"),
            "token_scope_grants": identity.get("token_scope_grants") or [],
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Authentication error: {e}")
        raise HTTPException(status_code=401, detail="Invalid credentials")


def _require_authenticated_user(current_user: dict) -> None:
    if current_user.get("is_anonymous"):
        raise HTTPException(status_code=401, detail="Authentication required")


def _actor_user_id(current_user: dict) -> str:
    return str(current_user.get("pidp_id") or current_user.get("id") or "")


def _normalize_business_card_text(value: str) -> str:
    lines = [line.strip() for line in (value or "").splitlines()]
    return "\n".join([line for line in lines if line])


def _extract_business_card_fields(ocr_text: str) -> dict[str, Any]:
    text = _normalize_business_card_text(ocr_text)
    lines = text.splitlines()
    lowered = [line.lower() for line in lines]

    email_match = re.search(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", text, re.IGNORECASE)
    phone_match = re.search(r"(?:\+?\d[\d\-\(\)\s]{7,}\d)", text)
    website_match = re.search(r"(https?://[^\s]+|www\.[^\s]+)", text, re.IGNORECASE)

    likely_name = lines[0] if lines else None
    likely_company = None
    likely_title = None
    title_keywords = {
        "ceo", "cto", "cfo", "coo", "founder", "manager", "director", "lead", "engineer",
        "president", "partner", "owner", "consultant", "developer", "designer",
    }
    for idx, line in enumerate(lines[1:5], start=1):
        tokens = set(token.strip(",. ").lower() for token in line.split())
        if tokens & title_keywords and not likely_title:
            likely_title = line
            continue
        if "@" not in line and not re.search(r"\d", line) and not likely_company:
            likely_company = line
        if likely_company and likely_title:
            break
        if idx > 4:
            break

    address_parts = []
    for idx, line in enumerate(lines):
        if re.search(r"\d{5}(?:-\d{4})?$", line) or any(
            token in lowered[idx] for token in ["street", "st.", "avenue", "ave", "road", "rd.", "suite", "blvd", "drive"]
        ):
            address_parts.append(line)
    address = ", ".join(address_parts) if address_parts else None

    return {
        "name": likely_name,
        "title": likely_title,
        "company": likely_company,
        "email": email_match.group(0).strip() if email_match else None,
        "phone": phone_match.group(0).strip() if phone_match else None,
        "website": website_match.group(0).strip() if website_match else None,
        "address": address,
        "raw_lines": lines,
    }


def _coerce_public_url_candidate(value: Optional[str], field_name: str) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.lower().startswith("www."):
        raw = f"https://{raw}"
    try:
        return _validate_public_url(raw, field_name)
    except HTTPException:
        return None


def _parse_scan_datetime_candidate(value: str) -> Optional[datetime]:
    candidate = str(value or "").strip()
    if not candidate:
        return None
    candidate = re.sub(r"\s+", " ", candidate.replace(" at ", " ").strip())
    formats = [
        "%B %d, %Y %I:%M %p",
        "%b %d, %Y %I:%M %p",
        "%B %d %Y %I:%M %p",
        "%b %d %Y %I:%M %p",
        "%B %d, %Y",
        "%b %d, %Y",
        "%m/%d/%Y %I:%M %p",
        "%m/%d/%Y",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    ]
    for fmt in formats:
        try:
            parsed = datetime.strptime(candidate, fmt)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except ValueError:
            continue
    return None


def _extract_public_urls_from_text(text: str, max_urls: int = 10) -> list[str]:
    if max_urls < 1:
        return []
    candidates = re.findall(r"(https?://[^\s<>'\"()]+|www\.[^\s<>'\"()]+)", text or "", flags=re.IGNORECASE)

    def _canonicalize_url(url: str) -> str:
        parsed = urlparse(url)
        host = (parsed.hostname or "").strip().lower()
        if host.startswith("www."):
            host = host[4:]
        path = parsed.path or ""
        if path != "/" and path.endswith("/"):
            path = path.rstrip("/")
        if not path:
            path = "/"
        return parsed._replace(
            netloc=host,
            path=path,
            fragment="",
            params="",
            query=parsed.query or "",
        ).geturl()

    urls: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalized = _coerce_public_url_candidate(candidate, "source_url")
        if not normalized:
            continue
        normalized = _canonicalize_url(normalized)
        key = normalized.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        urls.append(normalized)
        if len(urls) >= max_urls:
            break
    return urls


def _collect_event_links_from_html(source_url: str, html_text: str, max_links: int = 15) -> list[str]:
    soup = BeautifulSoup(html_text or "", "html.parser")
    links: list[str] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "").strip()
        if not href:
            continue
        absolute = urljoin(source_url, href)
        normalized = _coerce_public_url_candidate(absolute, "source_url")
        if not normalized:
            continue
        label = (anchor.get_text(" ", strip=True) or "").lower()
        href_lower = normalized.lower()
        if (
            "/event" not in href_lower
            and "register" not in href_lower
            and "ticket" not in href_lower
            and "event" not in label
            and "register" not in label
            and "ticket" not in label
            and "meetup" not in label
        ):
            continue
        key = normalized.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        links.append(normalized)
        if len(links) >= max_links:
            break
    return links


def _extract_event_candidate_from_jsonld(source_url: str, html_text: str) -> dict[str, Any]:
    soup = BeautifulSoup(html_text or "", "html.parser")
    scripts = soup.find_all("script", attrs={"type": "application/ld+json"})
    for script in scripts:
        raw_json = (script.string or script.get_text(strip=True) or "").strip()
        if not raw_json:
            continue
        try:
            parsed = json.loads(raw_json)
        except Exception:
            continue
        nodes: list[dict[str, Any]] = []
        if isinstance(parsed, dict):
            if isinstance(parsed.get("@graph"), list):
                nodes.extend([item for item in parsed["@graph"] if isinstance(item, dict)])
            else:
                nodes.append(parsed)
        elif isinstance(parsed, list):
            nodes.extend([item for item in parsed if isinstance(item, dict)])
        for node in nodes:
            node_type = str(node.get("@type") or "").lower()
            if "event" not in node_type:
                continue
            location_value = node.get("location")
            location_name = None
            if isinstance(location_value, dict):
                location_name = str(location_value.get("name") or location_value.get("address") or "").strip() or None
            elif isinstance(location_value, str):
                location_name = location_value.strip() or None
            return {
                "title": str(node.get("name") or "").strip() or None,
                "description": str(node.get("description") or "").strip() or None,
                "starts_at": _coerce_calendar_datetime(node.get("startDate")),
                "location": location_name,
                "source_url": _coerce_public_url_candidate(node.get("url"), "source_url")
                or _coerce_public_url_candidate(source_url, "source_url"),
            }
    return {}


async def _enrich_event_scan_from_links(
    *,
    ocr_text: str,
    seed_url: Optional[str],
    max_fetches: int = 3,
    max_links: int = 20,
) -> dict[str, Any]:
    source_candidates: list[str] = []
    if seed_url:
        normalized_seed = _coerce_public_url_candidate(seed_url, "source_url")
        if normalized_seed:
            source_candidates.append(normalized_seed)
    source_candidates.extend(_extract_public_urls_from_text(ocr_text, max_urls=max_links))

    deduped_candidates: list[str] = []
    seen_candidates: set[str] = set()
    for candidate in source_candidates:
        key = candidate.strip().lower()
        if key in seen_candidates:
            continue
        seen_candidates.add(key)
        deduped_candidates.append(candidate)

    visited_urls: list[str] = []
    discovered_links: list[str] = []
    event_candidate: dict[str, Any] = {}
    discovered_link_keys: set[str] = set()
    for candidate in deduped_candidates[:max_fetches]:
        safe_url = _ensure_public_fetch_url(candidate, "event_link")
        visited_urls.append(safe_url)
        timeout = httpx.Timeout(connect=8.0, read=8.0, write=8.0, pool=8.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.get(
                safe_url,
                headers={
                    "Accept": "text/html,application/xhtml+xml",
                    "User-Agent": "ArkavoOrgScanner/1.0",
                },
            )
        if not response.is_success:
            continue
        content_type = str(response.headers.get("content-type") or "").lower()
        if "html" not in content_type:
            continue
        html = response.text
        resolved_url = _coerce_public_url_candidate(str(response.url), "source_url") or safe_url
        candidate_from_jsonld = _extract_event_candidate_from_jsonld(resolved_url, html)
        if candidate_from_jsonld and not event_candidate:
            event_candidate = candidate_from_jsonld
        for link in _collect_event_links_from_html(resolved_url, html, max_links=max_links):
            key = link.lower()
            if key in discovered_link_keys:
                continue
            discovered_link_keys.add(key)
            discovered_links.append(link)
        if len(discovered_links) >= max_links:
            discovered_links = discovered_links[:max_links]
            break

    return {
        "visited_urls": visited_urls,
        "discovered_links": discovered_links[:max_links],
        "event_candidate": event_candidate,
    }


def _extract_event_fields_from_text(ocr_text: str) -> dict[str, Any]:
    events = _extract_events_from_text(ocr_text)
    if events:
        return events[0]

    text = _normalize_business_card_text(ocr_text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return {"title": None, "starts_at": None, "location": None, "website": None, "description": None, "raw_lines": lines}


def _extract_events_from_text(ocr_text: str) -> list[dict[str, Any]]:
    text = _normalize_business_card_text(ocr_text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return []

    extracted_urls = _extract_public_urls_from_text(text, max_urls=5)
    month_hint = re.compile(
        r"\b(jan|january|feb|february|mar|march|apr|april|may|jun|june|jul|july|aug|august|sep|sept|september|oct|october|nov|november|dec|december)\b",
        re.IGNORECASE,
    )
    time_hint = re.compile(r"\b\d{1,2}:\d{2}\s*(?:am|pm)\b", re.IGNORECASE)
    date_hint = re.compile(r"\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{2}-\d{2}", re.IGNORECASE)
    noisy_location_tokens = (
        "we look forward",
        "open houses",
        "open house",
        "transit administration",
        "department of transportation",
    )

    title = None
    for line in lines:
        lowered = line.lower()
        if "@" in lowered:
            continue
        if re.search(r"(https?://|www\.)", lowered):
            continue
        if re.search(r"\+?\d[\d\-\(\)\s]{7,}\d", line):
            continue
        if time_hint.search(lowered):
            continue
        if month_hint.search(lowered) or date_hint.search(lowered):
            continue
        title = line
        break
    title = title or lines[0]

    def _is_date_line(value: str) -> bool:
        normalized = value.strip().replace("–", "-").replace("—", "-")
        return bool(month_hint.search(normalized) or date_hint.search(normalized))

    def _parse_event_start_at(date_line: str, next_line: Optional[str]) -> Optional[datetime]:
        date_candidate = date_line.strip().replace("–", "-").replace("—", "-").rstrip(":").strip()
        starts_at = _parse_scan_datetime_candidate(date_candidate)
        if starts_at:
            return starts_at
        if not next_line:
            return None
        next_normalized = next_line.strip().replace("–", "-").replace("—", "-")
        time_match = re.search(r"\b\d{1,2}:\d{2}\s*(?:am|pm)\b", next_normalized, flags=re.IGNORECASE)
        if not time_match:
            return None
        combined = f"{date_candidate} {time_match.group(0)}"
        return _parse_scan_datetime_candidate(combined)

    events: list[dict[str, Any]] = []
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        if not _is_date_line(line):
            idx += 1
            continue
        next_line = lines[idx + 1] if (idx + 1) < len(lines) else None
        starts_at = _parse_event_start_at(line, next_line)
        if not starts_at:
            idx += 1
            continue

        location_start = idx + 1
        if next_line and time_hint.search(next_line):
            location_start = idx + 2
        location_parts: list[str] = []
        cursor = location_start
        while cursor < len(lines):
            candidate = lines[cursor].strip()
            lowered = candidate.lower()
            if _is_date_line(candidate):
                break
            if re.search(r"(https?://|www\.)", lowered):
                break
            if any(token in lowered for token in noisy_location_tokens):
                break
            if time_hint.search(lowered):
                cursor += 1
                continue
            location_parts.append(candidate)
            if len(location_parts) >= 2:
                break
            cursor += 1

        location = " ".join(location_parts).strip() or None
        event_payload = {
            "title": title,
            "starts_at": starts_at,
            "location": location,
            "website": extracted_urls[0] if extracted_urls else None,
            "links": extracted_urls,
            "description": None,
            "raw_lines": lines,
        }
        events.append(event_payload)
        idx = max(idx + 1, cursor)

    if events:
        return events

    # Fallback to single-event heuristics for less structured OCR.
    starts_at: Optional[datetime] = None
    for line in lines:
        normalized = line.strip().replace("–", "-").replace("—", "-")
        if _is_date_line(normalized):
            starts_at = _parse_scan_datetime_candidate(normalized.split(" - ", 1)[0].strip())
            if starts_at:
                break
    location = None
    location_markers = ("location", "venue", "address", "st.", "street", "ave", "avenue", "blvd", "road", "rd.")
    for line in lines:
        lowered = line.lower()
        if any(marker in lowered for marker in location_markers):
            location = line.split(":", 1)[-1].strip() if ":" in line else line
            break
    description_lines = [line for line in lines[1:5] if line != location]
    description = "\n".join(description_lines).strip() or None
    if description and len(description) > 2000:
        description = description[:2000]
    return [
        {
            "title": title,
            "starts_at": starts_at,
            "location": location,
            "website": extracted_urls[0] if extracted_urls else None,
            "links": extracted_urls,
            "description": description,
            "raw_lines": lines,
        }
    ]


def _extract_organization_fields_from_text(ocr_text: str) -> dict[str, Any]:
    text = _normalize_business_card_text(ocr_text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return {"name": None, "website": None, "description": None, "raw_lines": []}

    website_match = re.search(r"(https?://[^\s]+|www\.[^\s]+)", text, re.IGNORECASE)
    name = None
    org_tokens = ("inc", "llc", "ltd", "foundation", "association", "collective", "organization", "org", "corp", "company")
    for line in lines:
        lowered = line.lower()
        if re.search(r"(https?://|www\.)", lowered):
            continue
        if "@" in lowered:
            continue
        if any(token in lowered for token in org_tokens):
            name = line
            break
    if not name:
        name = lines[0]

    description_lines = [line for line in lines[1:5] if "@" not in line and not re.search(r"(https?://|www\.)", line, re.IGNORECASE)]
    description = "\n".join(description_lines).strip() or None
    if description and len(description) > 2000:
        description = description[:2000]

    return {
        "name": name,
        "website": website_match.group(0).strip() if website_match else None,
        "description": description,
        "raw_lines": lines,
    }


def _derive_org_name_from_website(website: Optional[str]) -> Optional[str]:
    raw = str(website or "").strip()
    if not raw:
        return None
    if raw.lower().startswith("www."):
        raw = f"https://{raw}"
    try:
        host = (urlparse(raw).hostname or "").strip().lower()
    except Exception:
        return None
    if not host:
        return None
    if host.startswith("www."):
        host = host[4:]
    label = host.split(".", 1)[0].strip()
    if not label:
        return None
    label = re.sub(r"[-_]+", " ", label)
    label = re.sub(r"\s+", " ", label).strip()
    if not label:
        return None
    return label.title()


def _derive_org_payload_for_person_scan(
    extracted_person: dict[str, Any],
    extracted_org: dict[str, Any],
) -> Optional[dict[str, Any]]:
    person_name = str(extracted_person.get("name") or "").strip()
    person_name_key = re.sub(r"\s+", " ", person_name).strip().lower()

    candidate_names: list[str] = []
    for candidate in [
        extracted_person.get("company"),
        extracted_org.get("name"),
    ]:
        value = str(candidate or "").strip()
        if not value:
            continue
        value_key = re.sub(r"\s+", " ", value).strip().lower()
        if person_name_key and value_key == person_name_key:
            continue
        if value not in candidate_names:
            candidate_names.append(value)

    website = (
        _coerce_public_url_candidate(extracted_org.get("website"), "source_url")
        or _coerce_public_url_candidate(extracted_person.get("website"), "source_url")
    )
    chosen_name = candidate_names[0] if candidate_names else None
    if not chosen_name and website:
        chosen_name = _derive_org_name_from_website(website)
    if not chosen_name:
        return None
    return {
        "name": chosen_name,
        "website": website,
        "description": extracted_org.get("description"),
        "raw_lines": extracted_org.get("raw_lines") or extracted_person.get("raw_lines") or [],
    }


def _normalize_scan_kind(value: Optional[str]) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"person", "organization", "event", "auto"}:
        return normalized
    return "auto"


def _score_scan_kind_candidates(
    ocr_text: str,
    extracted_person: dict[str, Any],
    extracted_org: dict[str, Any],
    extracted_event: dict[str, Any],
) -> dict[str, float]:
    text = _normalize_business_card_text(ocr_text).lower()

    person_score = 0.0
    if extracted_person.get("email"):
        person_score += 0.65
    if extracted_person.get("phone"):
        person_score += 0.2
    if extracted_person.get("name"):
        person_score += 0.1
    if extracted_person.get("title") or extracted_person.get("company"):
        person_score += 0.05

    organization_score = 0.0
    if extracted_org.get("name"):
        organization_score += 0.55
    if extracted_org.get("website"):
        organization_score += 0.25
    if extracted_org.get("description"):
        organization_score += 0.1
    if any(token in text for token in ["org", "organization", "inc", "llc", "nonprofit", "foundation"]):
        organization_score += 0.1

    event_score = 0.0
    if extracted_event.get("starts_at"):
        event_score += 0.55
    if extracted_event.get("website"):
        event_score += 0.2
    if extracted_event.get("location"):
        event_score += 0.15
    if any(token in text for token in ["event", "meetup", "conference", "workshop", "summit", "webinar"]):
        event_score += 0.1

    return {
        "person": max(0.0, min(1.0, person_score)),
        "organization": max(0.0, min(1.0, organization_score)),
        "event": max(0.0, min(1.0, event_score)),
    }


def _detect_scan_kind(ocr_text: str, extracted_person: dict[str, Any], extracted_org: dict[str, Any], extracted_event: dict[str, Any]) -> str:
    scores = _score_scan_kind_candidates(
        ocr_text=ocr_text,
        extracted_person=extracted_person,
        extracted_org=extracted_org,
        extracted_event=extracted_event,
    )
    sorted_scores = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    return sorted_scores[0][0] if sorted_scores else "person"


def _create_or_find_organization_from_scan(
    session: Session,
    *,
    actor_user_id: str,
    extracted_org: dict[str, Any],
    default_image_url: Optional[str] = None,
) -> Organization:
    name = str(extracted_org.get("name") or "").strip() or "Organization"
    source_url = _coerce_public_url_candidate(extracted_org.get("website"), "source_url")
    if source_url:
        existing = _find_org_by_source_url(session, source_url)
        if existing:
            if default_image_url and not (existing.image_url or "").strip():
                existing.image_url = default_image_url
            return existing
    existing_by_name = session.query(Organization).filter(func.lower(Organization.name) == name.lower()).first()
    if existing_by_name:
        if source_url:
            _add_org_source_url(existing_by_name, source_url)
        if default_image_url and not (existing_by_name.image_url or "").strip():
            existing_by_name.image_url = default_image_url
        return existing_by_name
    normalized_slug = _slugify(name)
    if normalized_slug:
        existing_by_slug = session.query(Organization).filter(Organization.slug == normalized_slug).first()
        if existing_by_slug:
            if source_url:
                _add_org_source_url(existing_by_slug, source_url)
            if default_image_url and not (existing_by_slug.image_url or "").strip():
                existing_by_slug.image_url = default_image_url
            return existing_by_slug

    org = Organization(
        id=uuid.uuid4(),
        name=name[:255],
        slug=_ensure_unique_org_slug(session, name),
        description=(str(extracted_org.get("description") or "").strip() or None),
        source_url=source_url,
        source_urls=[source_url] if source_url else [],
        image_url=default_image_url,
        tags=["source:scan"],
        seeded_from_events=False,
        claimed_by_user_id=None,
        created_by_user_id=actor_user_id,
    )
    session.add(org)
    return org


def _create_event_from_scan(
    session: Session,
    *,
    actor_user_id: str,
    extracted_event: dict[str, Any],
    default_image_url: Optional[str] = None,
) -> NetworkEvent:
    title = str(extracted_event.get("title") or "").strip() or "Untitled Event"
    starts_at = extracted_event.get("starts_at")
    location = str(extracted_event.get("location") or "").strip() or None
    source_url = _coerce_public_url_candidate(extracted_event.get("website"), "source_url")
    if source_url:
        existing = session.query(NetworkEvent).filter(NetworkEvent.source_url == source_url).first()
        if existing:
            if default_image_url and not (existing.image_url or "").strip():
                existing.image_url = default_image_url
            return existing
    duplicate_query = session.query(NetworkEvent).filter(func.lower(NetworkEvent.title) == title.lower())
    if starts_at is not None:
        duplicate_query = duplicate_query.filter(NetworkEvent.starts_at == starts_at)
    if location:
        duplicate_query = duplicate_query.filter(func.lower(NetworkEvent.location) == location.lower())
    existing_structured = duplicate_query.order_by(NetworkEvent.created_at.desc()).first()
    if existing_structured:
        if default_image_url and not (existing_structured.image_url or "").strip():
            existing_structured.image_url = default_image_url
        return existing_structured

    event = NetworkEvent(
        id=uuid.uuid4(),
        title=title[:255],
        slug=_ensure_unique_event_slug(session, title),
        description=(str(extracted_event.get("description") or "").strip() or None),
        starts_at=starts_at,
        ends_at=None,
        location=location,
        source_url=source_url,
        image_url=default_image_url,
        tags=["source:scan"],
        host_type=EventHostType.UNCLAIMED.value,
        host_user_id=None,
        host_org_id=None,
        claimed_by_user_id=None,
        created_by_user_id=actor_user_id,
        seeded_from_events=False,
    )
    session.add(event)
    return event


async def _ocr_business_card_with_openai(
    image_bytes: bytes,
    content_type: str,
    *,
    audit_hook: Optional[Callable[[str, Dict[str, Any]], None]] = None,
) -> str:
    if not ORG_OPENAI_API_KEY:
        raise HTTPException(status_code=503, detail="ORG_OPENAI_API_KEY is not configured")

    image_b64 = base64.b64encode(image_bytes).decode("ascii")
    prompt = (
        "You are extracting text from a business card image. Return only the visible text, preserving line breaks. "
        "Do not add commentary."
    )
    payload = {
        "model": ORG_BUSINESS_CARD_OCR_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:{content_type};base64,{image_b64}"}},
                ],
            }
        ],
        "temperature": 0,
    }
    headers = {
        "Authorization": f"Bearer {ORG_OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    timeout = httpx.Timeout(connect=15.0, read=90.0, write=30.0, pool=30.0)
    if audit_hook:
        audit_hook(
            "scan.ai.ocr.requested",
            {
                "provider": "openai",
                "model": ORG_BUSINESS_CARD_OCR_MODEL,
                "image_content_type": content_type,
                "image_size_bytes": len(image_bytes),
            },
        )
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            f"{ORG_OPENAI_API_BASE_URL}/chat/completions",
            json=payload,
            headers=headers,
        )
    if not response.is_success:
        if audit_hook:
            audit_hook(
                "scan.ai.ocr.failed",
                {
                    "provider": "openai",
                    "model": ORG_BUSINESS_CARD_OCR_MODEL,
                    "status_code": response.status_code,
                },
            )
        detail = response.text.strip() or f"OCR request failed ({response.status_code})"
        raise HTTPException(status_code=502, detail=detail)

    body = response.json()
    choices = body.get("choices") or []
    if not choices:
        raise HTTPException(status_code=502, detail="OCR response missing choices")
    message = (choices[0] or {}).get("message") or {}
    extracted = _extract_text_content_from_openai_message_content(message.get("content"))
    extracted = _normalize_business_card_text(extracted)
    if not extracted:
        raise HTTPException(status_code=422, detail="Unable to extract text from business card image")
    if audit_hook:
        audit_hook(
            "scan.ai.ocr.completed",
            {
                "provider": "openai",
                "model": ORG_BUSINESS_CARD_OCR_MODEL,
                "status_code": response.status_code,
                "output_chars": len(extracted),
            },
        )
    return extracted


def _extract_text_content_from_openai_message_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        extracted_parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text" and item.get("text"):
                extracted_parts.append(str(item["text"]))
        return "\n".join(extracted_parts)
    return ""


async def _summarize_scan_targets_with_openai(
    *,
    ocr_text: str,
    created_targets: List[Dict[str, Optional[str]]],
    audit_hook: Optional[Callable[[str, Dict[str, Any]], None]] = None,
) -> List[Dict[str, Optional[str]]]:
    if not created_targets:
        return created_targets
    if not ORG_SCAN_AI_SUMMARY_ENABLED or not ORG_OPENAI_API_KEY:
        return created_targets

    summarized_targets: List[Dict[str, Optional[str]]] = [dict(item) for item in created_targets]
    summarized_targets_payload = [
        {
            "type": str(item.get("type") or "").strip() or "resource",
            "id": str(item.get("id") or "").strip() or None,
            "slug": str(item.get("slug") or "").strip() or None,
            "name": str(item.get("name") or "").strip() or None,
        }
        for item in summarized_targets
    ]
    prompt_payload = {
        "ocr_text": (ocr_text or "")[:12000],
        "targets": summarized_targets_payload,
    }
    prompt = (
        "You summarize records created from OCR. For each target in `targets`, generate a concise factual "
        "summary (max 140 chars) using only OCR content. Do not invent. "
        "Return strict JSON: {\"items\":[{\"index\":0,\"summary\":\"...\"}, ...]}"
    )
    request_payload = {
        "model": ORG_BUSINESS_CARD_OCR_MODEL,
        "messages": [
            {"role": "system", "content": "Return valid JSON only."},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "text", "text": json.dumps(prompt_payload, ensure_ascii=True)},
                ],
            },
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    headers = {
        "Authorization": f"Bearer {ORG_OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    timeout = httpx.Timeout(connect=15.0, read=60.0, write=30.0, pool=30.0)
    if audit_hook:
        audit_hook(
            "scan.ai.summary.requested",
            {
                "provider": "openai",
                "model": ORG_BUSINESS_CARD_OCR_MODEL,
                "targets_count": len(summarized_targets),
            },
        )
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            f"{ORG_OPENAI_API_BASE_URL}/chat/completions",
            json=request_payload,
            headers=headers,
        )
    if not response.is_success:
        if audit_hook:
            audit_hook(
                "scan.ai.summary.failed",
                {
                    "provider": "openai",
                    "model": ORG_BUSINESS_CARD_OCR_MODEL,
                    "status_code": response.status_code,
                },
            )
        return summarized_targets

    body = response.json()
    choices = body.get("choices") or []
    if not choices:
        return summarized_targets
    message = (choices[0] or {}).get("message") or {}
    content_text = _extract_text_content_from_openai_message_content(message.get("content")).strip()
    if not content_text:
        return summarized_targets

    parsed: dict[str, Any] = {}
    try:
        parsed = json.loads(content_text)
    except Exception:
        if audit_hook:
            audit_hook(
                "scan.ai.summary.failed",
                {
                    "provider": "openai",
                    "model": ORG_BUSINESS_CARD_OCR_MODEL,
                    "reason": "invalid_json",
                },
            )
        return summarized_targets

    items = parsed.get("items")
    summaries_applied = 0
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                index = int(item.get("index"))
            except Exception:
                continue
            if index < 0 or index >= len(summarized_targets):
                continue
            summary = re.sub(r"\s+", " ", str(item.get("summary") or "").strip())
            if not summary:
                continue
            summarized_targets[index]["summary"] = summary[:140]
            summaries_applied += 1

    if audit_hook:
        audit_hook(
            "scan.ai.summary.completed",
            {
                "provider": "openai",
                "model": ORG_BUSINESS_CARD_OCR_MODEL,
                "targets_count": len(summarized_targets),
                "summaries_applied": summaries_applied,
            },
        )
    return summarized_targets


async def _extract_business_card_text(
    image_bytes: bytes,
    content_type: str,
    *,
    audit_hook: Optional[Callable[[str, Dict[str, Any]], None]] = None,
) -> tuple[str, str]:
    provider = ORG_BUSINESS_CARD_OCR_PROVIDER
    if provider == "openai":
        return await _ocr_business_card_with_openai(
            image_bytes=image_bytes,
            content_type=content_type,
            audit_hook=audit_hook,
        ), provider
    raise HTTPException(status_code=503, detail=f"Unsupported ORG_BUSINESS_CARD_OCR_PROVIDER '{provider}'")


async def _create_or_find_pidp_user_from_business_card(email: str, full_name: Optional[str]) -> dict[str, Any]:
    generated_password = secrets.token_urlsafe(24)
    payload = {
        "email": email,
        "password": generated_password,
        "full_name": full_name or None,
    }
    timeout = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            f"{PIDP_BASE_URL}/auth/register",
            json=payload,
        )
    if response.status_code in {200, 201}:
        data = response.json()
        return {
            "created": True,
            "pidp_user_id": str(data.get("id") or ""),
            "generated_password": generated_password,
        }
    if response.status_code == 409:
        return {
            "created": False,
            "pidp_user_id": None,
            "generated_password": None,
        }
    detail = response.text.strip() or f"Unable to create PIdP user ({response.status_code})"
    raise HTTPException(status_code=502, detail=detail)


def _send_business_card_added_email(
    recipient_email: str,
    recipient_name: Optional[str],
    submitted_by_name: Optional[str],
) -> None:
    if not ORG_SMTP_HOST:
        raise RuntimeError("SMTP is not configured (ORG_SMTP_HOST missing)")

    portal_url = ORG_PORTAL_BASE_URL or "https://portal.arkavo.org"
    display_name = (recipient_name or recipient_email).strip()
    submitter = (submitted_by_name or "an org admin").strip()

    msg = EmailMessage()
    msg["Subject"] = "You were added to Arkavo OrgPortal"
    msg["From"] = ORG_SMTP_FROM
    msg["To"] = recipient_email
    msg.set_content(
        (
            f"Hi {display_name},\n\n"
            f"{submitter} submitted your business card and added you to Arkavo OrgPortal.\n"
            f"You can sign in or register with this email at:\n{portal_url}\n\n"
            "If this was unexpected, please ignore this email.\n"
        )
    )

    with smtplib.SMTP(ORG_SMTP_HOST, ORG_SMTP_PORT, timeout=30) as smtp:
        if ORG_SMTP_STARTTLS:
            smtp.starttls()
        if ORG_SMTP_USERNAME:
            smtp.login(ORG_SMTP_USERNAME, ORG_SMTP_PASSWORD)
        smtp.send_message(msg)


def _record_business_card_email_outcome(submission_id: uuid.UUID, sent: bool, error_message: Optional[str]) -> None:
    session = db.SessionLocal()
    try:
        record = session.query(BusinessCardSubmission).filter(BusinessCardSubmission.id == submission_id).first()
        if not record:
            return
        record.notification_email_sent = bool(sent)
        record.notification_error = error_message
        record.updated_at = datetime.now(timezone.utc)
        session.commit()
    except Exception:
        session.rollback()
    finally:
        session.close()


def _send_business_card_email_task(
    submission_id: uuid.UUID,
    recipient_email: str,
    recipient_name: Optional[str],
    submitted_by_name: Optional[str],
) -> None:
    try:
        _send_business_card_added_email(
            recipient_email=recipient_email,
            recipient_name=recipient_name,
            submitted_by_name=submitted_by_name,
        )
        _record_business_card_email_outcome(submission_id=submission_id, sent=True, error_message=None)
    except Exception as exc:
        _record_business_card_email_outcome(submission_id=submission_id, sent=False, error_message=str(exc))


def _matrix_localpart_for_pidp_user(pidp_user_id: str) -> str:
    base = re.sub(r"[^a-z0-9._=\-/]+", "-", (pidp_user_id or "").strip().lower()).strip("-")
    if not base:
        base = hashlib.sha256(pidp_user_id.encode("utf-8")).hexdigest()[:24]
    localpart = f"org-{base}"
    return localpart[:254]


def _matrix_user_id_for_pidp_user(pidp_user_id: str) -> str:
    localpart = _matrix_localpart_for_pidp_user(pidp_user_id)
    return f"@{localpart}:{ORG_MATRIX_SERVER_NAME}"


def _matrix_bootstrap_password(pidp_user_id: str) -> str:
    if not ORG_MATRIX_PASSWORD_SECRET:
        raise HTTPException(status_code=503, detail="Matrix bootstrap secret is not configured")
    digest = hmac.new(
        ORG_MATRIX_PASSWORD_SECRET.encode("utf-8"),
        pidp_user_id.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"orgp-{digest}"


def _matrix_admin_headers() -> dict[str, str]:
    if not ORG_MATRIX_ADMIN_TOKEN:
        raise HTTPException(status_code=503, detail="Matrix bootstrap admin token is not configured")
    return {"Authorization": f"Bearer {ORG_MATRIX_ADMIN_TOKEN}"}


def _matrix_org_room_alias_candidates(org_slug: str) -> list[str]:
    normalized_slug = _slugify(org_slug)
    # Keep alias derivation deterministic and backward-compatible with likely variants.
    localparts = [
        f"org-{normalized_slug}",
        normalized_slug,
        f"org-{normalized_slug}-chat",
    ]
    aliases: list[str] = []
    seen: set[str] = set()
    for localpart in localparts:
        alias = f"#{localpart}:{ORG_MATRIX_SERVER_NAME}"
        if alias in seen:
            continue
        seen.add(alias)
        aliases.append(alias)
    return aliases


def _matrix_org_room_alias_localpart(org_slug: str) -> str:
    normalized_slug = _slugify(org_slug)
    return f"org-{normalized_slug}"


async def _matrix_room_id_from_alias(
    client: httpx.AsyncClient,
    room_alias: str,
) -> Optional[str]:
    encoded_alias = quote(room_alias, safe="")
    response = await client.get(f"{ORG_MATRIX_HOMESERVER_URL}/_matrix/client/v3/directory/room/{encoded_alias}")
    if response.status_code == 404:
        return None
    if not response.is_success:
        return None
    payload = response.json() if response.content else {}
    room_id = str(payload.get("room_id") or "").strip()
    return room_id or None


async def _resolve_org_public_chat_room(
    client: httpx.AsyncClient,
    org_slug: str,
) -> tuple[Optional[str], Optional[str]]:
    for alias in _matrix_org_room_alias_candidates(org_slug):
        room_id = await _matrix_room_id_from_alias(client, alias)
        if room_id:
            return room_id, alias
    return None, None


async def _matrix_create_public_org_room(
    client: httpx.AsyncClient,
    org_name: str,
    org_slug: str,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    alias_localpart = _matrix_org_room_alias_localpart(org_slug)
    primary_alias = f"#{alias_localpart}:{ORG_MATRIX_SERVER_NAME}"
    payload = {
        "visibility": "public",
        "preset": "public_chat",
        "name": (org_name or org_slug).strip()[:255] or org_slug,
        "topic": f"Public discussion space for {org_name or org_slug}",
        "room_alias_name": alias_localpart,
    }
    response = await client.post(
        f"{ORG_MATRIX_HOMESERVER_URL}/_matrix/client/v3/createRoom",
        headers=_matrix_admin_headers(),
        json=payload,
    )
    if response.is_success:
        body = response.json() if response.content else {}
        room_id = str(body.get("room_id") or "").strip()
        if room_id:
            logger.info("Provisioned Matrix public org room slug=%s room_id=%s", org_slug, room_id)
            return room_id, primary_alias, payload["name"]
        return None, None, None

    error_text = (response.text or "").strip().lower()
    # Race-safe behavior: if alias already exists, resolve it and continue.
    if response.status_code in {400, 409} and (
        "room alias" in error_text or "in use" in error_text or "m_room_in_use" in error_text
    ):
        existing_room_id = await _matrix_room_id_from_alias(client, primary_alias)
        if existing_room_id:
            logger.info("Reused existing Matrix public org room slug=%s room_id=%s", org_slug, existing_room_id)
            return existing_room_id, primary_alias, payload["name"]
    logger.warning(
        "Failed to provision Matrix public org room slug=%s status=%s body=%s",
        org_slug,
        response.status_code,
        (response.text or "").strip()[:400],
    )
    return None, None, None


async def _matrix_find_public_room_for_org(
    client: httpx.AsyncClient,
    org_name: str,
    org_slug: str,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    slug = _slugify(org_slug)
    name = (org_name or "").strip().lower()
    search_terms = [name, slug]
    for term in search_terms:
        if not term:
            continue
        response = await client.post(
            f"{ORG_MATRIX_HOMESERVER_URL}/_matrix/client/v3/publicRooms",
            json={
                "limit": 50,
                "filter": {"generic_search_term": term},
            },
        )
        if not response.is_success:
            continue
        payload = response.json() if response.content else {}
        chunk = payload.get("chunk") if isinstance(payload, dict) else None
        if not isinstance(chunk, list):
            continue
        for room in chunk:
            if not isinstance(room, dict):
                continue
            room_id = str(room.get("room_id") or "").strip()
            canonical_alias = str(room.get("canonical_alias") or "").strip() or None
            room_name = str(room.get("name") or "").strip() or None
            if not room_id:
                continue
            alias_text = (canonical_alias or "").lower()
            room_name_text = (room_name or "").lower()
            if slug and (slug in alias_text or slug in room_name_text):
                return room_id, canonical_alias, room_name
            if name and name in room_name_text:
                return room_id, canonical_alias, room_name
    return None, None, None


async def _matrix_upsert_user(
    client: httpx.AsyncClient,
    matrix_user_id: str,
    password: str,
    display_name: str,
) -> None:
    encoded_user_id = quote(matrix_user_id, safe="")
    resp = await client.put(
        f"{ORG_MATRIX_HOMESERVER_URL}/_synapse/admin/v2/users/{encoded_user_id}",
        headers=_matrix_admin_headers(),
        json={
            "password": password,
            "displayname": (display_name or matrix_user_id)[:255],
            "admin": False,
            "deactivated": False,
            "logout_devices": False,
        },
    )
    if not resp.is_success:
        detail = resp.text.strip() or f"Matrix admin user upsert failed ({resp.status_code})"
        raise HTTPException(status_code=502, detail=detail)


async def _matrix_login_password(
    client: httpx.AsyncClient,
    matrix_user_id: str,
    password: str,
) -> MatrixBootstrapSessionResponse:
    login_resp = await client.post(
        f"{ORG_MATRIX_HOMESERVER_URL}/_matrix/client/v3/login",
        json={
            "type": "m.login.password",
            "identifier": {
                "type": "m.id.user",
                "user": matrix_user_id,
            },
            "password": password,
        },
    )
    if not login_resp.is_success:
        detail = login_resp.text.strip() or f"Matrix login failed ({login_resp.status_code})"
        raise HTTPException(status_code=502, detail=detail)
    payload = login_resp.json()
    access_token = str(payload.get("access_token") or "").strip()
    user_id = str(payload.get("user_id") or "").strip()
    if not access_token or not user_id:
        raise HTTPException(status_code=502, detail="Matrix login response missing access_token or user_id")
    return MatrixBootstrapSessionResponse(
        access_token=access_token,
        user_id=user_id,
        device_id=payload.get("device_id"),
        homeserver_url=ORG_MATRIX_HOMESERVER_URL,
    )


async def _bootstrap_matrix_session_for_current_user(current_user: dict) -> MatrixBootstrapSessionResponse:
    _require_authenticated_user(current_user)
    pidp_user_id = _actor_user_id(current_user)
    if not pidp_user_id:
        raise HTTPException(status_code=401, detail="Authenticated user id is required")
    matrix_user_id = _matrix_user_id_for_pidp_user(pidp_user_id)
    matrix_password = _matrix_bootstrap_password(pidp_user_id)
    display_name = str(current_user.get("name") or current_user.get("email") or matrix_user_id).strip()

    timeout = httpx.Timeout(connect=10.0, read=15.0, write=15.0, pool=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        # Upsert guarantees the account exists and has the expected deterministic bootstrap password.
        await _matrix_upsert_user(
            client=client,
            matrix_user_id=matrix_user_id,
            password=matrix_password,
            display_name=display_name,
        )
        return await _matrix_login_password(
            client=client,
            matrix_user_id=matrix_user_id,
            password=matrix_password,
        )


def _slugify(value: str) -> str:
    return slugify(value)


def _ensure_unique_org_slug(session: Session, preferred: str) -> str:
    base = _slugify(preferred)
    candidate = base
    counter = 2
    while (
        session.query(Organization).filter(Organization.slug == candidate).first()
        or any(
            isinstance(obj, Organization) and getattr(obj, "slug", None) == candidate
            for obj in session.new
        )
    ):
        candidate = f"{base}-{counter}"
        counter += 1
    return candidate


def _ensure_unique_event_slug(session: Session, preferred: str) -> str:
    base = _slugify(preferred)
    candidate = base
    counter = 2
    while (
        session.query(NetworkEvent).filter(NetworkEvent.slug == candidate).first()
        or any(
            isinstance(obj, NetworkEvent) and getattr(obj, "slug", None) == candidate
            for obj in session.new
        )
    ):
        candidate = f"{base}-{counter}"
        counter += 1
    return candidate


def _ensure_unique_team_slug(session: Session, preferred: str) -> str:
    base = _slugify(preferred)
    candidate = base
    counter = 2
    while (
        session.query(Team).filter(Team.slug == candidate).first()
        or any(
            isinstance(obj, Team) and getattr(obj, "slug", None) == candidate
            for obj in session.new
        )
    ):
        candidate = f"{base}-{counter}"
        counter += 1
    return candidate


def _ensure_unique_contact_slug(session: Session, preferred: str, excluding_user_id: Optional[str] = None) -> str:
    base = _slugify(preferred)
    candidate = base
    counter = 2
    while True:
        existing = session.query(UserContactPage).filter(UserContactPage.slug == candidate).first()
        if not existing or (excluding_user_id and existing.user_id == excluding_user_id):
            return candidate
        candidate = f"{base}-{counter}"
        counter += 1


def _map_org(org: Organization, current_user_id: Optional[str] = None) -> OrganizationResponse:
    my_role = None
    if current_user_id:
        for membership in org.memberships or []:
            if membership.user_id == current_user_id:
                my_role = membership.role
                break
    return OrganizationResponse(
        id=org.id,
        name=org.name,
        slug=org.slug,
        description=org.description,
        source_url=org.source_url,
        source_urls=_org_source_urls(org),
        image_url=org.image_url,
        tags=list(org.tags or []),
        seeded_from_events=bool(org.seeded_from_events),
        claimed_by_user_id=org.claimed_by_user_id,
        created_by_user_id=org.created_by_user_id,
        membership_count=len(org.memberships or []),
        my_role=my_role,
        created_at=org.created_at,
        updated_at=org.updated_at,
    )


def _map_public_org(
    org: Organization,
    session: Session,
    redirected_from_slug: Optional[str] = None,
) -> PublicOrganizationResponse:
    now_utc = datetime.now(timezone.utc)
    upcoming_events_count = (
        session.query(NetworkEvent)
        .filter(
            NetworkEvent.host_org_id == org.id,
            (
                (NetworkEvent.ends_at.isnot(None) & (NetworkEvent.ends_at >= now_utc))
                | (NetworkEvent.ends_at.is_(None) & NetworkEvent.starts_at.isnot(None) & (NetworkEvent.starts_at >= now_utc))
            ),
        )
        .count()
    )
    pending_claim_requests_count = (
        session.query(OrganizationClaimRequest)
        .filter(
            OrganizationClaimRequest.organization_id == org.id,
            OrganizationClaimRequest.status == "pending",
        )
        .count()
    )
    return PublicOrganizationResponse(
        id=org.id,
        name=org.name,
        slug=org.slug,
        description=org.description,
        source_url=org.source_url,
        source_urls=_org_source_urls(org),
        image_url=org.image_url,
        tags=list(org.tags or []),
        seeded_from_events=bool(org.seeded_from_events),
        upcoming_events_count=upcoming_events_count,
        pending_claim_requests_count=pending_claim_requests_count,
        is_contested=bool(pending_claim_requests_count > 0),
        redirected_from_slug=redirected_from_slug,
        created_at=org.created_at,
        updated_at=org.updated_at,
    )


def _map_public_org_list_item(
    org: Organization,
    membership_count: int,
    upcoming_events_count: int,
    pending_claim_requests_count: int,
) -> PublicOrganizationListItemResponse:
    return PublicOrganizationListItemResponse(
        id=org.id,
        name=org.name,
        slug=org.slug,
        description=org.description,
        source_url=org.source_url,
        source_urls=_org_source_urls(org),
        image_url=org.image_url,
        tags=list(org.tags or []),
        seeded_from_events=bool(org.seeded_from_events),
        membership_count=int(membership_count or 0),
        upcoming_events_count=int(upcoming_events_count or 0),
        pending_claim_requests_count=int(pending_claim_requests_count or 0),
        is_contested=bool((pending_claim_requests_count or 0) > 0),
        created_at=org.created_at,
        updated_at=org.updated_at,
    )


def _map_governance_motion(motion: GovernanceMotion) -> GovernanceMotionResponse:
    return GovernanceMotionResponse(
        id=motion.id,
        type=motion.type,
        parent_motion_id=motion.parent_motion_id,
        title=motion.title,
        body=motion.body,
        proposed_body_diff=motion.proposed_body_diff,
        status=motion.status,
        proposer_type=motion.proposer_type,
        proposer_id=motion.proposer_user_id,
        proposer_name=motion.proposer_name,
        proposer_user_name=motion.proposer_user_name,
        proposer_org_id=motion.proposer_org_id,
        proposer_org_name=motion.proposer_org_name,
        seconder_id=motion.seconder_id,
        seconder_name=motion.seconder_name,
        discussion_deadline=motion.discussion_deadline,
        voting_deadline=motion.voting_deadline,
        quorum_required=motion.quorum_required,
        is_dissolution=(motion.type == GovernanceMotionType.DISSOLUTION.value),
        created_at=motion.created_at,
        updated_at=motion.updated_at,
    )


def _dissolution_required_yea(participating_voters: int) -> int:
    if participating_voters <= 0:
        return 0
    return (3 * participating_voters + 3) // 4


def _governance_vote_result(motion: GovernanceMotion) -> Dict[str, Any]:
    yea = sum(1 for v in motion.votes if v.choice == GovernanceVoteChoice.YEA.value)
    nay = sum(1 for v in motion.votes if v.choice == GovernanceVoteChoice.NAY.value)
    abstain = sum(1 for v in motion.votes if v.choice == GovernanceVoteChoice.ABSTAIN.value)
    participating_voters = yea + nay + abstain
    quorum_met = participating_voters >= int(motion.quorum_required or 0)
    threshold_rule = "simple_majority"
    required_yea = nay + 1
    passed = quorum_met and yea > nay
    if motion.type == GovernanceMotionType.DISSOLUTION.value:
        threshold_rule = "three_fourths_majority_of_participating_voters"
        required_yea = _dissolution_required_yea(participating_voters)
        passed = quorum_met and yea >= required_yea
    return {
        "yea": yea,
        "nay": nay,
        "abstain": abstain,
        "total_eligible": int(motion.quorum_required or 0),
        "participating_voters": participating_voters,
        "threshold_rule": threshold_rule,
        "required_yea": required_yea,
        "quorum_met": quorum_met,
        "passed": passed,
    }


def _governance_reaction_counts(motion: GovernanceMotion) -> GovernanceVoteCountsResponse:
    up = sum(1 for r in motion.reactions if r.direction == GovernanceReactionType.UP.value)
    down = sum(1 for r in motion.reactions if r.direction == GovernanceReactionType.DOWN.value)
    return GovernanceVoteCountsResponse(up=up, down=down, score=up - down)


def _get_dissolution_plan(
    session: Session,
    motion_id: uuid.UUID,
) -> Optional[GovernanceDissolutionPlan]:
    return (
        session.query(GovernanceDissolutionPlan)
        .filter(GovernanceDissolutionPlan.motion_id == motion_id)
        .first()
    )


def _validate_dissolution_payload(payload: GovernanceMotionCreate) -> None:
    if payload.type != GovernanceMotionType.DISSOLUTION.value:
        return
    if payload.parent_motion_id:
        raise HTTPException(status_code=422, detail="Dissolution motions cannot be amendments")
    asset_disposition = (payload.dissolution_asset_disposition or "").strip()
    recipient_name = (payload.dissolution_asset_recipient_name or "").strip()
    recipient_type = (payload.dissolution_asset_recipient_type or "").strip()
    if not asset_disposition:
        raise HTTPException(
            status_code=422,
            detail="dissolution_asset_disposition is required for dissolution motions",
        )
    if not recipient_name:
        raise HTTPException(
            status_code=422,
            detail="dissolution_asset_recipient_name is required for dissolution motions",
        )
    if recipient_type not in {"non_profit", "other_legal_entity"}:
        raise HTTPException(
            status_code=422,
            detail="dissolution_asset_recipient_type must be non_profit or other_legal_entity",
        )


def _can_manage_governance_motion(
    motion: GovernanceMotion,
    current_user: dict,
    session: Session,
) -> bool:
    if _is_sysadmin(current_user):
        return True
    user_id = _actor_user_id(current_user)
    if not user_id:
        return False
    if motion.proposer_user_id == user_id:
        return True
    if motion.proposer_type == GovernanceProposerType.ORG.value and motion.proposer_org_id:
        org = session.query(Organization).filter(Organization.id == motion.proposer_org_id).first()
        if org and _is_org_admin(org, current_user):
            return True
    return False


def _ensure_governance_transition(motion: GovernanceMotion, target_status: str) -> None:
    transitions = {
        GovernanceMotionStatus.PROPOSED.value: {
            GovernanceMotionStatus.SECONDED.value,
            GovernanceMotionStatus.WITHDRAWN.value,
            GovernanceMotionStatus.DISCUSSION.value,
        },
        GovernanceMotionStatus.SECONDED.value: {GovernanceMotionStatus.DISCUSSION.value},
        GovernanceMotionStatus.DISCUSSION.value: {
            GovernanceMotionStatus.VOTING.value,
            GovernanceMotionStatus.TABLED.value,
        },
        GovernanceMotionStatus.VOTING.value: {
            GovernanceMotionStatus.PASSED.value,
            GovernanceMotionStatus.FAILED.value,
        },
        GovernanceMotionStatus.TABLED.value: {GovernanceMotionStatus.DISCUSSION.value},
    }
    if not is_transition_allowed(motion.status, target_status, transitions):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status transition from {motion.status} to {target_status}",
        )


def _map_network_event(event: NetworkEvent, current_user: Optional[dict], session: Session) -> NetworkEventResponse:
    user_id = _actor_user_id(current_user or {})
    my_host_role = None

    if user_id:
        if event.claimed_by_user_id == user_id:
            my_host_role = "owner"
        elif event.host_type == EventHostType.INDIVIDUAL.value and event.host_user_id == user_id:
            my_host_role = "host_individual"
        elif event.host_type == EventHostType.ORG.value and event.host_org_id:
            org = session.query(Organization).filter(Organization.id == event.host_org_id).first()
            if org and _is_org_admin(org, current_user or {}):
                my_host_role = "host_org_admin"

    host_org_name = None
    if event.host_type == EventHostType.ORG.value and event.host_org_id:
        host_org = session.query(Organization).filter(Organization.id == event.host_org_id).first()
        if host_org:
            host_org_name = host_org.name
    source_url = (event.source_url or "").strip().lower()
    represented_in_codecollective_source = bool(
        event.seeded_from_events
        or "codecollective.us" in source_url
    )

    return NetworkEventResponse(
        id=event.id,
        title=event.title,
        slug=event.slug,
        description=event.description,
        starts_at=event.starts_at,
        ends_at=event.ends_at,
        location=event.location,
        source_url=event.source_url,
        image_url=event.image_url,
        tags=list(event.tags or []),
        host_type=event.host_type,
        host_user_id=event.host_user_id,
        host_org_id=event.host_org_id,
        host_org_name=host_org_name,
        claimed_by_user_id=event.claimed_by_user_id,
        created_by_user_id=event.created_by_user_id,
        seeded_from_events=bool(event.seeded_from_events),
        represented_in_codecollective_source=represented_in_codecollective_source,
        is_unclaimed=event.claimed_by_user_id is None,
        my_host_role=my_host_role,
        created_at=event.created_at,
        updated_at=event.updated_at,
    )


def _resolve_event_host_binding(
    *,
    host_type: str,
    host_user_id: Optional[str],
    host_org_id: Optional[uuid.UUID],
    current_user: dict,
    session: Session,
) -> tuple[str, Optional[str], Optional[uuid.UUID]]:
    user_id = _actor_user_id(current_user)
    normalized_type = host_type.strip().lower()
    normalized_user_id = host_user_id.strip() if host_user_id else None

    if normalized_type == EventHostType.UNCLAIMED.value:
        if normalized_user_id or host_org_id:
            raise HTTPException(status_code=422, detail="Unclaimed host type cannot include host_user_id or host_org_id")
        return EventHostType.UNCLAIMED.value, None, None

    if normalized_type == EventHostType.INDIVIDUAL.value:
        if host_org_id:
            raise HTTPException(status_code=422, detail="Individual host type cannot include host_org_id")
        target_user_id = normalized_user_id or user_id
        if not target_user_id:
            raise HTTPException(status_code=401, detail="Authentication required for individual host")
        if target_user_id != user_id and not _is_sysadmin(current_user):
            raise HTTPException(status_code=403, detail="Cannot assign event to a different user")
        return EventHostType.INDIVIDUAL.value, target_user_id, None

    if normalized_type == EventHostType.ORG.value:
        if normalized_user_id:
            raise HTTPException(status_code=422, detail="Org host type cannot include host_user_id")
        if not host_org_id:
            raise HTTPException(status_code=422, detail="host_org_id is required when host_type='org'")
        org = session.query(Organization).filter(Organization.id == host_org_id).first()
        if not org:
            raise HTTPException(status_code=404, detail="Host organization not found")
        if not _is_org_admin(org, current_user):
            raise HTTPException(status_code=403, detail="Organization admin access required for org-hosted events")
        return EventHostType.ORG.value, None, host_org_id

    raise HTTPException(status_code=422, detail="Unsupported host_type")


def _extract_bearer_token(authorization: Optional[str]) -> str:
    return extract_bearer_token(authorization)


def _require_ingest_auth(request: Request) -> None:
    expected = (os.getenv("ORG_INGEST_TOKEN") or "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="Ingest token is not configured")
    provided = (
        request.headers.get("x-org-ingest-token")
        or _extract_bearer_token(request.headers.get("authorization"))
    )
    if not provided or not secrets.compare_digest(provided.strip(), expected):
        raise HTTPException(status_code=401, detail="Invalid ingest token")


def _normalize_ingest_url(value: Optional[str]) -> Optional[str]:
    return normalize_ingest_url(value)


def _normalize_org_source_urls(values: Optional[List[str]]) -> List[str]:
    return normalize_org_source_urls(values)


def _org_source_urls(org: Organization) -> List[str]:
    urls = _normalize_org_source_urls(list(org.source_urls or []))
    canonical = _normalize_ingest_url(org.source_url)
    if canonical and canonical not in urls:
        urls.insert(0, canonical)
    return urls


def _set_org_source_urls(org: Organization, values: List[str]) -> None:
    normalized = _normalize_org_source_urls(values)
    org.source_urls = normalized
    if not org.source_url and normalized:
        org.source_url = normalized[0]


def _add_org_source_url(org: Organization, value: Optional[str]) -> None:
    url = _normalize_ingest_url(value)
    if not url:
        return
    merged = _org_source_urls(org)
    if url in merged:
        return
    merged.append(url)
    _set_org_source_urls(org, merged)


def _find_org_by_source_url(session: Session, value: Optional[str]) -> Optional[Organization]:
    url = _normalize_ingest_url(value)
    if not url:
        return None
    org = session.query(Organization).filter(Organization.source_url == url).first()
    if org:
        return org
    return session.query(Organization).filter(Organization.source_urls.contains([url])).first()


def _clean_ingest_tags(tags: Optional[List[str]], city: Optional[str] = None) -> List[str]:
    return clean_ingest_tags(tags, city)


def _derive_org_name(source_url: Optional[str], fallback: Optional[str] = None) -> str:
    return derive_org_name(source_url, fallback)


def _build_ingest_event_key(item: CalendarIngestEvent) -> str:
    if item.ingest_key and item.ingest_key.strip():
        return item.ingest_key.strip()
    material = "|".join(
        [
            str(item.city or "").strip().lower(),
            str(item.host_org_source_url or "").strip().lower(),
            str(item.source_url or "").strip().lower(),
            str(item.title or "").strip().lower(),
            item.starts_at.isoformat() if item.starts_at else "",
            item.ends_at.isoformat() if item.ends_at else "",
            str(item.location or "").strip().lower(),
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _coerce_calendar_datetime(value: Optional[Any]) -> Optional[datetime]:
    if value is None:
        return None
    text_value = str(value).strip()
    if not text_value:
        return None
    if text_value.endswith("Z"):
        text_value = text_value[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text_value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _render_public_event_location(value: Optional[Any]) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        location = value.strip()
        return location or None
    if not isinstance(value, dict):
        return None
    parts: list[str] = []
    for key in ("name", "address", "city", "state", "postalCode", "country"):
        field_value = str(value.get(key) or "").strip()
        if field_value:
            parts.append(field_value)
    if not parts and value.get("latitude") is not None and value.get("longitude") is not None:
        parts.append(f"{value.get('latitude')}, {value.get('longitude')}")
    return ", ".join(parts) if parts else None


def _derive_public_event_org_name(raw_event: Dict[str, Any], host_org_source_url: Optional[str]) -> str:
    for key in ("org_name", "orgName", "source_group", "group_name"):
        candidate = str(raw_event.get(key) or "").strip()
        if candidate:
            return candidate
    organizer = raw_event.get("organizer")
    if isinstance(organizer, dict):
        name = str(organizer.get("name") or "").strip()
        if name:
            return name
    if isinstance(organizer, str):
        name = organizer.strip()
        if name:
            return name
    if host_org_source_url:
        return _derive_org_name(host_org_source_url, None)
    return "Organization"


def _city_from_feed_url(feed_url: str) -> Optional[str]:
    return city_from_feed_url(feed_url)


def _build_ingest_payload_from_public_feed(
    feed_url: str,
    raw_events: List[Dict[str, Any]],
) -> CalendarIngestPayload:
    city = _city_from_feed_url(feed_url)
    events: list[CalendarIngestEvent] = []
    for raw_event in raw_events:
        if not isinstance(raw_event, dict):
            continue
        title = str(raw_event.get("name") or raw_event.get("title") or "").strip()
        if not title:
            continue
        source_url = _normalize_ingest_url(raw_event.get("url") or raw_event.get("source_url"))
        host_org_source_url = _normalize_ingest_url(raw_event.get("source") or raw_event.get("source_url"))
        host_org_image_url = _normalize_ingest_url(raw_event.get("orgImageUrl") or raw_event.get("org_image_url"))
        event_image_url = _normalize_ingest_url(raw_event.get("imageUrl") or raw_event.get("image_url")) or host_org_image_url
        host_org_name = _derive_public_event_org_name(raw_event, host_org_source_url)
        events.append(
            CalendarIngestEvent(
                title=title,
                description=str(raw_event.get("description") or "").strip() or None,
                starts_at=_coerce_calendar_datetime(raw_event.get("startDate") or raw_event.get("starts_at")),
                ends_at=_coerce_calendar_datetime(raw_event.get("endDate") or raw_event.get("ends_at")),
                location=_render_public_event_location(raw_event.get("location")),
                source_url=source_url,
                host_org_source_url=host_org_source_url,
                host_org_name=host_org_name,
                host_org_image_url=host_org_image_url,
                image_url=event_image_url,
                tags=raw_event.get("tags") if isinstance(raw_event.get("tags"), list) else None,
                city=str(raw_event.get("city") or city or "").strip() or None,
            )
        )
    return CalendarIngestPayload(
        source="codecollective-public-json",
        generated_at=datetime.now(timezone.utc),
        organizations=[],
        events=events,
    )


def _upsert_ingested_organization(
    session: Session,
    item: CalendarIngestOrganization,
) -> tuple[Organization, bool]:
    source_url = _normalize_ingest_url(item.source_url)
    name = _derive_org_name(source_url, item.name)
    tags = _clean_ingest_tags(item.tags, item.city)
    image_url = _normalize_ingest_url(item.image_url)
    description = (item.description or "").strip() or None

    org = _find_org_by_source_url(session, source_url)
    if not org:
        candidate_slug = _slugify(name)
        org = session.query(Organization).filter(Organization.slug == candidate_slug).first()

    created = False
    if not org:
        org = Organization(
            id=uuid.uuid4(),
            name=name,
            slug=_ensure_unique_org_slug(session, name),
            description=description,
            source_url=source_url,
            source_urls=[source_url] if source_url else [],
            image_url=image_url,
            tags=tags,
            seeded_from_events=True,
        )
        session.add(org)
        created = True
    else:
        if not org.claimed_by_user_id:
            org.name = name
            if description:
                org.description = description
        _add_org_source_url(org, source_url)
        if image_url:
            org.image_url = image_url
        if tags:
            merged_tags = sorted(set((org.tags or []) + tags))
            org.tags = merged_tags
        org.seeded_from_events = True
        org.updated_at = datetime.now(timezone.utc)
    return org, created


def _upsert_ingested_event(
    session: Session,
    item: CalendarIngestEvent,
    host_org_by_source: Dict[str, Organization],
) -> tuple[Optional[NetworkEvent], str]:
    if item.ends_at and item.starts_at and item.ends_at < item.starts_at:
        return None, "skipped"

    ingest_key = _build_ingest_event_key(item)
    source_url = _normalize_ingest_url(item.source_url)
    host_org_source_url = _normalize_ingest_url(item.host_org_source_url)
    image_url = _normalize_ingest_url(item.image_url)
    tags = _clean_ingest_tags(item.tags, item.city)

    host_org = host_org_by_source.get(host_org_source_url or "")
    title = item.title.strip()
    if not title:
        return None, "skipped"

    event = session.query(NetworkEvent).filter(NetworkEvent.ingest_key == ingest_key).first()
    if not event and source_url and item.starts_at:
        event = (
            session.query(NetworkEvent)
            .filter(
                NetworkEvent.source_url == source_url,
                NetworkEvent.title == title,
                NetworkEvent.starts_at == item.starts_at,
            )
            .first()
        )

    # source_url is globally unique. Some feeds reuse one URL across many events.
    # Keep the first binding and drop conflicting source_url values for new rows.
    resolved_source_url = source_url
    if not event and resolved_source_url:
        existing_source = session.query(NetworkEvent).filter(NetworkEvent.source_url == resolved_source_url).first()
        if existing_source:
            same_instance = (
                existing_source.title == title
                or (item.starts_at and existing_source.starts_at == item.starts_at)
            )
            if same_instance:
                event = existing_source
            else:
                resolved_source_url = None
        elif any(
            isinstance(obj, NetworkEvent) and getattr(obj, "source_url", None) == resolved_source_url
            for obj in session.new
        ):
            resolved_source_url = None

    created = False
    if not event:
        event = NetworkEvent(
            id=uuid.uuid4(),
            title=title,
            slug=_ensure_unique_event_slug(session, f"{title}-{item.starts_at.date()}" if item.starts_at else title),
            description=(item.description or "").strip() or None,
            starts_at=item.starts_at,
            ends_at=item.ends_at,
            location=(item.location or "").strip() or None,
            source_url=resolved_source_url,
            ingest_key=ingest_key,
            image_url=image_url,
            tags=tags,
            host_type=EventHostType.ORG.value if host_org else EventHostType.UNCLAIMED.value,
            host_org_id=host_org.id if host_org else None,
            host_user_id=None,
            claimed_by_user_id=None,
            seeded_from_events=True,
        )
        session.add(event)
        created = True
    else:
        event.ingest_key = event.ingest_key or ingest_key
        event.title = title
        event.description = (item.description or "").strip() or None
        event.starts_at = item.starts_at
        event.ends_at = item.ends_at
        event.location = (item.location or "").strip() or None
        if resolved_source_url and resolved_source_url != event.source_url:
            existing_source_owner = (
                session.query(NetworkEvent)
                .filter(NetworkEvent.source_url == resolved_source_url, NetworkEvent.id != event.id)
                .first()
            )
            if not existing_source_owner:
                event.source_url = resolved_source_url
        if image_url:
            event.image_url = image_url
        if tags:
            event.tags = sorted(set((event.tags or []) + tags))
        if host_org and not event.claimed_by_user_id:
            event.host_type = EventHostType.ORG.value
            event.host_org_id = host_org.id
            event.host_user_id = None
        event.seeded_from_events = True
        event.updated_at = datetime.now(timezone.utc)

    return event, "created" if created else "updated"


def _validate_public_url(url: Optional[str], field_name: str) -> Optional[str]:
    if not url:
        return None
    cleaned = url.strip()
    if not cleaned:
        return None
    try:
        parsed = urlparse(cleaned)
    except Exception:
        raise HTTPException(status_code=422, detail=f"{field_name} must be a valid URL")
    if parsed.scheme.lower() not in {"http", "https"}:
        raise HTTPException(status_code=422, detail=f"{field_name} must start with http:// or https://")
    hostname = (parsed.hostname or "").strip().lower()
    if not hostname:
        raise HTTPException(status_code=422, detail=f"{field_name} must include a hostname")
    if hostname in {"localhost"} or hostname.endswith(".local"):
        raise HTTPException(status_code=422, detail=f"{field_name} must use a public hostname")
    try:
        ip_value = ipaddress.ip_address(hostname)
    except ValueError:
        ip_value = None
    if ip_value and (
        ip_value.is_private
        or ip_value.is_loopback
        or ip_value.is_link_local
        or ip_value.is_multicast
        or ip_value.is_reserved
        or ip_value.is_unspecified
    ):
        raise HTTPException(status_code=422, detail=f"{field_name} must use a public hostname")
    return parsed._replace(fragment="").geturl()


def _is_disallowed_public_ip(ip_value: ipaddress._BaseAddress) -> bool:
    return (
        ip_value.is_private
        or ip_value.is_loopback
        or ip_value.is_link_local
        or ip_value.is_multicast
        or ip_value.is_reserved
        or ip_value.is_unspecified
    )


def _ensure_public_fetch_url(url: str, field_name: str = "source_url") -> str:
    normalized = _validate_public_url(url, field_name)
    if not normalized:
        raise HTTPException(status_code=422, detail=f"{field_name} must be a valid public URL")
    parsed = urlparse(normalized)
    hostname = (parsed.hostname or "").strip().lower()
    if not hostname:
        raise HTTPException(status_code=422, detail=f"{field_name} must include a hostname")

    try:
        resolved = socket.getaddrinfo(hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise HTTPException(status_code=422, detail=f"{field_name} host could not be resolved")
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"{field_name} host validation failed: {exc}")

    for result in resolved:
        candidate = result[4][0]
        try:
            ip_value = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if _is_disallowed_public_ip(ip_value):
            raise HTTPException(status_code=422, detail=f"{field_name} must resolve to a public host")
    return normalized


def _truncate_preview_text(value: Optional[str], limit: int) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw:
        return None
    compact = re.sub(r"\s+", " ", raw).strip()
    if len(compact) <= limit:
        return compact
    return compact[: max(0, limit - 1)].rstrip() + "…"


def _meta_content_from_soup(soup: BeautifulSoup, *, property_name: Optional[str] = None, name: Optional[str] = None) -> Optional[str]:
    selector: dict[str, str] = {}
    if property_name:
        selector["property"] = property_name
    if name:
        selector["name"] = name
    if not selector:
        return None
    tag = soup.find("meta", attrs=selector)
    if not tag:
        return None
    return _truncate_preview_text(str(tag.get("content") or ""), 5000)


async def _fetch_chat_link_preview(url: str) -> ChatLinkPreviewResponse:
    safe_url = _ensure_public_fetch_url(url, "url")
    parsed_safe = urlparse(safe_url)
    if parsed_safe.username or parsed_safe.password:
        raise HTTPException(status_code=422, detail="url must not include embedded credentials")
    if parsed_safe.port and parsed_safe.port not in {80, 443}:
        raise HTTPException(status_code=422, detail="url must use standard http/https ports")

    timeout = httpx.Timeout(connect=6.0, read=6.0, write=6.0, pool=6.0)
    headers = {
        "User-Agent": "ArkavoOrgPortalLinkPreview/1.0 (+https://dev.portal.arkavo.org)",
        "Accept": "text/html,application/xhtml+xml;q=0.9",
    }
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, max_redirects=4) as client:
        response = await client.get(safe_url, headers=headers)

    if not response.is_success:
        raise HTTPException(status_code=422, detail=f"Link returned HTTP {response.status_code}")
    if len(response.content or b"") > 1_000_000:
        raise HTTPException(status_code=422, detail="Link preview source is too large")
    content_type = str(response.headers.get("content-type") or "").lower()
    if "text/html" not in content_type and "application/xhtml+xml" not in content_type:
        raise HTTPException(status_code=422, detail="Link preview supports HTML pages only")

    resolved_url = _ensure_public_fetch_url(str(response.url), "url")
    soup = BeautifulSoup(response.text or "", "html.parser")
    title = (
        _meta_content_from_soup(soup, property_name="og:title")
        or _meta_content_from_soup(soup, name="twitter:title")
        or _truncate_preview_text(soup.title.get_text(" ", strip=True) if soup.title else "", 180)
    )
    description = (
        _meta_content_from_soup(soup, property_name="og:description")
        or _meta_content_from_soup(soup, name="description")
        or _meta_content_from_soup(soup, name="twitter:description")
    )
    if description:
        description = _truncate_preview_text(description, 320)

    image_raw = (
        _meta_content_from_soup(soup, property_name="og:image")
        or _meta_content_from_soup(soup, name="twitter:image")
    )
    image_url = _coerce_public_url_candidate(urljoin(resolved_url, image_raw), "image_url") if image_raw else None

    canonical_raw = ""
    canonical_link = soup.find("link", attrs={"rel": lambda value: value and "canonical" in str(value).lower()})
    if canonical_link and canonical_link.get("href"):
        canonical_raw = str(canonical_link.get("href") or "").strip()
    canonical_url = _coerce_public_url_candidate(urljoin(resolved_url, canonical_raw), "canonical_url") if canonical_raw else None
    canonical_url = canonical_url or resolved_url

    parsed_domain = urlparse(canonical_url or resolved_url)
    domain = (parsed_domain.hostname or "").strip().lower() or None
    site_name = (
        _meta_content_from_soup(soup, property_name="og:site_name")
        or _meta_content_from_soup(soup, name="application-name")
        or domain
    )
    return ChatLinkPreviewResponse(
        url=safe_url,
        canonical_url=canonical_url,
        title=_truncate_preview_text(title, 180),
        description=description,
        image_url=image_url,
        site_name=_truncate_preview_text(site_name, 80),
        domain=domain,
    )


def _dedupe_contact_links(links: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    deduped: list[dict[str, str]] = []
    for entry in links:
        label = str(entry.get("label") or "").strip()
        url = str(entry.get("url") or "").strip()
        if not label or not url:
            continue
        key = (label.lower(), url.lower())
        if key in seen:
            continue
        seen.add(key)
        deduped.append({"label": label, "url": url})
    return deduped


def _extract_contact_import_from_html(source_url: str, html_text: str) -> dict[str, Any]:
    soup = BeautifulSoup(html_text, "html.parser")
    title_text = (soup.title.get_text(" ", strip=True) if soup.title else "").strip()
    h1_text = ""
    first_h1 = soup.find("h1")
    if first_h1:
        h1_text = first_h1.get_text(" ", strip=True).strip()

    headline = ""
    if title_text:
        headline = title_text.split("|")[0].strip()
    if not headline and h1_text:
        headline = h1_text

    email_public: Optional[str] = None
    phone_public: Optional[str] = None
    linkedin_url: Optional[str] = None
    github_url: Optional[str] = None
    x_url: Optional[str] = None
    website_url: Optional[str] = None
    links: list[dict[str, str]] = []

    for anchor in soup.find_all("a", href=True):
        href = (anchor.get("href") or "").strip()
        if not href:
            continue
        label = anchor.get_text(" ", strip=True) or "Link"
        href_lower = href.lower()

        if href_lower.startswith("mailto:"):
            candidate = href.split(":", 1)[1].strip()
            if candidate and not email_public:
                email_public = candidate.lower()
            continue
        if href_lower.startswith("tel:"):
            candidate = href.split(":", 1)[1].strip()
            if candidate and not phone_public:
                phone_public = candidate
            continue
        if not href_lower.startswith("http://") and not href_lower.startswith("https://"):
            continue

        normalized = _validate_public_url(href, "imported_link_url")
        if not normalized:
            continue
        host = (urlparse(normalized).hostname or "").lower()
        if "linkedin.com" in host and not linkedin_url:
            linkedin_url = normalized
        elif "github.com" in host and not github_url:
            github_url = normalized
        elif ("twitter.com" in host or "x.com" in host) and not x_url:
            x_url = normalized
        elif not website_url and host not in {"www.linkedin.com", "linkedin.com", "github.com", "www.github.com", "twitter.com", "www.twitter.com", "x.com", "www.x.com"}:
            website_url = normalized
        links.append({"label": label.strip()[:120] or "Link", "url": normalized})

    body = soup.body
    body_text = body.get_text("\n", strip=True) if body else soup.get_text("\n", strip=True)
    bio = ""
    if body_text:
        pre_contact = body_text.split("Contact Information", 1)[0].strip()
        if h1_text:
            pre_contact = re.sub(rf"^\s*{re.escape(h1_text)}\s*", "", pre_contact, count=1, flags=re.IGNORECASE).strip()
        pre_contact = re.sub(r"\s+", " ", pre_contact).strip()
        if pre_contact:
            bio = pre_contact[:2000]

    photo_url: Optional[str] = None
    main_section = soup.find(id="main") or soup.body
    if main_section:
        img = main_section.find("img")
        if img and img.get("src"):
            photo_url = _validate_public_url(urljoin(source_url, img.get("src")), "photo_url")

    return {
        "headline": headline[:255] if headline else None,
        "bio": bio or None,
        "photo_url": photo_url,
        "email_public": email_public,
        "phone_public": phone_public,
        "linkedin_url": linkedin_url,
        "github_url": github_url,
        "x_url": x_url,
        "website_url": website_url,
        "links": _dedupe_contact_links(links)[:20],
    }


def _fetch_public_profile_import(source_url: str) -> dict[str, Any]:
    try:
        response = requests.get(
            source_url,
            timeout=12,
            headers={
                "User-Agent": "ArkavoOrgPortalProfileImporter/1.0 (+https://dev.portal.arkavo.org)",
                "Accept": "text/html,application/xhtml+xml",
            },
            allow_redirects=True,
        )
    except requests.RequestException as exc:
        raise HTTPException(status_code=422, detail=f"Failed to fetch source profile: {exc}")
    if response.status_code >= 400:
        raise HTTPException(status_code=422, detail=f"Source profile returned HTTP {response.status_code}")
    content_type = (response.headers.get("content-type") or "").lower()
    if "text/html" not in content_type and "application/xhtml+xml" not in content_type:
        raise HTTPException(status_code=422, detail="Source profile must be an HTML page")
    if len(response.content) > 2 * 1024 * 1024:
        raise HTTPException(status_code=422, detail="Source profile is too large")
    final_url = _ensure_public_fetch_url(response.url, "source_url")
    return _extract_contact_import_from_html(final_url, response.text)


def _apply_contact_import_to_record(
    contact: UserContactPage,
    imported: dict[str, Any],
    overwrite: bool,
) -> list[str]:
    changed: list[str] = []

    url_fields = {"photo_url", "linkedin_url", "github_url", "x_url", "website_url"}
    simple_fields = {
        "headline",
        "bio",
        "photo_url",
        "email_public",
        "phone_public",
        "linkedin_url",
        "github_url",
        "x_url",
        "website_url",
    }

    for field_name in simple_fields:
        candidate = imported.get(field_name)
        if candidate is None:
            continue
        current_value = getattr(contact, field_name, None)
        if not overwrite and current_value:
            continue
        if field_name in url_fields:
            candidate = _validate_public_url(candidate, field_name)
        if current_value != candidate:
            setattr(contact, field_name, candidate)
            changed.append(field_name)

    imported_links = imported.get("links") or []
    if imported_links:
        normalized_links = _dedupe_contact_links(
            [
                {"label": str(item.get("label") or "").strip(), "url": _validate_public_url(item.get("url"), "links.url") or ""}
                for item in imported_links
                if isinstance(item, dict) and item.get("url")
            ]
        )
        if overwrite:
            if (contact.links or []) != normalized_links:
                contact.links = normalized_links
                changed.append("links")
        else:
            merged = _dedupe_contact_links(list(contact.links or []) + normalized_links)
            if merged != (contact.links or []):
                contact.links = merged
                changed.append("links")

    return changed


def _throttle_action(key: str, limit: int, window_seconds: int) -> None:
    try:
        redis_client = db.redis_client
        if redis_client is None:
            return
        value = redis_client.incr(key)
        if value == 1:
            redis_client.expire(key, window_seconds)
        if int(value) > limit:
            raise HTTPException(status_code=429, detail="Rate limit exceeded for this action")
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning(f"Rate limit check skipped: {exc}")


def _request_client_ip(request: Request) -> str:
    forwarded_for = (request.headers.get("x-forwarded-for") or "").strip()
    candidate = ""
    if forwarded_for:
        candidate = forwarded_for.split(",")[0].strip()
    if not candidate:
        candidate = (request.headers.get("x-real-ip") or "").strip()
    if not candidate and request.client:
        candidate = (request.client.host or "").strip()
    if not candidate:
        return "unknown"
    try:
        ip_obj = ipaddress.ip_address(candidate)
        return ip_obj.compressed
    except ValueError:
        return "unknown"


def _business_card_extension_for_content_type(content_type: str) -> str:
    normalized = (content_type or "").strip().lower()
    if normalized == "image/jpeg":
        return ".jpg"
    if normalized == "image/png":
        return ".png"
    if normalized == "image/webp":
        return ".webp"
    return ".img"


def _business_card_storage_root() -> Path:
    return Path(ORG_BUSINESS_CARD_STORAGE_DIR).resolve()


def _business_card_storage_backend() -> str:
    backend = (ORG_BUSINESS_CARD_STORAGE_BACKEND or "local").strip().lower()
    if backend not in {"local", "s3"}:
        return "local"
    return backend


def _business_card_s3_object_key(
    *,
    submission_id: uuid.UUID,
    content_type: str,
    now: Optional[datetime] = None,
) -> str:
    ts = now or datetime.now(timezone.utc)
    extension = _business_card_extension_for_content_type(content_type)
    key_parts = [str(ts.year), f"{ts.month:02d}", f"{submission_id}{extension}"]
    if ORG_BUSINESS_CARD_S3_PREFIX:
        key_parts.insert(0, ORG_BUSINESS_CARD_S3_PREFIX)
    return "/".join(key_parts)


def _build_business_card_s3_client():
    if not ORG_BUSINESS_CARD_S3_ACCESS_KEY or not ORG_BUSINESS_CARD_S3_SECRET_KEY:
        raise RuntimeError("S3 storage credentials are not configured")
    try:
        import boto3
        from botocore.config import Config
    except Exception as exc:
        raise RuntimeError("boto3 is required for ORG_BUSINESS_CARD_STORAGE_BACKEND=s3") from exc

    session = boto3.session.Session()
    return session.client(
        "s3",
        endpoint_url=ORG_BUSINESS_CARD_S3_ENDPOINT_URL or None,
        aws_access_key_id=ORG_BUSINESS_CARD_S3_ACCESS_KEY,
        aws_secret_access_key=ORG_BUSINESS_CARD_S3_SECRET_KEY,
        region_name=ORG_BUSINESS_CARD_S3_REGION,
        use_ssl=ORG_BUSINESS_CARD_S3_USE_SSL,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def _ensure_business_card_s3_bucket() -> None:
    if not ORG_BUSINESS_CARD_STORAGE_ENABLED or _business_card_storage_backend() != "s3":
        return
    client = _build_business_card_s3_client()
    bucket = ORG_BUSINESS_CARD_S3_BUCKET
    try:
        client.head_bucket(Bucket=bucket)
        return
    except Exception:
        pass
    create_kwargs = {"Bucket": bucket}
    if ORG_BUSINESS_CARD_S3_REGION and ORG_BUSINESS_CARD_S3_REGION != "us-east-1":
        create_kwargs["CreateBucketConfiguration"] = {
            "LocationConstraint": ORG_BUSINESS_CARD_S3_REGION
        }
    client.create_bucket(**create_kwargs)


def _persist_business_card_image(
    *,
    submission_id: uuid.UUID,
    image_bytes: bytes,
    content_type: str,
) -> tuple[str, Optional[str], str]:
    if not ORG_BUSINESS_CARD_STORAGE_ENABLED:
        return ("disabled", None, "")
    backend = _business_card_storage_backend()
    if backend == "s3":
        object_key = _business_card_s3_object_key(
            submission_id=submission_id,
            content_type=content_type,
        )
        client = _build_business_card_s3_client()
        client.put_object(
            Bucket=ORG_BUSINESS_CARD_S3_BUCKET,
            Key=object_key,
            Body=image_bytes,
            ContentType=content_type or "application/octet-stream",
        )
        return ("s3", ORG_BUSINESS_CARD_S3_BUCKET, object_key)

    root = _business_card_storage_root()
    now = datetime.now(timezone.utc)
    relative_dir = Path(str(now.year), f"{now.month:02d}")
    target_dir = root / relative_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    extension = _business_card_extension_for_content_type(content_type)
    target_name = f"{submission_id}{extension}"
    target_path = target_dir / target_name
    temp_path = target_path.with_suffix(target_path.suffix + ".tmp")
    temp_path.write_bytes(image_bytes)
    temp_path.replace(target_path)
    return ("local", None, str(relative_dir / target_name))


def _resolve_business_card_storage_path(relative_path: str) -> Path:
    root = _business_card_storage_root()
    candidate = (root / relative_path).resolve()
    if not str(candidate).startswith(str(root)):
        raise HTTPException(status_code=400, detail="Invalid storage path")
    return candidate


def _load_business_card_image_bytes(
    *,
    storage_backend: str,
    storage_bucket: Optional[str],
    storage_path: str,
) -> bytes:
    backend = (storage_backend or "local").strip().lower()
    if backend == "s3":
        bucket = (storage_bucket or ORG_BUSINESS_CARD_S3_BUCKET).strip()
        if not bucket:
            raise HTTPException(status_code=500, detail="S3 bucket not configured for stored image")
        client = _build_business_card_s3_client()
        response = client.get_object(Bucket=bucket, Key=storage_path)
        body = response.get("Body")
        data = body.read() if body else b""
        if not data:
            raise HTTPException(status_code=404, detail="Stored image file missing")
        return data

    image_path = _resolve_business_card_storage_path(storage_path)
    if not image_path.exists() or not image_path.is_file():
        raise HTTPException(status_code=404, detail="Stored image file missing")
    return image_path.read_bytes()


def _scan_created_target_image_url(
    submission_id: uuid.UUID,
    *,
    target_type: str,
    target_id: Optional[str],
) -> Optional[str]:
    normalized_type = str(target_type or "").strip().lower()
    normalized_id = str(target_id or "").strip()
    if normalized_type not in {"organization", "event"} or not normalized_id:
        return None
    return f"/api/network/scans/{submission_id}/image/public/{normalized_type}/{normalized_id}"


def _enforce_business_card_duplicate_hash_guard(
    session: Session,
    *,
    image_sha256: str,
    duplicate_hash_limit: int,
    duplicate_hash_window_seconds: int,
) -> None:
    if duplicate_hash_limit <= 0 or duplicate_hash_window_seconds <= 0:
        return
    window_start = datetime.now(timezone.utc) - timedelta(seconds=duplicate_hash_window_seconds)
    existing_count = (
        session.query(func.count(BusinessCardSubmission.id))
        .filter(
            BusinessCardSubmission.image_sha256 == image_sha256,
            BusinessCardSubmission.created_at >= window_start,
        )
        .scalar()
        or 0
    )
    if int(existing_count) >= duplicate_hash_limit:
        raise HTTPException(
            status_code=429,
            detail="Duplicate business card submissions exceeded for this time window",
        )


def _audit_event(
    session: Session,
    *,
    actor: dict,
    event_type: str,
    target_type: str,
    target_id: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    session.add(
        NetworkAuditEvent(
            id=uuid.uuid4(),
            actor_user_id=_actor_user_id(actor) or None,
            actor_email=actor.get("email"),
            event_type=event_type,
            target_type=target_type,
            target_id=target_id,
            metadata_json=metadata or {},
        )
    )


def _event_source_file() -> Path:
    # /.../CodeCollective/portal/org-backend/org.py -> repo root is parents[2]
    return Path(__file__).resolve().parents[2] / "baltimore" / "event_sources.py"


def _load_event_sources() -> List[Dict[str, Any]]:
    file_path = _event_source_file()
    if not file_path.exists():
        logger.warning(f"Event source file not found: {file_path}")
        return []
    try:
        source_code = file_path.read_text(encoding="utf-8")
        parsed = ast.parse(source_code, filename=str(file_path))
        sources: list[dict[str, Any]] = []
        for node in parsed.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "sources":
                        value = ast.literal_eval(node.value)
                        if isinstance(value, list):
                            for item in value:
                                if isinstance(item, dict):
                                    sources.append(item)
        return sources
    except Exception as exc:
        logger.error(f"Failed to parse event sources: {exc}")
        return []


def _seed_organizations_from_event_sources(session: Session, force_update: bool = False) -> SeedOrganizationsResponse:
    sources = _load_event_sources()
    inserted = 0
    updated = 0

    for src in sources:
        name = str(src.get("name") or "").strip()
        source_url = str(src.get("url") or "").strip() or None
        if not name:
            continue
        tags = src.get("tags") if isinstance(src.get("tags"), list) else []
        image_url = str(src.get("orgImageUrl") or "").strip() or None

        org = _find_org_by_source_url(session, source_url)
        if not org:
            slug = _ensure_unique_org_slug(session, name)
            org = Organization(
                id=uuid.uuid4(),
                name=name,
                slug=slug,
                description=f"Seeded from Code Collective events source: {name}",
                source_url=source_url,
                source_urls=[source_url] if source_url else [],
                image_url=image_url,
                tags=tags,
                seeded_from_events=True,
            )
            session.add(org)
            inserted += 1
            continue

        if force_update:
            org.name = name
            _add_org_source_url(org, source_url)
            if image_url:
                org.image_url = image_url
            if tags:
                org.tags = tags
            org.seeded_from_events = True
            org.updated_at = datetime.now(timezone.utc)
            updated += 1

    session.commit()
    return SeedOrganizationsResponse(loaded=len(sources), inserted=inserted, updated=updated)


def _is_org_admin(org: Organization, current_user: dict) -> bool:
    if _is_sysadmin(current_user):
        return True
    current_user_id = _actor_user_id(current_user)
    if not current_user_id:
        return False
    if org.claimed_by_user_id == current_user_id:
        return True
    for membership in org.memberships or []:
        if membership.user_id == current_user_id and membership.role == "admin":
            return True
    return False


def _can_manage_org_for_merge(org: Organization, current_user: dict) -> bool:
    if _is_sysadmin(current_user):
        return True
    # For unclaimed organizations, any authenticated user can fold duplicates into
    # an org they already manage.
    if not org.claimed_by_user_id:
        return True
    return _is_org_admin(org, current_user)


def _is_any_org_admin(session: Session, current_user: dict) -> bool:
    if _is_sysadmin(current_user):
        return True
    user_id = _actor_user_id(current_user)
    if not user_id:
        return False
    claimed_org = (
        session.query(Organization.id)
        .filter(Organization.claimed_by_user_id == user_id)
        .first()
    )
    if claimed_org:
        return True
    member_admin = (
        session.query(OrganizationMembership.id)
        .filter(
            OrganizationMembership.user_id == user_id,
            OrganizationMembership.role == "admin",
        )
        .first()
    )
    return bool(member_admin)


def _has_active_team_membership(session: Session, user_id: str) -> bool:
    if not user_id:
        return False
    row = (
        session.query(TeamMembership.id)
        .join(Team, Team.id == TeamMembership.team_id)
        .filter(
            TeamMembership.user_id == user_id,
            TeamMembership.active.is_(True),
            Team.status == "active",
        )
        .first()
    )
    return bool(row)


def _has_recent_attendance(session: Session, user_id: str, days: int = 90) -> bool:
    if not user_id:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    row = (
        session.query(EventAttendance.id)
        .filter(
            EventAttendance.user_id == user_id,
            EventAttendance.attended_at >= cutoff,
        )
        .first()
    )
    return bool(row)


def _resolve_access_classes(session: Session, current_user: dict) -> AccessClassSnapshotResponse:
    if current_user.get("is_anonymous"):
        return AccessClassSnapshotResponse(
            is_public=True,
            is_attendee=False,
            is_member=False,
            is_org_admin=False,
            is_sysadmin=False,
            reasons=["Unauthenticated user defaults to Public class."],
        )

    user_id = _actor_user_id(current_user)
    is_sysadmin = _is_sysadmin(current_user)
    is_org_admin = _is_any_org_admin(session, current_user)
    is_attendee = _has_recent_attendance(session, user_id, days=90)
    is_member = is_org_admin or _has_active_team_membership(session, user_id)
    reasons: list[str] = []
    if is_sysadmin:
        reasons.append("Platform SysAdmin privileges are active.")
    if is_org_admin:
        reasons.append("User has organization admin authority (owner/membership or SysAdmin override).")
    if is_attendee:
        reasons.append("User has recorded attendance within the last 90 days.")
    if is_member:
        reasons.append("User is treated as Member due to org admin authority or active team participation.")
    if not reasons:
        reasons.append("Authenticated user defaults to Public class.")
    return AccessClassSnapshotResponse(
        is_public=True,
        is_attendee=is_attendee,
        is_member=is_member,
        is_org_admin=is_org_admin,
        is_sysadmin=is_sysadmin,
        reasons=reasons,
    )


def _claim_org_record(session: Session, org: Organization, current_user: dict) -> None:
    user_id = _actor_user_id(current_user)
    if not user_id:
        raise HTTPException(status_code=401, detail="Authentication required")
    if org.claimed_by_user_id and org.claimed_by_user_id != user_id:
        raise HTTPException(status_code=409, detail="Organization is already claimed")

    org.claimed_by_user_id = user_id
    if not org.created_by_user_id:
        org.created_by_user_id = user_id

    membership = (
        session.query(OrganizationMembership)
        .filter(
            OrganizationMembership.organization_id == org.id,
            OrganizationMembership.user_id == user_id,
        )
        .first()
    )
    if not membership:
        membership = OrganizationMembership(
            id=uuid.uuid4(),
            organization=org,
            user_id=user_id,
            user_email=current_user.get("email"),
            user_name=current_user.get("name"),
            role="admin",
        )
        session.add(membership)
    else:
        membership.role = "admin"
        membership.user_email = current_user.get("email")
        membership.user_name = current_user.get("name")

    _audit_event(
        session,
        actor=current_user,
        event_type="org.claimed",
        target_type="organization",
        target_id=str(org.id),
        metadata={"claimed_by": user_id},
    )

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


@app.post("/api/network/ingest/calendar", response_model=CalendarIngestResponse)
async def ingest_calendar_feed(
    payload: CalendarIngestPayload,
    request: Request,
    session: Session = Depends(get_db),
):
    _require_ingest_auth(request)
    _throttle_action("network:ingest:calendar", limit=120, window_seconds=3600)
    return _ingest_calendar_payload(session, payload)


@app.get("/api/network/seed", response_model=SeedOrganizationsResponse)
async def seed_organizations(
    force_update: bool = False,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    if not _is_sysadmin(current_user):
        raise HTTPException(status_code=403, detail="SysAdmin privileges required")
    return _seed_organizations_from_event_sources(session, force_update=force_update)


@app.get("/api/network/orgs", response_model=List[OrganizationResponse])
async def list_organizations(
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
    q: str = "",
    mine: bool = False,
    only_unclaimed: bool = False,
    limit: int = 250,
    offset: int = 0,
):
    _require_authenticated_user(current_user)
    safe_limit = max(1, min(limit, 1000))
    user_id = _actor_user_id(current_user)
    query = session.query(Organization).order_by(Organization.name.asc())
    if q.strip():
        needle = f"%{q.strip()}%"
        query = query.filter((Organization.name.ilike(needle)) | (Organization.slug.ilike(needle)))
    if only_unclaimed:
        query = query.filter(Organization.claimed_by_user_id.is_(None))
    safe_offset = max(0, min(offset, 100000))
    organizations = query.offset(safe_offset).limit(safe_limit).all()
    if mine and user_id:
        organizations = [
            org for org in organizations
            if org.claimed_by_user_id == user_id
            or any(m.user_id == user_id for m in org.memberships or [])
        ]
    return [_map_org(org, user_id) for org in organizations]


@app.get("/api/network/teams", response_model=List[TeamResponse])
async def list_teams(
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
    q: str = "",
    active_only: bool = True,
    limit: int = 200,
    offset: int = 0,
):
    _require_authenticated_user(current_user)
    safe_limit = max(1, min(limit, 1000))
    safe_offset = max(0, min(offset, 100000))
    query = session.query(Team)
    if q.strip():
        needle = f"%{q.strip()}%"
        query = query.filter((Team.name.ilike(needle)) | (Team.slug.ilike(needle)))
    if active_only:
        query = query.filter(Team.status == "active")
    return query.order_by(Team.name.asc()).offset(safe_offset).limit(safe_limit).all()


@app.post("/api/network/teams", response_model=TeamResponse)
async def create_team(
    payload: TeamCreate,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    if not _is_sysadmin(current_user):
        raise HTTPException(status_code=403, detail="SysAdmin privileges required")
    team = Team(
        id=uuid.uuid4(),
        name=payload.name.strip(),
        slug=_ensure_unique_team_slug(session, payload.name.strip()),
        description=(payload.description or "").strip() or None,
        status="active",
        created_by_user_id=_actor_user_id(current_user),
    )
    session.add(team)
    session.commit()
    session.refresh(team)
    return team


@app.post("/api/network/teams/{team_id}/members", response_model=TeamMembershipResponse)
async def upsert_team_membership(
    team_id: uuid.UUID,
    payload: TeamMembershipUpsert,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    if not _is_sysadmin(current_user):
        raise HTTPException(status_code=403, detail="SysAdmin privileges required")
    team = session.query(Team).filter(Team.id == team_id).first()
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    user_id = payload.user_id.strip()
    membership = (
        session.query(TeamMembership)
        .filter(
            TeamMembership.team_id == team.id,
            TeamMembership.user_id == user_id,
        )
        .first()
    )
    if not membership:
        membership = TeamMembership(
            id=uuid.uuid4(),
            team_id=team.id,
            user_id=user_id,
        )
        session.add(membership)
    membership.user_email = payload.user_email
    membership.user_name = payload.user_name
    membership.role = payload.role
    membership.active = payload.active
    session.commit()
    session.refresh(membership)
    return membership


@app.get("/api/network/orgs/public", response_model=List[PublicOrganizationListItemResponse])
async def list_public_organizations(
    session: Session = Depends(get_db),
    q: str = "",
    limit: int = 250,
    offset: int = 0,
    sort: str = Query("popular", pattern="^(popular|name|newest)$"),
):
    safe_limit = max(1, min(limit, 1000))
    safe_offset = max(0, min(offset, 100000))
    now_utc = datetime.now(timezone.utc)

    membership_counts = (
        session.query(
            OrganizationMembership.organization_id.label("organization_id"),
            func.count(OrganizationMembership.user_id).label("membership_count"),
        )
        .group_by(OrganizationMembership.organization_id)
        .subquery()
    )
    upcoming_event_counts = (
        session.query(
            NetworkEvent.host_org_id.label("organization_id"),
            func.count(NetworkEvent.id).label("upcoming_events_count"),
        )
        .filter(
            NetworkEvent.host_org_id.isnot(None),
            (
                (NetworkEvent.ends_at.isnot(None) & (NetworkEvent.ends_at >= now_utc))
                | (NetworkEvent.ends_at.is_(None) & NetworkEvent.starts_at.isnot(None) & (NetworkEvent.starts_at >= now_utc))
            ),
        )
        .group_by(NetworkEvent.host_org_id)
        .subquery()
    )
    pending_claim_counts = (
        session.query(
            OrganizationClaimRequest.organization_id.label("organization_id"),
            func.count(OrganizationClaimRequest.id).label("pending_claim_requests_count"),
        )
        .filter(OrganizationClaimRequest.status == "pending")
        .group_by(OrganizationClaimRequest.organization_id)
        .subquery()
    )

    membership_count_col = func.coalesce(membership_counts.c.membership_count, 0)
    upcoming_events_count_col = func.coalesce(upcoming_event_counts.c.upcoming_events_count, 0)
    pending_claim_requests_count_col = func.coalesce(pending_claim_counts.c.pending_claim_requests_count, 0)
    query = (
        session.query(Organization, membership_count_col, upcoming_events_count_col, pending_claim_requests_count_col)
        .outerjoin(membership_counts, membership_counts.c.organization_id == Organization.id)
        .outerjoin(upcoming_event_counts, upcoming_event_counts.c.organization_id == Organization.id)
        .outerjoin(pending_claim_counts, pending_claim_counts.c.organization_id == Organization.id)
    )
    if q.strip():
        needle = f"%{q.strip()}%"
        query = query.filter((Organization.name.ilike(needle)) | (Organization.slug.ilike(needle)))

    if sort == "name":
        query = query.order_by(Organization.name.asc())
    elif sort == "newest":
        query = query.order_by(Organization.created_at.desc(), Organization.name.asc())
    else:
        query = query.order_by(
            membership_count_col.desc(),
            upcoming_events_count_col.desc(),
            Organization.name.asc(),
        )

    rows = query.offset(safe_offset).limit(safe_limit).all()
    return [
        _map_public_org_list_item(
            org=org,
            membership_count=membership_count,
            upcoming_events_count=upcoming_events_count,
            pending_claim_requests_count=pending_claim_requests_count,
        )
        for org, membership_count, upcoming_events_count, pending_claim_requests_count in rows
    ]


@app.get("/api/network/orgs/public/{slug}", response_model=PublicOrganizationResponse)
async def get_public_organization(
    slug: str,
    session: Session = Depends(get_db),
):
    normalized = slug.strip().lower()
    org = session.query(Organization).filter(Organization.slug == normalized).first()
    if not org:
        merged_redirect = (
            session.query(NetworkAuditEvent)
            .filter(
                NetworkAuditEvent.event_type == "org.merged",
                NetworkAuditEvent.metadata_json["source_slug"].astext == normalized,
            )
            .order_by(NetworkAuditEvent.created_at.desc())
            .first()
        )
        target_slug = None
        if merged_redirect and isinstance(merged_redirect.metadata_json, dict):
            target_slug = str(merged_redirect.metadata_json.get("target_slug") or "").strip().lower() or None
        if target_slug:
            redirected_org = session.query(Organization).filter(Organization.slug == target_slug).first()
            if redirected_org:
                return _map_public_org(redirected_org, session, redirected_from_slug=normalized)
        raise HTTPException(status_code=404, detail="Organization not found")
    return _map_public_org(org, session)


@app.get("/api/network/orgs/public/{slug}/admins", response_model=List[PublicOrganizationAdminResponse])
async def list_public_organization_admins(
    slug: str,
    session: Session = Depends(get_db),
):
    normalized = slug.strip().lower()
    org = session.query(Organization).filter(Organization.slug == normalized).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    admins: list[PublicOrganizationAdminResponse] = []
    seen_user_ids: set[str] = set()
    for member in org.memberships or []:
        if member.role != "admin":
            continue
        seen_user_ids.add(member.user_id)
        admins.append(
            PublicOrganizationAdminResponse(
                user_id=member.user_id,
                user_name=member.user_name,
                user_email=member.user_email,
                role="admin",
            )
        )

    if org.claimed_by_user_id and org.claimed_by_user_id not in seen_user_ids:
        admins.append(
            PublicOrganizationAdminResponse(
                user_id=org.claimed_by_user_id,
                user_name=None,
                user_email=None,
                role="owner",
            )
        )

    return admins


@app.post("/api/network/orgs/public/{slug}/claim", response_model=OrganizationResponse)
async def claim_public_organization(
    slug: str,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    user_id = _actor_user_id(current_user)
    _throttle_action(f"network:claim-org:{user_id}", limit=20, window_seconds=3600)

    normalized = slug.strip().lower()
    org = session.query(Organization).filter(Organization.slug == normalized).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    _claim_org_record(session, org, current_user)
    session.commit()
    session.refresh(org)
    return _map_org(org, user_id)


@app.get("/api/network/orgs/public/{slug}/events", response_model=List[NetworkEventResponse])
async def list_public_organization_events(
    slug: str,
    session: Session = Depends(get_db),
    upcoming_only: bool = True,
    limit: int = 60,
    offset: int = 0,
):
    normalized = slug.strip().lower()
    org = session.query(Organization).filter(Organization.slug == normalized).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    safe_limit = max(1, min(limit, 200))
    safe_offset = max(0, min(offset, 100000))
    now_utc = datetime.now(timezone.utc)
    query = (
        session.query(NetworkEvent)
        .filter(NetworkEvent.host_org_id == org.id)
        .order_by(NetworkEvent.starts_at.asc().nullslast(), NetworkEvent.created_at.desc())
    )
    if upcoming_only:
        query = query.filter(
            (NetworkEvent.ends_at.isnot(None) & (NetworkEvent.ends_at >= now_utc))
            | (NetworkEvent.ends_at.is_(None) & NetworkEvent.starts_at.isnot(None) & (NetworkEvent.starts_at >= now_utc))
        )

    events = query.offset(safe_offset).limit(safe_limit).all()
    return [_map_network_event(event, None, session) for event in events]


@app.post("/api/network/orgs", response_model=OrganizationResponse)
async def create_organization(
    payload: OrganizationCreate,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    user_id = _actor_user_id(current_user)
    if not user_id:
        raise HTTPException(status_code=401, detail="Authentication required")
    _throttle_action(f"network:create-org:{user_id}", limit=10, window_seconds=3600)

    source_url = _validate_public_url(payload.source_url, "source_url")
    if source_url:
        existing = _find_org_by_source_url(session, source_url)
        if existing:
            raise HTTPException(status_code=409, detail="Organization for this source URL already exists")

    slug = _ensure_unique_org_slug(session, payload.name)
    org = Organization(
        id=uuid.uuid4(),
        name=payload.name.strip(),
        slug=slug,
        description=payload.description,
        source_url=source_url,
        source_urls=[source_url] if source_url else [],
        image_url=_validate_public_url(payload.image_url, "image_url"),
        tags=payload.tags or [],
        seeded_from_events=False,
        claimed_by_user_id=user_id if payload.claim_on_create else None,
        created_by_user_id=user_id,
    )
    session.add(org)
    if payload.claim_on_create:
        membership = OrganizationMembership(
            id=uuid.uuid4(),
            organization=org,
            user_id=user_id,
            user_email=current_user.get("email"),
            user_name=current_user.get("name"),
            role="admin",
        )
        session.add(membership)
    _audit_event(
        session,
        actor=current_user,
        event_type="org.created",
        target_type="organization",
        target_id=str(org.id),
        metadata={"slug": org.slug, "name": org.name, "claim_on_create": payload.claim_on_create},
    )
    session.commit()
    session.refresh(org)
    return _map_org(org, user_id)


@app.post("/api/network/orgs/{organization_id}/claim", response_model=OrganizationResponse)
async def claim_organization(
    organization_id: uuid.UUID,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    user_id = _actor_user_id(current_user)
    _throttle_action(f"network:claim-org:{user_id}", limit=20, window_seconds=3600)
    org = session.query(Organization).filter(Organization.id == organization_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    _claim_org_record(session, org, current_user)
    session.commit()
    session.refresh(org)
    return _map_org(org, user_id)


@app.patch("/api/network/orgs/{organization_id}", response_model=OrganizationResponse)
async def update_organization(
    organization_id: uuid.UUID,
    payload: OrganizationUpdate,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    user_id = _actor_user_id(current_user)
    _throttle_action(f"network:update-org:{user_id}", limit=80, window_seconds=3600)
    org = session.query(Organization).filter(Organization.id == organization_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    if not _is_org_admin(org, current_user):
        raise HTTPException(status_code=403, detail="Organization admin access required")

    changed_fields: List[str] = []

    if payload.name is not None:
        next_name = payload.name.strip()
        if not next_name:
            raise HTTPException(status_code=422, detail="Organization name cannot be empty")
        if next_name != org.name:
            org.name = next_name
            changed_fields.append("name")
    if payload.description is not None:
        next_description = payload.description.strip() or None
        if next_description != org.description:
            org.description = next_description
            changed_fields.append("description")
    if payload.image_url is not None:
        next_image_url = _validate_public_url(payload.image_url.strip() or None, "image_url")
        if next_image_url != org.image_url:
            org.image_url = next_image_url
            changed_fields.append("image_url")
    if payload.tags is not None:
        next_tags = sorted(set((payload.tags or [])))
        if next_tags != (org.tags or []):
            org.tags = next_tags
            changed_fields.append("tags")

    if not changed_fields:
        return _map_org(org, user_id)

    org.updated_at = datetime.now(timezone.utc)
    _audit_event(
        session,
        actor=current_user,
        event_type="org.updated",
        target_type="organization",
        target_id=str(org.id),
        metadata={"changed_fields": changed_fields},
    )
    session.commit()
    session.refresh(org)
    return _map_org(org, user_id)


@app.post("/api/network/orgs/{organization_id}/unclaim", response_model=OrganizationResponse)
async def unclaim_organization(
    organization_id: uuid.UUID,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    user_id = _actor_user_id(current_user)
    org = session.query(Organization).filter(Organization.id == organization_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    if org.claimed_by_user_id is None:
        return _map_org(org, user_id)
    if not _is_sysadmin(current_user) and org.claimed_by_user_id != user_id:
        raise HTTPException(status_code=403, detail="Only the claiming user or an admin can unclaim this organization")

    previous_owner = org.claimed_by_user_id
    org.claimed_by_user_id = None
    _audit_event(
        session,
        actor=current_user,
        event_type="org.unclaimed",
        target_type="organization",
        target_id=str(org.id),
        metadata={"previous_owner": previous_owner},
    )
    session.commit()
    session.refresh(org)
    return _map_org(org, user_id)


@app.post("/api/network/orgs/{organization_id}/merge", response_model=OrganizationResponse)
async def merge_organization(
    organization_id: uuid.UUID,
    payload: OrganizationMergeRequest,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    user_id = _actor_user_id(current_user)
    _throttle_action(f"network:merge-org:{user_id}", limit=40, window_seconds=3600)

    target_org = session.query(Organization).filter(Organization.id == organization_id).first()
    if not target_org:
        raise HTTPException(status_code=404, detail="Target organization not found")
    source_org = session.query(Organization).filter(Organization.id == payload.source_organization_id).first()
    if not source_org:
        raise HTTPException(status_code=404, detail="Source organization not found")
    if target_org.id == source_org.id:
        raise HTTPException(status_code=422, detail="Source and target organizations must be different")

    if not _is_org_admin(target_org, current_user):
        raise HTTPException(status_code=403, detail="Target organization admin access required")
    if not _can_manage_org_for_merge(source_org, current_user):
        raise HTTPException(status_code=403, detail="Source organization is claimed by another admin")

    # Merge source URLs and keep canonical target source_url stable.
    merged_source_urls = _org_source_urls(target_org)
    for url in _org_source_urls(source_org):
        if url not in merged_source_urls:
            merged_source_urls.append(url)
    _set_org_source_urls(target_org, merged_source_urls)

    # Merge descriptive fields without clobbering richer manual data.
    if not target_org.description and source_org.description:
        target_org.description = source_org.description
    if not target_org.image_url and source_org.image_url:
        target_org.image_url = source_org.image_url
    target_org.tags = sorted(set((target_org.tags or []) + (source_org.tags or [])))
    target_org.seeded_from_events = bool(target_org.seeded_from_events or source_org.seeded_from_events)
    if not target_org.created_by_user_id and source_org.created_by_user_id:
        target_org.created_by_user_id = source_org.created_by_user_id
    if not target_org.claimed_by_user_id and source_org.claimed_by_user_id:
        target_org.claimed_by_user_id = source_org.claimed_by_user_id

    # Move hosted events.
    source_events = session.query(NetworkEvent).filter(NetworkEvent.host_org_id == source_org.id).all()
    for event in source_events:
        event.host_org = target_org
        event.host_type = EventHostType.ORG.value
        event.host_user_id = None
        event.updated_at = datetime.now(timezone.utc)

    # Merge memberships, upgrading role to admin if either side is admin.
    target_members = {
        member.user_id: member
        for member in session.query(OrganizationMembership).filter(OrganizationMembership.organization_id == target_org.id).all()
    }
    source_members = session.query(OrganizationMembership).filter(OrganizationMembership.organization_id == source_org.id).all()
    for source_member in source_members:
        existing_member = target_members.get(source_member.user_id)
        if existing_member:
            if source_member.role == "admin":
                existing_member.role = "admin"
            if not existing_member.user_email and source_member.user_email:
                existing_member.user_email = source_member.user_email
            if not existing_member.user_name and source_member.user_name:
                existing_member.user_name = source_member.user_name
            existing_member.updated_at = datetime.now(timezone.utc)
            session.delete(source_member)
            continue

        source_member.organization_id = target_org.id
        source_member.updated_at = datetime.now(timezone.utc)
        target_members[source_member.user_id] = source_member

    previous_target_claimed_by = target_org.claimed_by_user_id
    _audit_event(
        session,
        actor=current_user,
        event_type="org.merged",
        target_type="organization",
        target_id=str(target_org.id),
        metadata={
            "source_organization_id": str(source_org.id),
            "source_slug": source_org.slug,
            "target_slug": target_org.slug,
            "events_reassigned": len(source_events),
            "target_claimed_by_before": previous_target_claimed_by,
            "target_claimed_by_after": target_org.claimed_by_user_id,
            "source_urls": _org_source_urls(source_org),
            "merged_source_urls": _org_source_urls(target_org),
        },
    )

    # Flush before delete so relationship rebinding is persisted and no hosted events
    # remain attached to source_org in this transaction.
    session.flush()
    remaining_source_events = (
        session.query(NetworkEvent.id)
        .filter(NetworkEvent.host_org_id == source_org.id)
        .limit(1)
        .all()
    )
    if remaining_source_events:
        raise HTTPException(
            status_code=409,
            detail="Organization merge blocked: source organization still has bound hosted events.",
        )

    session.delete(source_org)
    target_org.updated_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(target_org)
    return _map_org(target_org, user_id)


@app.get("/api/network/events", response_model=List[NetworkEventResponse])
async def list_network_events(
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
    q: str = "",
    mine: bool = False,
    only_unclaimed: bool = False,
    host_type: Optional[str] = None,
    limit: int = 250,
    offset: int = 0,
):
    _require_authenticated_user(current_user)
    safe_limit = max(1, min(limit, 1000))
    safe_offset = max(0, min(offset, 100000))
    query = session.query(NetworkEvent).order_by(NetworkEvent.starts_at.desc().nullslast(), NetworkEvent.created_at.desc())
    if q.strip():
        needle = f"%{q.strip()}%"
        query = query.filter(
            (NetworkEvent.title.ilike(needle))
            | (NetworkEvent.slug.ilike(needle))
            | (NetworkEvent.location.ilike(needle))
        )
    if only_unclaimed:
        query = query.filter(NetworkEvent.claimed_by_user_id.is_(None))
    if host_type and host_type.strip():
        normalized_host_type = host_type.strip().lower()
        if normalized_host_type not in {
            EventHostType.UNCLAIMED.value,
            EventHostType.INDIVIDUAL.value,
            EventHostType.ORG.value,
        }:
            raise HTTPException(status_code=422, detail="Invalid host_type filter")
        query = query.filter(NetworkEvent.host_type == normalized_host_type)

    events = query.offset(safe_offset).limit(safe_limit).all()
    user_id = _actor_user_id(current_user)
    if mine and user_id:
        filtered: list[NetworkEvent] = []
        for event in events:
            if event.claimed_by_user_id == user_id:
                filtered.append(event)
                continue
            if event.host_type == EventHostType.INDIVIDUAL.value and event.host_user_id == user_id:
                filtered.append(event)
                continue
            if event.host_type == EventHostType.ORG.value and event.host_org_id:
                host_org = session.query(Organization).filter(Organization.id == event.host_org_id).first()
                if host_org and _is_org_admin(host_org, current_user):
                    filtered.append(event)
        events = filtered

    return [_map_network_event(event, current_user, session) for event in events]


@app.get("/api/network/events/public", response_model=List[NetworkEventResponse])
async def list_public_network_events(
    session: Session = Depends(get_db),
    q: str = "",
    upcoming_only: bool = True,
    limit: int = 60,
    offset: int = 0,
):
    safe_limit = max(1, min(limit, 200))
    safe_offset = max(0, min(offset, 100000))
    now_utc = datetime.now(timezone.utc)

    query = session.query(NetworkEvent)
    if q.strip():
        needle = f"%{q.strip()}%"
        query = query.filter(
            (NetworkEvent.title.ilike(needle))
            | (NetworkEvent.slug.ilike(needle))
            | (NetworkEvent.location.ilike(needle))
            | (NetworkEvent.description.ilike(needle))
        )

    if upcoming_only:
        query = query.filter(
            (NetworkEvent.ends_at.isnot(None) & (NetworkEvent.ends_at >= now_utc))
            | (NetworkEvent.ends_at.is_(None) & NetworkEvent.starts_at.isnot(None) & (NetworkEvent.starts_at >= now_utc))
        )
    query = query.order_by(NetworkEvent.starts_at.asc().nullslast(), NetworkEvent.created_at.desc())
    events = query.offset(safe_offset).limit(safe_limit).all()
    return [_map_network_event(event, None, session) for event in events]


@app.get("/api/network/events/public.json", response_model=NetworkEventPublicFeedResponse)
async def list_public_network_events_json(
    session: Session = Depends(get_db),
    q: str = "",
    upcoming_only: bool = False,
):
    now_utc = datetime.now(timezone.utc)
    query = session.query(NetworkEvent)

    if q.strip():
        needle = f"%{q.strip()}%"
        query = query.filter(
            (NetworkEvent.title.ilike(needle))
            | (NetworkEvent.slug.ilike(needle))
            | (NetworkEvent.location.ilike(needle))
            | (NetworkEvent.description.ilike(needle))
        )

    if upcoming_only:
        query = query.filter(
            (NetworkEvent.ends_at.isnot(None) & (NetworkEvent.ends_at >= now_utc))
            | (NetworkEvent.ends_at.is_(None) & NetworkEvent.starts_at.isnot(None) & (NetworkEvent.starts_at >= now_utc))
        )

    events = query.order_by(NetworkEvent.starts_at.asc().nullslast(), NetworkEvent.created_at.desc()).all()
    mapped_events = [_map_network_event(event, None, session) for event in events]
    return NetworkEventPublicFeedResponse(
        generated_at=datetime.now(timezone.utc),
        total=len(mapped_events),
        events=mapped_events,
    )


@app.get("/api/network/events/public/{slug}", response_model=NetworkEventResponse)
async def get_public_network_event_by_slug(
    slug: str,
    session: Session = Depends(get_db),
):
    event = session.query(NetworkEvent).filter(NetworkEvent.slug == slug.strip().lower()).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    return _map_network_event(event, None, session)


@app.post("/api/network/events", response_model=NetworkEventResponse)
async def create_network_event(
    payload: NetworkEventCreate,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    user_id = _actor_user_id(current_user)
    if not user_id:
        raise HTTPException(status_code=401, detail="Authentication required")
    _throttle_action(f"network:create-event:{user_id}", limit=25, window_seconds=3600)

    if payload.ends_at and payload.starts_at and payload.ends_at < payload.starts_at:
        raise HTTPException(status_code=422, detail="ends_at must be greater than or equal to starts_at")

    source_url = _validate_public_url(payload.source_url, "source_url")
    if source_url:
        existing = session.query(NetworkEvent).filter(NetworkEvent.source_url == source_url).first()
        if existing:
            raise HTTPException(status_code=409, detail="Event for this source URL already exists")

    resolved_host_type, resolved_host_user_id, resolved_host_org_id = _resolve_event_host_binding(
        host_type=payload.host_type,
        host_user_id=payload.host_user_id,
        host_org_id=payload.host_org_id,
        current_user=current_user,
        session=session,
    )
    slug = _ensure_unique_event_slug(session, payload.title)
    event = NetworkEvent(
        id=uuid.uuid4(),
        title=payload.title.strip(),
        slug=slug,
        description=payload.description,
        starts_at=payload.starts_at,
        ends_at=payload.ends_at,
        location=payload.location.strip() if payload.location else None,
        source_url=source_url,
        image_url=_validate_public_url(payload.image_url, "image_url"),
        tags=payload.tags or [],
        host_type=resolved_host_type,
        host_user_id=resolved_host_user_id,
        host_org_id=resolved_host_org_id,
        claimed_by_user_id=user_id if payload.claim_on_create else None,
        created_by_user_id=user_id,
        seeded_from_events=False,
    )
    session.add(event)
    _audit_event(
        session,
        actor=current_user,
        event_type="event.created",
        target_type="network_event",
        target_id=str(event.id),
        metadata={
            "slug": event.slug,
            "title": event.title,
            "host_type": event.host_type,
            "claim_on_create": payload.claim_on_create,
        },
    )
    session.commit()
    session.refresh(event)
    return _map_network_event(event, current_user, session)


@app.post("/api/network/events/{event_id}/claim", response_model=NetworkEventResponse)
async def claim_network_event(
    event_id: uuid.UUID,
    payload: NetworkEventClaimRequest,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    user_id = _actor_user_id(current_user)
    _throttle_action(f"network:claim-event:{user_id}", limit=40, window_seconds=3600)
    event = session.query(NetworkEvent).filter(NetworkEvent.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    if event.claimed_by_user_id and event.claimed_by_user_id != user_id:
        raise HTTPException(status_code=409, detail="Event is already claimed")

    resolved_host_type, resolved_host_user_id, resolved_host_org_id = _resolve_event_host_binding(
        host_type=payload.host_type,
        host_user_id=payload.host_user_id,
        host_org_id=payload.host_org_id,
        current_user=current_user,
        session=session,
    )

    event.host_type = resolved_host_type
    event.host_user_id = resolved_host_user_id
    event.host_org_id = resolved_host_org_id
    event.claimed_by_user_id = user_id
    if not event.created_by_user_id:
        event.created_by_user_id = user_id
    event.updated_at = datetime.now(timezone.utc)
    _audit_event(
        session,
        actor=current_user,
        event_type="event.claimed",
        target_type="network_event",
        target_id=str(event.id),
        metadata={
            "claimed_by": user_id,
            "host_type": event.host_type,
            "host_user_id": event.host_user_id,
            "host_org_id": str(event.host_org_id) if event.host_org_id else None,
        },
    )
    session.commit()
    session.refresh(event)
    return _map_network_event(event, current_user, session)


@app.post("/api/network/events/{event_id}/unclaim", response_model=NetworkEventResponse)
async def unclaim_network_event(
    event_id: uuid.UUID,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    user_id = _actor_user_id(current_user)
    event = session.query(NetworkEvent).filter(NetworkEvent.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    if event.claimed_by_user_id is None:
        return _map_network_event(event, current_user, session)
    if not _is_sysadmin(current_user) and event.claimed_by_user_id != user_id:
        raise HTTPException(status_code=403, detail="Only the claiming user or an admin can unclaim this event")

    previous_owner = event.claimed_by_user_id
    event.claimed_by_user_id = None
    event.updated_at = datetime.now(timezone.utc)
    _audit_event(
        session,
        actor=current_user,
        event_type="event.unclaimed",
        target_type="network_event",
        target_id=str(event.id),
        metadata={"previous_owner": previous_owner},
    )
    session.commit()
    session.refresh(event)
    return _map_network_event(event, current_user, session)


@app.post("/api/network/events/{event_id}/attendance", response_model=EventAttendanceRecordResponse)
async def record_event_attendance(
    event_id: uuid.UUID,
    user_id: Optional[str] = None,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    event = session.query(NetworkEvent).filter(NetworkEvent.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    actor_user_id = _actor_user_id(current_user)
    target_user_id = (user_id or "").strip() or actor_user_id
    if target_user_id != actor_user_id and not _is_sysadmin(current_user):
        raise HTTPException(status_code=403, detail="Only SysAdmin can record attendance for another user")

    attendance = (
        session.query(EventAttendance)
        .filter(
            EventAttendance.event_id == event_id,
            EventAttendance.user_id == target_user_id,
        )
        .first()
    )
    if not attendance:
        attendance = EventAttendance(
            id=uuid.uuid4(),
            event_id=event_id,
            user_id=target_user_id,
        )
        session.add(attendance)

    if target_user_id == actor_user_id:
        attendance.user_email = current_user.get("email")
        attendance.user_name = current_user.get("name")
    attendance.attended_at = datetime.now(timezone.utc)
    attendance.source = "sysadmin_override" if target_user_id != actor_user_id else "self_checkin"
    attendance.verified_by_user_id = actor_user_id if target_user_id != actor_user_id else None
    session.commit()
    session.refresh(attendance)
    return attendance


@app.post("/api/network/scans", response_model=BusinessCardSubmissionResponse)
@app.post("/api/network/business-cards", response_model=BusinessCardSubmissionResponse)
async def submit_business_card(
    request: Request,
    background_tasks: BackgroundTasks,
    image: UploadFile = File(...),
    notes: Optional[str] = Form(None),
    scan_kind: Optional[str] = Form("auto"),
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    submission_id = uuid.uuid4()
    actor_user_id = _actor_user_id(current_user)

    def _scan_audit(event_type: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        _audit_event(
            session,
            actor=current_user,
            event_type=event_type,
            target_type="business_card_submission",
            target_id=str(submission_id),
            metadata=metadata or {},
        )

    ai_audit_steps: list[dict[str, Any]] = []

    def _scan_ai_audit_hook(event_type: str, metadata: Dict[str, Any]) -> None:
        payload = metadata or {}
        ai_audit_steps.append({"event_type": event_type, "metadata": payload})
        _scan_audit(event_type, payload)

    _scan_audit("scan.received", {"scan_kind_requested": _normalize_scan_kind(scan_kind)})
    runtime_settings = await get_business_card_runtime_settings()
    if not runtime_settings.get("enabled", True):
        raise HTTPException(status_code=503, detail="Business card submissions are temporarily disabled")

    client_ip = _request_client_ip(request)
    _throttle_action(
        f"network:business-card-submit:user:{actor_user_id}",
        limit=int(runtime_settings["per_user_limit_per_hour"]),
        window_seconds=3600,
    )
    _throttle_action(
        f"network:business-card-submit:ip:{client_ip}",
        limit=int(runtime_settings["per_ip_limit_per_hour"]),
        window_seconds=3600,
    )
    _throttle_action(
        "network:business-card-submit:global",
        limit=int(runtime_settings["global_limit_per_hour"]),
        window_seconds=3600,
    )

    content_type = (image.content_type or "").strip().lower()
    allowed_content_types = {
        item.strip().lower()
        for item in runtime_settings.get("allowed_content_types") or []
        if item and item.strip()
    } or ORG_BUSINESS_CARD_DEFAULT_ALLOWED_CONTENT_TYPES
    if content_type not in allowed_content_types:
        allowed = ", ".join(sorted(allowed_content_types))
        raise HTTPException(status_code=415, detail=f"Unsupported image content type '{content_type}'. Allowed: {allowed}")

    image_bytes = await image.read()
    if not image_bytes:
        raise HTTPException(status_code=422, detail="Image file is empty")
    max_bytes = int(runtime_settings.get("max_bytes") or ORG_BUSINESS_CARD_DEFAULT_MAX_BYTES)
    if len(image_bytes) > max_bytes:
        raise HTTPException(status_code=413, detail=f"Image exceeds {max_bytes} bytes")

    image_sha256 = hashlib.sha256(image_bytes).hexdigest()
    if not _is_sysadmin(current_user):
        _enforce_business_card_duplicate_hash_guard(
            session,
            image_sha256=image_sha256,
            duplicate_hash_limit=int(runtime_settings["duplicate_hash_limit"]),
            duplicate_hash_window_seconds=int(runtime_settings["duplicate_hash_window_seconds"]),
        )
    image_storage_path = ""
    image_storage_backend = ""
    image_storage_bucket = None
    image_storage_error = None
    try:
        image_storage_backend, image_storage_bucket, image_storage_path = _persist_business_card_image(
            submission_id=submission_id,
            image_bytes=image_bytes,
            content_type=content_type,
        )
    except Exception as exc:
        image_storage_error = str(exc)
        logger.error("Business card image storage failed: %s", exc)
        raise HTTPException(status_code=503, detail="Unable to store business card image")
    _scan_audit(
        "scan.image.persisted",
        {
            "storage_backend": image_storage_backend,
            "storage_bucket": image_storage_bucket,
            "content_type": content_type,
            "image_size_bytes": len(image_bytes),
        },
    )

    ocr_text, ocr_provider = await _extract_business_card_text(
        image_bytes=image_bytes,
        content_type=content_type,
        audit_hook=_scan_ai_audit_hook,
    )
    extracted_person = _extract_business_card_fields(ocr_text)
    extracted_org = _extract_organization_fields_from_text(ocr_text)
    extracted_events = _extract_events_from_text(ocr_text)
    extracted_event = extracted_events[0] if extracted_events else _extract_event_fields_from_text(ocr_text)
    person_derived_org = _derive_org_payload_for_person_scan(extracted_person, extracted_org)
    normalized_scan_kind = _normalize_scan_kind(scan_kind)
    classification_scores = _score_scan_kind_candidates(
        ocr_text=ocr_text,
        extracted_person=extracted_person,
        extracted_org=extracted_org,
        extracted_event=extracted_event,
    )
    ranked_scores = sorted(classification_scores.items(), key=lambda item: item[1], reverse=True)
    detected_scan_kind = _detect_scan_kind(
        ocr_text=ocr_text,
        extracted_person=extracted_person,
        extracted_org=extracted_org,
        extracted_event=extracted_event,
    )
    effective_scan_kind = detected_scan_kind if normalized_scan_kind == "auto" else normalized_scan_kind
    top_confidence = float(ranked_scores[0][1]) if ranked_scores else 0.0
    second_confidence = float(ranked_scores[1][1]) if len(ranked_scores) > 1 else 0.0
    confidence_margin = max(0.0, top_confidence - second_confidence)
    auto_clarification_enabled = bool(
        runtime_settings.get("auto_clarification_enabled", ORG_SCAN_AUTO_CLARIFICATION_ENABLED)
    )
    auto_min_confidence = max(
        0.0,
        min(1.0, float(runtime_settings.get("auto_min_confidence", ORG_SCAN_AUTO_MIN_CONFIDENCE))),
    )
    auto_min_margin = max(
        0.0,
        min(1.0, float(runtime_settings.get("auto_min_margin", ORG_SCAN_AUTO_MIN_MARGIN))),
    )
    clarification_required = False
    clarification_message: Optional[str] = None
    processing_status = "processed"
    if (
        normalized_scan_kind == "auto"
        and auto_clarification_enabled
        and (top_confidence < auto_min_confidence or confidence_margin < auto_min_margin)
    ):
        clarification_required = True
        processing_status = "clarification_required"
        clarification_message = (
            f"Scan classification confidence ({top_confidence:.2f}) was below policy. "
            "Please resubmit and select the correct scan type."
        )
        _scan_audit(
            "scan.classification.clarification_required",
            {
                "detected_scan_kind": detected_scan_kind,
                "classification_scores": classification_scores,
                "top_confidence": top_confidence,
                "confidence_margin": confidence_margin,
                "auto_min_confidence": auto_min_confidence,
                "auto_min_margin": auto_min_margin,
            },
        )
    event_link_enrichment_enabled = bool(runtime_settings.get("event_link_enrichment_enabled", ORG_SCAN_EVENT_LINK_ENRICHMENT_ENABLED))

    event_link_enrichment: Dict[str, Any] = {}
    if not clarification_required and effective_scan_kind == "event" and event_link_enrichment_enabled:
        event_link_enrichment = await _enrich_event_scan_from_links(
            ocr_text=ocr_text,
            seed_url=extracted_event.get("website"),
        )
        candidate = event_link_enrichment.get("event_candidate") or {}
        if candidate:
            if not extracted_event.get("title") and candidate.get("title"):
                extracted_event["title"] = candidate.get("title")
            if not extracted_event.get("description") and candidate.get("description"):
                extracted_event["description"] = candidate.get("description")
            if not extracted_event.get("starts_at") and candidate.get("starts_at"):
                extracted_event["starts_at"] = candidate.get("starts_at")
            if not extracted_event.get("location") and candidate.get("location"):
                extracted_event["location"] = candidate.get("location")
            if not extracted_event.get("website") and candidate.get("source_url"):
                extracted_event["website"] = candidate.get("source_url")
        discovered_links = event_link_enrichment.get("discovered_links") or []
        if discovered_links and not extracted_event.get("website"):
            extracted_event["website"] = discovered_links[0]
        if extracted_events:
            extracted_events[0] = extracted_event
        _scan_audit(
            "scan.event_link_enrichment.completed",
            {
                "visited_urls": event_link_enrichment.get("visited_urls") or [],
                "discovered_links_count": len(discovered_links),
                "event_candidate_found": bool(candidate),
            },
        )

    extracted = extracted_person
    if effective_scan_kind == "organization":
        extracted = {
            "name": extracted_org.get("name"),
            "title": None,
            "company": extracted_org.get("name"),
            "email": None,
            "phone": None,
            "website": extracted_org.get("website"),
            "address": None,
            "raw_lines": extracted_org.get("raw_lines") or [],
        }
    elif effective_scan_kind == "event":
        starts_at = extracted_event.get("starts_at")
        starts_at_iso = starts_at.isoformat() if isinstance(starts_at, datetime) else None
        extracted = {
            "name": extracted_event.get("title"),
            "title": None,
            "company": None,
            "email": None,
            "phone": None,
            "website": extracted_event.get("website"),
            "address": extracted_event.get("location"),
            "raw_lines": extracted_event.get("raw_lines") or [],
            "starts_at": starts_at_iso,
        }

    extracted_email = str(extracted_person.get("email") or "").strip().lower()
    pidp_user_result: dict[str, Any] = {"created": False, "pidp_user_id": None}
    created_targets: List[Dict[str, Optional[str]]] = []
    created_target_type: Optional[str] = None
    created_target_id: Optional[str] = None
    created_target_slug: Optional[str] = None
    created_target_name: Optional[str] = None

    def _append_created_target(
        target_type: str,
        *,
        target_id: Optional[str] = None,
        target_slug: Optional[str] = None,
        target_name: Optional[str] = None,
        target_image_url: Optional[str] = None,
    ) -> None:
        normalized_type = str(target_type or "").strip().lower()
        normalized_id = str(target_id or "").strip() or None
        normalized_slug = str(target_slug or "").strip() or None
        normalized_name = str(target_name or "").strip() or None
        if not normalized_type:
            return
        dedupe_key = (normalized_type, normalized_id or normalized_slug or normalized_name or "")
        for existing in created_targets:
            existing_key = (
                str(existing.get("type") or "").strip().lower(),
                str(existing.get("id") or existing.get("slug") or existing.get("name") or "").strip(),
            )
            if existing_key == dedupe_key:
                return
        target_url: Optional[str] = None
        if normalized_type == "organization" and normalized_slug:
            target_url = f"/orgs/{normalized_slug}"
        elif normalized_type == "event" and normalized_slug:
            target_url = f"/events/{normalized_slug}"
        created_targets.append(
            {
                "type": normalized_type,
                "id": normalized_id,
                "slug": normalized_slug,
                "name": normalized_name,
                "url": target_url,
                "image_url": str(target_image_url or "").strip() or None,
            }
        )

    normalized_org_name = str(extracted_org.get("name") or "").strip()
    normalized_person_derived_org_name = str((person_derived_org or {}).get("name") or "").strip()
    normalized_event_title = str(extracted_event.get("title") or "").strip()
    has_event_shape = bool(
        extracted_events
        or normalized_event_title
        or extracted_event.get("starts_at")
        or str(extracted_event.get("website") or "").strip()
    )
    should_create_person = False
    should_create_org = False
    should_create_event = False
    if not clarification_required:
        if normalized_scan_kind == "auto":
            should_create_person = bool(extracted_email)
            should_create_org = bool(normalized_org_name or (should_create_person and normalized_person_derived_org_name))
            should_create_event = has_event_shape
        elif normalized_scan_kind == "person":
            should_create_person = True
            should_create_org = bool(normalized_person_derived_org_name)
        elif normalized_scan_kind == "organization":
            should_create_org = True
        elif normalized_scan_kind == "event":
            should_create_event = True

    if not clarification_required and normalized_scan_kind == "auto" and not (
        should_create_person or should_create_org or should_create_event
    ):
        clarification_required = True
        processing_status = "clarification_required"
        clarification_message = (
            "No person, organization, or event fields were confidently detected. "
            "Please retry with a clearer image or choose scan type manually."
        )
        _scan_audit(
            "scan.classification.no_entities_detected",
            {
                "detected_scan_kind": detected_scan_kind,
                "classification_scores": classification_scores,
            },
        )

    created_org: Optional[Organization] = None
    if not clarification_required and should_create_org:
        org_payload = extracted_org
        if normalized_scan_kind == "person" and person_derived_org:
            org_payload = person_derived_org
        elif normalized_scan_kind == "auto" and should_create_person and person_derived_org:
            # In auto mode, prefer person-derived company for business-card patterns.
            org_payload = person_derived_org
        created_org = _create_or_find_organization_from_scan(
            session,
            actor_user_id=actor_user_id,
            extracted_org=org_payload,
            default_image_url=None,
        )
        created_org_image_url = _scan_created_target_image_url(
            submission_id,
            target_type="organization",
            target_id=str(created_org.id),
        )
        if created_org_image_url and (created_org.image_url or "").strip() != created_org_image_url:
            if not (created_org.image_url or "").strip():
                created_org.image_url = created_org_image_url
        _append_created_target(
            "organization",
            target_id=str(created_org.id),
            target_slug=created_org.slug,
            target_name=created_org.name,
            target_image_url=created_org.image_url,
        )

    if not clarification_required and should_create_event:
        event_candidates = extracted_events or [extracted_event]
        for event_candidate in event_candidates:
            event = _create_event_from_scan(
                session,
                actor_user_id=actor_user_id,
                extracted_event=event_candidate,
                default_image_url=None,
            )
            created_event_image_url = _scan_created_target_image_url(
                submission_id,
                target_type="event",
                target_id=str(event.id),
            )
            if created_event_image_url and not (event.image_url or "").strip():
                event.image_url = created_event_image_url
            if created_org and not event.host_org_id:
                event.host_type = EventHostType.ORG.value
                event.host_org_id = created_org.id
                event.host_user_id = None
            _append_created_target(
                "event",
                target_id=str(event.id),
                target_slug=event.slug,
                target_name=event.title,
                target_image_url=event.image_url,
            )

    if not clarification_required and should_create_person:
        if not extracted_email and normalized_scan_kind == "person":
            raise HTTPException(status_code=422, detail="Unable to detect email address from scanned person card")
        if extracted_email:
            pidp_user_result = await _create_or_find_pidp_user_from_business_card(
                email=extracted_email,
                full_name=extracted_person.get("name"),
            )
            _append_created_target(
                "person",
                target_id=str(pidp_user_result.get("pidp_user_id") or "").strip() or None,
                target_name=str(extracted_person.get("name") or extracted_email or "").strip() or None,
            )

    if not clarification_required and created_targets:
        created_targets = await _summarize_scan_targets_with_openai(
            ocr_text=ocr_text,
            created_targets=created_targets,
            audit_hook=_scan_ai_audit_hook,
        )

    if created_targets:
        primary = created_targets[0]
        created_target_type = str(primary.get("type") or "").strip() or None
        created_target_id = str(primary.get("id") or "").strip() or None
        created_target_slug = str(primary.get("slug") or "").strip() or None
        created_target_name = str(primary.get("name") or "").strip() or None

    submission = BusinessCardSubmission(
        id=submission_id,
        submitted_by_user_id=actor_user_id,
        submitted_by_email=current_user.get("email"),
        submitted_by_name=current_user.get("name"),
        image_filename=image.filename,
        image_content_type=content_type,
        image_size_bytes=len(image_bytes),
        image_sha256=image_sha256,
        image_storage_backend=image_storage_backend or None,
        image_storage_bucket=image_storage_bucket,
        image_storage_path=image_storage_path or None,
        image_storage_error=image_storage_error,
        ocr_provider=ocr_provider,
        ocr_text=ocr_text,
        extracted_name=extracted.get("name"),
        extracted_title=extracted.get("title"),
        extracted_company=extracted.get("company"),
        extracted_email=extracted_email or None,
        extracted_phone=extracted.get("phone"),
        extracted_website=extracted.get("website"),
        extracted_address=extracted.get("address"),
        extracted_metadata={
            "raw_lines": extracted.get("raw_lines") or [],
            "links": extracted_event.get("links") if isinstance(extracted_event.get("links"), list) else [],
            "events_detected": [
                {
                    "title": str(item.get("title") or "").strip() or None,
                    "starts_at": item.get("starts_at").isoformat() if isinstance(item.get("starts_at"), datetime) else None,
                    "location": str(item.get("location") or "").strip() or None,
                    "website": str(item.get("website") or "").strip() or None,
                }
                for item in extracted_events
            ],
            "scan_kind_requested": normalized_scan_kind,
            "scan_kind_detected": detected_scan_kind,
            "scan_kind": effective_scan_kind,
            "processing_status": processing_status,
            "clarification_required": clarification_required,
            "clarification_message": clarification_message,
            "classification_scores": classification_scores,
            "classification_top_confidence": top_confidence,
            "classification_confidence_margin": confidence_margin,
            "auto_min_confidence": auto_min_confidence,
            "auto_min_margin": auto_min_margin,
            "created_target_type": created_target_type,
            "created_target_id": created_target_id,
            "created_target_slug": created_target_slug,
            "created_target_name": created_target_name,
            "created_targets": created_targets,
            "event_starts_at": extracted.get("starts_at"),
            "event_link_enrichment_enabled": event_link_enrichment_enabled,
            "auto_clarification_enabled": auto_clarification_enabled,
            "event_link_enrichment": event_link_enrichment,
            "ai_audit_steps": ai_audit_steps,
        },
        notes=(notes or "").strip() or None,
        pidp_user_created=bool(pidp_user_result.get("created")),
        pidp_user_id=pidp_user_result.get("pidp_user_id"),
        notification_email_sent=False,
    )
    session.add(submission)
    _audit_event(
        session,
        actor=current_user,
        event_type="business_card.submitted",
        target_type="business_card_submission",
        target_id=str(submission.id),
        metadata={
            "scan_kind_requested": normalized_scan_kind,
            "scan_kind_detected": detected_scan_kind,
            "scan_kind": effective_scan_kind,
            "processing_status": processing_status,
            "clarification_required": clarification_required,
            "classification_top_confidence": top_confidence,
            "classification_confidence_margin": confidence_margin,
            "created_target_type": created_target_type,
            "created_target_id": created_target_id,
            "created_targets_count": len(created_targets),
            "extracted_email": submission.extracted_email,
            "pidp_user_created": submission.pidp_user_created,
            "ocr_provider": submission.ocr_provider,
            "event_link_enrichment_enabled": event_link_enrichment_enabled,
            "ai_call_count": len(ai_audit_steps),
        },
    )
    session.commit()
    session.refresh(submission)

    if not clarification_required and effective_scan_kind == "person" and extracted_email:
        background_tasks.add_task(
            _send_business_card_email_task,
            submission.id,
            extracted_email,
            submission.extracted_name,
            submission.submitted_by_name,
        )
    return submission


@app.get("/api/network/scans", response_model=List[BusinessCardSubmissionResponse])
@app.get("/api/network/business-cards", response_model=List[BusinessCardSubmissionResponse])
async def list_my_business_card_submissions(
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
    limit: int = 100,
    offset: int = 0,
):
    _require_authenticated_user(current_user)
    actor_user_id = _actor_user_id(current_user)
    if not actor_user_id:
        return []
    safe_limit = max(1, min(limit, 1000))
    safe_offset = max(0, min(offset, 100000))
    rows = (
        session.query(BusinessCardSubmission)
        .filter(BusinessCardSubmission.submitted_by_user_id == actor_user_id)
        .order_by(BusinessCardSubmission.created_at.desc(), BusinessCardSubmission.id.desc())
        .offset(safe_offset)
        .limit(safe_limit)
        .all()
    )
    return rows


@app.get("/api/network/scans/{submission_id}/image")
@app.get("/api/network/business-cards/{submission_id}/image")
async def get_my_business_card_submission_image(
    submission_id: uuid.UUID,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    actor_user_id = _actor_user_id(current_user)
    submission = (
        session.query(BusinessCardSubmission)
        .filter(BusinessCardSubmission.id == submission_id)
        .first()
    )
    if not submission:
        raise HTTPException(status_code=404, detail="Business card submission not found")
    if submission.submitted_by_user_id != actor_user_id and not _is_sysadmin(current_user):
        raise HTTPException(status_code=403, detail="Not authorized to view this scan image")
    if not submission.image_storage_path:
        raise HTTPException(status_code=404, detail="Stored image not available")

    download_name = (submission.image_filename or f"{submission.id}").strip() or f"{submission.id}"
    image_bytes = _load_business_card_image_bytes(
        storage_backend=(submission.image_storage_backend or "local"),
        storage_bucket=submission.image_storage_bucket,
        storage_path=submission.image_storage_path,
    )
    return Response(
        content=image_bytes,
        media_type=submission.image_content_type or "application/octet-stream",
        headers={
            "Content-Disposition": f'inline; filename="{download_name}"',
            "Cache-Control": "private, max-age=0, no-cache, no-store",
        },
    )


@app.get("/api/network/scans/{submission_id}/image/public/{target_type}/{target_id}")
async def get_public_scan_image_for_created_target(
    submission_id: uuid.UUID,
    target_type: str,
    target_id: str,
    session: Session = Depends(get_db),
):
    normalized_type = str(target_type or "").strip().lower()
    normalized_target_id = str(target_id or "").strip()
    if normalized_type not in {"organization", "event"} or not normalized_target_id:
        raise HTTPException(status_code=404, detail="Scan image not found")

    submission = (
        session.query(BusinessCardSubmission)
        .filter(BusinessCardSubmission.id == submission_id)
        .first()
    )
    if not submission or not submission.image_storage_path:
        raise HTTPException(status_code=404, detail="Scan image not found")

    allowed = False
    for target in submission.created_targets:
        if not isinstance(target, dict):
            continue
        if str(target.get("type") or "").strip().lower() != normalized_type:
            continue
        if str(target.get("id") or "").strip() != normalized_target_id:
            continue
        allowed = True
        break
    if not allowed:
        raise HTTPException(status_code=404, detail="Scan image not found")

    image_bytes = _load_business_card_image_bytes(
        storage_backend=(submission.image_storage_backend or "local"),
        storage_bucket=submission.image_storage_bucket,
        storage_path=submission.image_storage_path,
    )
    return Response(
        content=image_bytes,
        media_type=submission.image_content_type or "application/octet-stream",
        headers={
            "Cache-Control": "public, max-age=3600",
        },
    )


@app.post("/api/network/chat/bootstrap", response_model=MatrixBootstrapSessionResponse)
async def bootstrap_chat_session(
    current_user: dict = Depends(get_current_user),
):
    _require_authenticated_user(current_user)
    return await _bootstrap_matrix_session_for_current_user(current_user)


@app.get("/api/network/chat/rooms", response_model=List[OrgChatRoomDirectoryItemResponse])
async def list_org_chat_rooms_for_current_user(
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    user_id = _actor_user_id(current_user)
    if not user_id:
        return []

    status_by_org_id: dict[uuid.UUID, str] = {}

    attendee_rows = (
        session.query(NetworkEvent.host_org_id)
        .join(EventAttendance, EventAttendance.event_id == NetworkEvent.id)
        .filter(
            EventAttendance.user_id == user_id,
            NetworkEvent.host_org_id.isnot(None),
        )
        .distinct()
        .all()
    )
    for (org_id,) in attendee_rows:
        if not org_id:
            continue
        status_by_org_id[org_id] = "attendee"

    member_rows = (
        session.query(OrganizationMembership.organization_id, OrganizationMembership.role)
        .filter(OrganizationMembership.user_id == user_id)
        .all()
    )
    for org_id, role in member_rows:
        if not org_id:
            continue
        normalized_role = str(role or "").strip().lower()
        existing = status_by_org_id.get(org_id)
        if normalized_role == "admin":
            status_by_org_id[org_id] = "admin"
        elif existing != "admin":
            status_by_org_id[org_id] = "member"

    claimed_rows = (
        session.query(Organization.id)
        .filter(Organization.claimed_by_user_id == user_id)
        .all()
    )
    for (org_id,) in claimed_rows:
        if not org_id:
            continue
        status_by_org_id[org_id] = "admin"

    if not status_by_org_id:
        return []

    org_ids = list(status_by_org_id.keys())
    organizations = (
        session.query(Organization.id, Organization.name, Organization.slug)
        .filter(Organization.id.in_(org_ids))
        .all()
    )

    timeout = httpx.Timeout(connect=8.0, read=8.0, write=8.0, pool=8.0)
    response_items: list[OrgChatRoomDirectoryItemResponse] = []
    async with httpx.AsyncClient(timeout=timeout) as client:
        for org in organizations:
            relationship_status = status_by_org_id.get(org.id, "attendee")
            room_id, room_alias = await _resolve_org_public_chat_room(client, org.slug)
            room_name: Optional[str] = org.name
            if not room_id:
                room_id, discovered_alias, discovered_name = await _matrix_find_public_room_for_org(
                    client=client,
                    org_name=org.name,
                    org_slug=org.slug,
                )
                if discovered_alias:
                    room_alias = discovered_alias
                if discovered_name:
                    room_name = discovered_name
            if (
                not room_id
                and ORG_MATRIX_AUTO_PROVISION_PUBLIC_ORG_ROOMS
                and relationship_status in {"member", "admin"}
            ):
                try:
                    provisioned_room_id, provisioned_alias, provisioned_name = await _matrix_create_public_org_room(
                        client=client,
                        org_name=org.name,
                        org_slug=org.slug,
                    )
                    if provisioned_room_id:
                        room_id = provisioned_room_id
                        room_alias = provisioned_alias
                        if provisioned_name:
                            room_name = provisioned_name
                except Exception as exc:
                    logger.warning("Matrix room auto-provision skipped for org_slug=%s error=%s", org.slug, exc)
            if not room_id:
                continue
            response_items.append(
                OrgChatRoomDirectoryItemResponse(
                    organization_id=org.id,
                    organization_name=org.name,
                    organization_slug=org.slug,
                    relationship_status=relationship_status,
                    room_id=room_id,
                    room_alias=room_alias,
                    room_name=room_name,
                )
            )

    status_priority = {"admin": 0, "member": 1, "attendee": 2}
    response_items.sort(
        key=lambda item: (
            status_priority.get(item.relationship_status, 99),
            item.organization_name.lower(),
        )
    )
    return response_items


@app.get("/api/network/chat/link-preview", response_model=ChatLinkPreviewResponse)
async def get_chat_link_preview(
    request: Request,
    url: str = Query(..., min_length=5, max_length=2048),
    current_user: dict = Depends(get_current_user),
):
    _require_authenticated_user(current_user)
    user_id = _actor_user_id(current_user) or "unknown"
    _throttle_action(f"network:chat-link-preview:user:{user_id}", limit=120, window_seconds=3600)
    _throttle_action(f"network:chat-link-preview:ip:{_request_client_ip(request)}", limit=300, window_seconds=3600)
    return await _fetch_chat_link_preview(url)


@app.get("/api/network/orgs/{organization_id}/members", response_model=List[OrganizationMembershipResponse])
async def list_org_members(
    organization_id: uuid.UUID,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    org = session.query(Organization).filter(Organization.id == organization_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    members = (
        session.query(OrganizationMembership)
        .filter(OrganizationMembership.organization_id == organization_id)
        .order_by(OrganizationMembership.role.desc(), OrganizationMembership.user_name.asc())
        .all()
    )
    return members


@app.post("/api/network/orgs/{organization_id}/members", response_model=OrganizationMembershipResponse)
async def upsert_org_member(
    organization_id: uuid.UUID,
    payload: OrganizationMembershipUpsert,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    _throttle_action(f"network:upsert-member:{_actor_user_id(current_user)}", limit=120, window_seconds=3600)
    org = session.query(Organization).filter(Organization.id == organization_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    if not _is_org_admin(org, current_user):
        raise HTTPException(status_code=403, detail="Organization admin access required")

    member = (
        session.query(OrganizationMembership)
        .filter(
            OrganizationMembership.organization_id == organization_id,
            OrganizationMembership.user_id == payload.user_id.strip(),
        )
        .first()
    )
    if not member:
        member = OrganizationMembership(
            id=uuid.uuid4(),
            organization_id=organization_id,
            user_id=payload.user_id.strip(),
        )
        session.add(member)

    member.user_email = payload.user_email
    member.user_name = payload.user_name
    member.role = payload.role
    member.updated_at = datetime.now(timezone.utc)
    _audit_event(
        session,
        actor=current_user,
        event_type="org.member.upserted",
        target_type="organization_membership",
        target_id=f"{organization_id}:{member.user_id}",
        metadata={"organization_id": str(organization_id), "role": member.role},
    )
    session.commit()
    session.refresh(member)
    return member


@app.patch("/api/network/orgs/{organization_id}/members/{user_id}", response_model=OrganizationMembershipResponse)
async def update_org_member(
    organization_id: uuid.UUID,
    user_id: str,
    payload: OrganizationMembershipUpdate,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    _throttle_action(f"network:update-member:{_actor_user_id(current_user)}", limit=120, window_seconds=3600)
    org = session.query(Organization).filter(Organization.id == organization_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    if not _is_org_admin(org, current_user):
        raise HTTPException(status_code=403, detail="Organization admin access required")

    member = (
        session.query(OrganizationMembership)
        .filter(
            OrganizationMembership.organization_id == organization_id,
            OrganizationMembership.user_id == user_id,
        )
        .first()
    )
    if not member:
        raise HTTPException(status_code=404, detail="Member not found")
    member.role = payload.role
    member.updated_at = datetime.now(timezone.utc)
    _audit_event(
        session,
        actor=current_user,
        event_type="org.member.role_updated",
        target_type="organization_membership",
        target_id=f"{organization_id}:{member.user_id}",
        metadata={"organization_id": str(organization_id), "role": member.role},
    )
    session.commit()
    session.refresh(member)
    return member


@app.delete("/api/network/orgs/{organization_id}/members/{user_id}")
async def delete_org_member(
    organization_id: uuid.UUID,
    user_id: str,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    org = session.query(Organization).filter(Organization.id == organization_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    if not _is_org_admin(org, current_user):
        raise HTTPException(status_code=403, detail="Organization admin access required")

    member = (
        session.query(OrganizationMembership)
        .filter(
            OrganizationMembership.organization_id == organization_id,
            OrganizationMembership.user_id == user_id,
        )
        .first()
    )
    if not member:
        raise HTTPException(status_code=404, detail="Member not found")

    session.delete(member)
    _audit_event(
        session,
        actor=current_user,
        event_type="org.member.removed",
        target_type="organization_membership",
        target_id=f"{organization_id}:{user_id}",
        metadata={"organization_id": str(organization_id)},
    )
    session.commit()
    return {"ok": True}


@app.post("/api/network/orgs/{organization_id}/claim-requests", response_model=OrganizationClaimRequestResponse)
async def create_claim_request(
    organization_id: uuid.UUID,
    payload: OrganizationClaimRequestCreate,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    user_id = _actor_user_id(current_user)
    _throttle_action(f"network:create-claim-request:{user_id}", limit=20, window_seconds=3600)
    org = session.query(Organization).filter(Organization.id == organization_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    if not org.claimed_by_user_id:
        raise HTTPException(status_code=400, detail="Organization is unclaimed. Use /claim endpoint.")
    if org.claimed_by_user_id == user_id:
        raise HTTPException(status_code=400, detail="You already own this organization.")

    existing_pending = (
        session.query(OrganizationClaimRequest)
        .filter(
            OrganizationClaimRequest.organization_id == organization_id,
            OrganizationClaimRequest.requested_by_user_id == user_id,
            OrganizationClaimRequest.status == "pending",
        )
        .first()
    )
    if existing_pending:
        return existing_pending

    claim = OrganizationClaimRequest(
        id=uuid.uuid4(),
        organization_id=organization_id,
        requested_by_user_id=user_id,
        requested_by_email=current_user.get("email"),
        requested_by_name=current_user.get("name"),
        message=payload.message,
        status="pending",
    )
    session.add(claim)
    _audit_event(
        session,
        actor=current_user,
        event_type="org.claim_request.created",
        target_type="organization_claim_request",
        target_id=str(claim.id),
        metadata={"organization_id": str(organization_id)},
    )
    session.commit()
    session.refresh(claim)
    return claim


@app.get("/api/network/orgs/{organization_id}/claim-requests", response_model=List[OrganizationClaimRequestResponse])
async def list_claim_requests(
    organization_id: uuid.UUID,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
    status_filter: Optional[str] = Query(None, alias="status"),
):
    _require_authenticated_user(current_user)
    org = session.query(Organization).filter(Organization.id == organization_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    if not _is_org_admin(org, current_user):
        raise HTTPException(status_code=403, detail="Organization admin access required")

    query = session.query(OrganizationClaimRequest).filter(OrganizationClaimRequest.organization_id == organization_id)
    if status_filter:
        query = query.filter(OrganizationClaimRequest.status == status_filter)
    return query.order_by(OrganizationClaimRequest.created_at.desc()).all()


@app.get("/api/network/claim-requests", response_model=List[OrganizationClaimRequestQueueItemResponse])
async def list_claim_requests_queue(
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
    status_filter: str = Query("pending", alias="status"),
    limit: int = 200,
):
    _require_authenticated_user(current_user)
    if not _is_sysadmin(current_user):
        raise HTTPException(status_code=403, detail="SysAdmin privileges required")

    normalized_status = (status_filter or "pending").strip().lower()
    if normalized_status not in {"pending", "approved", "rejected", "all"}:
        raise HTTPException(status_code=422, detail="status must be one of: pending, approved, rejected, all")
    safe_limit = max(1, min(limit, 1000))

    query = (
        session.query(OrganizationClaimRequest, Organization)
        .join(Organization, Organization.id == OrganizationClaimRequest.organization_id)
    )
    if normalized_status != "all":
        query = query.filter(OrganizationClaimRequest.status == normalized_status)

    rows = (
        query.order_by(OrganizationClaimRequest.created_at.desc(), OrganizationClaimRequest.id.desc())
        .limit(safe_limit)
        .all()
    )
    return [
        OrganizationClaimRequestQueueItemResponse(
            id=claim.id,
            organization_id=org.id,
            organization_name=org.name,
            organization_slug=org.slug,
            organization_claimed_by_user_id=org.claimed_by_user_id,
            requested_by_user_id=claim.requested_by_user_id,
            requested_by_email=claim.requested_by_email,
            requested_by_name=claim.requested_by_name,
            message=claim.message,
            status=claim.status,
            reviewed_by_user_id=claim.reviewed_by_user_id,
            reviewed_at=claim.reviewed_at,
            created_at=claim.created_at,
        )
        for claim, org in rows
    ]


@app.post("/api/network/claim-requests/{claim_request_id}/approve", response_model=OrganizationResponse)
async def approve_claim_request(
    claim_request_id: uuid.UUID,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    claim = session.query(OrganizationClaimRequest).filter(OrganizationClaimRequest.id == claim_request_id).first()
    if not claim:
        raise HTTPException(status_code=404, detail="Claim request not found")
    org = session.query(Organization).filter(Organization.id == claim.organization_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    if not _is_org_admin(org, current_user):
        raise HTTPException(status_code=403, detail="Organization admin access required")
    if claim.status != "pending":
        raise HTTPException(status_code=400, detail="Claim request is no longer pending")

    org.claimed_by_user_id = claim.requested_by_user_id
    membership = (
        session.query(OrganizationMembership)
        .filter(
            OrganizationMembership.organization_id == org.id,
            OrganizationMembership.user_id == claim.requested_by_user_id,
        )
        .first()
    )
    if not membership:
        session.add(
            OrganizationMembership(
                id=uuid.uuid4(),
                organization_id=org.id,
                user_id=claim.requested_by_user_id,
                user_email=claim.requested_by_email,
                user_name=claim.requested_by_name,
                role="admin",
            )
        )
    else:
        membership.role = "admin"
        membership.user_email = claim.requested_by_email
        membership.user_name = claim.requested_by_name
        membership.updated_at = datetime.now(timezone.utc)

    claim.status = "approved"
    claim.reviewed_by_user_id = _actor_user_id(current_user)
    claim.reviewed_at = datetime.now(timezone.utc)
    claim.updated_at = datetime.now(timezone.utc)
    _audit_event(
        session,
        actor=current_user,
        event_type="org.claim_request.approved",
        target_type="organization_claim_request",
        target_id=str(claim.id),
        metadata={"organization_id": str(org.id), "new_owner": claim.requested_by_user_id},
    )
    session.commit()
    session.refresh(org)
    return _map_org(org, _actor_user_id(current_user))


@app.post("/api/network/claim-requests/{claim_request_id}/reject", response_model=OrganizationClaimRequestResponse)
async def reject_claim_request(
    claim_request_id: uuid.UUID,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    claim = session.query(OrganizationClaimRequest).filter(OrganizationClaimRequest.id == claim_request_id).first()
    if not claim:
        raise HTTPException(status_code=404, detail="Claim request not found")
    org = session.query(Organization).filter(Organization.id == claim.organization_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    if not _is_org_admin(org, current_user):
        raise HTTPException(status_code=403, detail="Organization admin access required")
    if claim.status != "pending":
        raise HTTPException(status_code=400, detail="Claim request is no longer pending")

    claim.status = "rejected"
    claim.reviewed_by_user_id = _actor_user_id(current_user)
    claim.reviewed_at = datetime.now(timezone.utc)
    claim.updated_at = datetime.now(timezone.utc)
    _audit_event(
        session,
        actor=current_user,
        event_type="org.claim_request.rejected",
        target_type="organization_claim_request",
        target_id=str(claim.id),
        metadata={"organization_id": str(org.id)},
    )
    session.commit()
    session.refresh(claim)
    return claim


@app.get("/api/network/audit-events")
async def list_network_audit_events(
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
    limit: int = 200,
    event_type_prefix: Optional[str] = None,
    target_type: Optional[str] = None,
):
    _require_authenticated_user(current_user)
    if not _is_sysadmin(current_user):
        raise HTTPException(status_code=403, detail="SysAdmin privileges required")
    safe_limit = max(1, min(limit, 2000))
    query = session.query(NetworkAuditEvent)
    if event_type_prefix:
        query = query.filter(NetworkAuditEvent.event_type.ilike(f"{event_type_prefix.strip()}%"))
    if target_type:
        query = query.filter(NetworkAuditEvent.target_type == target_type.strip())
    rows = query.order_by(NetworkAuditEvent.created_at.desc()).limit(safe_limit).all()
    return [
        {
            "id": str(row.id),
            "actor_user_id": row.actor_user_id,
            "actor_email": row.actor_email,
            "event_type": row.event_type,
            "target_type": row.target_type,
            "target_id": row.target_id,
            "metadata": row.metadata_json or {},
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in rows
    ]


def _default_contact_slug(current_user: dict) -> str:
    name = str(current_user.get("name") or "").strip()
    email = str(current_user.get("email") or "").strip()
    if name:
        return _slugify(name)
    if email and "@" in email:
        return _slugify(email.split("@", 1)[0])
    return _slugify(_actor_user_id(current_user) or "contact")


def _map_contact(contact: UserContactPage, request: Optional[Request]) -> ContactPageResponse:
    public_url = None
    if request:
        public_url = f"{str(request.base_url).rstrip('/')}/users/{contact.slug}"
    links = []
    for raw in (contact.links or []):
        if isinstance(raw, dict) and raw.get("label") and raw.get("url"):
            links.append(ContactLink(label=str(raw["label"]), url=str(raw["url"])))
    return ContactPageResponse(
        user_id=contact.user_id,
        user_name=contact.user_name or "User",
        slug=contact.slug,
        enabled=bool(contact.enabled),
        headline=contact.headline,
        bio=contact.bio,
        photo_url=contact.photo_url,
        email_public=contact.email_public,
        phone_public=contact.phone_public,
        linkedin_url=contact.linkedin_url,
        github_url=contact.github_url,
        x_url=contact.x_url,
        website_url=contact.website_url,
        source_profile_url=contact.source_profile_url,
        source_profile_imported_at=contact.source_profile_imported_at,
        links=links,
        public_url=public_url,
        updated_at=contact.updated_at,
    )


def _map_public_user_profile(contact: UserContactPage, request: Optional[Request], session: Session) -> PublicUserProfileResponse:
    public_url = None
    if request:
        public_url = f"{str(request.base_url).rstrip('/')}/users/{contact.slug}"
    links: list[ContactLink] = []
    for raw in (contact.links or []):
        if isinstance(raw, dict) and raw.get("label") and raw.get("url"):
            links.append(ContactLink(label=str(raw["label"]), url=str(raw["url"])))

    now_utc = datetime.now(timezone.utc)
    upcoming_events_count = (
        session.query(NetworkEvent)
        .filter(
            NetworkEvent.host_type == EventHostType.INDIVIDUAL.value,
            NetworkEvent.host_user_id == contact.user_id,
            (
                (NetworkEvent.ends_at.isnot(None) & (NetworkEvent.ends_at >= now_utc))
                | (NetworkEvent.ends_at.is_(None) & NetworkEvent.starts_at.isnot(None) & (NetworkEvent.starts_at >= now_utc))
            ),
        )
        .count()
    )
    return PublicUserProfileResponse(
        user_id=contact.user_id,
        user_name=contact.user_name or "User",
        slug=contact.slug,
        headline=contact.headline,
        bio=contact.bio,
        photo_url=contact.photo_url,
        email_public=contact.email_public,
        phone_public=contact.phone_public,
        linkedin_url=contact.linkedin_url,
        github_url=contact.github_url,
        x_url=contact.x_url,
        website_url=contact.website_url,
        links=links,
        public_url=public_url,
        upcoming_events_count=upcoming_events_count,
        updated_at=contact.updated_at,
    )


def _map_public_user_list_item(contact: UserContactPage, upcoming_events_count: int) -> PublicUserListItemResponse:
    return PublicUserListItemResponse(
        user_id=contact.user_id,
        user_name=contact.user_name or "User",
        slug=contact.slug,
        headline=contact.headline,
        photo_url=contact.photo_url,
        upcoming_events_count=int(upcoming_events_count or 0),
        updated_at=contact.updated_at,
    )


@app.get("/api/network/contact/me", response_model=ContactPageResponse)
async def get_my_contact_page(
    request: Request,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    user_id = _actor_user_id(current_user)
    contact = session.query(UserContactPage).filter(UserContactPage.user_id == user_id).first()
    if not contact:
        slug = _ensure_unique_contact_slug(session, _default_contact_slug(current_user))
        contact = UserContactPage(
            id=uuid.uuid4(),
            user_id=user_id,
            user_email=current_user.get("email"),
            user_name=current_user.get("name"),
            slug=slug,
            enabled=False,
            links=[],
        )
        session.add(contact)
        session.commit()
        session.refresh(contact)
    return _map_contact(contact, request)


@app.put("/api/network/contact/me", response_model=ContactPageResponse)
async def update_my_contact_page(
    payload: ContactPageUpdate,
    request: Request,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    user_id = _actor_user_id(current_user)
    _throttle_action(f"network:contact-update:{user_id}", limit=120, window_seconds=3600)
    contact = session.query(UserContactPage).filter(UserContactPage.user_id == user_id).first()
    if not contact:
        slug = _ensure_unique_contact_slug(session, _default_contact_slug(current_user))
        contact = UserContactPage(
            id=uuid.uuid4(),
            user_id=user_id,
            user_email=current_user.get("email"),
            user_name=current_user.get("name"),
            slug=slug,
            enabled=False,
            links=[],
        )
        session.add(contact)

    if payload.slug is not None:
        contact.slug = _ensure_unique_contact_slug(session, payload.slug, excluding_user_id=user_id)
    if payload.enabled is not None:
        contact.enabled = payload.enabled
    if payload.headline is not None:
        contact.headline = payload.headline
    if payload.bio is not None:
        contact.bio = payload.bio
    if payload.photo_url is not None:
        contact.photo_url = _validate_public_url(payload.photo_url, "photo_url")
    if payload.email_public is not None:
        contact.email_public = payload.email_public
    if payload.phone_public is not None:
        contact.phone_public = payload.phone_public
    if payload.linkedin_url is not None:
        contact.linkedin_url = _validate_public_url(payload.linkedin_url, "linkedin_url")
    if payload.github_url is not None:
        contact.github_url = _validate_public_url(payload.github_url, "github_url")
    if payload.x_url is not None:
        contact.x_url = _validate_public_url(payload.x_url, "x_url")
    if payload.website_url is not None:
        contact.website_url = _validate_public_url(payload.website_url, "website_url")
    if payload.links is not None:
        normalized_links: list[dict[str, str]] = []
        for item in payload.links:
            normalized_links.append(
                {
                    "label": item.label.strip(),
                    "url": _validate_public_url(item.url, f"links[{item.label}]") or item.url,
                }
            )
        contact.links = normalized_links

    contact.user_email = current_user.get("email")
    contact.user_name = current_user.get("name")
    contact.updated_at = datetime.now(timezone.utc)
    _audit_event(
        session,
        actor=current_user,
        event_type="contact_page.updated",
        target_type="user_contact_page",
        target_id=user_id,
        metadata={"enabled": bool(contact.enabled), "slug": contact.slug},
    )
    session.commit()
    session.refresh(contact)
    return _map_contact(contact, request)


@app.post("/api/network/contact/me/import", response_model=ContactImportResponse)
async def import_my_contact_page(
    payload: ContactImportPayload,
    request: Request,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    user_id = _actor_user_id(current_user)
    _throttle_action(f"network:contact-import:{user_id}", limit=30, window_seconds=3600)

    safe_source_url = _ensure_public_fetch_url(payload.source_url, "source_url")
    imported = _fetch_public_profile_import(safe_source_url)

    contact = session.query(UserContactPage).filter(UserContactPage.user_id == user_id).first()
    if not contact:
        slug = _ensure_unique_contact_slug(session, _default_contact_slug(current_user))
        contact = UserContactPage(
            id=uuid.uuid4(),
            user_id=user_id,
            user_email=current_user.get("email"),
            user_name=current_user.get("name"),
            slug=slug,
            enabled=False,
            links=[],
        )
        session.add(contact)

    changed_fields = _apply_contact_import_to_record(contact, imported, payload.overwrite)
    contact.source_profile_url = safe_source_url
    contact.source_profile_imported_at = datetime.now(timezone.utc)
    contact.user_email = current_user.get("email")
    contact.user_name = current_user.get("name")
    contact.updated_at = datetime.now(timezone.utc)

    _audit_event(
        session,
        actor=current_user,
        event_type="contact_page.imported",
        target_type="user_contact_page",
        target_id=user_id,
        metadata={
            "source_url": safe_source_url,
            "overwrite": bool(payload.overwrite),
            "changed_fields": changed_fields,
        },
    )
    session.commit()
    session.refresh(contact)
    return ContactImportResponse(
        contact=_map_contact(contact, request),
        imported_fields=changed_fields,
        source_url=safe_source_url,
    )


@app.get("/api/network/contact/{slug}", response_model=ContactPageResponse)
async def get_public_contact_page(
    slug: str,
    request: Request,
    session: Session = Depends(get_db),
):
    contact = session.query(UserContactPage).filter(UserContactPage.slug == _slugify(slug)).first()
    if not contact or not contact.enabled:
        raise HTTPException(status_code=404, detail="Contact page not found")
    return _map_contact(contact, request)


@app.get("/api/network/users/public", response_model=List[PublicUserListItemResponse])
async def list_public_user_profiles(
    session: Session = Depends(get_db),
    q: str = "",
    limit: int = 120,
    offset: int = 0,
    sort: str = Query("popular", pattern="^(popular|name|recent)$"),
):
    safe_limit = max(1, min(limit, 500))
    safe_offset = max(0, min(offset, 100000))
    now_utc = datetime.now(timezone.utc)

    upcoming_event_counts = (
        session.query(
            NetworkEvent.host_user_id.label("user_id"),
            func.count(NetworkEvent.id).label("upcoming_events_count"),
        )
        .filter(
            NetworkEvent.host_type == EventHostType.INDIVIDUAL.value,
            NetworkEvent.host_user_id.isnot(None),
            (
                (NetworkEvent.ends_at.isnot(None) & (NetworkEvent.ends_at >= now_utc))
                | (NetworkEvent.ends_at.is_(None) & NetworkEvent.starts_at.isnot(None) & (NetworkEvent.starts_at >= now_utc))
            ),
        )
        .group_by(NetworkEvent.host_user_id)
        .subquery()
    )

    upcoming_events_count_col = func.coalesce(upcoming_event_counts.c.upcoming_events_count, 0)
    query = (
        session.query(UserContactPage, upcoming_events_count_col)
        .outerjoin(upcoming_event_counts, upcoming_event_counts.c.user_id == UserContactPage.user_id)
        .filter(UserContactPage.enabled.is_(True))
    )

    if q.strip():
        needle = f"%{q.strip()}%"
        query = query.filter(
            (UserContactPage.user_name.ilike(needle))
            | (UserContactPage.slug.ilike(needle))
            | (UserContactPage.headline.ilike(needle))
            | (UserContactPage.bio.ilike(needle))
        )

    if sort == "name":
        query = query.order_by(UserContactPage.user_name.asc(), UserContactPage.slug.asc())
    elif sort == "recent":
        query = query.order_by(UserContactPage.updated_at.desc(), UserContactPage.user_name.asc())
    else:
        query = query.order_by(
            upcoming_events_count_col.desc(),
            UserContactPage.updated_at.desc(),
            UserContactPage.user_name.asc(),
        )

    rows = query.offset(safe_offset).limit(safe_limit).all()
    return [
        _map_public_user_list_item(contact=contact, upcoming_events_count=upcoming_events_count)
        for contact, upcoming_events_count in rows
    ]


@app.get("/api/network/users/public/{slug}", response_model=PublicUserProfileResponse)
async def get_public_user_profile(
    slug: str,
    request: Request,
    session: Session = Depends(get_db),
):
    contact = session.query(UserContactPage).filter(UserContactPage.slug == _slugify(slug)).first()
    if not contact or not contact.enabled:
        raise HTTPException(status_code=404, detail="Public user profile not found")
    return _map_public_user_profile(contact, request, session)


@app.get("/api/network/users/public/{slug}/events", response_model=List[NetworkEventResponse])
async def list_public_user_events(
    slug: str,
    session: Session = Depends(get_db),
    upcoming_only: bool = True,
    limit: int = 60,
    offset: int = 0,
):
    contact = session.query(UserContactPage).filter(UserContactPage.slug == _slugify(slug)).first()
    if not contact or not contact.enabled:
        raise HTTPException(status_code=404, detail="Public user profile not found")

    safe_limit = max(1, min(limit, 200))
    safe_offset = max(0, min(offset, 100000))
    now_utc = datetime.now(timezone.utc)
    query = (
        session.query(NetworkEvent)
        .filter(
            NetworkEvent.host_type == EventHostType.INDIVIDUAL.value,
            NetworkEvent.host_user_id == contact.user_id,
        )
        .order_by(NetworkEvent.starts_at.asc().nullslast(), NetworkEvent.created_at.desc())
    )
    if upcoming_only:
        query = query.filter(
            (NetworkEvent.ends_at.isnot(None) & (NetworkEvent.ends_at >= now_utc))
            | (NetworkEvent.ends_at.is_(None) & NetworkEvent.starts_at.isnot(None) & (NetworkEvent.starts_at >= now_utc))
        )

    events = query.offset(safe_offset).limit(safe_limit).all()
    return [_map_network_event(event, None, session) for event in events]


@app.get("/api/network/users", response_model=List[NetworkUserListItemResponse])
async def list_network_users(
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
    q: str = "",
    limit: int = 300,
    offset: int = 0,
    sort: str = Query("recent", pattern="^(recent|name)$"),
):
    _require_authenticated_user(current_user)
    safe_limit = max(1, min(limit, 1000))
    safe_offset = max(0, min(offset, 100000))

    query = session.query(Account).filter(Account.entity_type == EntityType.INDIVIDUAL)
    if q.strip():
        needle = f"%{q.strip()}%"
        query = query.filter(
            (Account.name.ilike(needle))
            | (Account.email.ilike(needle))
        )

    if sort == "name":
        query = query.order_by(Account.name.asc(), Account.created_at.desc())
    else:
        query = query.order_by(Account.created_at.desc(), Account.name.asc())

    users = query.offset(safe_offset).limit(safe_limit).all()
    user_ids = [str(user.id) for user in users]
    contacts = (
        session.query(UserContactPage)
        .filter(UserContactPage.user_id.in_(user_ids))
        .all()
        if user_ids
        else []
    )
    contact_by_user_id = {str(contact.user_id): contact for contact in contacts}

    return [
        NetworkUserListItemResponse(
            user_id=str(user.id),
            user_name=user.name,
            email=user.email,
            created_at=user.created_at,
            contact_slug=(contact_by_user_id.get(str(user.id)).slug if contact_by_user_id.get(str(user.id)) else None),
            contact_enabled=bool(contact_by_user_id.get(str(user.id)).enabled) if contact_by_user_id.get(str(user.id)) else False,
            headline=contact_by_user_id.get(str(user.id)).headline if contact_by_user_id.get(str(user.id)) else None,
            photo_url=contact_by_user_id.get(str(user.id)).photo_url if contact_by_user_id.get(str(user.id)) else None,
        )
        for user in users
    ]

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

@app.get("/api/accounts/me", response_model=AccountResponse)
async def get_my_account(
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db)
):
    """Get current user's account"""
    if current_user.get("is_anonymous"):
        raise HTTPException(status_code=401, detail="Authentication required")
    
    account = session.query(Account).filter_by(email=current_user["email"]).first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    return account

@app.get("/api/accounts/me/automation", response_model=AccountAutomationResponse)
async def get_my_account_automation(
    request: Request,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    """Automation-friendly account discovery endpoint for receive/payment workflows."""
    if current_user.get("is_anonymous"):
        raise HTTPException(status_code=401, detail="Authentication required")

    account = session.query(Account).filter_by(email=current_user["email"]).first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    base = str(request.base_url).rstrip("/")
    return {
        "account_id": account.id,
        "name": account.name,
        "email": account.email,
        "balance": account.balance,
        "currency": SYSTEM_CURRENCY,
        "account_endpoint": f"{base}/api/accounts/me",
        "incoming_transactions_endpoint": f"{base}/api/accounts/me/transactions/incoming?limit=50",
        "all_transactions_endpoint": f"{base}/api/accounts/me/transactions?limit=50",
        "send_payment_endpoint": f"{base}/api/transactions",
        "send_url_template": f"{base}/send?to={account.id}&amount={{amount}}",
        "updated_at": account.updated_at,
    }

@app.get("/api/accounts/{account_id}", response_model=AccountResponse)
async def get_account(
    account_id: uuid.UUID,
    session: Session = Depends(get_db)
):
    """Get account by ID"""
    account = session.query(Account).filter_by(id=account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    return account

@app.get("/api/accounts", response_model=List[AccountListItemResponse])
async def list_accounts(
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
    q: str = "",
    sort: str = "balance_desc",
    limit: int = 500,
):
    """List accounts for directory/search views."""
    if current_user.get("is_anonymous"):
        raise HTTPException(status_code=401, detail="Authentication required")

    safe_limit = max(1, min(limit, 2000))
    query = session.query(Account)

    if q.strip():
        needle = f"%{q.strip()}%"
        query = query.filter(
            (Account.name.ilike(needle)) | (Account.email.ilike(needle))
        )

    if sort == "balance_asc":
        query = query.order_by(Account.balance.asc(), Account.name.asc())
    elif sort == "name_asc":
        query = query.order_by(Account.name.asc())
    elif sort == "name_desc":
        query = query.order_by(Account.name.desc())
    else:
        query = query.order_by(Account.balance.desc(), Account.name.asc())

    return query.limit(safe_limit).all()


@app.get("/api/authz/me", response_model=AccessClassSnapshotResponse)
async def get_access_class_snapshot(
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    """Resolve constitutional access classes for the current user."""
    if current_user.get("is_anonymous"):
        raise HTTPException(status_code=401, detail="Authentication required")
    return _resolve_access_classes(session, current_user)


@app.get("/admin/me")
async def get_admin_status(current_user: dict = Depends(get_current_user)):
    """Check if current user is a platform sysadmin."""
    if current_user.get("is_anonymous"):
        return {"is_sysadmin": False}
    is_sysadmin = _is_sysadmin(current_user)
    return {"is_sysadmin": is_sysadmin}

@app.get("/api/admin/accounts", response_model=List[AccountListItemResponse])
async def list_admin_accounts(
    current_user: dict = Depends(get_current_user),
    credentials: HTTPAuthorizationCredentials = Depends(security),
    session: Session = Depends(get_db),
):
    """List sysadmin accounts by resolving PIDP users and SpiceDB admin membership."""
    if current_user.get("is_anonymous"):
        raise HTTPException(status_code=401, detail="Authentication required")
    if not _is_sysadmin(current_user):
        raise HTTPException(status_code=403, detail="SysAdmin privileges required")
    if not credentials:
        raise HTTPException(status_code=401, detail="Authentication required")

    token = credentials.credentials
    candidate_emails: set[str] = set(ORG_SYSADMIN_EMAILS)
    current_email = str(current_user.get("email") or "").strip().lower()
    if current_email:
        candidate_emails.add(current_email)
    if not candidate_emails:
        return []

    pidp_users: list[dict[str, Any]] = []
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            for email in sorted(candidate_emails):
                resp = await client.get(
                    f"{PIDP_BASE_URL}/auth/users",
                    params={"email": email},
                    headers={"Authorization": f"Bearer {token}"},
                )
                if not resp.is_success:
                    raise HTTPException(status_code=resp.status_code, detail="Unable to resolve PIDP users")
                response_payload = resp.json()
                if isinstance(response_payload, list):
                    pidp_users.extend(response_payload)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Failed to load PIDP users for admin list: {exc}")
        raise HTTPException(status_code=503, detail="Unable to load admin list")

    admin_emails: set[str] = set()
    for pidp_user in pidp_users:
        pidp_id = str(pidp_user.get("id") or "").strip()
        email = str(pidp_user.get("email") or "").strip().lower()
        if not pidp_id or not email:
            continue
        is_sysadmin = pidp_id in ORG_SYSADMIN_USER_IDS or await _spicedb_check_sysadmin(pidp_id)
        if is_sysadmin:
            admin_emails.add(email)

    if not admin_emails:
        return []

    admins = (
        session.query(Account)
        .filter(func.lower(Account.email).in_(admin_emails))
        .order_by(Account.balance.desc(), Account.name.asc())
        .all()
    )
    return admins

@app.patch("/api/accounts/me", response_model=AccountResponse)
async def update_account(
    update_data: AccountUpdate,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db)
):
    """Update current user's account information"""
    if current_user.get("is_anonymous"):
        raise HTTPException(status_code=401, detail="Authentication required")
    
    account = session.query(Account).filter_by(email=current_user["email"]).first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    # Create edit request for verification (for sensitive fields)
    edit_request = EditRequest(
        id=uuid.uuid4(),
        account_id=account.id,
        field_name="account_update",
        old_value=json.dumps({
            "name": account.name,
            "address": account.address,
            "business_type": account.business_type,
            "mission_statement": account.mission_statement
        }),
        new_value=json.dumps(update_data.dict(exclude_unset=True)),
        status="pending",
        message="Account information update request"
    )
    session.add(edit_request)
    
    # Update immediately for non-sensitive fields
    update_dict = update_data.dict(exclude_unset=True)
    for field, value in update_dict.items():
        setattr(account, field, value)
    
    account.updated_at = datetime.now(timezone.utc)
    
    session.commit()
    session.refresh(account)
    
    return account

# ============= TRANSACTION ENDPOINTS =============

@app.post("/api/transactions", response_model=TransactionResponse)
async def create_transaction(
    transaction_data: TransactionCreate,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
    conn: asyncpg.Connection = Depends(get_async_db)
):
    """Create a new financial transaction"""
    if current_user.get("is_anonymous"):
        raise HTTPException(status_code=401, detail="Authentication required")
    
    # Get sender account
    sender = session.query(Account).filter_by(email=current_user["email"]).first()
    if not sender:
        raise HTTPException(status_code=404, detail="Sender account not found")
    
    # Check if recipient exists (if specified)
    recipient = None
    if transaction_data.to_account_id:
        recipient = session.query(Account).filter_by(id=transaction_data.to_account_id).first()
        if not recipient:
            raise HTTPException(status_code=404, detail="Recipient account not found")
    
    # Check balance for outgoing transactions
    if transaction_data.transaction_type not in [TransactionType.UBI_PAYMENT, TransactionType.GRANT]:
        if sender.balance < transaction_data.amount:
            raise HTTPException(status_code=400, detail="Insufficient funds")
    
    # Use database transaction with asyncpg for better concurrency
    transaction_id = uuid.uuid4()
    
    try:
        # Update balances atomically
        if transaction_data.transaction_type not in [TransactionType.UBI_PAYMENT, TransactionType.GRANT]:
            await conn.execute("""
                UPDATE accounts 
                SET balance = balance - $1, updated_at = NOW()
                WHERE id = $2 AND balance >= $1
            """, float(transaction_data.amount), sender.id)
        
        if recipient:
            await conn.execute("""
                UPDATE accounts 
                SET balance = balance + $1, updated_at = NOW()
                WHERE id = $2
            """, float(transaction_data.amount), recipient.id)
        
        # Create transaction record
        transaction = Transaction(
            id=transaction_id,
            from_account_id=sender.id,
            to_account_id=recipient.id if recipient else None,
            amount=transaction_data.amount,
            transaction_type=transaction_data.transaction_type,
            description=transaction_data.description,
            reference_id=transaction_data.reference_id,
            tx_metadata=transaction_data.metadata
        )
        
        session.add(transaction)
        session.commit()
        session.refresh(transaction)
        
        # Cache transaction in Redis for quick access
        cache_key = f"transaction:{transaction_id}"
        db.redis_client.setex(
            cache_key,
            300,  # 5 minutes
            json.dumps({
                "id": str(transaction.id),
                "from_account_id": str(transaction.from_account_id) if transaction.from_account_id else None,
                "to_account_id": str(transaction.to_account_id) if transaction.to_account_id else None,
                "amount": str(transaction.amount),
                "transaction_type": transaction.transaction_type.value,
                "description": transaction.description,
                "timestamp": transaction.timestamp.isoformat(),
                "metadata": transaction.tx_metadata,
            })
        )
        
        return transaction
        
    except asyncpg.exceptions.CheckViolationError:
        session.rollback()
        raise HTTPException(status_code=400, detail="Transaction failed: constraint violation")
    except Exception as e:
        session.rollback()
        logger.error(f"Transaction failed: {e}")
        raise HTTPException(status_code=500, detail="Transaction failed")

@app.get("/api/accounts/me/transactions", response_model=List[TransactionResponse])
async def get_my_transactions(
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
    skip: int = 0,
    limit: int = 50
):
    """Get current user's transaction history"""
    if current_user.get("is_anonymous"):
        raise HTTPException(status_code=401, detail="Authentication required")
    
    account = session.query(Account).filter_by(email=current_user["email"]).first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    transactions = session.query(Transaction).filter(
        (Transaction.from_account_id == account.id) | 
        (Transaction.to_account_id == account.id)
    ).order_by(Transaction.timestamp.desc()).offset(skip).limit(limit).all()
    
    return transactions

@app.get("/api/accounts/me/transactions/incoming", response_model=List[TransactionResponse])
async def get_my_incoming_transactions(
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
    since: Optional[datetime] = None,
    limit: int = 50,
):
    """Get incoming transactions only for current user, suitable for polling automation."""
    if current_user.get("is_anonymous"):
        raise HTTPException(status_code=401, detail="Authentication required")

    account = session.query(Account).filter_by(email=current_user["email"]).first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    safe_limit = max(1, min(limit, 500))
    query = session.query(Transaction).filter(Transaction.to_account_id == account.id)
    if since is not None:
        query = query.filter(Transaction.timestamp >= since)
    return query.order_by(Transaction.timestamp.desc()).limit(safe_limit).all()

@app.get("/api/transactions/recent", response_model=List[RecentTransactionResponse])
async def get_recent_transactions(
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
    limit: int = 10,
):
    """Get most recent transactions across the org ledger."""
    if current_user.get("is_anonymous"):
        raise HTTPException(status_code=401, detail="Authentication required")

    safe_limit = max(1, min(limit, 100))
    txns = (
        session.query(Transaction)
        .order_by(Transaction.timestamp.desc())
        .limit(safe_limit)
        .all()
    )
    if not txns:
        return []

    account_ids: set[uuid.UUID] = set()
    for txn in txns:
        if txn.from_account_id:
            account_ids.add(txn.from_account_id)
        if txn.to_account_id:
            account_ids.add(txn.to_account_id)

    account_name_map: dict[uuid.UUID, str] = {}
    if account_ids:
        rows = session.query(Account.id, Account.name).filter(Account.id.in_(account_ids)).all()
        account_name_map = {row.id: row.name for row in rows}

    return [
        {
            "id": txn.id,
            "timestamp": txn.timestamp,
            "transaction_type": txn.transaction_type,
            "amount": txn.amount,
            "currency": txn.currency,
            "description": txn.description,
            "from_account_id": txn.from_account_id,
            "to_account_id": txn.to_account_id,
            "from_account_name": account_name_map.get(txn.from_account_id) if txn.from_account_id else None,
            "to_account_name": account_name_map.get(txn.to_account_id) if txn.to_account_id else None,
        }
        for txn in txns
    ]

# ============= UBI ENDPOINTS =============

@app.get("/api/ubi/eligibility")
async def get_ubi_eligibility(
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db)
):
    """Check UBI eligibility and next payment"""
    if current_user.get("is_anonymous"):
        raise HTTPException(status_code=401, detail="Authentication required")
    
    account = session.query(Account).filter_by(email=current_user["email"]).first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    eligibility = session.query(UBIEligibility).filter_by(account_id=account.id).first()
    
    if not eligibility:
        return {
            "is_eligible": False,
            "reason": "Not enrolled in UBI system"
        }
    
    # Check if payment is due
    if date.today() >= eligibility.next_payment_date:
        # Calculate UBI amount based on system metrics
        system_metrics = await get_system_metrics()
        ubi_amount = EconomicEngine.calculate_ubi_amount(
            account.balance,
            system_metrics["average_balance"]
        )
        
        # Process payment in background
        asyncio.create_task(process_ubi_payment(account.id, ubi_amount))
        
        return {
            "is_eligible": True,
            "payment_due": True,
            "estimated_amount": ubi_amount,
            "next_payment_date": eligibility.next_payment_date
        }
    
    return {
        "is_eligible": eligibility.is_eligible,
        "payment_due": False,
        "next_payment_date": eligibility.next_payment_date,
        "last_payment_amount": eligibility.last_payment_amount,
        "total_payments_received": eligibility.total_payments_received
    }

@app.get("/api/ubi/settings", response_model=UBIRuntimeSettingsResponse)
async def get_ubi_settings(
    current_user: dict = Depends(get_current_user),
):
    """Read runtime UBI settings used by the UBI worker."""
    if current_user.get("is_anonymous"):
        raise HTTPException(status_code=401, detail="Authentication required")
    return await get_ubi_runtime_settings()

@app.patch("/api/ubi/settings", response_model=UBIRuntimeSettingsResponse)
async def update_ubi_settings(
    payload: UBIRuntimeSettingsUpdate,
    current_user: dict = Depends(get_current_user),
):
    """Update runtime UBI settings used by the UBI worker."""
    if current_user.get("is_anonymous"):
        raise HTTPException(status_code=401, detail="Authentication required")
    if not _is_sysadmin(current_user):
        raise HTTPException(status_code=403, detail="SysAdmin access required")

    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        return await get_ubi_runtime_settings()

    interval_seconds = updates.get("interval_seconds")
    dena_annual = updates.get("dena_annual")
    dena_precision = updates.get("dena_precision")
    entity_types = updates.get("entity_types")
    entity_types_csv = ",".join(entity_types) if entity_types is not None else None

    try:
        await ensure_ubi_runtime_settings_table()
        async with db.async_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE ubi_runtime_settings
                SET interval_seconds = COALESCE($1, interval_seconds),
                    dena_annual = COALESCE($2, dena_annual),
                    dena_precision = COALESCE($3, dena_precision),
                    entity_types = COALESCE($4, entity_types),
                    updated_at = NOW(),
                    updated_by = $5
                WHERE id = 1
                """,
                interval_seconds,
                float(dena_annual) if dena_annual is not None else None,
                dena_precision,
                entity_types_csv,
                current_user.get("email"),
            )
    except Exception as exc:
        logger.error(f"Failed to update UBI settings: {exc}")
        raise HTTPException(status_code=503, detail="UBI settings service temporarily unavailable")
    return await get_ubi_runtime_settings()


@app.get("/api/admin/business-card/settings", response_model=BusinessCardAbuseSettingsResponse)
async def get_business_card_abuse_settings(
    current_user: dict = Depends(get_current_user),
):
    if current_user.get("is_anonymous"):
        raise HTTPException(status_code=401, detail="Authentication required")
    if not _is_sysadmin(current_user):
        raise HTTPException(status_code=403, detail="SysAdmin access required")
    return await get_business_card_runtime_settings()


@app.patch("/api/admin/business-card/settings", response_model=BusinessCardAbuseSettingsResponse)
async def update_business_card_abuse_settings(
    payload: BusinessCardAbuseSettingsUpdate,
    current_user: dict = Depends(get_current_user),
):
    if current_user.get("is_anonymous"):
        raise HTTPException(status_code=401, detail="Authentication required")
    if not _is_sysadmin(current_user):
        raise HTTPException(status_code=403, detail="SysAdmin access required")

    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        return await get_business_card_runtime_settings()

    allowed_content_types = updates.get("allowed_content_types")
    allowed_content_types_csv = ",".join(allowed_content_types) if allowed_content_types is not None else None
    try:
        await ensure_business_card_runtime_settings_table()
        async with db.async_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE business_card_runtime_settings
                SET enabled = COALESCE($1, enabled),
                    per_user_limit_per_hour = COALESCE($2, per_user_limit_per_hour),
                    per_ip_limit_per_hour = COALESCE($3, per_ip_limit_per_hour),
                    global_limit_per_hour = COALESCE($4, global_limit_per_hour),
                    duplicate_hash_limit = COALESCE($5, duplicate_hash_limit),
                    duplicate_hash_window_seconds = COALESCE($6, duplicate_hash_window_seconds),
                    max_bytes = COALESCE($7, max_bytes),
                    allowed_content_types = COALESCE($8, allowed_content_types),
                    event_link_enrichment_enabled = COALESCE($9, event_link_enrichment_enabled),
                    auto_clarification_enabled = COALESCE($10, auto_clarification_enabled),
                    auto_min_confidence = COALESCE($11, auto_min_confidence),
                    auto_min_margin = COALESCE($12, auto_min_margin),
                    updated_at = NOW(),
                    updated_by = $13
                WHERE id = 1
                """,
                updates.get("enabled"),
                updates.get("per_user_limit_per_hour"),
                updates.get("per_ip_limit_per_hour"),
                updates.get("global_limit_per_hour"),
                updates.get("duplicate_hash_limit"),
                updates.get("duplicate_hash_window_seconds"),
                updates.get("max_bytes"),
                allowed_content_types_csv,
                updates.get("event_link_enrichment_enabled"),
                updates.get("auto_clarification_enabled"),
                updates.get("auto_min_confidence"),
                updates.get("auto_min_margin"),
                current_user.get("email"),
            )
    except Exception as exc:
        logger.error(f"Failed to update business card abuse settings: {exc}")
        raise HTTPException(status_code=503, detail="Business card settings service temporarily unavailable")
    return await get_business_card_runtime_settings()


@app.get("/api/admin/scans/{submission_id}/image")
@app.get("/api/admin/business-cards/{submission_id}/image")
async def get_business_card_submission_image(
    submission_id: uuid.UUID,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    if current_user.get("is_anonymous"):
        raise HTTPException(status_code=401, detail="Authentication required")
    if not _is_sysadmin(current_user):
        raise HTTPException(status_code=403, detail="SysAdmin access required")

    submission = (
        session.query(BusinessCardSubmission)
        .filter(BusinessCardSubmission.id == submission_id)
        .first()
    )
    if not submission:
        raise HTTPException(status_code=404, detail="Business card submission not found")
    if not submission.image_storage_path:
        raise HTTPException(status_code=404, detail="Stored image not available")

    download_name = (submission.image_filename or f"{submission.id}").strip() or f"{submission.id}"
    image_bytes = _load_business_card_image_bytes(
        storage_backend=(submission.image_storage_backend or "local"),
        storage_bucket=submission.image_storage_bucket,
        storage_path=submission.image_storage_path,
    )
    return Response(
        content=image_bytes,
        media_type=submission.image_content_type or "application/octet-stream",
        headers={
            "Content-Disposition": f'inline; filename="{download_name}"',
            "Cache-Control": "private, max-age=0, no-cache, no-store",
        },
    )


@app.get("/api/admin/scans", response_model=List[BusinessCardSubmissionResponse])
@app.get("/api/admin/business-cards", response_model=List[BusinessCardSubmissionResponse])
async def list_business_card_submissions(
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
    limit: int = 100,
    offset: int = 0,
):
    if current_user.get("is_anonymous"):
        raise HTTPException(status_code=401, detail="Authentication required")
    if not _is_sysadmin(current_user):
        raise HTTPException(status_code=403, detail="SysAdmin access required")
    safe_limit = max(1, min(limit, 1000))
    safe_offset = max(0, min(offset, 100000))
    rows = (
        session.query(BusinessCardSubmission)
        .order_by(BusinessCardSubmission.created_at.desc(), BusinessCardSubmission.id.desc())
        .offset(safe_offset)
        .limit(safe_limit)
        .all()
    )
    return rows


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

# ============= STOCK MARKET ENDPOINTS =============

@app.post("/api/stocks", response_model=dict)
async def create_stock(
    stock_data: StockCreate,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db)
):
    """Create a new publicly traded company (admin/business only)"""
    if current_user.get("is_anonymous"):
        raise HTTPException(status_code=401, detail="Authentication required")
    
    account = session.query(Account).filter_by(email=current_user["email"]).first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    # Check if business is verified
    if not _is_sysadmin(current_user) and (account.entity_type != EntityType.BUSINESS or not account.is_verified):
        raise HTTPException(status_code=403, detail="Only verified businesses can issue stocks")
    
    # Check if ticker symbol already exists
    existing = session.query(Stock).filter_by(ticker_symbol=stock_data.ticker_symbol).first()
    if existing:
        raise HTTPException(status_code=400, detail="Ticker symbol already exists")
    
    # Create stock
    stock = Stock(
        id=uuid.uuid4(),
        company_name=stock_data.company_name,
        ticker_symbol=stock_data.ticker_symbol,
        current_price=stock_data.initial_price,
        day_open=stock_data.initial_price,
        day_high=stock_data.initial_price,
        day_low=stock_data.initial_price,
        total_shares=stock_data.total_shares,
        shares_outstanding=stock_data.total_shares,
        market_cap=stock_data.initial_price * stock_data.total_shares,
        sector=stock_data.sector,
        description=stock_data.description
    )
    
    session.add(stock)
    session.commit()
    
    # Reserve shares for the company
    holding = PortfolioHolding(
        id=uuid.uuid4(),
        account_id=account.id,
        stock_id=stock.id,
        quantity=stock_data.total_shares,
        average_purchase_price=stock_data.initial_price,
        total_invested=stock_data.initial_price * stock_data.total_shares
    )
    
    session.add(holding)
    session.commit()
    
    return {"stock_id": stock.id, "message": "Stock created successfully"}

@app.get("/api/stocks", response_model=List[dict])
async def list_stocks(
    session: Session = Depends(get_db),
    sector: Optional[str] = None,
    skip: int = 0,
    limit: int = 100
):
    """List all available stocks"""
    query = session.query(Stock).filter_by(is_active=True)
    
    if sector:
        query = query.filter_by(sector=sector)
    
    stocks = query.order_by(Stock.market_cap.desc()).offset(skip).limit(limit).all()
    
    return [
        {
            "id": stock.id,
            "company_name": stock.company_name,
            "ticker_symbol": stock.ticker_symbol,
            "current_price": stock.current_price,
            "day_change": ((stock.current_price - stock.day_open) / stock.day_open * 100) if stock.day_open > 0 else 0,
            "volume": stock.volume,
            "market_cap": stock.market_cap,
            "sector": stock.sector
        }
        for stock in stocks
    ]

@app.post("/api/stocks/orders")
async def place_stock_order(
    order_data: StockOrderCreate,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
    conn: asyncpg.Connection = Depends(get_async_db)
):
    """Place a stock market order"""
    if current_user.get("is_anonymous"):
        raise HTTPException(status_code=401, detail="Authentication required")
    
    # Check market hours
    if not is_market_open():
        raise HTTPException(status_code=400, detail="Market is closed")
    
    account = session.query(Account).filter_by(email=current_user["email"]).first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    stock = session.query(Stock).filter_by(id=order_data.stock_id).first()
    if not stock or not stock.is_active:
        raise HTTPException(status_code=404, detail="Stock not found or inactive")
    
    # Calculate order price
    order_price = order_data.limit_price if order_data.order_type == OrderType.LIMIT else stock.current_price
    total_cost = order_price * order_data.quantity if order_data.action == "buy" else Decimal('0.00')
    
    try:
        # Use database transaction for order placement
        async with conn.transaction():
            if order_data.action == "buy":
                # Check balance
                if account.balance < total_cost:
                    raise HTTPException(status_code=400, detail="Insufficient funds")
                
                # Reserve funds
                await conn.execute("""
                    UPDATE accounts 
                    SET balance = balance - $1, updated_at = NOW()
                    WHERE id = $2 AND balance >= $1
                """, float(total_cost), account.id)
                
            else:  # sell
                # Check holdings
                holding = await conn.fetchrow("""
                    SELECT quantity FROM portfolio_holdings 
                    WHERE account_id = $1 AND stock_id = $2
                """, account.id, stock.id)
                
                if not holding or holding['quantity'] < order_data.quantity:
                    raise HTTPException(status_code=400, detail="Insufficient shares")
            
            # Create order
            order_id = uuid.uuid4()
            await conn.execute("""
                INSERT INTO stock_orders 
                (id, account_id, stock_id, order_type, action, quantity, limit_price, status, timestamp)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NOW())
            """, order_id, account.id, stock.id, order_data.order_type.value, 
               order_data.action, order_data.quantity, 
               float(order_data.limit_price) if order_data.limit_price else None,
               OrderStatus.PENDING.value)
            
            # Try to match order immediately (simplified)
            await match_order(conn, order_id, stock, order_price, order_data.action)
            
            return {"order_id": order_id, "status": "placed"}
            
    except asyncpg.exceptions.CheckViolationError:
        raise HTTPException(status_code=400, detail="Order placement failed")
    except Exception as e:
        logger.error(f"Order placement failed: {e}")
        raise HTTPException(status_code=500, detail="Order placement failed")

async def match_order(
    conn: asyncpg.Connection,
    order_id: uuid.UUID,
    stock: Stock,
    price: Decimal,
    action: str
):
    """Match stock orders (simplified implementation)"""
    # In a real system, this would match against opposite orders
    # For now, execute immediately at current price
    
    await conn.execute("""
        UPDATE stock_orders 
        SET status = $1, executed_price = $2, executed_quantity = quantity, executed_at = NOW()
        WHERE id = $3
    """, OrderStatus.EXECUTED.value, float(stock.current_price), order_id)
    
    # Update stock price based on order
    price_impact = Decimal('0.001') * Decimal(stock.volume / max(stock.total_shares, 1))
    if action == "buy":
        new_price = stock.current_price * (Decimal('1.0') + price_impact)
    else:
        new_price = stock.current_price * (Decimal('1.0') - price_impact)
    
    await conn.execute("""
        UPDATE stocks 
        SET current_price = $1, 
            day_high = GREATEST(day_high, $1),
            day_low = LEAST(day_low, $1),
            volume = volume + 1,
            last_updated = NOW()
        WHERE id = $2
    """, float(new_price), stock.id)

# ============= PORTFOLIO ENDPOINTS =============

@app.get("/api/portfolio", response_model=dict)
async def get_portfolio(
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db)
):
    """Get user's investment portfolio"""
    if current_user.get("is_anonymous"):
        raise HTTPException(status_code=401, detail="Authentication required")
    
    account = session.query(Account).filter_by(email=current_user["email"]).first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    # Get holdings with current prices
    holdings = session.query(
        PortfolioHolding, Stock
    ).join(
        Stock, PortfolioHolding.stock_id == Stock.id
    ).filter(
        PortfolioHolding.account_id == account.id,
        PortfolioHolding.quantity > 0
    ).all()
    
    portfolio_value = Decimal('0.00')
    total_invested = Decimal('0.00')
    holdings_data = []
    
    for holding, stock in holdings:
        current_value = stock.current_price * holding.quantity
        portfolio_value += current_value
        total_invested += holding.total_invested or Decimal('0.00')
        
        holdings_data.append({
            "stock_id": stock.id,
            "ticker_symbol": stock.ticker_symbol,
            "company_name": stock.company_name,
            "quantity": holding.quantity,
            "average_price": holding.average_purchase_price,
            "current_price": stock.current_price,
            "current_value": current_value,
            "unrealized_gain": current_value - (holding.total_invested or Decimal('0.00'))
        })
    
    return {
        "account_id": account.id,
        "portfolio_value": portfolio_value,
        "total_invested": total_invested,
        "unrealized_gains": portfolio_value - total_invested,
        "holdings": holdings_data,
        "cash_balance": account.balance
    }

# ============= INSURANCE ENDPOINTS =============

@app.get("/api/insurance/policies", response_model=List[dict])
async def list_insurance_policies(
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    """List insurance policies for the current account"""
    if current_user.get("is_anonymous"):
        raise HTTPException(status_code=401, detail="Authentication required")

    account = session.query(Account).filter_by(email=current_user["email"]).first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    policies = (
        session.query(InsurancePolicy)
        .filter_by(account_id=account.id)
        .order_by(InsurancePolicy.start_date.desc())
        .all()
    )

    return [
        {
            "id": policy.id,
            "insurance_type": policy.insurance_type.value,
            "coverage_amount": policy.coverage_amount,
            "premium_amount": policy.premium_amount,
            "duration_years": policy.duration_years,
            "start_date": policy.start_date,
            "end_date": policy.end_date,
            "deductible": policy.deductible,
            "is_active": policy.is_active,
        }
        for policy in policies
    ]

@app.post("/api/insurance/policies", response_model=dict)
async def create_insurance_policy(
    policy_data: InsurancePolicyCreate,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db)
):
    """Purchase an insurance policy"""
    if current_user.get("is_anonymous"):
        raise HTTPException(status_code=401, detail="Authentication required")
    
    account = session.query(Account).filter_by(email=current_user["email"]).first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    # Calculate premium (simplified - would use actual risk assessment)
    risk_factors = {"age": 35, "health_score": 75, "location_risk": "medium"}
    premium = EconomicEngine.calculate_insurance_premium(
        policy_data.insurance_type,
        policy_data.coverage_amount,
        risk_factors
    )
    
    # Check balance
    if account.balance < premium:
        raise HTTPException(status_code=400, detail="Insufficient funds for premium")
    
    # Create policy
    policy = InsurancePolicy(
        id=uuid.uuid4(),
        account_id=account.id,
        insurance_type=policy_data.insurance_type,
        coverage_amount=policy_data.coverage_amount,
        premium_amount=premium,
        duration_years=policy_data.duration_years,
        start_date=date.today(),
        end_date=date.today() + timedelta(days=policy_data.duration_years * 365),
        beneficiaries=policy_data.beneficiaries,
        deductible=policy_data.deductible
    )
    
    # Deduct premium
    account.balance -= premium
    
    # Record transaction
    transaction = Transaction(
        id=uuid.uuid4(),
        from_account_id=account.id,
        amount=premium,
        transaction_type=TransactionType.INSURANCE_PREMIUM,
        description=f"{policy_data.insurance_type.value} insurance premium"
    )
    
    session.add(policy)
    session.add(transaction)
    session.commit()
    
    return {
        "policy_id": policy.id,
        "premium": premium,
        "coverage_amount": policy_data.coverage_amount,
        "start_date": policy.start_date,
        "end_date": policy.end_date
    }

# ============= FISCAL POLICY ENDPOINTS =============

@app.post("/api/fiscal/proposals", response_model=dict)
async def create_fiscal_proposal(
    proposal_data: FiscalProposalCreate,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db)
):
    """Create a new fiscal policy proposal"""
    if current_user.get("is_anonymous"):
        raise HTTPException(status_code=401, detail="Authentication required")
    
    account = session.query(Account).filter_by(email=current_user["email"]).first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    # Check if user can create proposals (e.g., verified account)
    if not account.is_verified and account.entity_type == EntityType.INDIVIDUAL:
        raise HTTPException(status_code=403, detail="Account must be verified to create proposals")
    
    # Create proposal
    proposal = FiscalProposal(
        id=uuid.uuid4(),
        title=proposal_data.title,
        description=proposal_data.description,
        policy_area=proposal_data.policy_area,
        proposed_budget=proposal_data.proposed_budget,
        duration_months=proposal_data.duration_months,
        expected_impact=proposal_data.expected_impact,
        created_by=account.id,
        voting_start=datetime.now(timezone.utc),
        voting_end=datetime.now(timezone.utc) + timedelta(days=proposal_data.voting_days),
        status="voting"
    )
    
    session.add(proposal)
    session.commit()
    
    # Cache in Redis for quick access
    cache_key = f"proposal:{proposal.id}"
    db.redis_client.setex(
        cache_key,
        3600,  # 1 hour
        json.dumps({
            "id": str(proposal.id),
            "title": proposal.title,
            "policy_area": proposal.policy_area.value,
            "proposed_budget": str(proposal.proposed_budget),
            "status": proposal.status,
            "voting_end": proposal.voting_end.isoformat()
        })
    )
    
    return {"proposal_id": proposal.id, "status": "created"}

@app.post("/api/fiscal/proposals/{proposal_id}/vote")
async def vote_on_proposal(
    proposal_id: uuid.UUID,
    vote_data: FiscalVoteCreate,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
    conn: asyncpg.Connection = Depends(get_async_db)
):
    """Vote on a fiscal proposal"""
    if current_user.get("is_anonymous"):
        raise HTTPException(status_code=401, detail="Authentication required")
    
    account = session.query(Account).filter_by(email=current_user["email"]).first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    proposal = session.query(FiscalProposal).filter_by(id=proposal_id).first()
    if not proposal:
        raise HTTPException(status_code=404, detail="Proposal not found")
    
    # Check if voting is still open
    if proposal.status != "voting" or datetime.now(timezone.utc) > proposal.voting_end:
        raise HTTPException(status_code=400, detail="Voting is closed")
    
    # Check if already voted
    existing_vote = session.query(FiscalVote).filter_by(
        proposal_id=proposal_id,
        account_id=account.id
    ).first()
    
    if existing_vote:
        raise HTTPException(status_code=400, detail="Already voted on this proposal")
    
    try:
        # Use database transaction for atomic vote
        async with conn.transaction():
            # Create vote
            await conn.execute("""
                INSERT INTO fiscal_votes (id, proposal_id, account_id, vote, rationale, timestamp)
                VALUES ($1, $2, $3, $4, $5, NOW())
            """, uuid.uuid4(), proposal_id, account.id, vote_data.vote.value, vote_data.rationale)
            
            # Update proposal vote counts atomically
            await conn.execute(f"""
                UPDATE fiscal_proposals 
                SET {vote_data.vote.value}_votes = {vote_data.vote.value}_votes + 1,
                    total_votes = total_votes + 1,
                    updated_at = NOW()
                WHERE id = $1
            """, proposal_id)
        
        return {"status": "vote_recorded", "vote": vote_data.vote}
        
    except Exception as e:
        logger.error(f"Vote failed: {e}")
        raise HTTPException(status_code=500, detail="Vote failed")

# ============= TAX ENDPOINTS =============

@app.post("/api/tax/calculate")
async def calculate_tax(
    tax_data: TaxEstimate,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db)
):
    """Calculate estimated tax liability"""
    if current_user.get("is_anonymous"):
        raise HTTPException(status_code=401, detail="Authentication required")
    
    account = session.query(Account).filter_by(email=current_user["email"]).first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    tax_amount = EconomicEngine.calculate_tax(tax_data.taxable_income, account.entity_type)
    
    # Check for existing tax record
    existing = session.query(TaxRecord).filter_by(
        account_id=account.id,
        tax_year=tax_data.tax_year
    ).first()
    
    if existing:
        return {
            "taxable_income": tax_data.taxable_income,
            "tax_amount": tax_amount,
            "already_paid": existing.paid_amount,
            "balance_due": tax_amount - existing.paid_amount,
            "due_date": existing.due_date
        }
    
    # Create tax record if doesn't exist
    tax_record = TaxRecord(
        id=uuid.uuid4(),
        account_id=account.id,
        tax_year=tax_data.tax_year,
        taxable_income=tax_data.taxable_income,
        tax_amount=tax_amount,
        due_date=date(tax_data.tax_year + 1, 4, 15)  # Tax day in US
    )
    
    session.add(tax_record)
    session.commit()
    
    return {
        "taxable_income": tax_data.taxable_income,
        "tax_amount": tax_amount,
        "due_date": tax_record.due_date,
        "record_id": tax_record.id
    }

@app.post("/api/tax/pay")
async def pay_taxes(
    record_id: uuid.UUID,
    amount: Decimal,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
    conn: asyncpg.Connection = Depends(get_async_db)
):
    """Pay taxes"""
    if current_user.get("is_anonymous"):
        raise HTTPException(status_code=401, detail="Authentication required")
    
    account = session.query(Account).filter_by(email=current_user["email"]).first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    tax_record = session.query(TaxRecord).filter_by(id=record_id, account_id=account.id).first()
    if not tax_record:
        raise HTTPException(status_code=404, detail="Tax record not found")
    
    if amount <= Decimal('0.00'):
        raise HTTPException(status_code=400, detail="Payment amount must be positive")
    
    if amount > tax_record.tax_amount - tax_record.paid_amount:
        raise HTTPException(status_code=400, detail="Payment exceeds tax due")
    
    if account.balance < amount:
        raise HTTPException(status_code=400, detail="Insufficient funds")
    
    try:
        async with conn.transaction():
            # Deduct from account
            await conn.execute("""
                UPDATE accounts 
                SET balance = balance - $1, updated_at = NOW()
                WHERE id = $2 AND balance >= $1
            """, float(amount), account.id)
            
            # Update tax record
            await conn.execute("""
                UPDATE tax_records 
                SET paid_amount = paid_amount + $1,
                    status = CASE 
                        WHEN paid_amount + $1 >= tax_amount THEN 'paid'
                        ELSE 'partial'
                    END,
                    paid_at = CASE 
                        WHEN paid_amount + $1 >= tax_amount THEN NOW()
                        ELSE paid_at
                    END,
                    updated_at = NOW()
                WHERE id = $2
            """, float(amount), record_id)
            
            # Record transaction
            await conn.execute("""
                INSERT INTO transactions 
                (id, from_account_id, amount, transaction_type, description, timestamp)
                VALUES ($1, $2, $3, $4, $5, NOW())
            """, uuid.uuid4(), account.id, float(amount), 
               TransactionType.TAX_PAYMENT.value, f"Tax payment for {tax_record.tax_year}")
        
        return {"paid": amount, "remaining": tax_record.tax_amount - tax_record.paid_amount - amount}
        
    except Exception as e:
        logger.error(f"Tax payment failed: {e}")
        raise HTTPException(status_code=500, detail="Tax payment failed")

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

# ============= GOVERNANCE API =============

def _governance_actor_name(current_user: dict) -> str:
    return (
        str(current_user.get("name") or "").strip()
        or str(current_user.get("email") or "").strip()
        or _actor_user_id(current_user)
        or "Unknown"
    )


def _get_governance_motion_or_404(session: Session, motion_id: uuid.UUID) -> GovernanceMotion:
    motion = session.query(GovernanceMotion).filter(GovernanceMotion.id == motion_id).first()
    if not motion:
        raise HTTPException(status_code=404, detail="Motion not found")
    return motion


@app.get("/api/governance/motions", response_model=List[GovernanceMotionResponse])
async def list_governance_motions(
    session: Session = Depends(get_db),
    search: str = Query("", alias="search"),
    status: Optional[List[str]] = Query(None, alias="status"),
    type: Optional[str] = Query(None, alias="type"),
    parent_motion_id: Optional[uuid.UUID] = Query(None, alias="parent_motion_id"),
):
    query = session.query(GovernanceMotion)
    needle = (search or "").strip()
    if needle:
        like = f"%{needle}%"
        query = query.filter(
            (GovernanceMotion.title.ilike(like))
            | (GovernanceMotion.body.ilike(like))
            | (GovernanceMotion.proposer_name.ilike(like))
        )
    if status:
        allowed_statuses = {
            GovernanceMotionStatus.PROPOSED.value,
            GovernanceMotionStatus.SECONDED.value,
            GovernanceMotionStatus.DISCUSSION.value,
            GovernanceMotionStatus.VOTING.value,
            GovernanceMotionStatus.PASSED.value,
            GovernanceMotionStatus.FAILED.value,
            GovernanceMotionStatus.TABLED.value,
            GovernanceMotionStatus.WITHDRAWN.value,
        }
        statuses = [item for item in status if item in allowed_statuses]
        if statuses:
            query = query.filter(GovernanceMotion.status.in_(statuses))
    if type:
        if type not in {
            GovernanceMotionType.MAIN.value,
            GovernanceMotionType.AMENDMENT.value,
            GovernanceMotionType.DISSOLUTION.value,
        }:
            raise HTTPException(status_code=422, detail="Invalid type filter")
        query = query.filter(GovernanceMotion.type == type)
    if parent_motion_id:
        query = query.filter(GovernanceMotion.parent_motion_id == parent_motion_id)

    rows = query.order_by(GovernanceMotion.created_at.desc()).all()
    return [_map_governance_motion(row) for row in rows]


@app.get("/api/governance/motions/{motion_id}", response_model=GovernanceMotionResponse)
async def get_governance_motion(
    motion_id: uuid.UUID,
    session: Session = Depends(get_db),
):
    motion = _get_governance_motion_or_404(session, motion_id)
    return _map_governance_motion(motion)


@app.get(
    "/api/governance/motions/{motion_id}/dissolution-plan",
    response_model=GovernanceDissolutionPlanResponse,
)
async def get_governance_dissolution_plan(
    motion_id: uuid.UUID,
    session: Session = Depends(get_db),
):
    motion = _get_governance_motion_or_404(session, motion_id)
    if motion.type != GovernanceMotionType.DISSOLUTION.value:
        raise HTTPException(status_code=404, detail="No dissolution plan for this motion")
    plan = _get_dissolution_plan(session, motion.id)
    if not plan:
        raise HTTPException(status_code=404, detail="Dissolution plan not found")
    return plan


@app.post("/api/governance/motions", response_model=GovernanceMotionResponse)
async def create_governance_motion(
    payload: GovernanceMotionCreate,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    user_id = _actor_user_id(current_user)
    _throttle_action(f"governance:create-motion:{user_id}", limit=40, window_seconds=3600)

    if payload.type == GovernanceMotionType.AMENDMENT.value and not payload.parent_motion_id:
        raise HTTPException(status_code=422, detail="parent_motion_id is required for amendments")
    if payload.type in {GovernanceMotionType.MAIN.value, GovernanceMotionType.DISSOLUTION.value} and payload.parent_motion_id:
        raise HTTPException(status_code=422, detail="parent_motion_id is only valid for amendments")
    _validate_dissolution_payload(payload)

    if payload.parent_motion_id:
        _get_governance_motion_or_404(session, payload.parent_motion_id)

    proposer_name = _governance_actor_name(current_user)
    proposer_user_name = proposer_name
    proposer_org_name = None
    proposer_org_id = None

    if payload.proposer_type == GovernanceProposerType.ORG.value:
        if not payload.proposer_org_id:
            raise HTTPException(status_code=422, detail="proposer_org_id is required for org proposer type")
        org = session.query(Organization).filter(Organization.id == payload.proposer_org_id).first()
        if not org:
            raise HTTPException(status_code=404, detail="Organization not found")
        if not _is_org_admin(org, current_user):
            raise HTTPException(status_code=403, detail="Organization admin access required")
        proposer_name = org.name
        proposer_org_name = org.name
        proposer_org_id = org.id

    motion = GovernanceMotion(
        id=uuid.uuid4(),
        type=payload.type,
        parent_motion_id=payload.parent_motion_id,
        title=payload.title.strip(),
        body=payload.body.strip(),
        proposed_body_diff=(payload.proposed_body_diff or "").strip() or None,
        status=GovernanceMotionStatus.PROPOSED.value,
        proposer_type=payload.proposer_type,
        proposer_user_id=user_id,
        proposer_name=proposer_name,
        proposer_user_name=proposer_user_name,
        proposer_org_id=proposer_org_id,
        proposer_org_name=proposer_org_name,
        quorum_required=int(payload.quorum_required),
    )
    session.add(motion)
    if payload.type == GovernanceMotionType.DISSOLUTION.value:
        session.add(
            GovernanceDissolutionPlan(
                id=uuid.uuid4(),
                motion_id=motion.id,
                asset_disposition=(payload.dissolution_asset_disposition or "").strip(),
                asset_recipient_name=(payload.dissolution_asset_recipient_name or "").strip(),
                asset_recipient_type=(payload.dissolution_asset_recipient_type or "").strip(),
                legal_compliance_notes=(payload.dissolution_legal_compliance_notes or "").strip() or None,
            )
        )
    _audit_event(
        session,
        actor=current_user,
        event_type="governance.motion.created",
        target_type="governance_motion",
        target_id=str(motion.id),
        metadata={
            "motion_type": motion.type,
            "proposer_type": motion.proposer_type,
            "proposer_org_id": str(motion.proposer_org_id) if motion.proposer_org_id else None,
            "is_dissolution": motion.type == GovernanceMotionType.DISSOLUTION.value,
        },
    )
    session.commit()
    session.refresh(motion)
    return _map_governance_motion(motion)


@app.post("/api/governance/motions/{motion_id}/second", response_model=GovernanceMotionResponse)
async def second_governance_motion(
    motion_id: uuid.UUID,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    motion = _get_governance_motion_or_404(session, motion_id)
    user_id = _actor_user_id(current_user)
    if motion.status != GovernanceMotionStatus.PROPOSED.value:
        raise HTTPException(status_code=400, detail="Motion must be in proposed status to second")
    if motion.proposer_user_id == user_id:
        raise HTTPException(status_code=400, detail="Proposer cannot second their own motion")
    motion.seconder_id = user_id
    motion.seconder_name = _governance_actor_name(current_user)
    motion.status = GovernanceMotionStatus.DISCUSSION.value
    motion.discussion_deadline = datetime.now(timezone.utc) + timedelta(days=2)
    motion.updated_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(motion)
    return _map_governance_motion(motion)


@app.post("/api/governance/motions/{motion_id}/open-voting", response_model=GovernanceMotionResponse)
async def open_governance_motion_voting(
    motion_id: uuid.UUID,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    motion = _get_governance_motion_or_404(session, motion_id)
    if not _can_manage_governance_motion(motion, current_user, session):
        raise HTTPException(status_code=403, detail="Motion management access required")
    if motion.status != GovernanceMotionStatus.DISCUSSION.value:
        raise HTTPException(status_code=400, detail="Motion must be in discussion status")
    _ensure_governance_transition(motion, GovernanceMotionStatus.VOTING.value)
    motion.status = GovernanceMotionStatus.VOTING.value
    motion.voting_deadline = datetime.now(timezone.utc) + timedelta(days=1)
    motion.updated_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(motion)
    return _map_governance_motion(motion)


@app.post("/api/governance/motions/{motion_id}/table", response_model=GovernanceMotionResponse)
async def table_governance_motion(
    motion_id: uuid.UUID,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    motion = _get_governance_motion_or_404(session, motion_id)
    if not _can_manage_governance_motion(motion, current_user, session):
        raise HTTPException(status_code=403, detail="Motion management access required")
    if motion.status != GovernanceMotionStatus.DISCUSSION.value:
        raise HTTPException(status_code=400, detail="Motion must be in discussion status")
    _ensure_governance_transition(motion, GovernanceMotionStatus.TABLED.value)
    motion.status = GovernanceMotionStatus.TABLED.value
    motion.updated_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(motion)
    return _map_governance_motion(motion)


@app.post("/api/governance/motions/{motion_id}/withdraw", response_model=GovernanceMotionResponse)
async def withdraw_governance_motion(
    motion_id: uuid.UUID,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    motion = _get_governance_motion_or_404(session, motion_id)
    user_id = _actor_user_id(current_user)
    if motion.proposer_user_id != user_id and not _is_sysadmin(current_user):
        raise HTTPException(status_code=403, detail="Only the proposer or an admin can withdraw this motion")
    if motion.status != GovernanceMotionStatus.PROPOSED.value:
        raise HTTPException(status_code=400, detail="Only proposed motions can be withdrawn")
    _ensure_governance_transition(motion, GovernanceMotionStatus.WITHDRAWN.value)
    motion.status = GovernanceMotionStatus.WITHDRAWN.value
    motion.updated_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(motion)
    return _map_governance_motion(motion)


@app.post("/api/governance/motions/{motion_id}/resolve", response_model=GovernanceMotionResponse)
async def resolve_governance_motion(
    motion_id: uuid.UUID,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    motion = _get_governance_motion_or_404(session, motion_id)
    if not _can_manage_governance_motion(motion, current_user, session):
        raise HTTPException(status_code=403, detail="Motion management access required")
    if motion.status != GovernanceMotionStatus.VOTING.value:
        raise HTTPException(status_code=400, detail="Motion must be in voting status")
    if motion.type == GovernanceMotionType.DISSOLUTION.value:
        plan = _get_dissolution_plan(session, motion.id)
        if not plan:
            raise HTTPException(
                status_code=422,
                detail="Dissolution motion cannot be resolved without an asset disposition plan",
            )
    result = _governance_vote_result(motion)
    next_status = GovernanceMotionStatus.PASSED.value if result["passed"] else GovernanceMotionStatus.FAILED.value
    _ensure_governance_transition(motion, next_status)
    motion.status = next_status
    motion.updated_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(motion)
    return _map_governance_motion(motion)


@app.post(
    "/api/governance/motions/{motion_id}/execute-dissolution",
    response_model=GovernanceDissolutionPlanResponse,
)
async def execute_governance_dissolution(
    motion_id: uuid.UUID,
    payload: GovernanceDissolutionExecuteRequest,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    """Record execution of a passed dissolution decision and asset disposition action."""
    _require_authenticated_user(current_user)
    if not _is_sysadmin(current_user):
        raise HTTPException(status_code=403, detail="SysAdmin privileges required")
    motion = _get_governance_motion_or_404(session, motion_id)
    if motion.type != GovernanceMotionType.DISSOLUTION.value:
        raise HTTPException(status_code=422, detail="Motion is not a dissolution motion")
    if motion.status != GovernanceMotionStatus.PASSED.value:
        raise HTTPException(status_code=422, detail="Dissolution can only be executed after a passed motion")
    plan = _get_dissolution_plan(session, motion.id)
    if not plan:
        raise HTTPException(status_code=422, detail="Missing dissolution asset disposition plan")
    plan.executed_at = datetime.now(timezone.utc)
    plan.executed_by_user_id = _actor_user_id(current_user)
    plan.execution_notes = (payload.execution_notes or "").strip() or None
    plan.updated_at = datetime.now(timezone.utc)
    _audit_event(
        session,
        actor=current_user,
        event_type="governance.dissolution.executed",
        target_type="governance_motion",
        target_id=str(motion.id),
        metadata={
            "asset_recipient_name": plan.asset_recipient_name,
            "asset_recipient_type": plan.asset_recipient_type,
        },
    )
    session.commit()
    session.refresh(plan)
    return plan


@app.post("/api/governance/motions/{motion_id}/votes", response_model=GovernanceMotionResponse)
async def cast_governance_motion_vote(
    motion_id: uuid.UUID,
    payload: GovernanceMotionVoteCastRequest,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    motion = _get_governance_motion_or_404(session, motion_id)
    if motion.status != GovernanceMotionStatus.VOTING.value:
        raise HTTPException(status_code=400, detail="Motion is not open for voting")
    user_id = _actor_user_id(current_user)
    existing = (
        session.query(GovernanceVote)
        .filter(
            GovernanceVote.motion_id == motion.id,
            GovernanceVote.voter_user_id == user_id,
        )
        .first()
    )
    if existing:
        existing.choice = payload.choice
        existing.voter_name = _governance_actor_name(current_user)
        existing.cast_at = datetime.now(timezone.utc)
    else:
        session.add(
            GovernanceVote(
                id=uuid.uuid4(),
                motion_id=motion.id,
                voter_user_id=user_id,
                voter_name=_governance_actor_name(current_user),
                choice=payload.choice,
            )
        )
    motion.updated_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(motion)
    return _map_governance_motion(motion)


@app.get("/api/governance/motions/{motion_id}/comments", response_model=List[GovernanceCommentResponse])
async def list_governance_motion_comments(
    motion_id: uuid.UUID,
    session: Session = Depends(get_db),
):
    _get_governance_motion_or_404(session, motion_id)
    rows = (
        session.query(GovernanceComment)
        .filter(GovernanceComment.motion_id == motion_id)
        .order_by(GovernanceComment.created_at.asc())
        .all()
    )
    return rows


@app.post("/api/governance/motions/{motion_id}/comments", response_model=GovernanceCommentResponse)
async def create_governance_motion_comment(
    motion_id: uuid.UUID,
    payload: GovernanceCommentCreate,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    _get_governance_motion_or_404(session, motion_id)
    row = GovernanceComment(
        id=uuid.uuid4(),
        motion_id=motion_id,
        author_id=_actor_user_id(current_user),
        author_name=_governance_actor_name(current_user),
        body=payload.body.strip(),
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _set_governance_reaction(
    motion: GovernanceMotion,
    current_user: dict,
    session: Session,
    direction: str,
) -> GovernanceReactionResponse:
    user_id = _actor_user_id(current_user)
    existing = (
        session.query(GovernanceReaction)
        .filter(
            GovernanceReaction.motion_id == motion.id,
            GovernanceReaction.user_id == user_id,
        )
        .first()
    )
    if existing and existing.direction == direction:
        session.delete(existing)
        user_vote = None
    elif existing:
        existing.direction = direction
        existing.updated_at = datetime.now(timezone.utc)
        user_vote = direction
    else:
        session.add(
            GovernanceReaction(
                id=uuid.uuid4(),
                motion_id=motion.id,
                user_id=user_id,
                direction=direction,
            )
        )
        user_vote = direction
    motion.updated_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(motion)
    counts = _governance_reaction_counts(motion)
    return GovernanceReactionResponse(score=counts.score, user_vote=user_vote)


@app.post("/api/governance/motions/{motion_id}/upvote", response_model=GovernanceReactionResponse)
async def upvote_governance_motion(
    motion_id: uuid.UUID,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    motion = _get_governance_motion_or_404(session, motion_id)
    return _set_governance_reaction(motion, current_user, session, GovernanceReactionType.UP.value)


@app.post("/api/governance/motions/{motion_id}/downvote", response_model=GovernanceReactionResponse)
async def downvote_governance_motion(
    motion_id: uuid.UUID,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    motion = _get_governance_motion_or_404(session, motion_id)
    return _set_governance_reaction(motion, current_user, session, GovernanceReactionType.DOWN.value)


@app.get("/api/governance/motions/{motion_id}/user-vote", response_model=GovernanceUserVoteResponse)
async def get_governance_motion_user_vote(
    motion_id: uuid.UUID,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    _get_governance_motion_or_404(session, motion_id)
    user_id = _actor_user_id(current_user)
    row = (
        session.query(GovernanceReaction)
        .filter(
            GovernanceReaction.motion_id == motion_id,
            GovernanceReaction.user_id == user_id,
        )
        .first()
    )
    return GovernanceUserVoteResponse(user_vote=row.direction if row else None)


@app.get("/api/governance/motions/{motion_id}/vote-counts", response_model=GovernanceVoteCountsResponse)
async def get_governance_motion_vote_counts(
    motion_id: uuid.UUID,
    session: Session = Depends(get_db),
):
    motion = _get_governance_motion_or_404(session, motion_id)
    return _governance_reaction_counts(motion)


@app.get("/api/governance/motions/{motion_id}/results", response_model=GovernanceVoteResultResponse)
async def get_governance_motion_results(
    motion_id: uuid.UUID,
    session: Session = Depends(get_db),
):
    motion = _get_governance_motion_or_404(session, motion_id)
    return GovernanceVoteResultResponse(**_governance_vote_result(motion))

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

# ============= HEALTH CHECK =============

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    try:
        # Check database
        async with db.async_pool.acquire() as conn:
            await conn.execute("SELECT 1")
        
        # Check Redis
        db.redis_client.ping()
        
        return {
            "status": "healthy",
            "database": "connected",
            "redis": "connected",
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        raise HTTPException(status_code=500, detail="Health check failed")

@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "name": "Democratic Economic System API",
        "version": "2.0.0",
        "description": "A comprehensive democratic economic system with UBI, stock market, insurance, and fiscal policy",
        "documentation": "/docs",
        "health": "/health"
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        reload=True
    )
