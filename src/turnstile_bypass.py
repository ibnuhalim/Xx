"""
Turnstile Bypass — Multi-approach solver.

Cara pakai di signup_flow.py:
    from .turnstile_bypass import verify_cf, is_turnstile_present, get_turnstile_token, inject_turnstile_token
"""

from __future__ import annotations

import asyncio
import json
from typing import Optional


async def verify_cf(page, timeout: float = 60.0) -> str:
    """
    Solve Turnstile menggunakan nodriver's built-in method.
    Return token string (kosong jika gagal).
    """
    try:
        try:
            result = await page.verify_cf(timeout=timeout)
        except TypeError:
            # versi nodriver lama tidak support timeout=
            result = await page.verify_cf()
        if result is None:
            return ""
        return str(result)
    except Exception as e:
        print(f"    ⚠️ verify_cf exception: {e}")
        return ""


async def is_turnstile_present(page) -> bool:
    """Cek apakah widget Turnstile ada di DOM."""
    try:
        return bool(await page.evaluate('''(() => {
            if (document.querySelector('input[name="cf_challenge_response"]')) return true;
            if (document.querySelector('input[name="cf-turnstile-response"]')) return true;
            const iframes = document.querySelectorAll("iframe");
            for (const f of iframes) {
                if (f.src && f.src.includes("challenges.cloudflare.com")) return true;
            }
            const body = document.body ? document.body.innerText : "";
            if (body.includes("Verify you are human") || body.includes("Let us know you are human")) return true;
            return false;
        })()''', return_by_value=True))
    except Exception:
        return False


async def get_turnstile_token(page) -> str:
    """Baca token dari hidden input cf-turnstile-response."""
    try:
        val = await page.evaluate('''(() => {
            const el = document.querySelector('input[name="cf-turnstile-response"]');
            return el ? (el.value || '') : '';
        })()''', return_by_value=True)
        return str(val or "")
    except Exception:
        return ""


async def inject_turnstile_token(page, token: str) -> bool:
    """Paksa tulis token ke semua hidden input Turnstile."""
    if not token:
        return False
    try:
        await page.evaluate(
            f'''(() => {{
                const t = {json.dumps(token)};
                document.querySelectorAll('input[name="cf-turnstile-response"]').forEach(el => {{
                    el.value = t;
                    el.dispatchEvent(new Event('change', {{bubbles:true}}));
                }});
                document.querySelectorAll('input[name="cf_challenge_response"]').forEach(el => {{
                    el.value = t;
                }});
            }})()'''
        )
        return True
    except Exception:
        return False


__all__ = [
    "verify_cf",
    "is_turnstile_present",
    "get_turnstile_token",
    "inject_turnstile_token",
]
