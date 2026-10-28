#!/usr/bin/env python3
"""
Cloudflare Auto Signup — Main Orchestrator (Cair mail backend)
...
"""

import argparse
import asyncio
import json
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import nodriver as uc

# Add src to path
sys.path.insert(0, str(Path(__file__).parent))

from src.email_generator import EmailGenerator
from src.signup_flow import signup
from src.email_verifier import verify_cloudflare_email
from src.token_creator import create_token, create_token_api
from src.token_validator import validate_token
from src.live_dashboard import DashboardState, LiveDashboard
from src.utils import (
    generate_password, generate_username, load_config, save_result,
    load_results, timestamp, format_account,
)

# Path Chrome eksplisit untuk lingkungan GitHub Actions
CHROME_PATHS = [
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
]

def get_chrome_path() -> str | None:
    """Cari path Chrome yang benar, prioritaskan google-chrome."""
    for path in CHROME_PATHS:
        if os.path.exists(path) and os.access(path, os.X_OK):
            return path
    return None

def parse_args():
    # ... (fungsi parse_args sama persis seperti sebelumnya) ...
    parser = argparse.ArgumentParser(
        description="Automated Cloudflare account creation with Workers AI tokens"
    )
    parser.add_argument("--accounts", "-n", type=int, default=1, help="Number of accounts to create (default: 1)")
    parser.add_argument("--config", "-c", type=str, default="config.json", help="Config file path")
    parser.add_argument("--proxy", "-p", type=str, default=None, help="Proxy URL")
    parser.add_argument("--output", "-o", type=str, default=None, help="Output JSON file")
    parser.add_argument("--delay", "-d", type=int, default=None, help="Delay between accounts")
    parser.add_argument("--headless", action="store_true", default=True, help="Run headless (default)")
    parser.add_argument("--no-headless", action="store_true", help="Force headed mode")
    parser.add_argument("--validate-only", action="store_true", help="Only validate token")
    parser.add_argument("--token", type=str, help="Token to validate")
    parser.add_argument("--account-id", type=str, help="Account ID for validation")
    parser.add_argument("--retry", type=int, default=3, help="Retry attempts per account")
    parser.add_argument("--workers", "-w", type=int, default=1, help="Concurrent workers")
    parser.add_argument("--no-dashboard", action="store_true", help="Disable Rich dashboard")
    parser.add_argument("--export-txt", type=str, default=None, help="Export valid results to txt")
    return parser.parse_args()

def _chrome_args() -> list[str]:
    """Argumen Chrome wajib untuk CI/Docker."""
    return [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--headless=new",
        "--disable-gpu",
        "--disable-software-rasterizer",
        "--disable-features=Translate,BackForwardCache,AcceptCHFrame,MediaRouter,OptimizationHints",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
        "--disable-backgrounding-occluded-windows",
        "--disable-sync",
        "--disable-extensions",
        "--disable-default-apps",
        "--disable-component-update",
        "--disable-domain-reliability",
        "--metrics-recording-only",
        "--mute-audio",
        "--window-size=1280,800",
        "--remote-debugging-port=0",
    ]

async def _start_browser(headless: bool, proxy: str | None) -> uc.Browser:
    """Start browser dengan retry dan path eksplisit."""
    chrome_path = get_chrome_path()
    if not chrome_path:
        raise RuntimeError(
            "Chrome tidak ditemukan di path yang dikenal. "
            "Pastikan google-chrome-stable terinstal di /usr/bin/."
        )
    print(f"  🚀 Meluncurkan Chrome dari: {chrome_path}")

    last_exc = None
    for attempt in range(1, 4):  # Retry hingga 3 kali
        try:
            browser = await uc.start(
                headless=headless,
                lang="en-US",
                proxy=proxy,
                sandbox=False,
                browser_executable_path=chrome_path, # Kunci: path eksplisit
                browser_args=_chrome_args(),
            )
            return browser
        except Exception as e:
            last_exc = e
            print(f"  ⚠️ Gagal meluncurkan browser (percobaan {attempt}/3): {e}")
            if attempt < 3:
                await asyncio.sleep(3)  # Jeda sebelum retry
    raise RuntimeError(f"Gagal meluncurkan browser setelah 3 percobaan: {last_exc}")

