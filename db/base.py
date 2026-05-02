import asyncpg
import redis
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


class Database:
    def __init__(
        self,
        *,
        database_url: str,
        async_db_url: str,
        redis_host: str,
        redis_port: int,
        redis_password: str,
        logger,
    ):
        self.database_url = database_url
        self.async_db_url = async_db_url
        self.redis_host = redis_host
        self.redis_port = redis_port
        self.redis_password = redis_password
        self.logger = logger
        self.engine = None
        self.SessionLocal = None
        self.redis_client = None
        self.async_pool = None

    async def connect(self):
        """Initialize database connections."""
        try:
            self.engine = create_engine(
                self.database_url,
                pool_size=20,
                max_overflow=30,
                pool_pre_ping=True,
                echo=False,
            )
            self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)

            self.async_pool = await asyncpg.create_pool(
                self.async_db_url,
                min_size=5,
                max_size=20,
                command_timeout=60,
            )

            self.redis_client = redis.Redis(
                host=self.redis_host,
                port=self.redis_port,
                password=self.redis_password if self.redis_password else None,
                decode_responses=True,
            )

            self.logger.info("Database connections established")
        except Exception as exc:
            self.logger.error(f"Failed to connect to database: {exc}")
            raise

    async def disconnect(self):
        """Close database connections."""
        if self.async_pool:
            await self.async_pool.close()
        if self.engine:
            self.engine.dispose()
        if self.redis_client:
            self.redis_client.close()
        self.logger.info("Database connections closed")

    def get_session(self):
        """Get database session for dependency injection."""
        session = self.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    async def get_async_conn(self):
        """Get async database connection."""
        async with self.async_pool.acquire() as conn:
            yield conn
