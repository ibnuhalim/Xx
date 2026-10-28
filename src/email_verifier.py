"""Cloudflare email verification via Cair inbox."""

from __future__ import annotations

import asyncio
import html
import re
import time
from typing import Any
from urllib.parse import unquote

import nodriver as uc

from .email_generator import EmailGenerator


class EmailVerifyResult:
    def __init__(self, success: bool, error: str = "", link: str = ""):
        self.success = success
        self.error = error
        self.link = link


CF_VERIFY_PATTERNS = [
    r'https://dash\.cloudflare\.com/[^\"><\s]+',
    r'https://www\.cloudflare\.com/[^\"><\s]+',
    r'https://cloudflare\.com/[^\"><\s]+',
]


def _mail_blob(mail: dict[str, Any]) -> str:
    """
    Gabungkan isi mail dengan prioritas field RAW.

    Catatan Cair: field `html`/`text` adalah versi sanitized yang MEMOTONG
    query string (mis. `?token=abc` -> `?tokensbc`). Selalu prioritaskan
    `rawHtml`/`rawText` agar link verifikasi tetap utuh.
    """
    parts: list[str] = []

    subject = mail.get("subject") or ""
    sender = mail.get("from") or mail.get("sender") or ""
    if isinstance(sender, dict):
        sender = sender.get("address") or sender.get("email") or ""
    if subject or sender:
        parts.append(f"{subject}\n{sender}")

    for key in (
        "rawText", "rawHtml",      # <- prioritas: query string utuh
        "text", "html",            # sanitized (jaga-jaga)
        "body", "content", "snippet",
    ):
        val = mail.get(key)
        if isinstance(val, str) and val.strip():
            parts.append(val)

    return "\n".join(parts)


def _is_cloudflare_verification(mail: dict[str, Any]) -> bool:
    blob = _mail_blob(mail).lower()
    return "cloudflare" in blob and any(
        w in blob for w in ("verify", "verification", "confirm", "activate", "email")
    )


def extract_verification_link(mail: dict[str, Any]) -> str:
    """Ekstrak link verifikasi Cloudflare dari isi mail."""
    blob = html.unescape(_mail_blob(mail))
    blob = blob.replace("\\/", "/")

    candidates: list[str] = []
    for pattern in CF_VERIFY_PATTERNS:
        candidates.extend(re.findall(pattern, blob, flags=re.I))

    cleaned: list[str] = []
    for url in candidates:
        url = url.rstrip("').,;]>\"\\")
        url = unquote(url)
        low = url.lower()
        if any(k in low for k in (
            "verify", "confirm", "activation", "email", "token", "challenge",
        )):
            cleaned.append(url)

    if not cleaned:
        for url in candidates:
            url = url.rstrip("').,;]>\"\\")
            if "dash.cloudflare.com" in url.lower():
                cleaned.append(unquote(url))

    return cleaned[0] if cleaned else ""


async def verify_cloudflare_email(
    page: uc.Tab,
    mail_api: str,
    jwt: str,
    timeout: int = 120,
    poll_interval: int = 5,
) -> EmailVerifyResult:
    """Poll inbox Cair, buka link verifikasi Cloudflare di sesi browser yang sama."""
    if not jwt:
        return EmailVerifyResult(False, error="missing_mail_jwt")

    print("  [verify] Menunggu email verifikasi Cloudflare...")
    gen = EmailGenerator(mail_api, [])
    start = time.time()

    try:
        while time.time() - start < timeout:
            try:
                mails = gen.check_inbox(jwt)
            except Exception as e:
                print(f"  [verify] inbox error: {e}")
                await asyncio.sleep(poll_interval)
                continue

            for mail in mails:
                mail_id = str(
                    mail.get("id") or mail.get("mail_id")
                    or mail.get("uid") or mail.get("_id") or ""
                )

                full = mail
                # Cair list tidak menyertakan body: fetch detail jika belum ada
                if mail_id and not any(
                    full.get(k) for k in
                    ("rawHtml", "rawText", "text", "html", "body", "raw")
                ):
                    try:
                        full = gen.get_mail(jwt, mail_id)
                    except Exception:
                        full = mail

                if not _is_cloudflare_verification(full):
                    continue

                link = extract_verification_link(full)
                if not link:
                    continue

                print("  [verify] Link verifikasi ditemukan; membuka...")
                await page.get(link)
                await asyncio.sleep(15)

                body_raw = await page.evaluate(
                    "document.body ? document.body.innerText : ''",
                    return_by_value=True,
                )
                body = str(body_raw).lower()
                url = str(await page.evaluate("location.href", return_by_value=True)).lower()

                if any(k in body for k in (
                    "verified", "success",
                    "email has been verified", "already verified",
                )):
                    return EmailVerifyResult(True, link=link)
                if "dash.cloudflare.com" in url and "login" not in url:
                    return EmailVerifyResult(True, link=link)
                if "expired" in body or "invalid" in body:
                    return EmailVerifyResult(
                        False, error="verification_link_invalid_or_expired", link=link,
                    )
                return EmailVerifyResult(True, link=link)

            await asyncio.sleep(poll_interval)

        return EmailVerifyResult(
            False, error=f"verification_email_not_found_after_{timeout}s",
        )
    finally:
        gen.close()


__all__ = [
    "EmailVerifyResult",
    "verify_cloudflare_email",
    "extract_verification_link",
]
