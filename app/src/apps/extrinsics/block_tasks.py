import time
from datetime import UTC, datetime

import structlog
from sentinel.v1.dto import ExtrinsicDTO
from sentinel.v1.providers.base import BlockchainProvider
from sentinel.v1.services.sentinel import sentinel_service

from apps.extrinsics.hyperparam_service import enrich_extrinsics_with_previous_values
from apps.extrinsics.models import Extrinsic
from apps.notifications import dispatch_block_notifications
from project.core.services import JsonLinesStorage

logger = structlog.get_logger()


class BlockExtrinsicsUnavailableError(RuntimeError):
    """Raised when the chain provider did not return any extrinsics for a block."""

    def __init__(self, block_number: int) -> None:
        super().__init__(f"Block {block_number} could not be read from the chain")
        self.block_number = block_number


def _sanitize_json(obj: object) -> object:
    r"""Remove null bytes from JSON data (PostgreSQL JSONB doesn't support \u0000)."""
    if isinstance(obj, str):
        return obj.replace("\x00", "")
    if isinstance(obj, dict):
        return {k: _sanitize_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_json(item) for item in obj]
    return obj


def _parse_extrinsic_record(record: dict) -> dict | None:
    """Parse an extrinsic record into Extrinsic model fields."""
    extrinsic_hash = record.get("extrinsic_hash", "")
    if not extrinsic_hash:
        return None

    call_data = record.get("call", {})
    call_args_list = call_data.get("call_args", [])

    # Extract netuid from record, call_args, or events (for register_network)
    netuid = record.get("netuid")
    if netuid is None:
        for arg in call_args_list:
            if arg.get("name") == "netuid":
                netuid = arg.get("value")
                break
    if netuid is None:
        call_function = call_data.get("call_function", "")
        if call_function in ("register_network", "register_network_with_identity"):
            for event in record.get("events", []):
                if event.get("event_id") == "NetworkAdded":
                    attrs = event.get("attributes")
                    if isinstance(attrs, dict):
                        netuid = attrs.get("netuid")
                    elif isinstance(attrs, (list, tuple)) and attrs:
                        netuid = attrs[0]
                    break

    # Determine success from status
    status = record.get("status", "")
    success = status.lower() == "success"

    # Extract error data from events if failed
    error_data = None
    events = record.get("events", [])
    if not success:
        for event in events:
            if event.get("event_id") == "ExtrinsicFailed":
                error_data = event.get("attributes")
                break

    return {
        "extrinsic_hash": extrinsic_hash,
        "block_number": record.get("block_number", 0),
        "block_timestamp": record.get("timestamp"),
        "extrinsic_index": record.get("index"),
        "call_module": call_data.get("call_module", ""),
        "call_function": call_data.get("call_function", ""),
        "call_args": _sanitize_json(call_args_list),
        "address": record.get("address") or "",
        "signature": _sanitize_json(record.get("signature")),
        "nonce": record.get("nonce"),
        "tip_rao": record.get("tip"),
        "success": success,
        "status": status,
        "error_data": _sanitize_json(error_data),
        "events": _sanitize_json(events),
        "netuid": netuid,
    }


