import os
import subprocess
import sys
from pathlib import Path

import docker_utils

current_dir = Path(os.path.abspath(os.path.dirname(__file__)))
container_app_dir = "/app"

DEFAULT_PROD_IMAGE = "ghcr.io/juliancoy/org-backend:latest"
DEFAULT_POSTGRES_IMAGE = "postgres:15-alpine"
DEFAULT_REDIS_IMAGE = "redis:7.2-alpine"
DEFAULT_SMTP_RELAY_IMAGE = "boky/postfix:latest"
DEFAULT_COCKROACH_CERT_DIR = current_dir.parent / "OrgPortal" / "certs" / "cockroach"


def _parse_simple_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists() or not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        values[key] = value
    return values


def _apply_resend_defaults() -> None:
    # Prefer service-local env file when present, then repo-root fallback.
    env_candidates = [
        current_dir / ".env.resend",
        current_dir.parent / ".env.resend",
    ]
    file_values: dict[str, str] = {}
    for candidate in env_candidates:
        parsed = _parse_simple_env_file(candidate)
        if parsed:
            file_values = parsed
            break

    for key, value in file_values.items():
        os.environ.setdefault(key, value)

    resend_key = os.getenv("RESEND_API_KEY", "").strip()
    if not resend_key:
        return

    # Map a Resend API key into SMTP relay defaults, while honoring explicit env overrides.
    os.environ.setdefault("ORG_ENABLE_SMTP_RELAY", "true")
    os.environ.setdefault("ORG_SMTP_RELAYHOST", "smtp.resend.com")
    os.environ.setdefault("ORG_SMTP_RELAYHOST_PORT", "587")
    os.environ.setdefault("ORG_SMTP_RELAY_USERNAME", "resend")
    os.environ.setdefault("ORG_SMTP_RELAY_PASSWORD", resend_key)


def _apply_openai_defaults() -> None:
    # Prefer service-local env file when present, then repo-root fallback.
    env_candidates = [
        current_dir / ".env.openai",
        current_dir.parent / ".env.openai",
    ]
    file_values: dict[str, str] = {}
    for candidate in env_candidates:
        parsed = _parse_simple_env_file(candidate)
        if parsed:
            file_values = parsed
            break

    for key, value in file_values.items():
        os.environ.setdefault(key, value)


def _apply_matrix_defaults() -> None:
    # Prefer service-local env file when present, then repo-root fallback.
    env_candidates = [
        current_dir / ".env.matrix",
        current_dir.parent / ".env.matrix",
    ]
    file_values: dict[str, str] = {}
    for candidate in env_candidates:
        parsed = _parse_simple_env_file(candidate)
        if parsed:
            file_values = parsed
            break

    for key, value in file_values.items():
        os.environ.setdefault(key, value)


def _apply_org_defaults() -> None:
    # Prefer service-local env file when present, then repo-root fallback.
    env_candidates = [
        current_dir / ".env.org",
        current_dir.parent / ".env.org",
    ]
    file_values: dict[str, str] = {}
    for candidate in env_candidates:
        parsed = _parse_simple_env_file(candidate)
        if parsed:
            file_values = parsed
            break

    for key, value in file_values.items():
        os.environ.setdefault(key, value)


