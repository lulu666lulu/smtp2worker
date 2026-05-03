#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import json
import logging
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from typing import Any


LOG = logging.getLogger("smtp2http")


@dataclass(frozen=True)
class Config:
    listen_host: str
    listen_port: int
    worker_url: str
    bridge_token: str
    smtp_username: str | None
    smtp_password: str | None
    hostname: str
    http_timeout: float
    max_message_bytes: int
    tls_cert_file: str | None
    tls_key_file: str | None
    implicit_tls: bool
    require_starttls: bool

    @property
    def auth_enabled(self) -> bool:
        return bool(self.smtp_username or self.smtp_password)


@dataclass
class UpstreamResult:
    status: int
    body: str

    @property
    def accepted(self) -> bool:
        return 200 <= self.status <= 299

    @property
    def permanent_failure(self) -> bool:
        return 400 <= self.status <= 499


class SMTPError(Exception):
    pass


class TooLargeError(SMTPError):
    pass


class SMTPConnection:
    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        config: Config,
        tls_context: ssl.SSLContext | None,
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.config = config
        self.tls_context = tls_context
        self.peer = writer.get_extra_info("peername")
        self.tls_active = bool(writer.get_extra_info("ssl_object"))
        self.reset_transaction()
        self.helo: str | None = None
        self.authenticated = not config.auth_enabled

    def reset_transaction(self) -> None:
        self.mail_from: str | None = None
        self.rcpt_to: list[str] = []

    async def serve(self) -> None:
        LOG.info("connection opened from %s", self.peer)
        try:
            await self.reply(220, f"{self.config.hostname} smtp2http ready")
            while not self.writer.is_closing():
                line = await self.readline()
                if line is None:
                    break
                if not line:
                    await self.reply(500, "Empty command")
                    continue
                await self.handle_command(line)
        except ConnectionResetError:
            LOG.info("connection reset by %s", self.peer)
        except Exception:
            LOG.exception("connection failed for %s", self.peer)
            if not self.writer.is_closing():
                await self.reply(451, "Requested action aborted: local error")
        finally:
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except Exception:
                pass
            LOG.info("connection closed from %s", self.peer)

    async def readline(self) -> str | None:
        raw = await self.reader.readline()
        if raw == b"":
            return None
        if len(raw) > 8192:
            await self.reply(500, "Line too long")
            return ""
        try:
            return raw.decode("utf-8", errors="replace").rstrip("\r\n")
        except UnicodeDecodeError:
            return raw.decode("latin1", errors="replace").rstrip("\r\n")

    async def reply(self, code: int, message: str | list[str]) -> None:
        if isinstance(message, list):
            for item in message[:-1]:
                self.writer.write(f"{code}-{item}\r\n".encode("utf-8"))
            self.writer.write(f"{code} {message[-1]}\r\n".encode("utf-8"))
        else:
            self.writer.write(f"{code} {message}\r\n".encode("utf-8"))
        await self.writer.drain()

    async def handle_command(self, line: str) -> None:
        command, _, arg = line.partition(" ")
        command = command.upper()
        arg = arg.strip()

        if command in {"EHLO", "HELO"}:
            self.helo = arg or None
            self.reset_transaction()
            await self.reply(250, self.capabilities(command == "EHLO"))
        elif command == "NOOP":
            await self.reply(250, "OK")
        elif command == "RSET":
            self.reset_transaction()
            await self.reply(250, "OK")
        elif command == "QUIT":
            await self.reply(221, "Bye")
            self.writer.close()
        elif command == "STARTTLS":
            await self.handle_starttls()
        elif command == "AUTH":
            await self.handle_auth(arg)
        elif command == "MAIL":
            await self.handle_mail(arg)
        elif command == "RCPT":
            await self.handle_rcpt(arg)
        elif command == "DATA":
            await self.handle_data()
        else:
            await self.reply(502, "Command not implemented")

    def capabilities(self, extended: bool) -> str | list[str]:
        if not extended:
            return f"{self.config.hostname} Hello"

        caps = [
            f"{self.config.hostname} Hello",
            f"SIZE {self.config.max_message_bytes}",
            "8BITMIME",
            "SMTPUTF8",
        ]
        if self.tls_context and not self.tls_active and hasattr(self.writer, "start_tls"):
            caps.append("STARTTLS")
        if self.config.auth_enabled and (self.tls_active or not self.config.require_starttls):
            caps.append("AUTH PLAIN LOGIN")
        caps.append("HELP")
        return caps

    async def handle_starttls(self) -> None:
        if self.tls_active:
            await self.reply(503, "TLS is already active")
            return
        if not self.tls_context:
            await self.reply(454, "TLS not available")
            return
        if not hasattr(self.writer, "start_tls"):
            await self.reply(454, "STARTTLS requires Python 3.11 or newer")
            return

        await self.reply(220, "Ready to start TLS")
        await self.writer.start_tls(self.tls_context)
        self.tls_active = True
        self.helo = None
        self.authenticated = not self.config.auth_enabled
        self.reset_transaction()

    async def handle_auth(self, arg: str) -> None:
        if not self.config.auth_enabled:
            await self.reply(503, "Authentication is not configured")
            return
        if self.authenticated:
            await self.reply(503, "Already authenticated")
            return
        if self.config.require_starttls and not self.tls_active:
            await self.reply(538, "Encryption required for requested authentication mechanism")
            return

        mechanism, _, initial_response = arg.partition(" ")
        mechanism = mechanism.upper()
        initial_response = initial_response.strip()

        if mechanism == "PLAIN":
            await self.auth_plain(initial_response)
        elif mechanism == "LOGIN":
            await self.auth_login(initial_response)
        else:
            await self.reply(504, "Unrecognized authentication type")

    async def auth_plain(self, initial_response: str) -> None:
        if not initial_response:
            await self.reply(334, "")
            initial_response = await self.readline() or ""

        try:
            decoded = base64.b64decode(initial_response, validate=True).decode("utf-8", "replace")
        except (binascii.Error, UnicodeDecodeError):
            await self.reply(501, "Invalid base64 data")
            return

        parts = decoded.split("\x00")
        username = parts[-2] if len(parts) >= 2 else ""
        password = parts[-1] if parts else ""
        await self.finish_auth(username, password)

    async def auth_login(self, initial_response: str) -> None:
        username = ""
        if initial_response:
            try:
                username = base64.b64decode(initial_response, validate=True).decode("utf-8", "replace")
            except (binascii.Error, UnicodeDecodeError):
                await self.reply(501, "Invalid username")
                return
        else:
            await self.reply(334, base64.b64encode(b"Username:").decode("ascii"))
            username_response = await self.readline() or ""
            try:
                username = base64.b64decode(username_response, validate=True).decode("utf-8", "replace")
            except (binascii.Error, UnicodeDecodeError):
                await self.reply(501, "Invalid username")
                return

        await self.reply(334, base64.b64encode(b"Password:").decode("ascii"))
        password_response = await self.readline() or ""
        try:
            password = base64.b64decode(password_response, validate=True).decode("utf-8", "replace")
        except (binascii.Error, UnicodeDecodeError):
            await self.reply(501, "Invalid password")
            return

        await self.finish_auth(username, password)

    async def finish_auth(self, username: str, password: str) -> None:
        if username == (self.config.smtp_username or "") and password == (self.config.smtp_password or ""):
            self.authenticated = True
            await self.reply(235, "2.7.0 Authentication successful")
        else:
            await self.reply(535, "5.7.8 Authentication credentials invalid")

    async def handle_mail(self, arg: str) -> None:
        if not await self.ready_for_mail():
            return
        if not arg.upper().startswith("FROM:"):
            await self.reply(501, "Syntax: MAIL FROM:<address>")
            return
        address = extract_path(arg[5:].strip())
        if address is None:
            await self.reply(501, "Bad sender address syntax")
            return
        self.reset_transaction()
        self.mail_from = address
        await self.reply(250, "2.1.0 Sender OK")

    async def handle_rcpt(self, arg: str) -> None:
        if not await self.ready_for_mail():
            return
        if self.mail_from is None:
            await self.reply(503, "Need MAIL before RCPT")
            return
        if not arg.upper().startswith("TO:"):
            await self.reply(501, "Syntax: RCPT TO:<address>")
            return
        address = extract_path(arg[3:].strip())
        if address is None:
            await self.reply(501, "Bad recipient address syntax")
            return
        self.rcpt_to.append(address)
        await self.reply(250, "2.1.5 Recipient OK")

    async def ready_for_mail(self) -> bool:
        if self.config.require_starttls and not self.tls_active:
            await self.reply(530, "Must issue STARTTLS first")
            return False
        if not self.authenticated:
            await self.reply(530, "Authentication required")
            return False
        return True

    async def handle_data(self) -> None:
        if not await self.ready_for_mail():
            return
        if self.mail_from is None or not self.rcpt_to:
            await self.reply(503, "Need MAIL and RCPT before DATA")
            return

        await self.reply(354, "End data with <CR><LF>.<CR><LF>")
        try:
            raw_message = await self.read_data()
            payload = build_payload(
                raw_message=raw_message,
                mail_from=self.mail_from,
                rcpt_to=self.rcpt_to,
                helo=self.helo,
                peer=str(self.peer),
            )
            result = await forward_to_worker(self.config, payload)
        except TooLargeError:
            await self.reply(552, "Message size exceeds fixed maximum message size")
            self.reset_transaction()
            return
        except Exception:
            LOG.exception("failed to forward message from %s to %s", self.mail_from, self.rcpt_to)
            await self.reply(451, "4.3.0 Upstream delivery failed")
            self.reset_transaction()
            return

        if result.accepted:
            LOG.info("forwarded message from %s to %s: HTTP %s", self.mail_from, self.rcpt_to, result.status)
            await self.reply(250, "2.0.0 Message accepted for delivery")
        elif result.permanent_failure:
            LOG.warning("worker rejected message permanently: HTTP %s %s", result.status, result.body[:300])
            await self.reply(550, "5.0.0 Worker rejected message")
        else:
            LOG.warning("worker rejected message temporarily: HTTP %s %s", result.status, result.body[:300])
            await self.reply(451, "4.3.0 Upstream delivery failed")
        self.reset_transaction()

    async def read_data(self) -> bytes:
        chunks: list[bytes] = []
        total = 0
        while True:
            line = await self.reader.readline()
            if line == b"":
                raise SMTPError("connection closed during DATA")
            if line in {b".\r\n", b".\n"}:
                break
            if line.startswith(b".."):
                line = line[1:]
            total += len(line)
            if total > self.config.max_message_bytes:
                raise TooLargeError()
            chunks.append(line)
        return b"".join(chunks)


