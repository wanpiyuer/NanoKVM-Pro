#!/usr/bin/env python3
"""Fixed-path NanoKVM microphone jack lease bridge; Python standard library only.

Production CLI has no path or environment overrides. Tests inject a JackBackend
and clock into LeaseController and use a temporary Unix socket.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import socketserver
import stat
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable


JACK_PATH = Path("/sys/kernel/config/usb_gadget/g0/functions/uac2.usb0/p_jack_connected")
DETECT_PATH = JACK_PATH.with_name("p_jack_detect")
UDC_PATH = Path("/sys/kernel/config/usb_gadget/g0/UDC")
CONFIG_LINK_PATH = Path("/sys/kernel/config/usb_gadget/g0/configs/c.1/uac2.usb0")
RUN_DIR = Path("/run/nanokvm-mic-lease")
SOCKET_PATH = RUN_DIR / "control.sock"
LOCK_PATH = RUN_DIR / "daemon.lock"
TTL_SECONDS = 10.0
RENEW_INTERVAL_MS = 2000
MAX_SEQ = 9007199254740991
MAX_OWNERS = 16384
MAX_REQUEST_BYTES = 2048
MAX_RESPONSE_BYTES = 4096
HEX32 = re.compile(r"^[0-9a-f]{32}$")
ACTIONS = frozenset(("claim", "commit", "renew", "release", "status"))


class ProtocolError(ValueError):
    pass


class JackBackend:
    """Only opens existing attributes. Never creates or rebinds USB objects."""

    def __init__(self, connected: Path, detect: Path, *, udc: Path | None = None,
                 config_link: Path | None = None):
        if (udc is None) != (config_link is None):
            raise ValueError("both binding guards are required")
        self.connected = connected
        self.detect = detect
        self.udc = udc
        self.config_link = config_link

    def ready(self) -> None:
        if self.detect.read_text(encoding="ascii").strip() != "1":
            raise OSError("jack detection is not enabled")
        if self.udc is not None:
            if not self.udc.read_text(encoding="ascii").strip():
                raise OSError("USB gadget is not bound")
            if (not self.config_link.is_symlink() or not self.config_link.exists()
                    or self.config_link.resolve() != self.connected.parent.resolve()):
                raise OSError("UAC2 function is not linked in the expected configuration")
        self.read()

    def read(self) -> int:
        value = self.connected.read_text(encoding="ascii").strip()
        if value not in ("0", "1"):
            raise OSError("invalid jack attribute value")
        return int(value)

    def write(self, value: int) -> int:
        if value not in (0, 1):
            raise ValueError("invalid jack state")
        # No O_CREAT, O_TRUNC, UDC write, gadget restart, or descriptor change.
        fd = os.open(self.connected, os.O_WRONLY | getattr(os, "O_CLOEXEC", 0))
        try:
            payload = f"{value}\n".encode("ascii")
            if os.write(fd, payload) != len(payload):
                raise OSError("short jack attribute write")
        finally:
            os.close(fd)
        observed = self.read()
        if observed != value:
            raise OSError("jack attribute readback mismatch")
        return observed


@dataclass
class Lease:
    owner: str
    seq: int
    phase: str
    deadline: float


class LeaseController:
    def __init__(self, jack: JackBackend, *, clock: Callable[[], float] = time.monotonic,
                 ttl: float = TTL_SECONDS, max_owners: int = MAX_OWNERS,
                 boot_epoch: str | None = None):
        if ttl <= 0 or max_owners < 1:
            raise ValueError("invalid controller limits")
        self.jack = jack
        self.clock = clock
        self.ttl = ttl
        self.max_owners = max_owners
        self.boot_epoch = boot_epoch or secrets.token_hex(16)
        if not HEX32.fullmatch(self.boot_epoch):
            raise ValueError("invalid boot epoch")
        self.closed: dict[str, int] = {}
        self.lease: Lease | None = None
        self.fault = False
        self.lock = threading.RLock()
        # Performed only after the production singleton lock is held.
        self.jack.ready()
        self.jack.write(0)

    def _remember(self, owner: str, seq: int) -> bool:
        if owner not in self.closed and len(self.closed) >= self.max_owners:
            return False
        self.closed[owner] = max(self.closed.get(owner, 0), seq)
        return True

    def _fault(self, *, try_off: bool) -> None:
        if self.lease is not None:
            self._remember(self.lease.owner, self.lease.seq)
            self.lease = None
        self.fault = True
        if try_off:
            try:
                self.jack.write(0)
            except (OSError, ValueError, UnicodeError):
                pass

    def _close(self) -> bool:
        current = self.lease
        if current is None:
            return True
        self._remember(current.owner, current.seq)
        self.lease = None
        try:
            self.jack.write(0)
            return True
        except (OSError, ValueError, UnicodeError):
            self._fault(try_off=True)
            return False

    def tick(self) -> None:
        with self.lock:
            if self.lease is not None and self.clock() >= self.lease.deadline:
                self._close()

    def shutdown(self) -> None:
        with self.lock:
            self._close()
            # Graceful service shutdown is a fail-closed boundary, like startup.
            # SIGKILL requires the separate systemd ExecStopPost failsafe helper.
            self.jack.write(0)

    def _response(self, ok: bool, code: str) -> dict:
        observed = None
        if not self.fault:
            try:
                if self.lease is not None:
                    self.jack.ready()
                observed = self.jack.read()
                if self.lease is not None:
                    expected = 1 if self.lease.phase == "active" else 0
                    if observed != expected:
                        self._fault(try_off=True)
                        observed = None
            except (OSError, ValueError, UnicodeError):
                self._fault(try_off=self.lease is not None)
        if self.fault:
            ok, code = False, "hardware_error"
        lease = self.lease
        return {
            "version": 1, "ok": ok, "code": code,
            "phase": "fault" if self.fault else (lease.phase if lease else "idle"),
            "jack": observed,
            "ttlMs": max(0, math.ceil((lease.deadline - self.clock()) * 1000)) if lease else 0,
            "leaseSeq": lease.seq if lease else None,
            "bootEpoch": self.boot_epoch,
        }

    @staticmethod
    def validate(request: object) -> dict:
        if not isinstance(request, dict):
            raise ProtocolError("request must be an object")
        action = request.get("action")
        if not isinstance(action, str) or action not in ACTIONS:
            raise ProtocolError("unknown action")
        expected = {"action"} if action == "status" else {"action", "owner", "seq", "bootEpoch"}
        if set(request) != expected:
            raise ProtocolError("unexpected or missing fields")
        if action != "status":
            if not isinstance(request["owner"], str) or not HEX32.fullmatch(request["owner"]):
                raise ProtocolError("invalid owner")
            if not isinstance(request["bootEpoch"], str) or not HEX32.fullmatch(request["bootEpoch"]):
                raise ProtocolError("invalid boot epoch")
            if type(request["seq"]) is not int or not 1 <= request["seq"] <= MAX_SEQ:
                raise ProtocolError("invalid sequence")
        return request

    def handle(self, request: object) -> dict:
        with self.lock:
            self.tick()
            try:
                request = self.validate(request)
            except ProtocolError:
                return self._response(False, "invalid_request")
            if self.fault:
                return self._response(False, "hardware_error")
            action = request["action"]
            if action == "status":
                return self._response(True, "status")
            if request["bootEpoch"] != self.boot_epoch:
                return self._response(False, "epoch_mismatch")
            owner, seq = request["owner"], request["seq"]

            if action == "release":
                if not self._remember(owner, seq):
                    return self._response(False, "capacity")
                if self.lease is not None and self.lease.owner == owner and self.lease.seq <= seq:
                    self._close()
                return self._response(True, "released")

            if seq <= self.closed.get(owner, 0):
                return self._response(False, "stale")

            current = self.lease
            same = current is not None and (current.owner, current.seq) == (owner, seq)
            if action == "claim":
                if current is not None:
                    # An idempotent retry neither downgrades active to reserved
                    # nor extends its lifetime. Only commit/renew extend it.
                    return self._response(same, "claimed" if same else "busy")
                if not self._remember(owner, 0):
                    return self._response(False, "capacity")
                try:
                    self.jack.ready()
                    self.jack.write(0)
                except (OSError, ValueError, UnicodeError):
                    self._remember(owner, seq)
                    self._fault(try_off=True)
                    return self._response(False, "hardware_error")
                self.lease = Lease(owner, seq, "reserved", self.clock() + self.ttl)
                return self._response(True, "claimed")

            if not same:
                return self._response(False, "not_owner")
            if action == "commit":
                if current.phase == "reserved":
                    try:
                        self.jack.ready()
                        self.jack.write(1)
                    except (OSError, ValueError, UnicodeError):
                        self._fault(try_off=True)
                        return self._response(False, "hardware_error")
                    current.phase = "active"
                current.deadline = self.clock() + self.ttl
                return self._response(True, "committed")

            # renew cannot create a lease or write jack=1.
            current.deadline = self.clock() + self.ttl
            return self._response(True, "renewed")


def _strict_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("duplicate field")
        result[key] = value
    return result


def decode_request(raw: bytes) -> object:
    if len(raw) > MAX_REQUEST_BYTES or not raw.endswith(b"\n"):
        raise ProtocolError("invalid request length or framing")
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_object,
                          parse_constant=lambda _: (_ for _ in ()).throw(ProtocolError("invalid number")))
    except (ValueError, UnicodeError) as exc:
        raise ProtocolError("invalid JSON") from exc


def encode_response(response: dict) -> bytes:
    return (json.dumps(response, separators=(",", ":"), allow_nan=False) + "\n").encode("ascii")


def transport_error(code: str, epoch: str | None = None) -> dict:
    return {"version": 1, "ok": False, "code": code, "phase": "fault", "jack": None,
            "ttlMs": 0, "leaseSeq": None, "bootEpoch": epoch}


class LeaseRequestHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        self.request.settimeout(0.25)
        raw = bytearray()
        try:
            while b"\n" not in raw and len(raw) <= MAX_REQUEST_BYTES:
                part = self.request.recv(min(512, MAX_REQUEST_BYTES + 1 - len(raw)))
                if not part:
                    break
                raw.extend(part)
            request = decode_request(bytes(raw))
            result = self.server.controller.handle(request)
        except (ProtocolError, OSError):
            result = self.server.controller.handle({})
        try:
            self.request.sendall(encode_response(result))
        except OSError:
            pass  # A disconnected caller does not cancel a committed action.


class LeaseServer(socketserver.TCPServer):
    # socketserver omits UnixStreamServer on Windows even where socket.AF_UNIX
    # exists. Selecting AF_UNIX explicitly gives the same implementation on
    # both platforms; this class never binds an AF_INET/AF_INET6 TCP address.
    address_family = getattr(socket, "AF_UNIX", -1)
    request_queue_size = 16

    def __init__(self, socket_path: Path, controller: LeaseController):
        if not hasattr(socket, "AF_UNIX"):
            raise OSError("AF_UNIX is not available")
        self.controller = controller
        self.timeout = 0.1
        super().__init__(str(socket_path), LeaseRequestHandler)
        os.chmod(socket_path, 0o600)

    def handle_error(self, request, client_address) -> None:
        # Do not continue serving after an unhandled internal exception.
        raise RuntimeError("lease request handling failed")


def run_server(server: LeaseServer, stop: threading.Event) -> None:
    while not stop.is_set():
        server.controller.tick()
        server.handle_request()


def parse_client_args(arguments: list[str]) -> dict:
    if arguments == ["status"]:
        return {"action": "status"}
    if len(arguments) != 4 or not re.fullmatch(r"[1-9][0-9]{0,15}", arguments[2]):
        raise ProtocolError("invalid arguments")
    request = {"action": arguments[0], "owner": arguments[1], "seq": int(arguments[2]),
               "bootEpoch": arguments[3]}
    return LeaseController.validate(request)


def call_socket(socket_path: Path, request: dict, *, timeout: float = 1.0) -> dict:
    if not hasattr(socket, "AF_UNIX"):
        return transport_error("unavailable", request.get("bootEpoch"))
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(str(socket_path))
            client.sendall(encode_response(request))
            raw = bytearray()
            while b"\n" not in raw and len(raw) <= MAX_RESPONSE_BYTES:
                chunk = client.recv(min(512, MAX_RESPONSE_BYTES + 1 - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
            if not raw.endswith(b"\n") or len(raw) > MAX_RESPONSE_BYTES:
                raise ProtocolError("invalid response")
            result = json.loads(raw.decode("ascii"), object_pairs_hook=_strict_object)
            if not isinstance(result, dict) or result.get("version") != 1:
                raise ProtocolError("invalid response")
            return result
    except (OSError, ValueError, UnicodeError):
        return transport_error("unavailable", request.get("bootEpoch"))


def acquire_singleton_lock(path: Path) -> int:
    """Nonblocking lock shared by daemon and stop-post; caller closes the fd."""
    import fcntl
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RuntimeError("unexpected singleton lock file")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BaseException:
        os.close(fd)
        raise


def failsafe_off(jack: JackBackend, lock_path: Path) -> bool:
    """A duplicate daemon's ExecStopPost must not close a live owner's jack."""
    try:
        fd = acquire_singleton_lock(lock_path)
    except BlockingIOError:
        return False  # Another daemon still owns the endpoint; leave it alone.
    try:
        jack.write(0)
        return True
    finally:
        os.close(fd)


def prepare_run_dir() -> None:
    RUN_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    if RUN_DIR.is_symlink() or not RUN_DIR.is_dir():
        raise RuntimeError("unsafe runtime directory")
    os.chmod(RUN_DIR, 0o700)


def serve_production() -> int:
    if os.name != "posix" or os.geteuid() != 0:
        raise RuntimeError("daemon requires root on Linux")
    prepare_run_dir()
    # Acquire before entering cleanup: a duplicate daemon owns nothing.
    lock_fd = acquire_singleton_lock(LOCK_PATH)
    controller = None
    server = None
    stop = threading.Event()
    try:
        # Do not change jack state or remove a socket owned by another daemon.
        if SOCKET_PATH.exists() or SOCKET_PATH.is_symlink():
            if not stat.S_ISSOCK(SOCKET_PATH.lstat().st_mode):
                raise RuntimeError("unexpected control socket path")
            SOCKET_PATH.unlink()
        controller = LeaseController(JackBackend(JACK_PATH, DETECT_PATH,
                                                udc=UDC_PATH, config_link=CONFIG_LINK_PATH))
        server = LeaseServer(SOCKET_PATH, controller)
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, lambda _signum, _frame: stop.set())
        run_server(server, stop)
        return 0
    finally:
        if server is not None:
            server.server_close()
            SOCKET_PATH.unlink(missing_ok=True)
        try:
            if controller is not None:
                controller.shutdown()
        finally:
            os.close(lock_fd)


def main(arguments: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if arguments is None else arguments)
    if arguments and arguments[0] == "client":
        try:
            request = parse_client_args(arguments[1:])
            response = call_socket(SOCKET_PATH, request)
        except ProtocolError:
            response = transport_error("invalid_request")
        # The existing RunScript API discards output on nonzero exit. All
        # expected client failures remain structured and must be checked by UI.
        sys.stdout.write(encode_response(response).decode("ascii"))
        return 0
    try:
        if arguments == ["serve"]:
            return serve_production()
        if arguments == ["failsafe-off"]:
            if os.name != "posix" or os.geteuid() != 0:
                raise RuntimeError("failsafe requires root on Linux")
            prepare_run_dir()
            failsafe_off(JackBackend(JACK_PATH, DETECT_PATH), LOCK_PATH)
            return 0
        raise ProtocolError("expected serve, client, or failsafe-off")
    except Exception:
        # No owner/token/request contents in logs.
        print("mic lease operation failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
