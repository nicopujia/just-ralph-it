"""Systemd socket-activated uvicorn launcher.

Reads the socket FD passed by systemd (SD_LISTEN_FDS protocol, FD 3)
and runs uvicorn with it. Works around uvicorn hardcoding AF_UNIX in --fd.
"""

import os
import socket
import sys

# Ensure src/ is on the path (systemd sets WorkingDirectory but not PYTHONPATH)
sys.path.insert(0, os.path.join(os.getcwd(), "src"))

import uvicorn

SD_LISTEN_FDS_START = 3

sock = socket.fromfd(SD_LISTEN_FDS_START, socket.AF_INET, socket.SOCK_STREAM)
sock.setblocking(False)
sock.set_inheritable(True)

config = uvicorn.Config("app.main:app", log_level="info")
server = uvicorn.Server(config)
server.config.loaded = False

# Patch serve to pass our socket
_original_serve = server.serve

async def _serve_with_socket(sockets=None):
    return await _original_serve(sockets=[sock])

server.serve = _serve_with_socket
server.run()
