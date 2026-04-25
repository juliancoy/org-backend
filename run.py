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
        print(f"Warning: failed to pull prod image {requested_image}")
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
        "accessible image, or enable ORG_ALLOW_LOCAL_PROD_BUILD=true."
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
    frontend_host: str | None,
) -> dict:
    db_name = os.getenv("ORG_DB_NAME", "org")
    db_user = os.getenv("ORG_DB_USER", "org")
    db_password = os.getenv("ORG_DB_PASSWORD", "orgchange")
    db_port = int(os.getenv("ORG_DB_PORT", "5432"))
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

    allowed_origins_default = ",".join(
        [
            host
            for host in [
                frontend_host,
                os.getenv("ORG_EXTRA_ALLOWED_ORIGIN", "").strip() or None,
            ]
            if host
        ]
    )
    return {
        "REDIS_HOST": os.getenv("ORG_REDIS_HOST", redis_name),
        "REDIS_PORT": os.getenv("ORG_REDIS_PORT", "6379"),
        "REDIS_PASSWORD": os.getenv("ORG_REDIS_PASSWORD", ""),
        "PIDP_JWKS_URL": os.getenv("ORG_PIDP_JWKS_URL", f"http://{prefix}pidp-dev:8000/.well-known/jwks.json"),
        "PIDP_BASE_URL": os.getenv("ORG_PIDP_BASE_URL", f"http://{prefix}pidp-dev:8000"),
        "PIDP_JWT_ISSUER": os.getenv("PIDP_JWT_ISSUER", ""),
        "PIDP_JWT_AUDIENCE": os.getenv("PIDP_JWT_AUDIENCE", ""),
        "COCKROACH_DB_URL": cockroach_sync,
        "COCKROACH_ASYNC_URL": cockroach_async,
        "SPICEDB_HTTP_URL": os.getenv("SPICEDB_HTTP_URL", ""),
        "SPICEDB_PRESHARED_KEY": os.getenv("SPICEDB_PRESHARED_KEY", ""),
        "ORG_ADMIN_USER_IDS": os.getenv("ORG_ADMIN_USER_IDS", ""),
        "ORG_ADMIN_GROUP": os.getenv("ORG_ADMIN_GROUP", "admins"),
        "ORG_RESOURCE_ID": os.getenv("ORG_RESOURCE_ID", "portal"),
        "MODERATOR_EMAILS": os.getenv("MODERATOR_EMAILS", ""),
        "ENCRYPTION_KEY": os.getenv("ORG_ENCRYPTION_KEY", "temporary-key-please-change"),
        "ALLOWED_ORIGINS": os.getenv("ALLOWED_ORIGINS", allowed_origins_default),
        "FRONTEND_URL": os.getenv("FRONTEND_URL", frontend_host or ""),
        "ORG_BACKEND_URL": os.getenv("ORG_BACKEND_URL", ""),
        "ORG_INGEST_TOKEN": os.getenv("ORG_INGEST_TOKEN", ""),
        "WATCHFILES_FORCE_POLLING": "true",
    }


def run(prefix: str, network_name: str) -> None:
    docker_utils.ensure_network(network_name)

    prod_base = os.getenv("ORG_PROD_PUBLIC_BASE_URL")
    dev_base = os.getenv("ORG_DEV_PUBLIC_BASE_URL") or _derive_dev_base(prod_base)
    prod_frontend_host = _host_from_base(prod_base)
    dev_frontend_host = _host_from_base(dev_base) or prod_frontend_host

    postgres_name = prefix + "orgdb"
    redis_name = prefix + "org-redis"
    prod_name = prefix + "org"
    dev_name = prefix + "org-dev"

    env_base_prod = _common_env(prefix, postgres_name, redis_name, prod_frontend_host)
    env_base_dev = _common_env(prefix, postgres_name, redis_name, dev_frontend_host)

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

    prod_image = _resolve_prod_image()
    prod = {
        "image": prod_image,
        "name": prod_name,
        "environment": {
            **env_base_prod,
            "BACKEND_IMAGE_RUNNING": prod_image,
        },
        "network": network_name,
        "restart_policy": {"Name": "always"},
        "detach": True,
        "command": [
            "uvicorn",
            "org:app",
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
        "volumes": {str(current_dir): {"bind": container_app_dir, "mode": "rw"}},
        "environment": {
            **env_base_dev,
            "BACKEND_IMAGE_RUNNING": "dev-local-build",
        },
        "network": network_name,
        "restart_policy": {"Name": "always"},
        "detach": True,
        "command": [
            "uvicorn",
            "org:app",
            "--host",
            "0.0.0.0",
            "--port",
            "8001",
            "--reload",
            "--reload-dir",
            container_app_dir,
        ],
    }

    # Converge to this launcher's configuration even if legacy containers exist.
    for name in (prod_name, dev_name, postgres_name, redis_name, "port_test"):
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

    docker_utils.run_container(postgres)
    docker_utils.wait_for_db(network_name, db_url=f"{postgres_name}:5432", db_user=db_user)
    docker_utils.run_container(redis)
    docker_utils.wait_for_port(redis_name, 6379, network_name, retries=60, delay=2)
    docker_utils.run_container(prod)
    docker_utils.run_container(dev)


if __name__ == "__main__":
    if len(sys.argv) >= 3:
        prefix = sys.argv[1]
        network_name = sys.argv[2]
    else:
        prefix = ""
        network_name = "arkavo"
    run(prefix, network_name)
