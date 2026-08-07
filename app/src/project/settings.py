# pyright: reportArgumentType=false, reportCallIssue=false, reportAssignmentType=false
import logging
from collections.abc import Callable
from datetime import timedelta
from typing import TypedDict

import environ
import structlog
from celery.schedules import crontab
from django.utils.log import CallbackFilter
from kombu import Queue
from structlog.typing import Processor, WrappedLogger

root = environ.Path(__file__) - 2

env = environ.Env(DEBUG=(bool, False))

env.read_env(root("../../.env"), overwrite=False)

ENV = env("ENV")


SECRET_KEY = env("SECRET_KEY")
DEBUG = env("DEBUG")
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=["localhost"])
ROOT_URLCONF = "project.urls"
WSGI_APPLICATION = "project.wsgi.application"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


INSTALLED_APPS = [
    "django_prometheus",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django_extensions",
    "django_probes",
    "django_structlog",
    "abstract_block_dumper",
    "project.core",
    "apps.notifications",
    "apps.extrinsics",
    "apps.metagraph",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django_structlog.middlewares.RequestMiddleware",
]

AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
]

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [root("project/templates")],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]


DATABASES = {"default": env.db_url("DATABASE_URL")}


AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_L10N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = "/var/static"
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage",
    },
}
MEDIA_URL = "/media/"
MEDIA_ROOT = env("MEDIA_ROOT")

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# Security
# redirect HTTP to HTTPS
if env.bool("HTTPS_REDIRECT") and not DEBUG:
    SECURE_SSL_REDIRECT = True
    SECURE_REDIRECT_EXEMPT = []  # type: ignore
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
else:
    SECURE_SSL_REDIRECT = False

if CORS_ENABLED := env.bool("CORS_ENABLED"):
    INSTALLED_APPS.append("corsheaders")
    MIDDLEWARE = ["corsheaders.middleware.CorsMiddleware", *MIDDLEWARE]
    CORS_ALLOWED_ORIGINS = env.list("CORS_ALLOWED_ORIGINS", default=[])
    CORS_ALLOWED_ORIGIN_REGEXES = env.list("CORS_ALLOWED_ORIGIN_REGEXES", default=[])
    CORS_ALLOW_ALL_ORIGINS = env.bool("CORS_ALLOW_ALL_ORIGINS")

REDIS_HOST = env("REDIS_HOST")
REDIS_PORT = env.int("REDIS_PORT")
REDIS_URL = f"redis://{REDIS_HOST}:{REDIS_PORT}"

CELERY_BROKER_URL = env("CELERY_BROKER_URL")
CELERY_RESULT_BACKEND = CELERY_BROKER_URL
CELERY_RESULT_EXPIRES = int(timedelta(days=1).total_seconds())
CELERY_MESSAGE_COMPRESSION = "gzip"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"

CELERY_TASK_CREATE_MISSING_QUEUES = False
CELERY_TASK_QUEUES = (Queue("celery"), Queue("metagraph"))
CELERY_TASK_ANNOTATIONS = {"*": {"acks_late": True, "reject_on_worker_lost": True}}
CELERY_TASK_ROUTES = {
    "apps.metagraph.block_tasks.store_metagraph": {"queue": "metagraph"},
    "*": {"queue": "celery"},
}
CELERY_TASK_TIME_LIMIT = int(timedelta(minutes=5).total_seconds())
CELERY_BEAT_SCHEDULE = {
    "ingest-validator-apy-epochs": {
        "task": "apps.metagraph.tasks.ingest_validator_apy_epochs",
        # Incremental: each tick scans only new snapshot ids plus a fixed
        # overlap, so the 15-min cadence is cheap (the old full MV refresh
        # was hourly only because it burned ~3 min of CPU per run).
        "schedule": timedelta(minutes=15),
    },
    "update-snapshot-health-metrics": {
        "task": "apps.metagraph.tasks.update_snapshot_health_metrics",
        "schedule": timedelta(minutes=72),
    },
    "cleanup-expired-data": {
        "task": "project.core.tasks.cleanup_expired_data",
        "schedule": crontab(hour=3, minute=30),  # daily, low-traffic UTC hour
    },
}
CELERY_TASK_ALWAYS_EAGER = env.bool("CELERY_TASK_ALWAYS_EAGER")
CELERY_TASK_EAGER_PROPAGATES = env.bool("CELERY_TASK_EAGER_PROPAGATES", default=False)

