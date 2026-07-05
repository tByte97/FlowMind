from __future__ import annotations

import base64
import hashlib
import json
import queue
import random
import socket
import struct
import threading
import time
from typing import Any


GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class LiveTelemetryPublisher:
    def __init__(self, host: str = "127.0.0.1", port: int = 8765) -> None:
        self._host = host
        self._port = port
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._server_socket: socket.socket | None = None
        self._clients: list[socket.socket] = []
        self._lock = threading.Lock()
        self._ready_event = threading.Event()
        self._pending_messages: list[str] = []

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._ready_event.clear()
        self._thread = threading.Thread(target=self._run_server, daemon=True)
        self._thread.start()
        self._ready_event.wait(timeout=2.0)

    def _run_server(self) -> None:
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_socket.setblocking(False)
        server_socket.bind((self._host, self._port))
        server_socket.listen(5)
        self._server_socket = server_socket
        self._ready_event.set()
        try:
            while not self._stop_event.is_set():
                try:
                    client_socket, _ = server_socket.accept()
                except BlockingIOError:
                    time.sleep(0.05)
                    continue
                except OSError:
                    break
                if self._handle_handshake(client_socket):
                    with self._lock:
                        self._clients.append(client_socket)
                    self._replay_pending(client_socket)
                else:
                    client_socket.close()
        finally:
            for client_socket in list(self._clients):
                try:
                    client_socket.close()
                except Exception:
                    pass
            self._clients.clear()
            try:
                server_socket.close()
            except Exception:
                pass
            self._server_socket = None

    def _handle_handshake(self, client_socket: socket.socket) -> bool:
        try:
            request_text = self._read_http_request(client_socket)
        except Exception:
            return False
        headers = self._parse_headers(request_text)
        if headers.get("upgrade", "").lower() != "websocket":
            return False
        if headers.get("connection", "").lower().find("upgrade") < 0:
            return False
        websocket_key = headers.get("sec-websocket-key")
        if not websocket_key:
            return False
        accept_key = base64.b64encode(
            hashlib.sha1(websocket_key.encode("ascii") + GUID.encode("ascii")).digest()
        ).decode("ascii")
        response = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept_key}\r\n"
            "\r\n"
        ).encode("ascii")
        client_socket.sendall(response)
        client_socket.settimeout(0.2)
        return True

    def _read_http_request(self, client_socket: socket.socket) -> str:
        buffer = b""
        while b"\r\n\r\n" not in buffer:
            chunk = client_socket.recv(4096)
            if not chunk:
                raise RuntimeError("Connection closed before handshake")
            buffer += chunk
        return buffer.decode("latin-1")

    def _parse_headers(self, request_text: str) -> dict[str, str]:
        headers: dict[str, str] = {}
        for line in request_text.split("\r\n"):
            if ":" in line:
                name, value = line.split(":", 1)
                headers[name.strip().lower()] = value.strip()
        return headers

    def _replay_pending(self, client_socket: socket.socket) -> None:
        with self._lock:
            pending = list(self._pending_messages)
        for message in pending:
            try:
                self._send_frame(client_socket, message)
            except Exception:
                break

    def publish(self, payload: dict[str, Any]) -> None:
        message = json.dumps(payload)
        with self._lock:
            self._pending_messages.append(message)
            clients = list(self._clients)
        for client_socket in clients:
            try:
                self._send_frame(client_socket, message)
            except Exception:
                with self._lock:
                    if client_socket in self._clients:
                        self._clients.remove(client_socket)
                try:
                    client_socket.close()
                except Exception:
                    pass

    def _send_frame(self, client_socket: socket.socket, message: str) -> None:
        payload = message.encode("utf-8")
        if len(payload) < 126:
            header = bytes([0x81, len(payload)])
        elif len(payload) < 65536:
            header = bytes([0x81, 126]) + struct.pack("!H", len(payload))
        else:
            header = bytes([0x81, 127]) + struct.pack("!Q", len(payload))
        client_socket.sendall(header + payload)

    def stop(self) -> None:
        self._stop_event.set()
        server_socket = self._server_socket
        if server_socket is not None:
            try:
                server_socket.close()
            except Exception:
                pass
        with self._lock:
            for client_socket in list(self._clients):
                try:
                    client_socket.close()
                except Exception:
                    pass
            self._clients.clear()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None


