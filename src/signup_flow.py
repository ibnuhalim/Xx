"""
Cloudflare Signup Flow — Automated account creation via headless browser.

Fix utama:
1. Turnstile: tunggu auto-solve -> verify_cf -> iframe click -> inject token.
   SELALU cek token non-kosong SEBELUM klik submit.
2. Terms checkbox: otomatis dicentang jika ada yang unchecked & visible.
3. Error reporting: hanya ambil [aria-invalid], [role=alert], [data-testid*=error].
   Tidak lagi menyeret semua <p> di halaman.
"""

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Optional

import nodriver as uc

from .turnstile_bypass import (
    verify_cf,
    is_turnstile_present,
    get_turnstile_token,
    inject_turnstile_token,
)


CLOUDFLARE_SIGNUP_URL = "https://dash.cloudflare.com/sign-up"

EMAIL_SELECTORS = [
    'input[name="email"]',
    'input[type="email"]',
    'input#email',
    'input[id="email"]',
    'input[autocomplete="email"]',
    'input[aria-label="Email"]',
    '[data-testid="signup-email"]',
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
            "success": self.success, "email": self.email,
            "password": self.password, "account_id": self.account_id,
            "error": self.error,
        }


# ------------------------------------------------------------------ debug

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
            state = await page.evaluate('''(() => {
                const ts = document.querySelector('input[name="cf-turnstile-response"]');
                const cbs = Array.from(document.querySelectorAll('input[type="checkbox"]'));
                return JSON.stringify({
                    turnstile_len: ts ? (ts.value||'').length : -1,
                    checkboxes: cbs.map(c => ({name: c.name||'', id: c.id||'', checked: c.checked, visible: !!(c.offsetWidth||c.offsetHeight)})),
                    body: (document.body ? document.body.innerText.substring(0,800) : '')
                });
            })()''', return_by_value=True)
            (DEBUG_DIR / f"{tag}.txt").write_text(
                f"URL: {url}\nTITLE: {title}\nSTATE: {state}\n",
                encoding="utf-8",
            )
            print(f"    🐛 debug: {tag} (url={url})")
        except Exception:
            pass
    except Exception:
        pass


# ------------------------------------------------------------------ page state

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
            "document.body ? document.body.innerText.substring(0,500) : ''",
            return_by_value=True,
        ))
    except Exception:
        body = ""
    low = (title + " " + body).lower()
    is_challenge = any(k in low for k in (
        "just a moment", "checking your browser", "verifying you are human",
        "attention required", "please wait",
    ))
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
    return {"url": url, "title": title, "body": body,
            "is_challenge": is_challenge, "has_form": has_form}


async def _wait_challenge_done(page: uc.Tab, timeout: int = 180) -> bool:
    start = time.time()
    last_verify = 0
    while time.time() - start < timeout:
        st = await _page_state(page)
        elapsed = int(time.time() - start)
        if not st["is_challenge"] and st["has_form"]:
            print(f"    ✅ Challenge selesai setelah {elapsed}s (form muncul)")
            return True
        if not st["is_challenge"] and "/sign-up" in st["url"]:
            print(f"    ✅ Challenge selesai setelah {elapsed}s")
            return True
        if st["is_challenge"] and (time.time() - last_verify) >= 10:
            last_verify = time.time()
            try:
                if await is_turnstile_present(page):
                    print(f"    → Challenge stuck ({elapsed}s), verify_cf...")
                    await verify_cf(page, timeout=30)
                    await asyncio.sleep(3)
                    continue
            except Exception:
                pass
        if elapsed > 0 and elapsed % 20 == 0:
            print(f"    … masih challenge ({elapsed}s)")
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


# ------------------------------------------------------------------ turnstile

