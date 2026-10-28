"""
Cloudflare Signup Flow — Automated account creation.

Update kunci:
- Turnstile HARUS benar-benar sukses (iframe hijau mengecil) sebelum submit.
- Verifikasi success DIULANG tepat sebelum klik submit (bukan hanya saat solve).
- Jika success hilang (token expire / iframe reset), solve ulang.
"""

import asyncio
import json
import random
import re
import time
from pathlib import Path
from typing import Optional

import nodriver as uc

from .turnstile_bypass import (
    is_turnstile_present,
    get_turnstile_token,
    get_turnstile_state,
    solve_turnstile_and_verify,
    wait_for_turnstile_success,
)


CLOUDFLARE_SIGNUP_URL = "https://dash.cloudflare.com/sign-up"

EMAIL_SELECTORS = [
    'input[name="email"]', 'input[type="email"]', 'input#email',
    'input[autocomplete="email"]', 'input[aria-label="Email"]',
    '[data-testid="signup-email"]',
]
PASSWORD_SELECTORS = [
    'input[name="password"]', 'input[type="password"]', 'input#password',
    'input[autocomplete="new-password"]', 'input[aria-label="Password"]',
]
SUBMIT_SELECTORS = [
    'button[type="submit"]', 'button:has-text("Sign up")',
    'button:has-text("Create Account")', 'button:has-text("Sign Up")',
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
        return {"success": self.success, "email": self.email, "password": self.password,
                "account_id": self.account_id, "error": self.error}


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
            url = str(await page.evaluate("location.href", return_by_value=True))
            ts_state = await get_turnstile_state(page)
            state = await page.evaluate('''(() => {
                const btn = document.querySelector('button[type="submit"]');
                const em = document.querySelector('input[name="email"]') ||
                           document.querySelector('input[type="email"]');
                const pw = document.querySelector('input[name="password"]') ||
                           document.querySelector('input[type="password"]');
                const cbs = Array.from(document.querySelectorAll('input[type="checkbox"]'));
                return JSON.stringify({
                    email_value_len: em ? (em.value || '').length : -1,
                    password_value_len: pw ? (pw.value || '').length : -1,
                    submit_disabled: btn ? btn.disabled : null,
                    checkboxes: cbs.map(c => ({
                        name: c.name || '', id: c.id || '',
                        checked: c.checked,
                        visible: !!(c.offsetWidth || c.offsetHeight)
                    })),
                    body: (document.body ? document.body.innerText.substring(0, 800) : '')
                });
            })()''', return_by_value=True)
            (DEBUG_DIR / f"{tag}.txt").write_text(
                f"URL: {url}\nTURNSTILE: {ts_state}\nSTATE: {state}\n",
                encoding="utf-8",
            )
            print(f"    🐛 debug: {tag}")
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
            "document.body ? document.body.innerText.substring(0, 500) : ''",
            return_by_value=True))
    except Exception:
        body = ""
    low = (title + " " + body).lower()
    is_challenge = any(k in low for k in (
        "just a moment", "checking your browser", "verifying you are human",
        "attention required", "please wait"))
    try:
        has_form = bool(await page.evaluate("""(() => !!(
            document.querySelector('input[name="email"]') ||
            document.querySelector('input[type="email"]')))()""", return_by_value=True))
    except Exception:
        has_form = False
    return {"url": url, "title": title, "body": body,
            "is_challenge": is_challenge, "has_form": has_form}


async def _wait_challenge_done(page: uc.Tab, timeout: int = 180) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        st = await _page_state(page)
        elapsed = int(time.time() - start)
        if not st["is_challenge"] and st["has_form"]:
            print(f"    ✅ Challenge selesai setelah {elapsed}s (form muncul)")
            return True
        if not st["is_challenge"] and "/sign-up" in st["url"]:
            print(f"    ✅ Challenge selesai setelah {elapsed}s")
            return True
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


# ------------------------------------------------------------------ password fill

