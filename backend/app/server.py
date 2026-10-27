"""The HTTP server of the panel on a server machine: `python -m app.server` (deploy/linux/jobsearchpanel.service).

One process, one application and its scheduler, up to two listening sockets:
- APP_HOST:APP_PORT — the panel as before: the laptop, the SSH tunnel;
- 127.0.0.1:PANEL_PUBLIC_PORT, only when PANEL_PUBLIC_URL is set — the target of the reverse proxy (Caddy) of the
  public address, where everything needs a session from the bot's one-time link (app/web_access.py).
A second process for the second address is not an option: two panels revoke each other's hh.ru app token and share
the bot's getUpdates. uvicorn serves both sockets from one event loop; a request tells its socket by `scope["server"]`.
Without PANEL_PUBLIC_URL this is plain `uvicorn app.main:app --host APP_HOST --port APP_PORT --no-access-log`.

No access log, ever: the sign-in link carries its token in the query string.
"""

from __future__ import annotations

import socket
import sys

import uvicorn
from pydantic import ValidationError

from app.config import Settings, get_settings

APP = "app.main:app"


def server_config(settings: Settings) -> uvicorn.Config:
    """The config of the server; creating it sets up uvicorn's logging (without the access log)."""
    return uvicorn.Config(APP, host=settings.app_host, port=settings.app_port, access_log=False)


def listening_sockets(settings: Settings) -> list[socket.socket]:
    """The bound sockets, by uvicorn's own `bind_socket` (it logs «Uvicorn running on …» for each). Call after
    `server_config`: these configs leave the logging as it is (`log_config=None`) — a default config would set it
    up again with the access log."""

    def bind(host: str, port: int) -> socket.socket:
        return uvicorn.Config(APP, host=host, port=port, access_log=False, log_config=None).bind_socket()

    sockets = [bind(settings.app_host, settings.app_port)]
    if settings.panel_public_url:
        # the proxy lives on this machine: the public entrance is never bound to an outside interface
        sockets.append(bind("127.0.0.1", settings.panel_public_port))
    return sockets


def main() -> None:
    try:
        settings = get_settings()
    except ValidationError as exc:
        from app.main import _report_bad_settings  # stderr (the journal) and logs/app.log, as the app does

        _report_bad_settings(exc)
        sys.exit(2)
    config = server_config(settings)
    uvicorn.Server(config).run(sockets=listening_sockets(settings))


if __name__ == "__main__":
    main()