CELERY_WORKER_SEND_TASK_EVENTS = True
CELERY_TASK_SEND_SENT_EVENT = True
CELERY_WORKER_PREFETCH_MULTIPLIER = env.int("CELERY_WORKER_PREFETCH_MULTIPLIER")
CELERY_BROKER_POOL_LIMIT = env.int("CELERY_BROKER_POOL_LIMIT")
CELERY_WORKER_MAX_TASKS_PER_CHILD = env.int("CELERY_WORKER_MAX_TASKS_PER_CHILD", default=50)

DJANGO_STRUCTLOG_CELERY_ENABLED = True

LOG_LEVEL = env("LOG_LEVEL", default="INFO")


def exclude_pidbox_notifications(record: logging.LogRecord) -> bool:
    """Exclude Flower worker-ping notifications from Celery logs."""
    return "pidbox received method" not in record.getMessage()


class StructlogEnvProcessor:
    """Add env vars to structlog event dict, so that they become part of all log messages."""

    def __init__(self, vars: list[str]):
        self.env_data = {var.lower(): value for var in vars if (value := env(var, default=None))}

    def __call__(self, logger, method_name, event_dict):
        return event_dict | self.env_data


LOGGING_ENV_VARS_PROCESSOR = StructlogEnvProcessor(vars=["INSTANCE_ID_SUBST"])

LOGGING_CALLSITE_PARAMETERS_PROCESSOR = structlog.processors.CallsiteParameterAdder(
    [
        structlog.processors.CallsiteParameter.PATHNAME,
        structlog.processors.CallsiteParameter.FUNC_NAME,
        structlog.processors.CallsiteParameter.LINENO,
    ]
)

LOGGING_FOREIGN_PRE_CHAIN = [
    structlog.stdlib.add_log_level,
    structlog.stdlib.add_logger_name,
    LOGGING_ENV_VARS_PROCESSOR,
    structlog.processors.TimeStamper(fmt="iso"),
    structlog.processors.format_exc_info,
    LOGGING_CALLSITE_PARAMETERS_PROCESSOR,
]

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "console": {
            "()": structlog.stdlib.ProcessorFormatter,
            "processor": structlog.dev.ConsoleRenderer(),
            "foreign_pre_chain": LOGGING_FOREIGN_PRE_CHAIN,
        },
        "json": {
            "()": structlog.stdlib.ProcessorFormatter,
            "processor": structlog.processors.JSONRenderer(),
            "foreign_pre_chain": LOGGING_FOREIGN_PRE_CHAIN,
        },
    },
    "filters": {
        "exclude_pidbox_notifications": {
            # these are notifications about Flower pinging workers
            "()": CallbackFilter,
            "callback": exclude_pidbox_notifications,
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "console" if DEBUG else "json",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": LOG_LEVEL,
    },
    "loggers": {
        "django": {
            "level": LOG_LEVEL,
            "propagate": True,
        },
        "django_structlog": {
            "level": LOG_LEVEL,
            "propagate": True,
        },
        "celery": {
            "level": LOG_LEVEL,
            "propagate": True,
        },
        "psycopg.pq": {
            "propagate": False,
        },
        "parso": {
            "level": "INFO",
        },
        "websockets": {
            "level": "WARNING",
        },
    },
}


class _StructlogConfiguration(TypedDict):
    processors: list[Processor]
    logger_factory: Callable[..., WrappedLogger]
    cache_logger_on_first_use: bool


STRUCTLOG_CONFIGURATION: _StructlogConfiguration = {
    "processors": [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.filter_by_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        LOGGING_ENV_VARS_PROCESSOR,
        LOGGING_CALLSITE_PARAMETERS_PROCESSOR,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
    ],
    "logger_factory": structlog.stdlib.LoggerFactory(),
    "cache_logger_on_first_use": True,
}
structlog.configure(**STRUCTLOG_CONFIGURATION)

SENTRY_DSN = env("SENTRY_DSN")
if SENTRY_DSN:
    import sentry_sdk
    from sentry_sdk.integrations.celery import CeleryIntegration
    from sentry_sdk.integrations.django import DjangoIntegration
    from sentry_sdk.integrations.logging import LoggingIntegration, ignore_logger
    from sentry_sdk.integrations.redis import RedisIntegration

    SENTRY_ENVIRONMENT = env("SENTRY_ENVIRONMENT", default=ENV)
    sentry_sdk.init(  # type: ignore[abstract]
        dsn=SENTRY_DSN,
        environment=SENTRY_ENVIRONMENT,
        ignore_errors=[
            KeyboardInterrupt,
            SystemExit,
            BrokenPipeError,
        ],
        integrations=[
            DjangoIntegration(),
            CeleryIntegration(),
            RedisIntegration(),
            LoggingIntegration(
                level=logging.INFO,
                event_level=logging.ERROR,
            ),
        ],
    )
    ignore_logger("django.security.DisallowedHost")
    ignore_logger("django_structlog.celery.receivers")


