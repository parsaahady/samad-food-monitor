"""Unauthenticated, read-only connectivity checks; never print response bodies."""
from __future__ import annotations

import http.client
import json
import os
import re
import socket
import ssl
import time
from dataclasses import dataclass

TIMEOUT = 8
MAX_BODY = 1024 * 1024
TARGETS = (("samad.app", "/food/reserve"), ("dining.sbu.ac.ir", "/"),
           ("dining.sbu.ac.ir", "/rest/config/release"))


@dataclass
class Result:
    dns: str = "NOT TESTED"
    tcp: str = "NOT TESTED"
    tls: str = "NOT TESTED"
    https: str = "NOT TESTED"
    remote_ip: str = "-"
    status: str = "-"
    content_type: str = "-"
    elapsed_ms: int = 0
    release: str = "NOT TESTED"
    error: str = "-"
    tls_detail: str = "-"


def tls_failure_detail(exc: BaseException) -> str:
    """Return only predefined TLS labels and numeric verification codes, never exception text."""
    if isinstance(exc, ssl.SSLCertVerificationError):
        code = getattr(exc, "verify_code", None)
        labels = {
            9: "CERT_NOT_YET_VALID",
            10: "CERT_EXPIRED",
            18: "SELF_SIGNED_CERT",
            19: "SELF_SIGNED_CERT_IN_CHAIN",
            20: "ISSUER_CERT_UNAVAILABLE",
            21: "UNABLE_TO_VERIFY_FIRST_CERT",
            23: "CERT_REVOKED",
            62: "HOSTNAME_MISMATCH",
        }
        if type(code) is int and 0 <= code <= 9999:
            return f"CERT_VERIFY_FAILED ({labels.get(code, 'OTHER_VERIFY_ERROR')}, code {code})"
        return "CERT_VERIFY_FAILED (code unavailable)"
    if isinstance(exc, ssl.SSLError):
        reason = getattr(exc, "reason", None)
        allowed = {
            "SSLV3_ALERT_HANDSHAKE_FAILURE", "TLSV1_ALERT_PROTOCOL_VERSION",
            "TLSV1_ALERT_INTERNAL_ERROR", "TLSV1_UNRECOGNIZED_NAME",
            "UNEXPECTED_EOF_WHILE_READING", "WRONG_VERSION_NUMBER",
            "NO_SHARED_CIPHER", "CERTIFICATE_VERIFY_FAILED",
        }
        return f"HANDSHAKE_FAILED ({reason})" if reason in allowed else "HANDSHAKE_FAILED (unknown reason)"
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "HANDSHAKE_TIMEOUT"
    if isinstance(exc, ConnectionResetError):
        return "CONNECTION_RESET"
    if isinstance(exc, ConnectionAbortedError):
        return "CONNECTION_ABORTED"
    return "TLS_TRANSPORT_FAILURE"


def valid_release(body: bytes) -> bool:
    try:
        obj = json.loads(body)
        payload = obj.get("payload") if isinstance(obj, dict) else None
        value = payload.get("releaseTime") if isinstance(payload, dict) else None
        return isinstance(value, str) and re.fullmatch(r"[0-9]{12}", value) is not None
    except (ValueError, UnicodeDecodeError):
        return False


def probe(host: str, path: str, context: ssl.SSLContext | None = None) -> Result:
    result = Result()
    start = time.monotonic()
    raw = None
    conn = None
    stage = "DNS"
    try:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        if not addresses:
            raise socket.gaierror("no addresses")
        result.dns = "SUCCESS"
        stage = "TCP"
        # Bound each connection attempt, and use at most two resolved addresses.
        for family, socktype, proto, _, address in addresses[:2]:
            raw = socket.socket(family, socktype, proto)
            raw.settimeout(TIMEOUT)
            try:
                raw.connect(address)
                break
            except OSError:
                raw.close()
                raw = None
        if raw is None:
            raise OSError("connection failed")
        result.tcp = "SUCCESS"
        result.remote_ip = raw.getpeername()[0]
        stage = "TLS"
        tls = (context if context is not None else ssl.create_default_context()).wrap_socket(raw, server_hostname=host)
        raw = None  # the TLS socket now owns it
        result.tls = "SUCCESS"
        stage = "HTTPS"
        conn = http.client.HTTPSConnection(host, timeout=TIMEOUT)
        conn.sock = tls
        conn.request("GET", path, headers={"Accept": "application/json, text/html;q=0.9", "Connection": "close"})
        response = conn.getresponse()
        result.status = str(response.status)
        # A server-controlled header is never printed verbatim.
        media = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
        result.content_type = media if re.fullmatch(r"[a-z0-9.+-]+/[a-z0-9.+-]+", media) else "unknown"
        if not 200 <= response.status < 300:
            result.https = "FAILURE"
            result.error = (f"HTTP_{response.status}" if response.status in (403, 429)
                            else "SERVER_ERROR" if response.status >= 500 else f"HTTP_{response.status}")
        else:
            result.https = "SUCCESS"
            if path == "/rest/config/release":
                body = response.read(MAX_BODY + 1)
                result.release = "VALID" if len(body) <= MAX_BODY and valid_release(body) else "INVALID"
                if result.release == "INVALID":
                    result.error = "INVALID_RELEASE_RESPONSE"
    except (OSError, ssl.SSLError, http.client.HTTPException, TimeoutError) as exc:
        setattr(result, stage.lower(), "FAILURE")
        if stage == "TLS":
            result.tls_detail = tls_failure_detail(exc)
        if stage == "DNS":
            result.error = "DNS_FAILURE"
        elif stage == "TLS" and isinstance(exc, ssl.SSLError):
            result.error = "TLS_FAILURE"
        elif isinstance(exc, (TimeoutError, socket.timeout)):
            result.error = "API_TIMEOUT"
        else:
            result.error = f"{stage}_FAILURE"
    finally:
        if conn:
            conn.close()
        if raw:
            raw.close()
        result.elapsed_ms = round((time.monotonic() - start) * 1000)
    return result


def run_probe(host: str, path: str) -> Result:
    # Retry transient failures once, never rate limits or permanent 4xx responses.
    result = probe(host, path)
    if result.error in {"DNS_FAILURE", "TCP_FAILURE", "TLS_FAILURE", "HTTPS_FAILURE", "API_TIMEOUT", "SERVER_ERROR"}:
        time.sleep(1)
        result = probe(host, path)
    return result


def main() -> int:
    lines = ["# Samad Network Diagnostics", "", "Unauthenticated GET only; response bodies are not logged.", ""]
    success = True
    for host, path in TARGETS:
        r = run_probe(host, path)
        success &= r.https == "SUCCESS" and (path != "/rest/config/release" or r.release == "VALID")
        lines += [f"## https://{host}{path}", f"- DNS: {r.dns}", f"- TCP: {r.tcp}",
                  f"- TLS: {r.tls}", f"- TLS Detail: {r.tls_detail}", f"- HTTPS: {r.https}", f"- Remote IP: {r.remote_ip}",
                  f"- HTTP Status: {r.status}", f"- Content-Type: {r.content_type}",
                  f"- Response Time: {r.elapsed_ms} ms", f"- Release Endpoint: {r.release}",
                  f"- Error: {r.error}", ""]
    report = "\n".join(lines)
    print(report)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
            summary.write(report + "\n")
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
