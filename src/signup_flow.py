"""
Cloudflare Signup Flow — Automated account creation via headless browser.

Handles:
1. Navigate to sign-up page (with challenge wait)
2. Fill email + password (multi-selector fallback)
3. Solve Turnstile CAPTCHA
4. Submit form
5. Extract Account ID from redirect URL

Di lingkungan CI, halaman signup Cloudflare kadang didahului halaman
challenge ("Just a moment...") atau layout berubah. Modul ini:
- Menunggu sampai challenge selesai
- Mencoba beberapa selector email/password
- Menyimpan screenshot + HTML ke ./debug/ saat gagal
"""

import asyncio
import os
import re
import time
from pathlib import Path
from typing import Optional

import nodriver as uc

from .turnstile_bypass import verify_cf, is_turnstile_present


CLOUDFLARE_SIGNUP_URL = "https://dash.cloudflare.com/sign-up"

# Beberapa selector kandidat (Cloudflare kadang mengganti struktur DOM / testid)
EMAIL_SELECTORS = [
    'input[name="email"]',
    'input[type="email"]',
    'input#email',
    'input[id="email"]',
    'input[autocomplete="email"]',
    'input[aria-label="Email"]',
    '[data-testid="signup-email"]',
    'form input:not([type="hidden"]):not([type="checkbox"])',
]

PASSWORD_SELECTORS = [
    'input[name="password"]',
    'input[type="password"]',
    'input#password',
    'input[id="password"]',
    'input[autocomplete="new-password"]',
    'input[aria-label="Password"]',
]

SUBMIT_SELECTORS = [
    'button[type="submit"]',
    'button:has-text("Sign up")',
    'button:has-text("Create Account")',
    'button:has-text("Sign Up")',
    '[data-testid="signup-submit"]',
]

DEBUG_DIR = Path("debug")


class SignupResult:
    def __init__(
        self,
        success: bool,
        email: str = "",
        password: str = "",
        account_id: str = "",
        error: str = "",
        page_url: str = "",
    ):
        self.success = success
        self.email = email
        self.password = password
        self.account_id = account_id
        self.error = error
        self.page_url = page_url

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "email": self.email,
            "password": self.password,
            "account_id": self.account_id,
            "error": self.error,
        }


async def _save_debug(page: uc.Tab, tag: str) -> None:
    """Simpan screenshot + HTML untuk diagnosis saat gagal."""
    try:
        DEBUG_DIR.mkdir(exist_ok=True)
        try:
            await page.save_screenshot(str(DEBUG_DIR / f"{tag}.png"), full_page=True)
        except Exception:
            pass
        try:
            html = await page.get_content()
            (DEBUG_DIR / f"{tag}.html").write_text(str(html), encoding="utf-8")
        except Exception:
            pass
        try:
            url = await page.evaluate("location.href", return_by_value=True)
            title = await page.evaluate("document.title", return_by_value=True)
            (DEBUG_DIR / f"{tag}.txt").write_text(
                f"URL: {url}\nTITLE: {title}\n", encoding="utf-8"
            )
            print(f"    🐛 debug tersimpan: {tag}.png/.html/.txt (url={url})")
        except Exception:
            pass
    except Exception:
        pass


async def _page_state(page: uc.Tab) -> dict:
    """Snapshot URL + title + deteksi challenge."""
    try:
        url = str(await page.evaluate("location.href", return_by_value=True))
    except Exception:
        url = ""
    try:
        title = str(await page.evaluate("document.title", return_by_value=True))
    except Exception:
        title = ""
    try:
        body = str(await page.evaluate(
            "document.body ? document.body.innerText.substring(0, 500) : ''",
            return_by_value=True,
        ))
    except Exception:
        body = ""
    low = (title + " " + body).lower()
    is_challenge = any(k in low for k in (
        "just a moment", "checking your browser", "verifying you are human",
        "attention required", "please wait",
    ))
    return {"url": url, "title": title, "body": body, "is_challenge": is_challenge}


async def _wait_challenge_done(page: uc.Tab, timeout: int = 60) -> bool:
    """Tunggu challenge Cloudflare selesai."""
    start = time.time()
    while time.time() - start < timeout:
        st = await _page_state(page)
        if not st["is_challenge"]:
            return True
        await asyncio.sleep(3)
    return False


async def _find_first(page: uc.Tab, selectors: list[str], timeout_each: float = 5.0):
    """Coba tiap selector; kembalikan (selector, element) pertama yang cocok."""
    for sel in selectors:
        try:
            el = await page.select(sel, timeout=timeout_each)
            if el:
                return sel, el
        except Exception:
            continue
    return None, None


