"""Notificação desktop opcional (winotify no Windows). Nunca levanta exceção."""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)
APP_ID = "Union Veículos"


def notify(title: str, message: str, url: str | None = None) -> None:
    if os.environ.get("UNION_DISABLE_NOTIFY") == "1":
        return
    try:
        from winotify import Notification
    except ImportError:
        return
    try:
        toast = Notification(app_id=APP_ID, title=title, msg=message)
        if url:
            toast.add_actions(label="Abrir painel", launch=url)
        toast.show()
    except Exception:  # notificação nunca derruba o job
        log.exception("falha ao notificar")