async def _solve_turnstile_robust(page: uc.Tab, timeout: int = 120) -> str:
    """
    Strategi berlapis:
    1. Cek token yang mungkin sudah auto-solve (tunggu 15s).
    2. verify_cf (retry 3x).
    3. Klik iframe Turnstile (retry 3x).
    4. Verifikasi token benar-benar masuk ke hidden input; kalau tidak, inject paksa.
    """
    start = time.time()

    # Lapis 1: auto-solve
    print("    → Menunggu auto-solve (15s)...")
    for _ in range(15):
        tok = await get_turnstile_token(page)
        if tok and len(tok) > 10:
            print(f"    ✅ auto-solved ({len(tok)} chars)")
            return tok
        await asyncio.sleep(1)

    # Lapis 2: verify_cf dengan retry
    for attempt in range(3):
        if time.time() - start > timeout:
            break
        print(f"    → verify_cf attempt {attempt+1}...")
        tok = await verify_cf(page, timeout=45)
        if tok and len(tok) > 10:
            await asyncio.sleep(2)
            in_form = await get_turnstile_token(page)
            if not in_form or len(in_form) < 10:
                print(f"    → inject token ke form ({len(tok)} chars)...")
                await inject_turnstile_token(page, tok)
            return tok
        await asyncio.sleep(3)

    # Lapis 3: klik iframe manual
    for attempt in range(3):
        if time.time() - start > timeout:
            break
        try:
            print(f"    → klik iframe Turnstile (attempt {attempt+1})...")
            iframe = await page.select(
                'iframe[src*="challenges.cloudflare.com"]', timeout=5
            )
            if iframe:
                await iframe.click()
                await asyncio.sleep(10)
                tok = await get_turnstile_token(page)
                if tok and len(tok) > 10:
                    return tok
        except Exception as e:
            print(f"    ⚠️ iframe click: {e}")
        await asyncio.sleep(3)

    return ""


# ------------------------------------------------------------------ terms checkbox

async def _ensure_terms_checked(page: uc.Tab) -> None:
    """Centang semua checkbox visible yang belum checked."""
    try:
        cbs_json = await page.evaluate('''(() => {
            const cbs = Array.from(document.querySelectorAll('input[type="checkbox"]'));
            return JSON.stringify(cbs.map((cb, i) => ({
                idx: i,
                name: cb.name || '',
                id: cb.id || '',
                checked: cb.checked,
                visible: !!(cb.offsetWidth || cb.offsetHeight)
            })));
        })()''', return_by_value=True)
        cbs = json.loads(cbs_json) if cbs_json else []
        for cb in cbs:
            if cb.get("visible") and not cb.get("checked"):
                label = cb.get("name") or cb.get("id") or f"#{cb['idx']}"
                print(f"    → centang checkbox: {label}")
                try:
                    await page.evaluate(f'''(() => {{
                        const cbs = document.querySelectorAll('input[type="checkbox"]');
                        const el = cbs[{cb['idx']}];
                        if (el && !el.checked) el.click();
                    }})()''')
                    await asyncio.sleep(0.6)
                except Exception as e:
                    print(f"    ⚠️ checkbox click: {e}")
    except Exception as e:
        print(f"    ⚠️ _ensure_terms_checked: {e}")


# ------------------------------------------------------------------ errors

async def _get_real_errors(page: uc.Tab) -> list:
    """Ambil HANYA error validasi nyata, bukan teks statis."""
    try:
        raw = await page.evaluate('''(() => {
            const errs = [];
            document.querySelectorAll('[aria-invalid="true"]').forEach(el => {
                const l = el.getAttribute('aria-label') || el.name || el.id || '?';
                errs.push('invalid: ' + l);
            });
            document.querySelectorAll('[role="alert"]').forEach(el => {
                const t = (el.textContent || '').trim();
                if (t && t.length < 200) errs.push(t);
            });
            document.querySelectorAll('[data-testid*="error"], .error-message').forEach(el => {
                const t = (el.textContent || '').trim();
                if (t && t.length < 200) errs.push(t);
            });
            document.querySelectorAll('input[type="checkbox"]:invalid').forEach(el => {
                errs.push('checkbox required: ' + (el.name || el.id || '?'));
            });
            const ts = document.querySelector('input[name="cf-turnstile-response"]');
            if (ts && !ts.value) errs.push('turnstile EMPTY');
            return JSON.stringify(errs);
        })()''', return_by_value=True)
        try:
            return json.loads(raw) if raw else []
        except Exception:
            return [str(raw)[:200]] if raw else []
    except Exception:
        return []


# ------------------------------------------------------------------ main flow