def store_block_extrinsics(block_number: int, provider: BlockchainProvider) -> dict:
    """
    Store extrinsics from the given block number.

    Fetches extrinsics from the blockchain, stores them as JSONL artifacts,
    and syncs them to Django models.

    Raises:
        BlockUnavailableError: The provider returned no extrinsics for the block.

    """
    t0 = time.monotonic()

    service = sentinel_service(provider)
    block = service.ingest_block(block_number)
    extrinsics = block.extrinsics
    timestamp = block.timestamp

    t1 = time.monotonic()
    logger.debug(
        "Block ingested",
        block_number=block_number,
        extrinsic_count=len(extrinsics) if extrinsics else 0,
        duration_s=round(t1 - t0, 3),
    )

    if not extrinsics:
        raise BlockExtrinsicsUnavailableError(block_number)

    t2 = time.monotonic()

    # Store to JSONL artifact
    artifact_count = store_extrinsics_artifact(extrinsics, block_number, timestamp)
    t3 = time.monotonic()
    logger.debug(
        "Artifacts stored", block_number=block_number, artifact_count=artifact_count, duration_s=round(t3 - t2, 3)
    )

    # Sync to Django models
    db_count = sync_extrinsics_to_db(extrinsics, block_number, timestamp)
    t4 = time.monotonic()
    logger.debug("DB sync completed", block_number=block_number, db_count=db_count, duration_s=round(t4 - t3, 3))

    logger.debug(
        "Stored and synced extrinsics",
        block_number=block_number,
        artifact_count=artifact_count,
        db_count=db_count,
        total_duration_s=round(t4 - t0, 3),
    )

    return {"artifact_count": artifact_count, "db_count": db_count, "elapsed_ms": round((t4 - t0) * 1000)}


def store_extrinsics_artifact(extrinsics: list[ExtrinsicDTO], block_number: int, timestamp: int | None) -> int:
    """Store extrinsics to JSONL artifact files."""
    return 0
    extrinsics_storage = JsonLinesStorage("data/bittensor/extrinsics/{date}.jsonl")
    if not extrinsics:
        return 0

    # Convert timestamp to date string for partitioning (timestamp is in milliseconds)
    date_str = datetime.fromtimestamp(timestamp / 1000, tz=UTC).strftime("%Y-%m-%d") if timestamp else "unknown"

    for extrinsic in extrinsics:
        extrinsics_storage.append(
            {
                "block_number": block_number,
                "timestamp": timestamp,
                **extrinsic.model_dump(),
            },
            date=date_str,
        )
    return len(extrinsics)


def sync_extrinsics_to_db(extrinsics: list[ExtrinsicDTO], block_number: int, timestamp: int | None) -> int:
    """Sync extrinsics to Django Extrinsic model."""
    if not extrinsics:
        return 0

    t0 = time.monotonic()

    # Build records for database
    records_to_create = []
    parsed_for_notifications = []
    existing_hashes = set(
        Extrinsic.objects.filter(block_number=block_number).values_list("extrinsic_hash", flat=True),
    )

    t1 = time.monotonic()
    logger.debug(
        "sync_extrinsics_to_db: fetched existing hashes",
        block_number=block_number,
        existing_count=len(existing_hashes),
        duration_s=round(t1 - t0, 3),
    )

    for extrinsic in extrinsics:
        record = {
            "block_number": block_number,
            "timestamp": timestamp,
            **extrinsic.model_dump(),
        }
        parsed = _parse_extrinsic_record(record)
        if not parsed:
            continue

        # Skip if already exists
        if parsed["extrinsic_hash"] in existing_hashes:
            continue

        parsed_for_notifications.append(parsed)
        records_to_create.append(Extrinsic(**parsed))

    if records_to_create:
        Extrinsic.objects.bulk_create(records_to_create, ignore_conflicts=True)

    t2 = time.monotonic()
    logger.debug(
        "sync_extrinsics_to_db: bulk_create done",
        block_number=block_number,
        created_count=len(records_to_create),
        duration_s=round(t2 - t1, 3),
    )

    # Enrich with previous hyperparam values and send aggregated Discord notification
    enriched = enrich_extrinsics_with_previous_values(parsed_for_notifications)

    t3 = time.monotonic()
    logger.debug("sync_extrinsics_to_db: enrichment done", block_number=block_number, duration_s=round(t3 - t2, 3))

    try:
        dispatch_block_notifications(block_number, enriched)
    except Exception:  # noqa: BLE001
        logger.exception("sync_extrinsics_to_db: notification dispatch failed", block_number=block_number)

    t4 = time.monotonic()
    logger.debug(
        "sync_extrinsics_to_db: notifications dispatched", block_number=block_number, duration_s=round(t4 - t3, 3)
    )

    return len(records_to_create)