class LiveTelemetryClient:
    def __init__(self, url: str) -> None:
        self._url = url
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self._socket: socket.socket | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._connect_once()
            except Exception:
                time.sleep(0.2)

    def _connect_once(self) -> None:
        parsed = self._parse_url(self._url)
        client_socket = socket.create_connection(parsed, timeout=1.0)
        client_socket.settimeout(0.2)
        self._socket = client_socket
        self._perform_handshake(client_socket, parsed[0], parsed[1])
        while not self._stop_event.is_set():
            try:
                message = self._read_frame(client_socket)
            except TimeoutError:
                continue
            except (ConnectionResetError, ConnectionAbortedError, OSError):
                break
            if message is None:
                continue
            self._queue.put(json.loads(message))
        self._socket = None
        client_socket.close()

    def _parse_url(self, url: str) -> tuple[str, int]:
        if not url.startswith("ws://"):
            raise ValueError("Only ws:// URLs are supported")
        host_port = url[len("ws://") :]
        host, _, port_text = host_port.partition(":")
        port = int(port_text or "80")
        return host, port

    def _perform_handshake(self, client_socket: socket.socket, host: str, port: int) -> None:
        key = base64.b64encode(random.randbytes(16)).decode("ascii")
        request = (
            f"GET / HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        ).encode("ascii")
        client_socket.sendall(request)
        response = self._read_http_response(client_socket)
        headers = self._parse_headers(response)
        expected = base64.b64encode(hashlib.sha1(key.encode("ascii") + GUID.encode("ascii")).digest()).decode("ascii")
        if headers.get("sec-websocket-accept", "") != expected:
            raise RuntimeError("Invalid websocket handshake response")

    def _read_http_response(self, client_socket: socket.socket) -> str:
        buffer = b""
        while b"\r\n\r\n" not in buffer:
            chunk = client_socket.recv(4096)
            if not chunk:
                raise RuntimeError("Connection closed during handshake")
            buffer += chunk
        return buffer.decode("latin-1")

    def _parse_headers(self, response_text: str) -> dict[str, str]:
        headers: dict[str, str] = {}
        for line in response_text.split("\r\n"):
            if ":" in line:
                name, value = line.split(":", 1)
                headers[name.strip().lower()] = value.strip()
        return headers

    def _read_frame(self, client_socket: socket.socket) -> str | None:
        header = self._recv_exact(client_socket, 2)
        if not header:
            return None
        opcode = header[0] & 0x0F
        if opcode == 8:
            raise ConnectionResetError("WebSocket closed")
        if opcode != 1:
            return None
        payload_length = header[1] & 0x7F
        if payload_length == 126:
            payload_length = struct.unpack("!H", self._recv_exact(client_socket, 2))[0]
        elif payload_length == 127:
            payload_length = struct.unpack("!Q", self._recv_exact(client_socket, 8))[0]
        masked = bool(header[1] & 0x80)
        if masked:
            mask = self._recv_exact(client_socket, 4)
        else:
            mask = b""
        payload = self._recv_exact(client_socket, payload_length)
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return payload.decode("utf-8")

    def _recv_exact(self, client_socket: socket.socket, size: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < size:
            chunk = client_socket.recv(size - len(chunks))
            if not chunk:
                raise ConnectionResetError("Connection closed")
            chunks.extend(chunk)
        return bytes(chunks)

    def wait_for_update(self, timeout: float = 1.0) -> dict[str, Any]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                return self._queue.get_nowait()
            except queue.Empty:
                time.sleep(0.01)
        raise TimeoutError("No websocket update received")

    def stop(self) -> None:
        self._stop_event.set()
        socket_to_close = self._socket
        if socket_to_close is not None:
            try:
                socket_to_close.close()
            except Exception:
                pass
        self._socket = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