PROMETHEUS_EXPORT_MIGRATIONS = env.bool("PROMETHEUS_EXPORT_MIGRATIONS")

# Bittensor / Block Dumper

BITTENSOR_NETWORK = env.str("BITTENSOR_NETWORK", default="finney")
BITTENSOR_SECONDS_PER_BLOCK = env.int("BITTENSOR_SECONDS_PER_BLOCK", default=12)

# Reconnect policy for the long-running sync daemons. Opening a chain connection is a
# network call that fails exactly when the endpoint is already unhealthy, so the daemons
# retry it with exponential backoff instead of dying. The failure at
# ALERT_AFTER_ATTEMPTS is logged at error level once per outage, which reaches Sentry.
BITTENSOR_RECONNECT_INITIAL_DELAY_SECONDS = env.int("BITTENSOR_RECONNECT_INITIAL_DELAY_SECONDS", default=1)
BITTENSOR_RECONNECT_MAX_DELAY_SECONDS = env.int("BITTENSOR_RECONNECT_MAX_DELAY_SECONDS", default=60)
BITTENSOR_RECONNECT_ALERT_AFTER_ATTEMPTS = env.int("BITTENSOR_RECONNECT_ALERT_AFTER_ATTEMPTS", default=5)
PYLON_URL = env("PYLON_URL", default="http://localhost:8090")

BLOCK_DUMPER_START_FROM_BLOCK = "current"
BLOCK_DUMPER_POLL_INTERVAL = 5
BLOCK_TASK_RETRY_BACKOFF = 1
BLOCK_DUMPER_MAX_ATTEMPTS = 3
BLOCK_TASK_MAX_RETRY_DELAY_MINUTES = 1440

# Metagraph
METAGRAPH_NETUIDS: list[int] | None = env.list("METAGRAPH_NETUIDS", default=[], cast=int) or None
METAGRAPH_LITE = env.bool("METAGRAPH_LITE", default=False)

# Data retention (docs/superpowers/specs/2026-07-07-data-retention-design.md).
# Two windows: DATA_RETENTION_DAYS keeps non-validator neuron snapshots (+
# their mechanism metrics); DATA_RETENTION_BULK_DAYS is a shorter window for
# the bulk tables (weight, bond, collateral, extrinsics), which grow much
# faster and have no analytics reading old rows. Validator neuron snapshots
# (+ their mechanism metrics) are kept forever regardless of either window,
# because the APY materialized views read them.
DATA_RETENTION_DAYS = env.int("DATA_RETENTION_DAYS", default=90)
DATA_RETENTION_BULK_DAYS = env.int("DATA_RETENTION_BULK_DAYS", default=40)
DATA_RETENTION_BATCH_SIZE = env.int("DATA_RETENTION_BATCH_SIZE", default=20000)
DATA_RETENTION_BATCH_SLEEP_SECONDS = env.float("DATA_RETENTION_BATCH_SLEEP_SECONDS", default=0.2)


SENTINEL_STORAGES = {
    "local": {
        "BACKEND_NAME": "fsspec-local",
        "OPTIONS": {"base_path": str(MEDIA_ROOT)},
    },
    "s3": {
        "BACKEND_NAME": "fsspec-s3",
        "OPTIONS": {
            "bucket": env("SENTINEL_STORAGE_S3_BUCKET", default=""),
            "base_path": env("SENTINEL_STORAGE_S3_BASE_PATH", default=""),
            "aws_region": env("SENTINEL_STORAGE_S3_AWS_REGION", default=None),
            "aws_access_key_id": env("SENTINEL_STORAGE_S3_AWS_ACCESS_KEY_ID", default=None),
            "aws_secret_access_key": env("SENTINEL_STORAGE_S3_AWS_SECRET_ACCESS_KEY", default=None),
        },
    },
}

# Debug toolbar (dev only)
if DEBUG_TOOLBAR := env.bool("DEBUG_TOOLBAR"):
    DEBUG_TOOLBAR_CONFIG = {"SHOW_TOOLBAR_CALLBACK": lambda _request: True}
    INSTALLED_APPS.append("debug_toolbar")
    MIDDLEWARE = ["debug_toolbar.middleware.DebugToolbarMiddleware", *MIDDLEWARE]
