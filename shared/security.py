# -*- coding: utf-8 -*-
"""Подпись запросов между сервисами (HMAC-SHA256)."""
import hashlib
import hmac
import time

HDR_SIG = "X-MARS-Sig"
HDR_TS = "X-MARS-Ts"
MAX_SKEW = 60  # секунд


def sign(secret, method, path, body, ts=None):
    ts = str(int(ts if ts is not None else time.time()))
    msg = "\n".join([ts, method.upper(), path]).encode("utf-8") + b"\n" + (body or b"")
    sig = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return {HDR_TS: ts, HDR_SIG: sig}


def verify(secret, method, path, body, headers):
    """True, если подпись верна и запрос свежий."""
    ts = headers.get(HDR_TS) or ""
    sig = headers.get(HDR_SIG) or ""
    try:
        if abs(time.time() - int(ts)) > MAX_SKEW:
            return False
    except ValueError:
        return False
    expect = sign(secret, method, path, body, ts)[HDR_SIG]
    return hmac.compare_digest(expect, sig)
