"""Test standard verified TLS configurations against the public release endpoint."""
from __future__ import annotations

import os
import ssl

from samad_network_diagnostic import probe

HOST = "dining.sbu.ac.ir"
PATH = "/rest/config/release"
PROFILES = ("TLS_1_2", "TLS_1_3", "DEFAULT_ALPN_HTTP_1_1")


def make_context(profile: str) -> ssl.SSLContext:
    # Keep system trust and hostname verification; never lower the security level.
    context = ssl.create_default_context()
    if profile == "TLS_1_2":
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.maximum_version = ssl.TLSVersion.TLSv1_2
    elif profile == "TLS_1_3":
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        context.maximum_version = ssl.TLSVersion.TLSv1_3
    elif profile == "DEFAULT_ALPN_HTTP_1_1":
        context.set_alpn_protocols(["http/1.1"])
    else:
        raise ValueError("unknown TLS profile")
    return context


def main() -> int:
    lines = ["# SBU verified TLS compatibility", "", "Public release endpoint only; certificate and hostname verification enabled.", ""]
    successes = 0
    for profile in PROFILES:
        result = probe(HOST, PATH, make_context(profile))
        success = result.https == "SUCCESS" and result.release == "VALID"
        successes += success
        # Values are fixed labels, numeric HTTP status, or a sanitized diagnostic category.
        line = (f"{profile}: TLS={result.tls}; TLS detail={result.tls_detail}; "
                f"HTTPS={result.https}; HTTP status={result.status}; "
                f"Release={result.release}; Error={result.error}")
        lines.append(f"- {line}")
        # Annotations also make these safe, fixed diagnostics available through GitHub's API.
        print(f"::notice title=TLS compatibility::{line}")
    report = "\n".join(lines) + "\n"
    print(report)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
            summary.write(report)
    return 0 if successes else 1


if __name__ == "__main__":
    raise SystemExit(main())
