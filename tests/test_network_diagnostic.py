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