def _env_truthy(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _docker_image_exists(image_ref: str) -> bool:
    proc = subprocess.run(
        ["docker", "image", "inspect", image_ref],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc.returncode == 0


def _ensure_dev_image(image_ref: str) -> None:
    if _docker_image_exists(image_ref):
        return
    subprocess.check_call(
        [
            "docker",
            "build",
            "-f",
            str(current_dir / "Dockerfile"),
            "-t",
            image_ref,
            str(current_dir),
        ]
    )


def _resolve_prod_image() -> str:
    return (os.getenv("ORG_PROD_IMAGE") or "").strip() or DEFAULT_PROD_IMAGE


def _ensure_prod_image_available(requested_image: str) -> tuple[str, str]:
    skip_pull = _env_truthy("ORG_SKIP_PROD_PULL", default=False)
    fallback_build = _env_truthy("ORG_ALLOW_LOCAL_PROD_BUILD", default=True)

    if not skip_pull:
        pull_proc = subprocess.run(["docker", "pull", requested_image])
        if pull_proc.returncode == 0:
            return requested_image, "pulled"
        print(
            f"Warning: failed to pull prod image {requested_image}. "
            "Ensure org-backend release workflow has published this tag and "
            "the GHCR package is readable by this host."
        )
    else:
        print("Skipping prod image pull because ORG_SKIP_PROD_PULL is enabled")

    if _docker_image_exists(requested_image):
        print(f"Using cached local prod image: {requested_image}")
        return requested_image, "local-cache"

    if fallback_build:
        local_tag = os.getenv("ORG_PROD_LOCAL_IMAGE", "org-backend-prod:local")
        print(
            "Prod image unavailable via registry/local cache; "
            f"building local fallback image as {local_tag}"
        )
        subprocess.check_call(
            [
                "docker",
                "build",
                "-f",
                str(current_dir / "Dockerfile"),
                "-t",
                local_tag,
                str(current_dir),
            ]
        )
        return local_tag, "local-build-fallback"

    raise RuntimeError(
        "Unable to start org prod container: registry pull failed and no local image exists. "
        "Either make image readable, run `docker login ghcr.io`, set ORG_PROD_IMAGE to an "
        "accessible image (for example `ghcr.io/juliancoy/org-backend:sha-<commit>`), "
        "or enable ORG_ALLOW_LOCAL_PROD_BUILD=true."
    )


def _normalize_public_base(url: str | None) -> str | None:
    value = (url or "").strip()
    if not value:
        return None
    if not value.startswith(("http://", "https://")):
        value = "https://" + value
    if not value.endswith("/"):
        value += "/"
    return value


def _derive_dev_base(prod_base: str | None) -> str | None:
    normalized = _normalize_public_base(prod_base)
    if not normalized:
        return None
    if "://org." in normalized:
        return normalized.replace("://org.", "://dev.org.", 1)
    return normalized


def _host_from_base(url: str | None) -> str | None:
    normalized = _normalize_public_base(url)
    if not normalized:
        return None
    host = normalized.split("://", 1)[1].strip("/")
    return host or None


def _common_env(
    prefix: str,
    postgres_name: str,
    redis_name: str,
    smtp_name: str,
    frontend_host: str | None,
) -> dict:
    db_name = os.getenv("ORG_DB_NAME", "org")
    db_user = os.getenv("ORG_DB_USER", "org")
    db_password = os.getenv("ORG_DB_PASSWORD", "orgchange")
    db_port = int(os.getenv("ORG_DB_PORT", "5432"))
    use_cockroach = _env_truthy("ORG_USE_COCKROACH", default=True)
    if use_cockroach:
        cockroach_host = os.getenv("ORG_COCKROACH_HOST", f"{prefix}cockroach")
        cockroach_port = int(os.getenv("ORG_COCKROACH_SQL_PORT", "26257"))
        cockroach_db = os.getenv("ORG_COCKROACH_DB", db_name)
        cockroach_cert_dir = os.getenv("ORG_COCKROACH_CERT_CONTAINER_DIR", "/cockroach-certs")
        cockroach_query = (
            "sslmode=verify-full"
            f"&sslrootcert={cockroach_cert_dir}/ca.crt"
            f"&sslcert={cockroach_cert_dir}/client.root.crt"
            f"&sslkey={cockroach_cert_dir}/client.root.key"
        )
        cockroach_sync = (
            os.getenv("COCKROACH_DB_URL")
            or f"cockroachdb://root@{cockroach_host}:{cockroach_port}/{cockroach_db}?{cockroach_query}"
        )
        cockroach_async = (
            os.getenv("COCKROACH_ASYNC_URL")
            or f"postgresql://root@{cockroach_host}:{cockroach_port}/{cockroach_db}?{cockroach_query}"
        )
    else:
        cockroach_sync = (
            os.getenv("COCKROACH_DB_URL")
            or (
                f"postgresql+psycopg2://{db_user}:{db_password}"
                f"@{postgres_name}:{db_port}/{db_name}"
            )
        )
        cockroach_async = (
            os.getenv("COCKROACH_ASYNC_URL")
            or (
                f"postgresql://{db_user}:{db_password}"
                f"@{postgres_name}:{db_port}/{db_name}"
            )
        )

    default_native_origins = (
        os.getenv("ORG_NATIVE_ALLOWED_ORIGINS")
        or "capacitor://localhost,http://localhost,http://127.0.0.1,https://localhost"
    )
    origin_candidates: list[str] = []
    if frontend_host:
        if frontend_host.startswith(("http://", "https://", "capacitor://")):
            origin_candidates.append(frontend_host)
        else:
            origin_candidates.append(f"https://{frontend_host}")
    extra_allowed_origin = os.getenv("ORG_EXTRA_ALLOWED_ORIGIN", "").strip()
    if extra_allowed_origin:
        origin_candidates.append(extra_allowed_origin)
    origin_candidates.extend([item.strip() for item in default_native_origins.split(",") if item.strip()])
    allowed_origins_default = ",".join(origin_candidates)
    smtp_relay_enabled = _env_truthy("ORG_ENABLE_SMTP_RELAY", default=True)
    smtp_default_host = smtp_name if smtp_relay_enabled else ""
    smtp_starttls_default = "false" if smtp_relay_enabled else "true"
    business_card_storage_dir = os.getenv(
        "ORG_BUSINESS_CARD_STORAGE_DIR",
        "/var/lib/org/business-cards",
    )
    business_card_storage_backend = os.getenv(
        "ORG_BUSINESS_CARD_STORAGE_BACKEND",
        "s3",
    )

    return {
        "REDIS_HOST": os.getenv("ORG_REDIS_HOST", redis_name),
        "REDIS_PORT": os.getenv("ORG_REDIS_PORT", "6379"),
        "REDIS_PASSWORD": os.getenv("ORG_REDIS_PASSWORD", ""),
        "PIDP_JWKS_URL": os.getenv("ORG_PIDP_JWKS_URL", f"http://{prefix}pidp-dev:8000/.well-known/jwks.json"),
        "PIDP_BASE_URL": os.getenv("ORG_PIDP_BASE_URL", f"http://{prefix}pidp-dev:8000"),
        "PIDP_APP_SLUG": os.getenv("ORG_PIDP_APP_SLUG", "code-collective"),
        "PIDP_JWT_ISSUER": os.getenv("PIDP_JWT_ISSUER", ""),
        "PIDP_JWT_AUDIENCE": os.getenv("PIDP_JWT_AUDIENCE", ""),
        "COCKROACH_DB_URL": cockroach_sync,
        "COCKROACH_ASYNC_URL": cockroach_async,
        "SPICEDB_HTTP_URL": os.getenv("SPICEDB_HTTP_URL", ""),
        "SPICEDB_PRESHARED_KEY": os.getenv("SPICEDB_PRESHARED_KEY", ""),
        "ORG_SYSADMIN_USER_IDS": os.getenv("ORG_SYSADMIN_USER_IDS", ""),
        "ORG_SYSADMIN_EMAILS": os.getenv("ORG_SYSADMIN_EMAILS", ""),
        "ORG_SYSADMIN_GROUP": os.getenv("ORG_SYSADMIN_GROUP", "admins"),
        "ORG_SYSADMIN_RESOURCE_ID": os.getenv("ORG_SYSADMIN_RESOURCE_ID", "portal"),
        "MODERATOR_EMAILS": os.getenv("MODERATOR_EMAILS", ""),
        "ENCRYPTION_KEY": os.getenv("ORG_ENCRYPTION_KEY", "temporary-key-please-change"),
        "ALLOWED_ORIGINS": os.getenv("ALLOWED_ORIGINS", allowed_origins_default),
        "FRONTEND_URL": os.getenv("FRONTEND_URL", frontend_host or ""),
        "ORG_BACKEND_URL": os.getenv("ORG_BACKEND_URL", ""),
        "ORG_INGEST_TOKEN": os.getenv("ORG_INGEST_TOKEN", ""),
        "ORG_PUBLIC_CALENDAR_FEEDS": os.getenv(
            "ORG_PUBLIC_CALENDAR_FEEDS",
            "https://codecollective.us/baltimore/upcoming_events.json",
        ),
        "ORG_PUBLIC_CALENDAR_PULL_INTERVAL_SECONDS": os.getenv(
            "ORG_PUBLIC_CALENDAR_PULL_INTERVAL_SECONDS",
            "900",
        ),
        "ORG_PUBLIC_CALENDAR_PULL_ENABLED": os.getenv(
            "ORG_PUBLIC_CALENDAR_PULL_ENABLED",
            "true",
        ),
        "ORG_MATRIX_HOMESERVER_URL": os.getenv("ORG_MATRIX_HOMESERVER_URL", "http://synapse:8008"),
        "ORG_MATRIX_SERVER_NAME": os.getenv("ORG_MATRIX_SERVER_NAME", "matrix.arkavo.org"),
        "ORG_MATRIX_ADMIN_TOKEN": os.getenv("ORG_MATRIX_ADMIN_TOKEN", ""),
        "ORG_MATRIX_PASSWORD_SECRET": os.getenv("ORG_MATRIX_PASSWORD_SECRET", ""),
        "ORG_MATRIX_AUTO_PROVISION_PUBLIC_ORG_ROOMS": os.getenv(
            "ORG_MATRIX_AUTO_PROVISION_PUBLIC_ORG_ROOMS",
            "true",
        ),
        "ORG_ALLOWED_PAT_SCOPES": os.getenv("ORG_ALLOWED_PAT_SCOPES", "org_portal,org_mcp,org_admin"),
        "ORG_BUSINESS_CARD_MAX_BYTES": os.getenv("ORG_BUSINESS_CARD_MAX_BYTES", str(8 * 1024 * 1024)),
        "ORG_BUSINESS_CARD_ALLOWED_CONTENT_TYPES": os.getenv(
            "ORG_BUSINESS_CARD_ALLOWED_CONTENT_TYPES",
            "image/jpeg,image/png,image/webp",
        ),
        "ORG_BUSINESS_CARD_OCR_PROVIDER": os.getenv("ORG_BUSINESS_CARD_OCR_PROVIDER", "openai"),
        "ORG_BUSINESS_CARD_OCR_MODEL": os.getenv("ORG_BUSINESS_CARD_OCR_MODEL", "gpt-4.1-mini"),
        "ORG_BUSINESS_CARD_STORAGE_ENABLED": os.getenv("ORG_BUSINESS_CARD_STORAGE_ENABLED", "true"),
        "ORG_BUSINESS_CARD_STORAGE_BACKEND": business_card_storage_backend,
        "ORG_BUSINESS_CARD_STORAGE_DIR": business_card_storage_dir,
        "ORG_BUSINESS_CARD_S3_ENDPOINT_URL": os.getenv(
            "ORG_BUSINESS_CARD_S3_ENDPOINT_URL",
            "http://minio:9000",
        ),
        "ORG_BUSINESS_CARD_S3_BUCKET": os.getenv(
            "ORG_BUSINESS_CARD_S3_BUCKET",
            "org-business-cards",
        ),
        "ORG_BUSINESS_CARD_S3_REGION": os.getenv(
            "ORG_BUSINESS_CARD_S3_REGION",
            "us-east-1",
        ),
        "ORG_BUSINESS_CARD_S3_ACCESS_KEY": os.getenv(
            "ORG_BUSINESS_CARD_S3_ACCESS_KEY",
            os.getenv("MINIO_ROOT_USER", "minio"),
        ),
        "ORG_BUSINESS_CARD_S3_SECRET_KEY": os.getenv(
            "ORG_BUSINESS_CARD_S3_SECRET_KEY",
            os.getenv("MINIO_ROOT_PASSWORD", "changeme"),
        ),
        "ORG_BUSINESS_CARD_S3_USE_SSL": os.getenv(
            "ORG_BUSINESS_CARD_S3_USE_SSL",
            "false",
        ),
        "ORG_BUSINESS_CARD_S3_PREFIX": os.getenv(
            "ORG_BUSINESS_CARD_S3_PREFIX",
            "business-cards",
        ),
        "ORG_OPENAI_API_KEY": os.getenv("ORG_OPENAI_API_KEY", ""),
        "ORG_OPENAI_API_BASE_URL": os.getenv("ORG_OPENAI_API_BASE_URL", "https://api.openai.com/v1"),
        "ORG_SMTP_HOST": os.getenv("ORG_SMTP_HOST", smtp_default_host),
        "ORG_SMTP_PORT": os.getenv("ORG_SMTP_PORT", "587"),
        "ORG_SMTP_USERNAME": os.getenv("ORG_SMTP_USERNAME", ""),
        "ORG_SMTP_PASSWORD": os.getenv("ORG_SMTP_PASSWORD", ""),
        "ORG_SMTP_FROM": os.getenv("ORG_SMTP_FROM", "noreply@arkavo.org"),
        "ORG_SMTP_STARTTLS": os.getenv("ORG_SMTP_STARTTLS", smtp_starttls_default),
        "ORG_PORTAL_BASE_URL": os.getenv("ORG_PORTAL_BASE_URL", ""),
        "ORG_ENABLE_BACKGROUND_JOBS": os.getenv("ORG_ENABLE_BACKGROUND_JOBS", "true"),
        "ORG_ENABLE_SAMPLE_DATA": os.getenv("ORG_ENABLE_SAMPLE_DATA", "false"),
        "ORG_WORKER_LOCK_ENABLED": os.getenv("ORG_WORKER_LOCK_ENABLED", "false"),
        "ORG_WORKER_LOCK_SECONDS": os.getenv("ORG_WORKER_LOCK_SECONDS", "300"),
        "WATCHFILES_FORCE_POLLING": "true",
    }


def run(prefix: str, network_name: str) -> None:
    _apply_org_defaults()
    _apply_resend_defaults()
    _apply_openai_defaults()
    _apply_matrix_defaults()
    docker_utils.ensure_network(network_name)

    prod_base = os.getenv("ORG_PROD_PUBLIC_BASE_URL")
    dev_base = os.getenv("ORG_DEV_PUBLIC_BASE_URL") or _derive_dev_base(prod_base)
    prod_frontend_host = _host_from_base(prod_base)
    dev_frontend_host = _host_from_base(dev_base) or prod_frontend_host

    postgres_name = prefix + "orgdb"
    redis_name = prefix + "org-redis"
    smtp_name = prefix + "org-smtp-relay"
    prod_name = prefix + "org"
    dev_name = prefix + "org-dev"
    worker_name = prefix + "org-worker"
    business_card_storage_dir = os.getenv(
        "ORG_BUSINESS_CARD_STORAGE_DIR",
        "/var/lib/org/business-cards",
    )
    business_card_storage_volume = prefix + "ORG_BUSINESS_CARD_STORAGE"
    use_cockroach = _env_truthy("ORG_USE_COCKROACH", default=True)
    cockroach_cert_dir = Path(os.getenv("ORG_COCKROACH_CERT_DIR", str(DEFAULT_COCKROACH_CERT_DIR)))
    cockroach_cert_mount = {
        str(cockroach_cert_dir): {
            "bind": os.getenv("ORG_COCKROACH_CERT_CONTAINER_DIR", "/cockroach-certs"),
            "mode": "ro",
        }
    } if use_cockroach else {}

    env_base_prod = _common_env(prefix, postgres_name, redis_name, smtp_name, prod_frontend_host)
    env_base_dev = _common_env(prefix, postgres_name, redis_name, smtp_name, dev_frontend_host)

    db_name = os.getenv("ORG_DB_NAME", "org")
    db_user = os.getenv("ORG_DB_USER", "org")
    db_password = os.getenv("ORG_DB_PASSWORD", "orgchange")
    postgres = {
        "image": os.getenv("ORG_POSTGRES_IMAGE", DEFAULT_POSTGRES_IMAGE),
        "detach": True,
        "name": postgres_name,
        "network": network_name,
        "restart_policy": {"Name": "always"},
        "user": "postgres",
        "environment": {
            "POSTGRES_DB": db_name,
            "POSTGRES_USER": db_user,
            "POSTGRES_PASSWORD": db_password,
        },
        "volumes": {
            prefix + "ORG_POSTGRES": {"bind": "/var/lib/postgresql/data", "mode": "rw"},
        },
        "healthcheck": {
            "test": ["CMD-SHELL", "pg_isready -U " + db_user],
            "interval": 5000000000,
            "timeout": 5000000000,
            "retries": 20,
        },
    }

    redis = {
        "image": os.getenv("ORG_REDIS_IMAGE", DEFAULT_REDIS_IMAGE),
        "detach": True,
        "name": redis_name,
        "network": network_name,
        "restart_policy": {"Name": "always"},
        "volumes": {
            prefix + "ORG_REDIS": {"bind": "/data", "mode": "rw"},
        },
        "command": ["redis-server", "--appendonly", "yes"],
    }

    smtp_relay_enabled = _env_truthy("ORG_ENABLE_SMTP_RELAY", default=True)
    relayhost = os.getenv("ORG_SMTP_RELAYHOST", "").strip()
    relayhost_port = os.getenv("ORG_SMTP_RELAYHOST_PORT", "587").strip()
    relayhost_target = f"{relayhost}:{relayhost_port}" if relayhost else ""
    smtp_relay = {
        "image": os.getenv("ORG_SMTP_RELAY_IMAGE", DEFAULT_SMTP_RELAY_IMAGE),
        "detach": True,
        "name": smtp_name,
        "network": network_name,
        "restart_policy": {"Name": "always"},
        "environment": {
            "HOSTNAME": os.getenv("ORG_SMTP_HOSTNAME", "org-smtp-relay.local"),
            "ALLOWED_SENDER_DOMAINS": os.getenv("ORG_SMTP_ALLOWED_SENDER_DOMAINS", "arkavo.org"),
            "RELAYHOST": relayhost_target,
            "RELAYHOST_USERNAME": os.getenv("ORG_SMTP_RELAY_USERNAME", ""),
            "RELAYHOST_PASSWORD": os.getenv("ORG_SMTP_RELAY_PASSWORD", ""),
        },
    }

    prod_image = _resolve_prod_image()
    prod = {
        "image": prod_image,
        "name": prod_name,
        "volumes": {
            business_card_storage_volume: {
                "bind": business_card_storage_dir,
                "mode": "rw",
            },
            **cockroach_cert_mount,
        },
        "environment": {
            **env_base_prod,
            "BACKEND_IMAGE_RUNNING": prod_image,
            "ORG_RUNTIME_ROLE": "api",
        },
        "network": network_name,
        "restart_policy": {"Name": "always"},
        "detach": True,
        "command": [
            "uvicorn",
            "main:app",
            "--host",
            "0.0.0.0",
            "--port",
            "8001",
        ],
    }

    dev_image = os.getenv("ORG_DEV_IMAGE", "org-backend-dev")
    _ensure_dev_image(dev_image)
    dev = {
        "image": dev_image,
        "name": dev_name,
        "volumes": {
            str(current_dir): {"bind": container_app_dir, "mode": "rw"},
            business_card_storage_volume: {
                "bind": business_card_storage_dir,
                "mode": "rw",
            },
            **cockroach_cert_mount,
        },
        "environment": {
            **env_base_dev,
            "BACKEND_IMAGE_RUNNING": "dev-local-build",
            "ORG_RUNTIME_ROLE": "api",
        },
        "network": network_name,
        "restart_policy": {"Name": "always"},
        "detach": True,
        "command": [
            "uvicorn",
            "main:app",
            "--host",
            "0.0.0.0",
            "--port",
            "8001",
            "--log-config",
            "/app/uvicorn_log_config.json",
            "--reload",
            "--reload-dir",
            container_app_dir,
        ],
    }
    worker = {
        "image": dev_image,
        "name": worker_name,
        "volumes": {
            str(current_dir): {"bind": container_app_dir, "mode": "rw"},
            business_card_storage_volume: {
                "bind": business_card_storage_dir,
                "mode": "rw",
            },
            **cockroach_cert_mount,
        },
        "environment": {
            **env_base_dev,
            "BACKEND_IMAGE_RUNNING": "dev-local-build-worker",
            "ORG_RUNTIME_ROLE": "worker",
        },
        "network": network_name,
        "restart_policy": {"Name": "always"},
        "detach": True,
        "command": ["python", "worker.py"],
    }

    # Converge to this launcher's configuration even if legacy containers exist.
    for name in (prod_name, dev_name, worker_name, postgres_name, redis_name, smtp_name, "port_test"):
        try:
            container = docker_utils.DOCKER_CLIENT.containers.get(name)
            container.stop()
            container.remove(force=True)
        except Exception:
            pass

    resolved_prod_image, image_source = _ensure_prod_image_available(prod_image)
    print(f"Using org prod image: {resolved_prod_image} ({image_source})")
    print(f"Org prod base: {_normalize_public_base(prod_base) or 'unchanged'}")
    print(f"Org dev base: {_normalize_public_base(dev_base) or 'unchanged'}")
    prod["image"] = resolved_prod_image
    prod["environment"]["BACKEND_IMAGE_RUNNING"] = resolved_prod_image

    if not use_cockroach:
        docker_utils.run_container(postgres)
        docker_utils.wait_for_db(network_name, db_url=f"{postgres_name}:5432", db_user=db_user)
    docker_utils.run_container(redis)
    docker_utils.wait_for_port(redis_name, 6379, network_name, retries=60, delay=2)
    if smtp_relay_enabled:
        docker_utils.run_container(smtp_relay)
        docker_utils.wait_for_port(smtp_name, 587, network_name, retries=60, delay=2)
    docker_utils.run_container(prod)
    docker_utils.run_container(dev)
    docker_utils.run_container(worker)


if __name__ == "__main__":
    if len(sys.argv) >= 3:
        prefix = sys.argv[1]
        network_name = sys.argv[2]
    else:
        prefix = ""
        network_name = "arkavo"
    run(prefix, network_name)