def extract_path(value: str) -> str | None:
    value = value.strip()
    if value.startswith("<"):
        end = value.find(">")
        if end == -1:
            return None
        return value[1:end]
    return value.split()[0] if value else None


def build_payload(raw_message: bytes, mail_from: str, rcpt_to: list[str], helo: str | None, peer: str) -> dict[str, Any]:
    parsed = BytesParser(policy=policy.default).parsebytes(raw_message)
    text, html = extract_message_bodies(parsed)
    headers = {key: value for key, value in parsed.items()}

    return {
        "envelope": {
            "from": mail_from,
            "to": rcpt_to,
        },
        "message": {
            "from": parsed.get("From") or mail_from,
            "to": parsed.get("To"),
            "cc": parsed.get("Cc"),
            "replyTo": parsed.get("Reply-To"),
            "subject": parsed.get("Subject") or "",
            "text": text,
            "html": html,
            "headers": headers,
            "rawBase64": base64.b64encode(raw_message).decode("ascii"),
        },
        "smtp": {
            "helo": helo,
            "peer": peer,
            "receivedAt": int(time.time()),
        },
    }


def extract_message_bodies(message: Any) -> tuple[str, str]:
    text_parts: list[str] = []
    html_parts: list[str] = []

    if message.is_multipart():
        for part in message.walk():
            if part.is_multipart():
                continue
            if part.get_content_disposition() == "attachment":
                continue
            append_body_part(part, text_parts, html_parts)
    else:
        append_body_part(message, text_parts, html_parts)

    return "\n".join(text_parts).strip(), "\n".join(html_parts).strip()