async def _type_password_cdp(page: uc.Tab, password: str) -> None:
    """Isi password per karakter via CDP agar React state update."""
    try:
        focused = await page.evaluate("""(() => {
            const pw = document.querySelector('input[name="password"]') ||
                       document.querySelector('input[type="password"]');
            if (!pw) return false;
            pw.focus();
            pw.click();
            return true;
        })()""", return_by_value=True)
        if not focused:
            print("    ⚠️ password tidak bisa difokuskan")
            return
        await asyncio.sleep(0.4)

        # Clear
        await page.send(uc.cdp.input_.dispatch_key_event("keyDown", key="a", code="KeyA",
                                                          modifiers=2))  # 2 = Ctrl
        await page.send(uc.cdp.input_.dispatch_key_event("keyUp", key="a", code="KeyA",
                                                          modifiers=2))
        await page.send(uc.cdp.input_.dispatch_key_event("keyDown", key="Backspace",
                                                          code="Backspace"))
        await page.send(uc.cdp.input_.dispatch_key_event("keyUp", key="Backspace",
                                                          code="Backspace"))
        await asyncio.sleep(0.3)

        for ch in password:
            code = f"Key{ch.upper()}" if ch.isalpha() else ch
            await page.send(uc.cdp.input_.dispatch_key_event("keyDown", key=ch, code=code))
            await page.send(uc.cdp.input_.dispatch_char_event(text=ch))
            await page.send(uc.cdp.input_.dispatch_key_event("keyUp", key=ch, code=code))
            await asyncio.sleep(0.05)

        await asyncio.sleep(0.8)
        await page.evaluate("""(() => {
            const pw = document.querySelector('input[name="password"]') ||
                       document.querySelector('input[type="password"]');
            if (!pw) return;
            pw.dispatchEvent(new Event('input', {bubbles: true}));
            pw.dispatchEvent(new Event('change', {bubbles: true}));
            pw.dispatchEvent(new Event('blur', {bubbles: true}));
        })()""")
        await asyncio.sleep(0.5)
    except Exception as e:
        print(f"    ⚠️ _type_password_cdp: {e}")


# ------------------------------------------------------------------ checkbox

async def _check_terms_aggressive(page: uc.Tab) -> int:
    count = 0
    try:
        native_json = await page.evaluate("""(() => {
            const cbs = Array.from(document.querySelectorAll('input[type="checkbox"]'));
            return JSON.stringify(cbs.map((cb, i) => ({
                i, name: cb.name || '', id: cb.id || '',
                checked: cb.checked,
                visible: !!(cb.offsetWidth || cb.offsetHeight)
            })));
        })()""", return_by_value=True)
        for cb in (json.loads(native_json) if native_json else []):
            if cb.get("visible") and not cb.get("checked"):
                print(f"    → centang checkbox: {cb.get('name') or cb.get('id') or cb['i']}")
                await page.evaluate(f"""(() => {{
                    const cbs = document.querySelectorAll('input[type="checkbox"]');
                    const el = cbs[{cb['i']}];
                    if (el && !el.checked) {{
                        el.click();
                        const lbl = el.closest('label') ||
                                    document.querySelector(`label[for="${{el.id}}"]`);
                        if (lbl) lbl.click();
                    }}
                }})()""")
                await asyncio.sleep(0.8)
                count += 1
    except Exception as e:
        print(f"    ⚠️ checkbox: {e}")
    return count


# ------------------------------------------------------------------ submit

async def _wait_submit_enabled(page: uc.Tab, timeout: int = 15) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        try:
            state = await page.evaluate("""(() => {
                const btn = document.querySelector('button[type="submit"]');
                if (!btn) return 'missing';
                return btn.disabled ? 'disabled' : 'enabled';
            })()""", return_by_value=True)
            if state == "enabled":
                return True
        except Exception:
            pass
        await asyncio.sleep(1)
    return False