async def signup(page: uc.Tab, email: str, password: str,
                 max_wait: int = 45, retry_turnstile: bool = True) -> SignupResult:

    print(f"    → Buka {CLOUDFLARE_SIGNUP_URL}")
    await page.get(CLOUDFLARE_SIGNUP_URL)

    print("    → Menunggu challenge (jika ada, hingga 180s)...")
    ok = await _wait_challenge_done(page, timeout=180)
    if not ok:
        st = await _page_state(page)
        await _save_debug(page, "signup_challenge_stuck")
        return SignupResult(False, email=email,
                            error=f"Challenge stuck: {st['title']!r}", page_url=st["url"])

    await asyncio.sleep(5)
    st = await _page_state(page)
    print(f"    → Page: {st['url']} | title={st['title'][:60]!r}")

    # Email
    print("    → Field email...")
    email_sel, email_input = await _find_first(page, EMAIL_SELECTORS, 5.0)
    if not email_input:
        await _save_debug(page, "no_email_field")
        return SignupResult(False, email=email,
                            error="Email input not found", page_url=st["url"])
    print(f"    ✅ {email_sel}")
    await email_input.click()
    await asyncio.sleep(0.4)
    await email_input.send_keys(email)
    await asyncio.sleep(1)

    # Password
    print("    → Field password...")
    pw_sel, pw_input = await _find_first(page, PASSWORD_SELECTORS, 5.0)
    if not pw_input:
        await _save_debug(page, "no_password_field")
        return SignupResult(False, email=email,
                            error="Password input not found", page_url=st["url"])
    print(f"    ✅ {pw_sel}")
    await pw_input.click()
    await asyncio.sleep(0.4)
    await pw_input.send_keys(password)
    await asyncio.sleep(2)

    try:
        await page.evaluate("window.scrollBy(0, 400)")
    except Exception:
        pass
    await asyncio.sleep(2)

    # Turnstile
    turnstile_present = await is_turnstile_present(page)
    if turnstile_present:
        print("    → Turnstile terdeteksi, solving robust...")
        token = await _solve_turnstile_robust(page, timeout=120)
        if not token or len(token) < 10:
            await _save_debug(page, "turnstile_failed")
            return SignupResult(False, email=email,
                                error="Turnstile solve failed: token empty",
                                page_url=st["url"])
        print(f"    ✅ Turnstile token ({len(token)} chars)")
    else:
        print("    → Turnstile tidak terdeteksi")

    # TERMS CHECKBOX
    await _ensure_terms_checked(page)
    await asyncio.sleep(1)

    # Submit
    print("    → Tombol submit...")
    submit_sel, submit_btn = await _find_first(page, SUBMIT_SELECTORS, 4.0)
    if not submit_btn:
        await _save_debug(page, "no_submit")
        return SignupResult(False, email=email,
                            error="Submit button not found", page_url=st["url"])
    print(f"    ✅ {submit_sel}")

    # Verifikasi token SEBELUM klik
    pre_token = await get_turnstile_token(page)
    if not pre_token or len(pre_token) < 10:
        print(f"    ⚠️ Token kosong SEBELUM submit, mencoba inject...")
        await _solve_turnstile_robust(page, timeout=60)
        pre_token = await get_turnstile_token(page)
        if not pre_token or len(pre_token) < 10:
            await _save_debug(page, "token_empty_before_submit")
            return SignupResult(False, email=email,
                                error="Token empty right before submit",
                                page_url=st["url"])

    try:
        await submit_btn.scroll_into_view()
    except Exception:
        pass
    await asyncio.sleep(1)
    await submit_btn.click()
    print("    → Submit diklik, menunggu redirect...")

    for i in range(max_wait):
        await asyncio.sleep(1)
        url = str(await page.evaluate("location.href", return_by_value=True))
        if "/sign-up" not in url:
            print(f"    ✅ Redirect ke: {url}")
            break

    await asyncio.sleep(8)

    url = str(await page.evaluate("location.href", return_by_value=True))
    match = re.search(r"/([a-f0-9]{32})", url)
    if match:
        return SignupResult(True, email=email, password=password,
                            account_id=match.group(1), page_url=url)

    await _save_debug(page, "signup_no_redirect")
    errs = await _get_real_errors(page)
    err_text = " | ".join(errs) if errs else f"Redirect failed: {url[:120]}"
    return SignupResult(False, email=email, error=err_text, page_url=url)