def append_body_part(part: Any, text_parts: list[str], html_parts: list[str]) -> None:
    content_type = part.get_content_type()
    if content_type not in {"text/plain", "text/html"}:
        return
    try:
        content = part.get_content()
    except LookupError:
        payload = part.get_payload(decode=True) or b""
        charset = part.get_content_charset() or "utf-8"
        content = payload.decode(charset, "replace")

    if content_type == "text/html":
        html_parts.append(str(content))
    else:
        text_parts.append(str(content))


def post_to_worker(config: Config, payload: dict[str, Any]) -> UpstreamResult:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        config.worker_url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {config.bridge_token}",
            "X-SMTP2HTTP-Token": config.bridge_token,
            "User-Agent": "smtp2http/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=config.http_timeout) as response:
            response_body = response.read(8192).decode("utf-8", "replace")
            return UpstreamResult(status=response.status, body=response_body)
    except urllib.error.HTTPError as exc:
        response_body = exc.read(8192).decode("utf-8", "replace")
        return UpstreamResult(status=exc.code, body=response_body)


async def forward_to_worker(config: Config, payload: dict[str, Any]) -> UpstreamResult:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, post_to_worker, config, payload)


def load_tls_context(config: Config) -> ssl.SSLContext | None:
    if not config.tls_cert_file and not config.tls_key_file:
        return None
    if not config.tls_cert_file or not config.tls_key_file:
        raise SystemExit("TLS_CERT_FILE and TLS_KEY_FILE must be set together")
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(config.tls_cert_file, config.tls_key_file)
    return context


