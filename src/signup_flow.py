"""
Cloudflare Signup Flow — Automated account creation via headless browser.
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
    def __init__(self, success, email="", password="", account_id="", error="", page_url=""):
        self.success = success
        self.email = email
        self.password = password
        self.account_id = account_id
        self.error = error
        self.page_url = page_url

    def to_dict(self):
        return {
            "success": self.success, "email": self.email, "password": self.password,
            "account_id": self.account_id, "error": self.error,
        }


async def _save_debug(page: uc.Tab, tag: str) -> None:
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
            body = await page.evaluate(
                "document.body ? document.body.innerText.substring(0, 1000) : ''",
                return_by_value=True,
            )
            (DEBUG_DIR / f"{tag}.txt").write_text(
                f"URL: {url}\nTITLE: {title}\n\nBODY:\n{body}\n", encoding="utf-8"
            )
            print(f"    🐛 debug: {tag} (url={url}, title={title})")
        except Exception:
            pass
    except Exception:
        pass


async def _page_state(page: uc.Tab) -> dict:
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
        "attention required", "please wait", "cf-browser-verification",
    ))
    # Deteksi apakah form signup sudah muncul
    try:
        has_form = bool(await page.evaluate(
            """(() => !!(
                document.querySelector('input[name="email"]') ||
                document.querySelector('input[type="email"]') ||
                document.querySelector('input[autocomplete="email"]')
            ))()""",
            return_by_value=True,
        ))
    except Exception:
        has_form = False
    return {
        "url": url, "title": title, "body": body,
        "is_challenge": is_challenge, "has_form": has_form,
    }


async def _wait_challenge_done(page: uc.Tab, timeout: int = 180) -> bool:
    """
    Tunggu challenge selesai. Strategi:
    1. Tunggu auto-pass (headed mode sering auto-pass dalam 10-30 detik).
    2. Jika stuck >10 detik, aktif panggil verify_cf (klik Turnstile).
    3. Ulangi sampai timeout.
    """
    start = time.time()
    last_verify = 0
    while time.time() - start < timeout:
        st = await _page_state(page)
        elapsed = int(time.time() - start)

        # Selesai: bukan challenge DAN form sudah muncul
        if not st["is_challenge"] and st["has_form"]:
            print(f"    ✅ Challenge selesai setelah {elapsed}s (form muncul)")
            return True
        if not st["is_challenge"] and "/sign-up" in st["url"]:
            # Halaman bukan challenge, meski form belum terdeteksi
            print(f"    ✅ Challenge selesai setelah {elapsed}s")
            return True

        # Aktif solve Turnstile tiap ~10 detik jika challenge ada
        if st["is_challenge"] and (time.time() - last_verify) >= 10:
            last_verify = time.time()
            try:
                if await is_turnstile_present(page):
                    print(f"    → Challenge stuck ({elapsed}s), mencoba verify_cf...")
                    await verify_cf(page, timeout=30)
                    await asyncio.sleep(3)
                    continue
            except Exception as e:
                print(f"    ⚠️ verify_cf: {e}")

        if elapsed > 0 and elapsed % 20 == 0:
            print(f"    … masih challenge ({elapsed}s) title={st['title'][:50]!r}")
        await asyncio.sleep(3)
    return False


async def _find_first(page: uc.Tab, selectors: list, timeout_each: float = 5.0):
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
    print(f"    → Buka {CLOUDFLARE_SIGNUP_URL}")
    await page.get(CLOUDFLARE_SIGNUP_URL)

    print("    → Menunggu challenge (jika ada, hingga 180s)...")
    ok = await _wait_challenge_done(page, timeout=180)
    if not ok:
        st = await _page_state(page)
        await _save_debug(page, "signup_challenge_stuck")
        return SignupResult(
            False, email=email,
            error=f"Challenge stuck: title={st['title']!r} url={st['url']}",
            page_url=st["url"],
        )

    await asyncio.sleep(5)
    st = await _page_state(page)
    print(f"    → Page: {st['url']} | title={st['title'][:60]!r} | form={st['has_form']}")

    if "/sign-up" not in st["url"]:
        await _save_debug(page, "signup_redirect")
        return SignupResult(
            False, email=email,
            error=f"Redirect tidak terduga: {st['url']}",
            page_url=st["url"],
        )

    # Email
    print("    → Mencari field email...")
    email_sel, email_input = await _find_first(page, EMAIL_SELECTORS, timeout_each=5.0)
    if not email_input:
        await _save_debug(page, "signup_no_email_field")
        return SignupResult(
            False, email=email,
            error=f"Email input not found ({len(EMAIL_SELECTORS)} selector)",
            page_url=st["url"],
        )
    print(f"    ✅ email field: {email_sel}")
    await email_input.click()
    await asyncio.sleep(0.5)
    await email_input.send_keys(email)
    await asyncio.sleep(1)

    # Password
    print("    → Mencari field password...")
    pw_sel, pw_input = await _find_first(page, PASSWORD_SELECTORS, timeout_each=5.0)
    if not pw_input:
        await _save_debug(page, "signup_no_password_field")
        return SignupResult(
            False, email=email,
            error=f"Password input not found ({len(PASSWORD_SELECTORS)} selector)",
            page_url=st["url"],
        )
    print(f"    ✅ password field: {pw_sel}")
    await pw_input.click()
    await asyncio.sleep(0.5)
    await pw_input.send_keys(password)
    await asyncio.sleep(2)

    try:
        await page.evaluate("window.scrollBy(0, 400)")
    except Exception:
        pass
    await asyncio.sleep(3)

    # Turnstile (in-page, bukan interstitial)
    turnstile_present = await is_turnstile_present(page)
    if turnstile_present:
        print("    → Turnstile signup widget terdeteksi, solving...")
        try:
            token = await verify_cf(page, timeout=60)
            print(f"    ✅ Turnstile solved: {str(token)[:20]}...")
        except Exception as e:
            if retry_turnstile:
                await asyncio.sleep(5)
                try:
                    token = await verify_cf(page, timeout=60)
                    print(f"    ✅ Turnstile retry: {str(token)[:20]}...")
                except Exception as e2:
                    await _save_debug(page, "signup_turnstile_fail")
                    return SignupResult(False, email=email,
                        error=f"Turnstile failed: {e2}", page_url=st["url"])
            else:
                await _save_debug(page, "signup_turnstile_fail")
                return SignupResult(False, email=email,
                    error=f"Turnstile failed: {e}", page_url=st["url"])
    else:
        print("    → Turnstile signup tidak terdeteksi")
    await asyncio.sleep(5)

    # Submit
    print("    → Mencari tombol submit...")
    submit_sel, submit_btn = await _find_first(page, SUBMIT_SELECTORS, timeout_each=4.0)
    if not submit_btn:
        await _save_debug(page, "signup_no_submit")
        return SignupResult(
            False, email=email,
            error=f"Submit button not found ({len(SUBMIT_SELECTORS)} selector)",
            page_url=st["url"],
        )
    print(f"    ✅ submit button: {submit_sel}")
    try:
        await submit_btn.scroll_into_view()
    except Exception:
        pass
    await asyncio.sleep(1)
    await submit_btn.click()

    print("    → Menunggu redirect...")
    for i in range(max_wait):
        await asyncio.sleep(1)
        url = str(await page.evaluate("location.href", return_by_value=True))
        if "/sign-up" not in url:
            print(f"    ✅ Redirect ke: {url}")
            break
    await asyncio.sleep(10)

    url = str(await page.evaluate("location.href", return_by_value=True))
    match = re.search(r"/([a-f0-9]{32})", url)
    if match:
        return SignupResult(
            True, email=email, password=password,
            account_id=match.group(1), page_url=url,
        )

    await _save_debug(page, "signup_no_redirect")
    try:
        error_msgs = await page.evaluate(
            """Array.from(document.querySelectorAll('p, [role="alert"], .error, [data-testid*="error"]'))
                .map(e => (e.textContent || '').trim())
                .filter(t => t.length > 0 && t.length < 300).slice(0, 5)""",
            return_by_value=True,
        )
    except Exception:
        error_msgs = None
    return SignupResult(
        False, email=email,
        error=str(error_msgs) if error_msgs else f"Redirect failed: {url[:120]}",
        page_url=url,
    )
