"""
Turnstile Bypass — Solver dengan verifikasi visual success.

Fungsi utama untuk signup_flow:
    solve_turnstile_and_verify(page, timeout) -> (bool_success, token)

Alur:
1. Cek apakah sudah auto-solved (diam <15s).
2. Kalau belum: klik checkbox "Verify you are human" via CDP mouse
   di koordinat iframe (mendekati sisi kiri tengah).
3. Tunggu sampai iframe mengecil (<100px) ATAU token muncul (indikasi success).
4. Fallback: verify_cf() built-in nodriver, lalu ulangi wait.
5. Return (True, token) hanya jika benar-benar sukses.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Optional

import nodriver as uc


async def verify_cf(page, timeout: float = 60.0) -> str:
    """Solve Turnstile via nodriver built-in. Return token atau ''."""
    try:
        try:
            result = await page.verify_cf(timeout=timeout)
        except TypeError:
            result = await page.verify_cf()
        return "" if result is None else str(result)
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
            if (body.includes("Verify you are human") ||
                body.includes("Let us know you are human")) return true;
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


async def get_turnstile_state(page) -> dict:
    """
    Ambil state Turnstile:
    - solved: True jika iframe mengecil (<100px) atau token > 20 chars.
    - iframe_w/h: ukuran iframe aktif.
    - token_len: panjang token di hidden input.
    """
    try:
        raw = await page.evaluate('''(() => {
            const token_el = document.querySelector('input[name="cf-turnstile-response"]');
            const token = token_el ? (token_el.value || '') : '';

            const iframes = document.querySelectorAll('iframe[src*="challenges.cloudflare.com"]');
            let iframe_w = 0, iframe_h = 0;
            for (const f of iframes) {
                const r = f.getBoundingClientRect();
                if (r.width > 0 && r.height > 0) {
                    iframe_w = r.width;
                    iframe_h = r.height;
                    break;
                }
            }

            // Success indicator:
            // - iframe mengecil (dari ~300x65 ke ~45x45 atau lebih kecil)
            // - atau token sudah ada di form
            const iframe_small = iframe_w > 0 && iframe_w < 100;
            const has_token = token.length > 20;
            const solved = iframe_small || has_token;

            return JSON.stringify({
                token_len: token.length,
                iframe_w: Math.round(iframe_w),
                iframe_h: Math.round(iframe_h),
                iframe_small: iframe_small,
                has_token: has_token,
                solved: solved
            });
        })()''', return_by_value=True)
        try:
            return json.loads(raw) if raw else {"solved": False, "token_len": 0,
                                                "iframe_w": 0, "iframe_h": 0}
        except Exception:
            return {"solved": False, "token_len": 0, "iframe_w": 0, "iframe_h": 0}
    except Exception:
        return {"solved": False, "token_len": 0, "iframe_w": 0, "iframe_h": 0}


async def click_turnstile_checkbox(page) -> bool:
    """
    Klik checkbox "Verify you are human" di dalam iframe.
    Karena cross-origin, kita klik via koordinat CDP mouse event.
    Checkbox biasanya di ~30px dari kiri, ~tengah tinggi iframe.
    """
    try:
        raw = await page.evaluate('''(() => {
            const iframes = document.querySelectorAll('iframe[src*="challenges.cloudflare.com"]');
            for (const f of iframes) {
                const r = f.getBoundingClientRect();
                if (r.width > 100 && r.height > 30) {
                    return JSON.stringify({
                        x: r.left + 30,
                        y: r.top + r.height / 2,
                        w: r.width,
                        h: r.height
                    });
                }
            }
            return null;
        })()''', return_by_value=True)

        if not raw:
            print("    ⚠️ iframe checkbox tidak ditemukan (mungkin sudah solved)")
            return False

        pos = json.loads(raw)
        print(f"    → klik checkbox di ({int(pos['x'])}, {int(pos['y'])}) "
              f"[iframe {int(pos['w'])}x{int(pos['h'])}]")

        # Mouse move + press + release via CDP
        await page.send(uc.cdp.input_.dispatch_mouse_event(
            "mouseMoved", x=pos["x"], y=pos["y"],
        ))
        await asyncio.sleep(0.2)
        await page.send(uc.cdp.input_.dispatch_mouse_event(
            "mousePressed", x=pos["x"], y=pos["y"],
            button=uc.cdp.input_.MouseButton.LEFT, click_count=1,
        ))
        await asyncio.sleep(0.1)
        await page.send(uc.cdp.input_.dispatch_mouse_event(
            "mouseReleased", x=pos["x"], y=pos["y"],
            button=uc.cdp.input_.MouseButton.LEFT, click_count=1,
        ))
        return True
    except Exception as e:
        print(f"    ⚠️ click_turnstile_checkbox: {e}")
        return False


async def wait_for_turnstile_success(
    page, timeout: int = 90, poll_interval: float = 1.5,
) -> tuple[bool, str]:
    """
    Tunggu sampai Turnstile benar-benar sukses.
    Return (success, token).
    """
    start = time.time()
    last_log = 0
    while time.time() - start < timeout:
        state = await get_turnstile_state(page)
        if state.get("solved"):
            token = await get_turnstile_token(page)
            if not token:
                # iframe kecil tapi token kosong -> coba inject dari verify_cf
                try:
                    fallback = await verify_cf(page, timeout=15)
                    if fallback and len(fallback) > 20:
                        await inject_turnstile_token(page, fallback)
                        token = fallback
                except Exception:
                    pass
            if token and len(token) > 20:
                elapsed = int(time.time() - start)
                print(f"    ✅ Turnstile SUCCESS setelah {elapsed}s "
                      f"(token {len(token)} chars, iframe {state['iframe_w']}x{state['iframe_h']})")
                return True, token
        # log berkala
        elapsed = int(time.time() - start)
        if elapsed - last_log >= 10:
            last_log = elapsed
            print(f"    … masih menunggu success ({elapsed}s) "
                  f"iframe={state.get('iframe_w')}x{state.get('iframe_h')} "
                  f"token_len={state.get('token_len')}")
        await asyncio.sleep(poll_interval)
    return False, ""


async def solve_turnstile_and_verify(page, timeout: int = 150) -> tuple[bool, str]:
    """
    Alur lengkap:
    1. Cek auto-solve (diam 10s).
    2. Klik checkbox jika belum solved.
    3. Tunggu success (iframe mengecil / token muncul).
    4. Fallback: verify_cf() kemudian ulangi wait.
    Return (success, token).
    """
    start = time.time()
    print("    → Menunggu auto-solve (10s)...")
    for _ in range(10):
        state = await get_turnstile_state(page)
        if state.get("solved"):
            tok = await get_turnstile_token(page)
            if tok and len(tok) > 20:
                print(f"    ✅ auto-solved ({len(tok)} chars)")
                return True, tok
        await asyncio.sleep(1)

    # Klik checkbox
    print("    → Klik checkbox 'Verify you are human'...")
    clicked = await click_turnstile_checkbox(page)
    if clicked:
        print("    → Menunggu Turnstile success setelah klik...")
        ok, tok = await wait_for_turnstile_success(page, timeout=60)
        if ok:
            return True, tok
    else:
        print("    ⚠️ Checkbox tidak bisa diklik, coba verify_cf langsung")

    # Fallback verify_cf
    print("    → Fallback: verify_cf()...")
    for attempt in range(3):
        if time.time() - start > timeout:
            break
        try:
            await verify_cf(page, timeout=30)
        except Exception as e:
            print(f"    ⚠️ verify_cf attempt {attempt+1}: {e}")
        ok, tok = await wait_for_turnstile_success(page, timeout=30)
        if ok:
            return True, tok
        await asyncio.sleep(2)

    print("    ❌ Turnstile tidak bisa diselesaikan")
    return False, ""


__all__ = [
    "verify_cf",
    "is_turnstile_present",
    "get_turnstile_token",
    "inject_turnstile_token",
    "get_turnstile_state",
    "click_turnstile_checkbox",
    "wait_for_turnstile_success",
    "solve_turnstile_and_verify",
                               ]