async def run(config: Config) -> None:
    tls_context = load_tls_context(config)
    server_ssl = tls_context if config.implicit_tls else None
    server = await asyncio.start_server(
        lambda reader, writer: SMTPConnection(reader, writer, config, tls_context).serve(),
        config.listen_host,
        config.listen_port,
        ssl=server_ssl,
    )

    sockets = ", ".join(str(sock.getsockname()) for sock in server.sockets or [])
    LOG.info("smtp2http listening on %s", sockets)
    if config.auth_enabled:
        LOG.info("SMTP AUTH enabled for user %s", config.smtp_username)
    else:
        LOG.warning("SMTP AUTH disabled; keep this listener private or bind to 127.0.0.1")

    async with server:
        await server.serve_forever()


def parse_args(argv: list[str]) -> Config:
    parser = argparse.ArgumentParser(description="SMTP to HTTP bridge for Cloudflare Worker mail delivery")
    parser.add_argument("--listen-host", default=os.getenv("SMTP_LISTEN_HOST", "127.0.0.1"))
    parser.add_argument("--listen-port", type=int, default=int(os.getenv("SMTP_LISTEN_PORT", "2525")))
    parser.add_argument("--worker-url", default=os.getenv("WORKER_URL"))
    parser.add_argument("--bridge-token", default=os.getenv("BRIDGE_TOKEN"))
    parser.add_argument("--smtp-username", default=os.getenv("SMTP_USERNAME"))
    parser.add_argument("--smtp-password", default=os.getenv("SMTP_PASSWORD"))
    parser.add_argument("--hostname", default=os.getenv("SMTP_HOSTNAME", "smtp2http.local"))
    parser.add_argument("--http-timeout", type=float, default=float(os.getenv("HTTP_TIMEOUT", "15")))
    parser.add_argument("--max-message-bytes", type=int, default=int(os.getenv("MAX_MESSAGE_BYTES", str(1024 * 1024))))
    parser.add_argument("--tls-cert-file", default=os.getenv("TLS_CERT_FILE"))
    parser.add_argument("--tls-key-file", default=os.getenv("TLS_KEY_FILE"))
    parser.add_argument("--implicit-tls", action="store_true", default=os.getenv("IMPLICIT_TLS", "").lower() == "true")
    parser.add_argument("--require-starttls", action="store_true", default=os.getenv("REQUIRE_STARTTLS", "").lower() == "true")
    parser.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "INFO"))
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not args.worker_url:
        parser.error("--worker-url or WORKER_URL is required")
    if not args.bridge_token:
        parser.error("--bridge-token or BRIDGE_TOKEN is required")
    if bool(args.smtp_username) != bool(args.smtp_password):
        parser.error("SMTP_USERNAME and SMTP_PASSWORD must be set together")
    if args.require_starttls and not args.implicit_tls and not hasattr(asyncio.StreamWriter, "start_tls"):
        parser.error("--require-starttls needs Python 3.11 or newer; use --implicit-tls on this Python")

    return Config(
        listen_host=args.listen_host,
        listen_port=args.listen_port,
        worker_url=args.worker_url,
        bridge_token=args.bridge_token,
        smtp_username=args.smtp_username,
        smtp_password=args.smtp_password,
        hostname=args.hostname,
        http_timeout=args.http_timeout,
        max_message_bytes=args.max_message_bytes,
        tls_cert_file=args.tls_cert_file,
        tls_key_file=args.tls_key_file,
        implicit_tls=args.implicit_tls,
        require_starttls=args.require_starttls,
    )


def main(argv: list[str] | None = None) -> int:
    config = parse_args(argv or sys.argv[1:])
    try:
        asyncio.run(run(config))
    except KeyboardInterrupt:
        LOG.info("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
