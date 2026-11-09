"""One-time codes from an authenticator app (completion program, package P20; master PS-01): the founder console's code (founder.py, stage B6 of the sign-in
and split). The accounts' own second factor of the old sign-in, with its recovery codes, was retired with it in stage B8 (design B.8).

Written from the texts of the two RFCs, with Python's own `hmac`, `hashlib` and `base64` (no new dependency):

* RFC 4226 (HOTP), section 5.3: HS = HMAC-SHA-1(K, C) over the 8-byte big-endian counter C; the dynamic truncation takes the low 4 bits of the last byte of HS
  as an offset, the 31 bits of the four bytes from there (the top bit masked off) as a number, and the code is that number modulo 10^Digit, left-padded with
  zeros. Section 4, R6: the shared secret is at least 128 bits, 160 recommended; here it is 160 random bits.
* RFC 6238 (TOTP), section 4: the counter is T = floor((Unix time - T0) / X), with T0 = 0 and X = 30 seconds by default, and HMAC-SHA-256 or HMAC-SHA-512 may
  stand in for HMAC-SHA-1. Section 5.2: a validator may accept a code from one time step before (and here one after, for a clock that runs fast), and must not
  accept the same code twice: the step a code was accepted at is kept, and a code of that step or an earlier one is refused.

The secret is given to the app as Base32 (RFC 4648, section 6, without the padding) in a key URI of the form authenticator apps read
(otpauth://totp/Issuer:account?secret=...&issuer=Issuer&algorithm=SHA1&digits=6&period=30), which is a published convention of the apps, not an RFC.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

#: What the app is called in an authenticator app
ISSUER = "Gridlytics"
DIGITS = 6
PERIOD = 30
#: Time steps either side of now a code is accepted from (RFC 6238, section 5.2)
WINDOW = 1
SECRET_BYTES = 20
_ALGORITHMS = {"SHA1": hashlib.sha1, "SHA256": hashlib.sha256, "SHA512": hashlib.sha512}
#: The digits a phone or keyboard in the region may type for 0 to 9: Arabic-Indic (U+0660 to U+0669), Eastern Arabic-Indic (U+06F0 to U+06F9) and full-width
#: (U+FF10 to U+FF19); a code typed in them is read as the same digits
_DIGITS = str.maketrans({chr(base + n): str(n) for base in (0x0660, 0x06F0, 0xFF10) for n in range(10)})


def new_secret() -> str:
    """A fresh shared secret of 160 random bits, as Base32 without the padding (RFC 4226, section 4, R6)."""
    return base64.b32encode(secrets.token_bytes(SECRET_BYTES)).decode("ascii").rstrip("=")


def secret_bytes(secret: str) -> bytes:
    """The key of a Base32 secret: case and spaces ignored, the padding put back. Raises ValueError for one that is not Base32."""
    text = "".join(secret.split()).upper()
    if not text or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567" for c in text):
        raise ValueError("The secret is not Base32")
    return base64.b32decode(text + "=" * (-len(text) % 8))


def hotp(key: bytes, counter: int, digits: int = DIGITS, algorithm: str = "SHA1") -> str:
    """RFC 4226, section 5.3: HMAC over the 8-byte counter, dynamic truncation, modulo 10^digits."""
    if not 6 <= digits <= 10:
        raise ValueError("A code has 6 to 10 digits")
    digest = hmac.new(key, struct.pack(">Q", counter), _ALGORITHMS[algorithm]).digest()
    offset = digest[-1] & 0x0F
    number = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(number % 10 ** digits).zfill(digits)


#: The clock the time steps are read from (a test sets its own)
clock = time.time


def step(at: float | None = None, period: int = PERIOD) -> int:
    """RFC 6238, section 4: the time step of a moment, T = floor((Unix time - T0) / X) with T0 = 0."""
    return int((clock() if at is None else at) // period)


def totp(key: bytes, at: float | None = None, digits: int = DIGITS, algorithm: str = "SHA1", period: int = PERIOD) -> str:
    """RFC 6238: HOTP with the time step as the counter."""
    return hotp(key, step(at, period), digits, algorithm)


def verify(secret: str, code, last_step: int = -1, at: float | None = None) -> int | None:
    """The time step a code is good for, within WINDOW steps of now and later than the last step a code was accepted at (a code is never accepted twice); None
    when it is not good. A code that is not six digits 0 to 9 (or the same digits in Arabic-Indic or full-width form) is not good."""
    if not isinstance(code, str):
        return None
    code = code.replace(" ", "").translate(_DIGITS)
    if len(code) != DIGITS or not (code.isascii() and code.isdigit()):
        return None
    try:
        key = secret_bytes(secret)
    except ValueError:
        return None
    now = step(at)
    for t in range(now - WINDOW, now + WINDOW + 1):
        if t > last_step and hmac.compare_digest(hotp(key, t), code):
            return t
    return None


def uri(secret: str, account: str, issuer: str = ISSUER) -> str:
    """The key URI an authenticator app reads from a QR code (otpauth://totp/...): the account is shown under the issuer's name."""
    label = quote(f"{issuer}:{account}", safe="@:")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer, safe='')}&algorithm=SHA1&digits={DIGITS}&period={PERIOD}"
