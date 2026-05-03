import os


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


def _parse_content_types_csv(value: str) -> set[str]:
    parsed = {
        item.strip().lower()
        for item in (value or "").split(",")
        if item.strip()
    }
    return parsed or {"image/jpeg", "image/png", "image/webp"}


def _env_truthy(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


_default_native_origins = "capacitor://localhost,http://localhost,http://127.0.0.1,https://localhost"
ALLOWED_ORIGINS = _parse_allowed_origins(
    ",".join(
        [
            os.environ.get("ALLOWED_ORIGINS", ""),
            os.environ.get("ORG_NATIVE_ALLOWED_ORIGINS", _default_native_origins),
        ]
    )
)

DATABASE_URL = os.environ.get(
    "COCKROACH_DB_URL",
    "cockroachdb://root@cockroach:9000/defaultdb?sslmode=disable"
)
ASYNC_DB_URL = os.environ.get(
    "COCKROACH_ASYNC_URL",
    "postgresql://root@cockroach:9000/defaultdb?sslmode=disable"
)
REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
REDIS_PORT = int(os.environ.get("REDIS_PORT", 6379))
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD", "")
PIDP_JWKS_URL = os.environ.get("PIDP_JWKS_URL", "http://pidp:8000/.well-known/jwks.json")
PIDP_BASE_URL = os.environ.get("PIDP_BASE_URL", "http://pidp:8000")
PIDP_APP_SLUG = (os.environ.get("PIDP_APP_SLUG", "code-collective") or "code-collective").strip() or "code-collective"
PIDP_JWT_ISSUER = os.environ.get("PIDP_JWT_ISSUER")
PIDP_JWT_AUDIENCE = os.environ.get("PIDP_JWT_AUDIENCE")
ORG_ALLOWED_PAT_SCOPES = {
    item.strip()
    for item in os.environ.get("ORG_ALLOWED_PAT_SCOPES", "org_portal,org_mcp,org_admin").split(",")
    if item.strip()
}
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
ORG_BUSINESS_CARD_S3_USE_SSL = os.environ.get("ORG_BUSINESS_CARD_S3_USE_SSL", "true").strip().lower() in {"1", "true", "yes", "on"}
ORG_BUSINESS_CARD_S3_SERVER_SIDE_ENCRYPTION = os.environ.get(
    "ORG_BUSINESS_CARD_S3_SERVER_SIDE_ENCRYPTION",
    "",
).strip()
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
ORG_MATRIX_AUTO_PROVISION_PUBLIC_EVENT_ROOMS = os.environ.get(
    "ORG_MATRIX_AUTO_PROVISION_PUBLIC_EVENT_ROOMS",
    "true",
).strip().lower() in {"1", "true", "yes", "on"}
ORG_CHAT_FEED_CACHE_TTL_SECONDS = max(
    3,
    int(os.environ.get("ORG_CHAT_FEED_CACHE_TTL_SECONDS", "15")),
)
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
ORG_RUNTIME_ROLE = os.environ.get("ORG_RUNTIME_ROLE", "all").strip().lower() or "all"
ORG_ENABLE_BACKGROUND_JOBS = _env_truthy("ORG_ENABLE_BACKGROUND_JOBS", default=True)
ORG_ENABLE_SAMPLE_DATA = _env_truthy("ORG_ENABLE_SAMPLE_DATA", default=False)
ORG_WORKER_LOCK_ENABLED = _env_truthy("ORG_WORKER_LOCK_ENABLED", default=False)
ORG_WORKER_LOCK_SECONDS = max(
    30,
    int(os.environ.get("ORG_WORKER_LOCK_SECONDS", "300")),
)
DEFAULT_UBI_INTERVAL_SECONDS = int(os.environ.get("UBI_INTERVAL_SECONDS", "60"))
DEFAULT_DENA_ANNUAL_RAW = os.environ.get("DENA_ANNUAL", "1")
DEFAULT_DENA_PRECISION = int(os.environ.get("DENA_PRECISION", "6"))
DEFAULT_UBI_ENTITY_TYPES = [
    item.strip()
    for item in os.environ.get("UBI_ENTITY_TYPES", "individual").split(",")
    if item.strip()
]
