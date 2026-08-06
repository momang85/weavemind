"""Progress push helpers for orchestrator → Redis → web_ui → frontend."""
import logging

logger = logging.getLogger(__name__)

def push_progress(messaging, task_id: str, update_type: str, payload: dict) -> None:
    """Publish partial progress to Redis. web_ui listener picks this up
    and merges into _task_results. Frontend polls every 2s and renders."""
    try:
        msg = {
            "task_id": task_id,
            "type": update_type,
            "payload": payload,
        }
        messaging.publish("orchestrator:response", msg)
        messaging.publish("orchestrator:progress", msg)
    except Exception as exc:
        logger.warning("push_progress failed for %s: %s", task_id, exc)
