"""
Cloudflare Signup Flow — Automated account creation.

Fix #11:
- Checkbox ToS: deteksi input native DAN custom [role=checkbox], klik label-nya.
- Password: dispatch event 'input' & 'change' setelah send_keys agar React state update.
- Submit: click() -> jika tidak redirect, coba Enter -> lalu Space.
- Debug: simpan state checkbox & validitas form sebelum submit.
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
        except Exception: pass
        try:
            html = await page.get_content()
            (DEBUG_DIR / f"{tag}.html").write_text(str(html), encoding="utf-8")
        except Exception: pass
        try:
            url = str(await page.evaluate("location.href", return_by_value=True))
            state = await page.evaluate('''(() => {
                const ts = document.querySelector('input[name="cf-turnstile-response"]');
                const cbs = Array.from(document.querySelectorAll('input[type="checkbox"]'));
                const roleCbs = Array.from(document.querySelectorAll('[role="checkbox"]'));
                const btn = document.querySelector('button[type="submit"]');
                const em = document.querySelector('input[name="email"]');
                const pw = document.querySelector('input[name="password"]');
                return JSON.stringify({
                    turnstile_len: ts ? (ts.value||'').length : -1,
                    email_invalid: em ? em.getAttribute('aria-invalid') : null,
                    password_invalid: pw ? pw.getAttribute('aria-invalid') : null,
                    submit_disabled: btn ? btn.disabled : null,
                    native_checkboxes: cbs.map(c => ({name:c.name||'', id:c.id||'', checked:c.checked, visible:!!(c.offsetWidth||c.offsetHeight)})),
                    role_checkboxes: roleCbs.map(c => ({label:(c.getAttribute('aria-label')||''), checked:c.getAttribute('aria-checked')})),
                    body: (document.body ? document.body.innerText.substring(0,800) : '')
                });
            })()''', return_by_value=True)
            (DEBUG_DIR / f"{tag}.txt").write_text(
                f"URL: {url}\nSTATE: {state}\n", encoding="utf-8")
            print(f"    🐛 debug: {tag}")
        except Exception: pass
    except Exception: pass


# ------------------------------------------------------------------ page state

async def _page_state(page: uc.Tab) -> dict:
    try:
        url = str(await page.evaluate("location.href", return_by_value=True))
    except Exception: url = ""
    try:
        title = str(await page.evaluate("document.title", return_by_value=True))
    except Exception: title = ""
    try:
        body = str(await page.evaluate(
            "document.body ? document.body.innerText.substring(0,500) : ''",
            return_by_value=True))
    except Exception: body = ""
    low = (title + " " + body).lower()
    is_challenge = any(k in low for k in (
        "just a moment", "checking your browser", "verifying you are human",
        "attention required", "please wait"))
    try:
        has_form = bool(await page.evaluate("""(() => !!(
            document.querySelector('input[name="email"]') ||
            document.querySelector('input[type="email"]')))()""", return_by_value=True))
    except Exception: has_form = False
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
                    await asyncio.sleep(3); continue
            except Exception: pass
        if elapsed > 0 and elapsed % 20 == 0:
            print(f"    … masih challenge ({elapsed}s)")
        await asyncio.sleep(3)
    return False


async def _find_first(page: uc.Tab, selectors: list, timeout_each: float = 5.0):
    for sel in selectors:
        try:
            el = await page.select(sel, timeout=timeout_each)
            if el: return sel, el
        except Exception: continue
    return None, None


# ------------------------------------------------------------------ password

async def _fill_password_react_safe(page: uc.Tab, pw_input, password: str) -> None:
    """
    Isi password + paksa React melihat perubahan lewat event input/change.
    Beberapa SPA Cloudflare tidak update state hanya dari send_keys.
    """
    await pw_input.click()
    await asyncio.sleep(0.4)
    await pw_input.send_keys(password)
    await asyncio.sleep(0.5)
    try:
        await page.evaluate(f'''(() => {{
            const pw = document.querySelector('input[name="password"]') ||
                       document.querySelector('input[type="password"]');
            if (!pw) return;
            const val = {json.dumps(password)};
            const setter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value').set;
            setter.call(pw, val);
            pw.dispatchEvent(new Event('input', {{bubbles:true}}));
            pw.dispatchEvent(new Event('change', {{bubbles:true}}));
            pw.dispatchEvent(new Event('blur', {{bubbles:true}}));
        }})()''')
        await asyncio.sleep(1)
    except Exception as e:
        print(f"    ⚠️ password react-safe: {e}")


# ------------------------------------------------------------------ checkbox

async def _check_terms_aggressive(page: uc.Tab) -> int:
    """
    Centang checkbox ToS. Tangani:
    - <input type=checkbox> native (klik input + label terdekat).
    - [role="checkbox"] kustom (klik elemen).
    Return: jumlah yang berhasil dicentang.
    """
    count = 0
    try:
        native_json = await page.evaluate('''(() => {
            const cbs = Array.from(document.querySelectorAll('input[type="checkbox"]'));
            return JSON.stringify(cbs.map((cb, i) => ({
                i, name: cb.name||'', id: cb.id||'',
                checked: cb.checked,
                visible: !!(cb.offsetWidth||cb.offsetHeight)
            })));
        })()''', return_by_value=True)
        for cb in (json.loads(native_json) if native_json else []):
            if cb.get("visible") and not cb.get("checked"):
                label = cb.get("name") or cb.get("id") or f"#{cb['i']}"
                print(f"    → centang native checkbox: {label}")
                await page.evaluate(f'''(() => {{
                    const cbs = document.querySelectorAll('input[type="checkbox"]');
                    const el = cbs[{cb['i']}];
                    if (el && !el.checked) {{
                        el.click();
                        // klik label terdekat jika ada
                        const lbl = el.closest('label') ||
                                    document.querySelector(`label[for="${{el.id}}"]`);
                        if (lbl) lbl.click();
                    }}
                }})()''')
                await asyncio.sleep(0.8)
                count += 1
    except Exception as e:
        print(f"    ⚠️ native checkbox: {e}")

    try:
        role_json = await page.evaluate('''(() => {
            const els = Array.from(document.querySelectorAll('[role="checkbox"]'));
            return JSON.stringify(els.map((el, i) => ({
                i, label: el.getAttribute('aria-label')||'',
                checked: el.getAttribute('aria-checked')
            })));
        })()''', return_by_value=True)
        for cb in (json.loads(role_json) if role_json else []):
            if cb.get("checked") != "true":
                print(f"    → centang role=checkbox: {cb.get('label','?')}")
                await page.evaluate(f'''(() => {{
                    const els = document.querySelectorAll('[role="checkbox"]');
                    const el = els[{cb['i']}];
                    if (el) el.click();
                }})()''')
                await asyncio.sleep(0.8)
                count += 1
    except Exception as e:
        print(f"    ⚠️ role checkbox: {e}")

    return count


# ------------------------------------------------------------------ turnstile

async def _solve_turnstile_robust(page: uc.Tab, timeout: int = 120) -> str:
    start = time.time()
    print("    → Menunggu auto-solve (15s)...")
    for _ in range(15):
        tok = await get_turnstile_token(page)
        if tok and len(tok) > 10:
            print(f"    ✅ auto-solved ({len(tok)} chars)")
            return tok
        await asyncio.sleep(1)

    for attempt in range(3):
        if time.time() - start > timeout: break
        print(f"    → verify_cf attempt {attempt+1}...")
        tok = await verify_cf(page, timeout=45)
        if tok and len(tok) > 10:
            await asyncio.sleep(2)
            in_form = await get_turnstile_token(page)
            if not in_form or len(in_form) < 10:
                await inject_turnstile_token(page, tok)
            return tok
        await asyncio.sleep(3)

    for attempt in range(3):
        if time.time() - start > timeout: break
        try:
            print(f"    → klik iframe Turnstile (attempt {attempt+1})...")
            iframe = await page.select('iframe[src*="challenges.cloudflare.com"]', timeout=5)
            if iframe:
                await iframe.click()
                await asyncio.sleep(10)
                tok = await get_turnstile_token(page)
                if tok and len(tok) > 10: return tok
        except Exception as e:
            print(f"    ⚠️ iframe click: {e}")
        await asyncio.sleep(3)
    return ""


# ------------------------------------------------------------------ submit

async def _try_submit(page: uc.Tab, submit_btn, max_wait: int = 45) -> str:
    """Klik submit -> cek redirect. Jika gagal, coba Enter, lalu Space."""
    attempts = ["click", "enter", "space"]
    for i, mode in enumerate(attempts):
        print(f"    → submit mode: {mode} ({i+1}/{len(attempts)})")
        try:
            if mode == "click":
                try: await submit_btn.scroll_into_view()
                except Exception: pass
                await asyncio.sleep(0.5)
                await submit_btn.click()
            elif mode == "enter":
                try: await submit_btn.scroll_into_view()
                except Exception: pass
                await submit_btn.send_keys("\n")
            elif mode == "space":
                try: await submit_btn.scroll_into_view()
                except Exception: pass
                await submit_btn.send_keys(" ")
        except Exception as e:
            print(f"    ⚠️ submit {mode}: {e}")

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
        return SignupResult(False, email=email, error="Email input not found", page_url=st["url"])
    print(f"    ✅ {email_sel}")
    await email_input.click()
    await asyncio.sleep(0.4)
    await email_input.send_keys(email)
    await asyncio.sleep(1)

    # Password (react-safe)
    print("    → Field password (react-safe)...")
    pw_sel, pw_input = await _find_first(page, PASSWORD_SELECTORS, 5.0)
    if not pw_input:
        await _save_debug(page, "no_password_field")
        return SignupResult(False, email=email, error="Password input not found", page_url=st["url"])
    print(f"    ✅ {pw_sel}")
    await _fill_password_react_safe(page, pw_input, password)
    await asyncio.sleep(2)

    try: await page.evaluate("window.scrollBy(0, 400)")
    except Exception: pass
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

    # Terms checkbox (agresif)
    n = await _check_terms_aggressive(page)
    print(f"    → checkbox dicentang: {n}")
    await asyncio.sleep(1)

    # Submit
    print("    → Tombol submit...")
    submit_sel, submit_btn = await _find_first(page, SUBMIT_SELECTORS, 4.0)
    if not submit_btn:
        await _save_debug(page, "no_submit")
        return SignupResult(False, email=email, error="Submit button not found", page_url=st["url"])
    print(f"    ✅ {submit_sel}")

    # pre-submit state check
    await _save_debug(page, "pre_submit_state")

    new_url = await _try_submit(page, submit_btn, max_wait=max_wait)
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
                        error=f"Redirect failed setelah 3 mode submit: {url[:120]}",
                        page_url=url)