async def signup(
    page: uc.Tab,
    email: str,
    password: str,
    max_wait: int = 30,
    retry_turnstile: bool = True,
) -> SignupResult:
    """
    Execute the Cloudflare signup flow.
    """
    # 1) Navigate to signup
    print(f"    → Buka {CLOUDFLARE_SIGNUP_URL}")
    await page.get(CLOUDFLARE_SIGNUP_URL)

    # 2) Tunggu challenge Cloudflare selesai (Just a moment, dsb.)
    print("    → Menunggu challenge (jika ada)...")
    challenge_ok = await _wait_challenge_done(page, timeout=60)
    if not challenge_ok:
        st = await _page_state(page)
        await _save_debug(page, "signup_challenge_stuck")
        return SignupResult(
            False, email=email,
            error=f"Challenge tidak selesai: title={st['title']!r} url={st['url']}",
            page_url=st["url"],
        )

    # 3) Extra wait agar React selesai render form
    await asyncio.sleep(5)
    st = await _page_state(page)
    print(f"    → Page loaded: {st['url']} | title={st['title'][:60]!r}")

    if "/sign-up" not in st["url"]:
        await _save_debug(page, "signup_redirect")
        return SignupResult(
            False, email=email,
            error=f"Redirect tidak terduga: {st['url']}",
            page_url=st["url"],
        )

    # 4) Cari email input dengan fallback selector
    print("    → Mencari field email...")
    email_sel, email_input = await _find_first(page, EMAIL_SELECTORS, timeout_each=5.0)
    if not email_input:
        await _save_debug(page, "signup_no_email_field")
        return SignupResult(
            False, email=email,
            error=f"Email input not found (dicoba {len(EMAIL_SELECTORS)} selector)",
            page_url=st["url"],
        )
    print(f"    ✅ email field: {email_sel}")

    await email_input.click()
    await asyncio.sleep(0.5)
    await email_input.send_keys(email)
    await asyncio.sleep(1)

    # 5) Cari password input dengan fallback
    print("    → Mencari field password...")
    pw_sel, pw_input = await _find_first(page, PASSWORD_SELECTORS, timeout_each=5.0)
    if not pw_input:
        await _save_debug(page, "signup_no_password_field")
        return SignupResult(
            False, email=email,
            error=f"Password input not found (dicoba {len(PASSWORD_SELECTORS)} selector)",
            page_url=st["url"],
        )
    print(f"    ✅ password field: {pw_sel}")

    await pw_input.click()
    await asyncio.sleep(0.5)
    await pw_input.send_keys(password)
    await asyncio.sleep(2)

    # 6) Scroll supaya Turnstile kelihatan
    try:
        await page.evaluate("window.scrollBy(0, 400)")
    except Exception:
        pass
    await asyncio.sleep(3)

    # 7) Solve Turnstile jika ada
    turnstile_present = await is_turnstile_present(page)
    if turnstile_present:
        print("    → Turnstile terdeteksi, solving...")
        try:
            token = await verify_cf(page, timeout=60)
            print(f"    ✅ Turnstile solved: {str(token)[:20]}...")
        except Exception as e:
            if retry_turnstile:
                await asyncio.sleep(5)
                try:
                    token = await verify_cf(page, timeout=60)
                    print(f"    ✅ Turnstile solved (retry): {str(token)[:20]}...")
                except Exception as e2:
                    await _save_debug(page, "signup_turnstile_fail")
                    return SignupResult(
                        False, email=email,
                        error=f"Turnstile failed: {e2}",
                        page_url=st["url"],
                    )
            else:
                await _save_debug(page, "signup_turnstile_fail")
                return SignupResult(
                    False, email=email,
                    error=f"Turnstile failed: {e}",
                    page_url=st["url"],
                )
    else:
        print("    → Turnstile tidak terdeteksi (lanjut)")
    await asyncio.sleep(5)

    # 8) Submit form
    print("    → Mencari tombol submit...")
    submit_sel, submit_btn = await _find_first(page, SUBMIT_SELECTORS, timeout_each=4.0)
    if not submit_btn:
        await _save_debug(page, "signup_no_submit")
        return SignupResult(
            False, email=email,
            error=f"Submit button not found (dicoba {len(SUBMIT_SELECTORS)} selector)",
            page_url=st["url"],
        )
    print(f"    ✅ submit button: {submit_sel}")

    try:
        await submit_btn.scroll_into_view()
    except Exception:
        pass
    await asyncio.sleep(1)
    await submit_btn.click()

    # 9) Tunggu redirect
    print("    → Menunggu redirect...")
    for i in range(max_wait):
        await asyncio.sleep(1)
        url = str(await page.evaluate("location.href", return_by_value=True))
        if "/sign-up" not in url:
            print(f"    ✅ Redirect ke: {url}")
            break
    await asyncio.sleep(10)

    # 10) Ekstrak Account ID
    url = str(await page.evaluate("location.href", return_by_value=True))
    match = re.search(r"/([a-f0-9]{32})", url)
    if match:
        account_id = match.group(1)
        return SignupResult(
            True,
            email=email,
            password=password,
            account_id=account_id,
            page_url=url,
        )

    # 11) Gagal — cek pesan error, simpan debug
    await _save_debug(page, "signup_no_redirect")
    try:
        error_msgs = await page.evaluate(
            """
            Array.from(document.querySelectorAll('p, [role="alert"], .error, [data-testid*="error"]'))
                .map(e => (e.textContent || '').trim())
                .filter(t => t.length > 0 && t.length < 300)
                .slice(0, 5)
            """,
            return_by_value=True,
        )
    except Exception:
        error_msgs = None

    error = str(error_msgs) if error_msgs else f"Redirect failed: {url[:120]}"
    return SignupResult(False, email=email, error=error, page_url=url)
