"""TLS context for talking to the NanoKVM's own HTTPS server over loopback.

Shared module: edit _shared/localtls.py, then run `python3 _tools/sync_shared.py`.

The server uses a self-signed certificate stored on the device. Rather than
disabling verification, trust exactly that certificate (pinning). The hostname
isn't checked because the certificate is issued for "localhost" while clients
connect to 127.0.0.1; pinning the one certificate is the stronger check anyway.
Off-device (tests, previews) the file doesn't exist and an unverified context is
used; callers must only ever use this for loopback URLs.
"""

from __future__ import annotations

import os
import ssl

DEVICE_CERT = "/etc/kvm/server.crt"
DEVICE_MARKER = "/etc/kvm"   # present on NanoKVM devices


def context(cert_path: str = DEVICE_CERT) -> ssl.SSLContext:
    if os.path.isfile(cert_path):
        ctx = ssl.create_default_context(cafile=cert_path)
        ctx.check_hostname = False
        # Pinning one exact certificate; strict X.509 extension checks (Python 3.13
        # default) could reject a valid self-signed device cert for no benefit.
        ctx.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
        return ctx
    if os.path.isdir(DEVICE_MARKER):
        raise ssl.SSLError(f"device certificate {cert_path} is missing; refusing an unverified connection")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)  # off-device (tests, previews): loopback-only callers
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx
