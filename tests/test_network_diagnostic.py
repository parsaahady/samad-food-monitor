"""Offline checks of the unauthenticated diagnostic, with no external traffic."""
from scripts import samad_network_diagnostic as diagnostic
import socket


def test_release_structure():
    assert diagnostic.valid_release(b'{"payload":{"releaseTime":"260115103000"}}')
    assert not diagnostic.valid_release(b'{"payload":{"releaseTime":"garbage"}}')
    assert not diagnostic.valid_release(b'not json')
    assert not diagnostic.valid_release(b'[]')


def test_dns_failure(monkeypatch):
    def fail(*args, **kwargs):
        raise socket.gaierror("sensitive details")
    monkeypatch.setattr(socket, "getaddrinfo", fail)
    result = diagnostic.probe("example.test", "/")
    assert result.dns == "FAILURE"
    assert result.error == "DNS_FAILURE"
    assert result.https == "NOT TESTED"


def test_https_valid_and_no_body_leak(monkeypatch, capsys):
    monkeypatch.setattr(diagnostic, "TARGETS", (("example.test", "/rest/config/release"),))
    monkeypatch.setattr(diagnostic, "run_probe", lambda *args: diagnostic.Result(dns="SUCCESS", tcp="SUCCESS", tls="SUCCESS", https="SUCCESS", release="VALID"))
    assert diagnostic.main() == 0
    assert "Release Endpoint: VALID" in capsys.readouterr().out


def test_invalid_release_fails_workflow(monkeypatch):
    monkeypatch.setattr(diagnostic, "TARGETS", (("example.test", "/rest/config/release"),))
    monkeypatch.setattr(diagnostic, "run_probe", lambda *args: diagnostic.Result(https="SUCCESS", release="INVALID"))
    assert diagnostic.main() == 1


def test_probe_https_release(monkeypatch):
    class Socket:
        def settimeout(self, value): pass
        def connect(self, address): pass
        def getpeername(self): return ("203.0.113.1", 443)
        def close(self): pass
    class Context:
        def wrap_socket(self, raw, server_hostname): return raw
    class Response:
        status = 200
        def getheader(self, name, default): return "application/json; charset=utf-8"
        def read(self, n): return b'{"payload":{"releaseTime":"260115103000"}}'
    class Connection:
        def __init__(self, *args, **kwargs): pass
        def request(self, method, path, headers): assert method == "GET"
        def getresponse(self): return Response()
        def close(self): pass
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("203.0.113.1", 443))])
    monkeypatch.setattr(socket, "socket", lambda *a: Socket())
    monkeypatch.setattr(diagnostic.ssl, "create_default_context", lambda: Context())
    monkeypatch.setattr(diagnostic.http.client, "HTTPSConnection", Connection)
    result = diagnostic.probe("example.test", "/rest/config/release")
    assert (result.dns, result.tcp, result.tls, result.https, result.release) == ("SUCCESS", "SUCCESS", "SUCCESS", "SUCCESS", "VALID")
    assert result.remote_ip == "203.0.113.1"
    assert result.content_type == "application/json"


def test_tls_verification_failure_is_reported_without_exception_text(monkeypatch, capsys):
    import ssl

    class Socket:
        def settimeout(self, value): pass
        def connect(self, address): pass
        def getpeername(self): return ("203.0.113.1", 443)
        def close(self): pass

    class Context:
        def wrap_socket(self, raw, server_hostname):
            assert server_hostname == "example.test"
            exc = ssl.SSLCertVerificationError("secret details must not be logged")
            exc.verify_code = 10
            exc.verify_message = "secret details must not be logged"
            raise exc

    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("203.0.113.1", 443))])
    monkeypatch.setattr(socket, "socket", lambda *a: Socket())
    monkeypatch.setattr(ssl, "create_default_context", lambda: Context())
    result = diagnostic.probe("example.test", "/rest/config/release")
    assert result.tls == "FAILURE"
    assert result.https == "NOT TESTED"
    assert result.error == "TLS_FAILURE"
    assert result.tls_detail == "CERT_VERIFY_FAILED (CERT_EXPIRED, code 10)"
    monkeypatch.setattr(diagnostic, "TARGETS", (("example.test", "/rest/config/release"),))
    monkeypatch.setattr(diagnostic, "run_probe", lambda *args: result)
    assert diagnostic.main() == 1
    output = capsys.readouterr().out
    assert "CERT_EXPIRED" in output
    assert "secret details" not in output


def test_tls_error_details_are_allowlisted():
    import ssl
    exc = ssl.SSLCertVerificationError("sensitive")
    exc.verify_code = 62
    assert diagnostic.tls_failure_detail(exc) == "CERT_VERIFY_FAILED (HOSTNAME_MISMATCH, code 62)"
    exc.verify_code = 999
    assert diagnostic.tls_failure_detail(exc) == "CERT_VERIFY_FAILED (OTHER_VERIFY_ERROR, code 999)"
    assert diagnostic.tls_failure_detail(ssl.SSLError("sensitive")) == "HANDSHAKE_FAILED (unknown reason)"
    assert diagnostic.tls_failure_detail(ConnectionResetError("sensitive")) == "CONNECTION_RESET"
    assert diagnostic.tls_failure_detail(TimeoutError("sensitive")) == "HANDSHAKE_TIMEOUT"


def test_tls_compatibility_profiles_keep_certificate_checks():
    import importlib.util
    import pathlib
    import ssl
    import sys
    import pytest

    # The script also runs directly without installing the project.
    monkey_path = pathlib.Path(diagnostic.__file__).parent
    sys.path.insert(0, str(monkey_path))
    try:
        spec = importlib.util.spec_from_file_location("tls_compat", monkey_path / "samad_tls_compatibility.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(monkey_path))
    for profile in module.PROFILES:
        context = module.make_context(profile)
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname
    assert module.make_context("TLS_1_2").maximum_version == ssl.TLSVersion.TLSv1_2
    assert module.make_context("TLS_1_3").minimum_version == ssl.TLSVersion.TLSv1_3
    with pytest.raises(ValueError):
        module.make_context("unverified")