async def create_account(config: dict, proxy: str = None, headless: bool = True) -> dict:
    # ... (bagian generate credentials dan email sama) ...
    username = generate_username()
    password = generate_password()
    token_name = config.get("token_name", "workers-ai-auto")
    mail_api = config.get("mail_api") or "https://cair.eu.cc"
    domains = config.get("mail_domains") or []
    domain = random.choice(domains) if domains else None

    email_gen = EmailGenerator(mail_api, domains)
    try:
        mail = email_gen.create(username=username, domain=domain)
        email = mail["email"]
        if not config.get("mail_domains") and email_gen.domains:
            config["mail_domains"] = list(email_gen.domains)
    except Exception as e:
        return {"status": "error", "error": f"Email creation failed: {e}", "email": f"{username}@{domain or '?'}"}
    finally:
        email_gen.close()

    print(f"  📧 {email}")

    browser = None
    try:
        browser = await _start_browser(headless, proxy)
        # ... (sisa alur signup, verifikasi, token, validasi SAMA PERSIS) ...
        print("  [1/4] Signing up...")
        page = await browser.get("https://dash.cloudflare.com/sign-up")
        signup_result = await signup(page, email, password)
        if not signup_result.success:
            return {"status": "error", "email": email, "password": password, "error": f"Signup failed: {signup_result.error}"}
        account_id = signup_result.account_id
        print(f"  🆔 Account ID: {account_id}")

        print("  [2/4] Verifying Cloudflare email...")
        verify_result = await verify_cloudflare_email(
            page, mail_api=mail_api, jwt=mail.get("jwt", ""),
            timeout=config.get("email_verify_timeout", 120),
            poll_interval=config.get("email_verify_poll_interval", 5),
        )
        print(f"  {'✅' if verify_result.success else '⚠️'} Email verification: {verify_result.success}")

        print("  [3/4] Creating API token...")
        if verify_result.success:
            token_result = await create_token_api(page, account_id, token_name)
            if not token_result.success:
                token_result = await create_token(page, account_id, token_name)
        else:
            token_result = await create_token(page, account_id, token_name)
        api_token = token_result.token if token_result.success else ""
        print(f"  {'🔑' if api_token else '⚠️'} Token: {api_token[:30] + '...' if api_token else 'failed'}")

        token_valid, model_count = False, 0
        if api_token:
            print("  [4/4] Validating token...")
            validation = validate_token(api_token, account_id)
            token_valid, model_count = validation.valid, validation.workers_ai_models
            print(f"  {'✅' if token_valid else '❌'} Validation: {token_valid} ({model_count} models)")

        return {
            "email": email, "password": password, "jwt": mail.get("jwt", ""),
            "account_id": account_id, "api_token": api_token, "token_valid": token_valid,
            "workers_ai_models": model_count, "token_name": token_name,
            "email_verified": verify_result.success,
            "email_verify_error": "" if verify_result.success else verify_result.error,
            "status": "full" if token_valid else ("signup_only" if account_id else "error"),
            "created_at": timestamp(), "proxy_used": proxy or "direct",
        }
    except Exception as e:
        return {"status": "error", "email": email, "password": password, "error": str(e), "created_at": timestamp()}
    finally:
        if browser:
            try: browser.stop()
            except Exception: pass
            await asyncio.sleep(3)

# ... (fungsi export_txt dan main() sama persis seperti versi terakhir) ...
def export_txt(results: list[dict], output_path: str, proxy_pool: str = "None") -> int:
    # ... (implementasi sama) ...
    lines = ["Cloudflare Workers AI keys for 9Router", ""]
    count = 0
    for result in results:
        token = result.get("api_token", "")
        account_id = result.get("account_id", "")
        if token.startswith("cfut_") and account_id and result.get("token_valid"):
            count += 1
            lines.extend([f"[{count}]", f"Name: {result.get('email', f'key-{count}')}", f"API Key: {token}", f"Account ID: {account_id}", "Priority: 1", f"Proxy Pool: {proxy_pool}", ""])
    lines.extend(["--- Summary ---", f"Valid keys: {count}"])
    Path(output_path).write_text("\n".join(lines) + "\n")
    return count