async def _submit_via_keyboard(page: uc.Tab, max_wait: int = 45) -> str:
    """Submit via keyboard events (Enter/Space) — pola yang paling lolos Turnstile."""
    await asyncio.sleep(random.uniform(1.5, 3.5))
    await page.evaluate("""(() => {
        const btn = document.querySelector('button[type="submit"]');
        if (btn) btn.focus();
    })()""")
    await asyncio.sleep(0.5)

    for key, code in [("\n", "Enter"), (" ", "Space")]:
        print(f"    → submit via keyboard: {code}")
        await page.send(uc.cdp.input_.dispatch_key_event("keyDown", key=key, code=code))
        await page.send(uc.cdp.input_.dispatch_key_event("keyUp", key=key, code=code))
        for _ in range(max_wait):
            await asyncio.sleep(1)
            url = str(await page.evaluate("location.href", return_by_value=True))
            if "/sign-up" not in url:
                return url
        await asyncio.sleep(2)
    return ""


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
        return SignupResult(False, email=email, error="Email input not found",
                            page_url=st["url"])
    print(f"    ✅ {email_sel}")
    await email_input.click()
    await asyncio.sleep(0.4)
    await email_input.send_keys(email)
    await asyncio.sleep(1)

    # Password
    print("    → Field password (CDP per karakter)...")
    pw_sel, pw_input = await _find_first(page, PASSWORD_SELECTORS, 5.0)
    if not pw_input:
        await _save_debug(page, "no_password_field")
        return SignupResult(False, email=email, error="Password input not found",
                            page_url=st["url"])
    print(f"    ✅ {pw_sel}")
    await _type_password_cdp(page, password)
    await asyncio.sleep(2)

    try:
        await page.evaluate("window.scrollBy(0, 400)")
    except Exception:
        pass
    await asyncio.sleep(2)

    # ---- TURNSTILE: harus SUCCESS sebelum submit ----
    turnstile_present = await is_turnstile_present(page)
    if not turnstile_present:
        print("    ⚠️ Turnstile tidak terdeteksi (lanjut tanpa solve)")
    else:
        print("    → Turnstile terdeteksi, memulai solve + verify...")
        ok, token = await solve_turnstile_and_verify(page, timeout=150)
        if not ok:
            await _save_debug(page, "turnstile_failed")
            return SignupResult(False, email=email,
                                error="Turnstile gagal diselesaikan (tidak ada success state)",
                                page_url=st["url"])
        print(f"    ✅ Turnstile sukses, token {len(token)} chars")

    # Checkbox ToS
    n = await _check_terms_aggressive(page)
    print(f"    → checkbox ToS dicentang: {n}")
    await asyncio.sleep(1)

    # Submit
    print("    → Tombol submit...")
    submit_sel, submit_btn = await _find_first(page, SUBMIT_SELECTORS, 4.0)
    if not submit_btn:
        await _save_debug(page, "no_submit")
        return SignupResult(False, email=email, error="Submit button not found",
                            page_url=st["url"])
    print(f"    ✅ {submit_sel}")

    print("    → Menunggu submit enabled (max 15s)...")
    enabled = await _wait_submit_enabled(page, timeout=15)
    print(f"    → submit enabled: {enabled}")

    # ---- VERIFIKASI ULANG TURNSTILE SEBELUM SUBMIT ----
    if turnstile_present:
        print("    → Verifikasi ulang Turnstile tepat sebelum submit...")
        state = await get_turnstile_state(page)
        if not state.get("solved"):
            print("    ⚠️ Success hilang, solve ulang...")
            ok2, token2 = await solve_turnstile_and_verify(page, timeout=90)
            if not ok2:
                await _save_debug(page, "turnstile_lost_before_submit")
                return SignupResult(False, email=email,
                                    error="Turnstile success hilang sebelum submit",
                                    page_url=st["url"])
        # Tunggu token stabil 2 detik
        for _ in range(2):
            tok = await get_turnstile_token(page)
            if not tok or len(tok) < 20:
                print("    ⚠️ token kosong sebelum submit, tunggu 3s...")
                await asyncio.sleep(3)
            else:
                break
        print(f"    ✅ Turnstile success dikonfirmasi sebelum submit")

    await _save_debug(page, "pre_submit_state")

    # Submit via keyboard
    new_url = await _submit_via_keyboard(page, max_wait=max_wait)
    if new_url:
        print(f"    ✅ Redirect ke: {new_url}")
        await asyncio.sleep(8)
        match = re.search(r"/([a-f0-9]{32})", new_url)
        if match:
            return SignupResult(True, email=email, password=password,
                                account_id=match.group(1), page_url=new_url)

    await _save_debug(page, "signup_no_redirect")
    url = str(await page.evaluate("location.href", return_by_value=True))
    return SignupResult(False, email=email,
                        error=f"Redirect failed setelah submit: {url[:120]}",
                        page_url=url)
