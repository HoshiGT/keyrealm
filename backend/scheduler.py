"""
定时任务调度器
APScheduler BackgroundScheduler + ThreadPoolExecutor
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Callable, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from . import database as db
from . import plugin_manager

_scheduler: Optional[BackgroundScheduler] = None
_executor = ThreadPoolExecutor(max_workers=10)

# 由 api.py 注入，用于推送事件到前端
_event_callback: Optional[Callable] = None
# 由 api.py 注入，用于获取解密密钥
_get_key_callback: Optional[Callable[[], Optional[bytes]]] = None


def set_callbacks(event_cb: Callable, get_key_cb: Callable[[], Optional[bytes]]):
    global _event_callback, _get_key_callback
    _event_callback = event_cb
    _get_key_callback = get_key_cb


def _push_event(event_name: str, detail: dict):
    if _event_callback:
        try:
            _event_callback(event_name, detail)
        except Exception as e:
            print(f"[scheduler] 事件推送失败: {e}")


def _get_key() -> Optional[bytes]:
    if _get_key_callback:
        return _get_key_callback()
    return None


def verify_single_credential(cred_id: int, trigger_type: str = "schedule") -> dict:
    """验证单条凭据，返回结果 dict"""
    from .crypto import decrypt_dict

    key = _get_key()
    if key is None:
        return {"status": "error", "message": "保险库已锁定，无法验证"}

    cred = db.get_credential(cred_id)
    if not cred:
        return {"status": "error", "message": "凭据不存在"}

    try:
        data = decrypt_dict(key, cred["data_encrypted"])
    except Exception as e:
        return {"status": "error", "message": f"解密失败: {e}"}

    start = time.monotonic()
    result = plugin_manager.verify_credential(cred["credential_type"], data)
    duration_ms = int((time.monotonic() - start) * 1000)

    status_detail = result.to_dict() if hasattr(result, "to_dict") else {
        "status": result.status,
        "message": result.message,
        "account_info": getattr(result, "account_info", None),
    }

    now = datetime.now(timezone.utc).isoformat()
    db.update_credential(
        cred_id,
        status=result.status,
        status_detail=status_detail,
        last_verified_at=now,
    )
    db.add_verification_record(
        credential_id=cred_id,
        status=result.status,
        result_data=status_detail,
        error_message=result.message if result.status == "error" else "",
        trigger_type=trigger_type,
        duration_ms=duration_ms,
    )

    updated = db.get_credential(cred_id)
    if updated:
        _push_event("vault:credential_updated", updated)

    return status_detail


def batch_verify(
    scope: str = "all",
    group_id: Optional[int] = None,
    credential_type: Optional[str] = None,
    trigger_type: str = "manual",
):
    """批量验证，在后台线程池执行"""
    creds = db.list_credentials(
        group_id=group_id if scope == "group" else None,
        credential_type=credential_type if scope == "type" else None,
    )
    total = len(creds)
    if total == 0:
        _push_event("vault:batch_verify_done", {"total": 0, "valid": 0, "invalid": 0, "error": 0})
        return

    def _run():
        valid = invalid = error = 0
        for i, cred in enumerate(creds, 1):
            result = verify_single_credential(cred["id"], trigger_type=trigger_type)
            status = result.get("status", "error")
            if status == "valid":
                valid += 1
            elif status == "invalid":
                invalid += 1
            else:
                error += 1
            _push_event("vault:batch_verify_progress", {
                "current": i,
                "total": total,
                "credential_name": cred["name"],
                "status": status,
            })
        _push_event("vault:batch_verify_done", {
            "total": total, "valid": valid, "invalid": invalid, "error": error,
        })

    _executor.submit(_run)


def _run_schedule(schedule: dict):
    """执行定时任务"""
    scope = schedule.get("target_scope", "all")
    target_id = schedule.get("target_id")
    # target_id 是多态字段：scope=="group" 时为分组 ID（int），
    # scope=="type" 时为凭据类型名（str，前端直接存入 target_id）。
    batch_verify(
        scope=scope,
        group_id=target_id if scope == "group" else None,
        credential_type=target_id if scope == "type" else None,
        trigger_type="schedule",
    )
    db.update_schedule(schedule["id"], last_run_at=datetime.now(timezone.utc).isoformat())


def _build_trigger(trigger_type: str, trigger_args: dict):
    if trigger_type == "interval":
        return IntervalTrigger(**trigger_args)
    elif trigger_type == "cron":
        return CronTrigger(**trigger_args)
    raise ValueError(f"不支持的触发器类型: {trigger_type}")


def start_scheduler():
    """启动调度器，加载所有已保存的计划"""
    global _scheduler
    if _scheduler and _scheduler.running:
        return

    _scheduler = BackgroundScheduler(executor=_executor)
    _scheduler.start()

    for sched in db.list_schedules():
        if not sched.get("enabled"):
            continue
        try:
            trigger = _build_trigger(sched["trigger_type"], sched["trigger_args"])
            job = _scheduler.add_job(
                _run_schedule,
                trigger=trigger,
                args=[sched],
                id=f"schedule_{sched['id']}",
                replace_existing=True,
            )
            if job and job.next_run_time:
                db.update_schedule(sched["id"], next_run_at=job.next_run_time.isoformat())
        except Exception as e:
            print(f"[scheduler] 加载计划失败 id={sched['id']}: {e}")


def stop_scheduler():
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
    _scheduler = None


def reload_schedules():
    """重新加载所有计划（增删改后调用）"""
    if not _scheduler:
        return
    # 移除所有旧 job
    for job in _scheduler.get_jobs():
        job.remove()
    # 重新添加
    for sched in db.list_schedules():
        if not sched.get("enabled"):
            continue
        try:
            trigger = _build_trigger(sched["trigger_type"], sched["trigger_args"])
            job = _scheduler.add_job(
                _run_schedule,
                trigger=trigger,
                args=[sched],
                id=f"schedule_{sched['id']}",
                replace_existing=True,
            )
            if job and job.next_run_time:
                db.update_schedule(sched["id"], next_run_at=job.next_run_time.isoformat())
        except Exception as e:
            print(f"[scheduler] 重载计划失败 id={sched['id']}: {e}")
