import logging
from datetime import datetime, timedelta
from typing import Optional

from redis.asyncio import Redis

from config import Config
from globals import logger

client = Redis.from_url(Config.REDIS_URI)

REDIS_PREFIX = f"AIMIDDLEWARE_{Config.ENVIRONMENT}_"
RECONCILIATION_THRESHOLD = 0.10


async def reconcile_usage(
    org_id: str,
    bridge_id: str,
    entity_type: str,
    period: str,
    start_date: datetime,
) -> bool:
    """
    Compare Redis usage with TimescaleDB for a specific entity.
    If difference exceeds threshold, correct Redis.

    Args:
        org_id: Organization ID
        bridge_id: Bridge ID
        entity_type: 'bridge', 'folder', or 'apikey'
        period: 'daily', 'weekly', or 'monthly'
        start_date: When this period started

    Returns:
        True if reconciliation succeeded, False otherwise
    """
    try:
        from models.Timescale.connections import get_session

        start_iso = start_date.isoformat()
        redis_key = f"{REDIS_PREFIX}quota:{org_id}:{entity_type}:{bridge_id}:{period}:{start_iso}"

        redis_usage = float(await client.get(redis_key) or 0)

        period_end = _calculate_period_end(start_date, period)
        query = """
            SELECT COALESCE(SUM(cost_usd), 0) as total_cost
            FROM usage_events
            WHERE org_id = %s
            AND timestamp >= %s
            AND timestamp < %s
            AND status = 'success'
        """

        params = [org_id, start_date, period_end]

        if entity_type == "bridge":
            query += " AND bridge_id = %s"
            params.append(bridge_id)
        elif entity_type == "folder":
            query += " AND folder_id = %s"
            params.append(bridge_id)
        elif entity_type == "apikey":
            query += " AND apikey_id = %s"
            params.append(bridge_id)

        session = get_session()
        result = session.execute(query, params)
        row = result.fetchone()
        timescale_usage = float(row[0]) if row else 0

        difference = abs(redis_usage - timescale_usage)
        difference_pct = (
            (difference / timescale_usage) if timescale_usage > 0 else 0
        )

        logger.info(
            f"[Reconciliation] {entity_type} {bridge_id}: Redis={redis_usage:.2f}, "
            f"TimescaleDB={timescale_usage:.2f}, Diff={difference:.2f} ({difference_pct*100:.1f}%)"
        )

        if difference_pct > RECONCILIATION_THRESHOLD:
            logger.warning(
                f"[Reconciliation] Large difference detected for {entity_type} {bridge_id}. "
                f"Correcting Redis from {redis_usage:.2f} to {timescale_usage:.2f}"
            )
            await client.set(redis_key, str(timescale_usage))

        return True

    except Exception as e:
        logger.error(f"Error in reconcile_usage: {str(e)}")
        return False


async def reconcile_all_entities(org_id: str) -> dict:
    """
    Run reconciliation for all entities in an organization.
    Called periodically (e.g., every 5 minutes).

    Returns:
        {
            'success': bool,
            'reconciled': int,
            'errors': int,
        }
    """
    try:
        from models.mongo_connection import db

        result = {"success": True, "reconciled": 0, "errors": 0}

        bridges = await db["bridges"].find({"org_id": org_id}).to_list(None)

        for bridge in bridges:
            bridge_id = bridge["_id"]

            periods = [
                ("daily", bridge.get("bridge_start_date", datetime.utcnow())),
                ("weekly", bridge.get("bridge_start_date", datetime.utcnow())),
                ("monthly", bridge.get("bridge_start_date", datetime.utcnow())),
            ]

            for period, start_date in periods:
                success = await reconcile_usage(
                    org_id, bridge_id, "bridge", period, start_date
                )
                if success:
                    result["reconciled"] += 1
                else:
                    result["errors"] += 1

                if bridge.get("folder_id"):
                    success = await reconcile_usage(
                        org_id,
                        bridge.get("folder_id"),
                        "folder",
                        period,
                        start_date,
                    )
                    if success:
                        result["reconciled"] += 1
                    else:
                        result["errors"] += 1

                if bridge.get("apikey_id"):
                    success = await reconcile_usage(
                        org_id,
                        bridge.get("apikey_id"),
                        "apikey",
                        period,
                        start_date,
                    )
                    if success:
                        result["reconciled"] += 1
                    else:
                        result["errors"] += 1

        logger.info(
            f"[Reconciliation] Completed for org {org_id}: "
            f"reconciled={result['reconciled']}, errors={result['errors']}"
        )
        return result

    except Exception as e:
        logger.error(f"Error in reconcile_all_entities: {str(e)}")
        return {"success": False, "reconciled": 0, "errors": 0}


def _calculate_period_end(start_date: datetime, period: str) -> datetime:
    """Calculate when the current period ends."""
    if period == "daily":
        return start_date + timedelta(days=1)
    elif period == "weekly":
        return start_date + timedelta(weeks=1)
    elif period == "monthly":
        if start_date.month == 12:
            return start_date.replace(year=start_date.year + 1, month=1)
        else:
            return start_date.replace(month=start_date.month + 1)
    return start_date + timedelta(days=30)
