"""Optional crane-rover peer UDP transport."""
from __future__ import annotations

import json
import logging
import socket
import threading

from .state import DashboardState

class PeerUdpListener:
    def __init__(self, state: DashboardState, host: str, port: int, max_age_sec: float):
        self.state = state
        self.host = host
        self.port = int(port)
        self.max_age_sec = float(max_age_sec)

    def start(self) -> None:
        thread = threading.Thread(target=self._run, name="peer-udp-listener", daemon=True)
        thread.start()
        logging.info("UDP peer listener on %s:%s", self.host, self.port)

    def _run(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((self.host, self.port))
        except OSError as exc:
            logging.warning("UDP peer listener disabled: %s", exc)
            return

        while True:
            try:
                data, addr = sock.recvfrom(8192)
                payload = json.loads(data.decode("utf-8"))
                self.state.update_from_peer_udp(payload, addr[0], self.max_age_sec)
            except Exception as exc:
                logging.debug("UDP peer packet ignored: %s", exc)