async def main():
    # ... (implementasi main() sama persis, hanya memanggil create_account yang baru) ...
    args = parse_args()
    config = load_config(args.config)
    proxy = args.proxy or config.get("proxy")
    output_file = args.output or config.get("output_file", "results.json")
    delay = args.delay if args.delay is not None else config.get("delay_between_accounts", 300)
    num_accounts = args.accounts
    max_retry = args.retry
    headless = not args.no_headless if args.no_headless else True

    if args.validate_only:
        if not args.token or not args.account_id: sys.exit(1)
        result = validate_token(args.token, args.account_id)
        print(f"Valid: {result.valid}, Models: {result.workers_ai_models}")
        sys.exit(0 if result.valid else 1)

    print("=" * 60)
    print("☁️  Cloudflare Auto Signup — Workers AI Token Creator")
    print("=" * 60)
    print(f"  Mail backend: Cair ({config.get('mail_api')})")
    print(f"  Accounts to create: {num_accounts}")
    print(f"  Proxy: {proxy or 'None (direct)'}")
    print(f"  Headless: {headless}")
    print("=" * 60)

    workers = max(1, args.workers)
    results = load_results(output_file)
    run_results: list[dict] = []
    save_lock = asyncio.Lock()
    queue: asyncio.Queue[int] = asyncio.Queue()
    for i in range(num_accounts): queue.put_nowait(i + 1)

    dashboard_state = DashboardState(total=num_accounts, workers=workers)

    async def run_one(worker_id: int, index: int) -> dict:
        dashboard_state.update(worker_id, "signup", "Starting signup", index=index)
        result = {"status": "error", "error": "not_started"}
        success = False
        for attempt in range(max_retry):
            if attempt > 0:
                dashboard_state.update(worker_id, "signup", f"Retry {attempt}/{max_retry - 1}", index=index)
                await asyncio.sleep(30)
            result = await create_account(config=config, proxy=proxy, headless=headless)
            if result.get("email"):
                dashboard_state.update(worker_id, "validate", result.get("status", "done"), email=result["email"], index=index)
            if result.get("status") in ("full", "signup_only"):
                success = True
                break
            if any(m in str(result.get("error", "")).lower() for m in ("rate", "unable", "connection closed", "no close frame")):
                dashboard_state.update(worker_id, "failed", "Retry cooldown", email=result.get("email", ""), index=index)
                await asyncio.sleep(delay)
            else:
                break
        async with save_lock:
            save_result(result, output_file)
            results.append(result)
            run_results.append(result)
        dashboard_state.finish(worker_id, success, format_account(result) if success else result.get("error", "unknown"))
        return result

    async def worker(worker_id: int):
        while not queue.empty():
            index = await queue.get()
            print(f"\n{'─' * 50}\n  Account {index}/{num_accounts} (Worker {worker_id})\n{'─' * 50}")
            try: await run_one(worker_id, index)
            finally:
                queue.task_done()
                if delay and not queue.empty():
                    dashboard_state.update(worker_id, "queued", f"Cooldown {delay}s")
                    await asyncio.sleep(delay)

    with LiveDashboard(dashboard_state, enabled=not args.no_dashboard) as live:
        tasks = [asyncio.create_task(worker(wid)) for wid in range(1, workers + 1)]
        while any(not t.done() for t in tasks):
            live.refresh()
            await asyncio.sleep(0.5)
        await asyncio.gather(*tasks)
        live.refresh()

    created = sum(1 for r in run_results if r.get("status") in ("full", "signup_only"))
    print(f"\n{'=' * 60}\n📊 Results: {created} created, {len(run_results) - created} failed\n💾 Saved to: {output_file}")
    if args.export_txt:
        exported = export_txt(results, args.export_txt, proxy_pool=("None" if not proxy else "configured"))
        print(f"🧩 TXT export: {args.export_txt} ({exported} valid keys)")
    print(f"{'=' * 60}")

if __name__ == "__main__":
    asyncio.run(main())
