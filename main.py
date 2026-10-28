#!/usr/bin/env python3
"""
Cloudflare Auto Signup — Main Orchestrator (Cair mail backend)

Automates the full pipeline:
1. Generate temp email via Cair (cair.eu.cc)
2. Sign up for Cloudflare account (with Turnstile bypass)
3. Verify Cloudflare email via Cair inbox
4. Create Account API Token (Workers AI permissions)
5. Validate token against Workers AI API
6. Save results to JSON

Usage:
    python main.py                          # Create 1 account (headed via Xvfb)
    python main.py --accounts 5             # Create 5 accounts
    python main.py --headless               # Force headless mode
    python main.py --proxy http://user:pass@host:port
    python main.py --config custom.json
    python main.py --validate-only --token cfut_xxx --account-id xxx

Requirements:
    - Linux with Xvfb (xvfb-run)
    - Python 3.10+
    - Google Chrome installed
    - nodriver, opencv-python-headless, httpx
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
    generate_password,
    generate_username,
    load_config,
    save_result,
    load_results,
    timestamp,
    wait_with_progress,
    format_account,
)


# ------------------------------------------------------------------ Chrome utils

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


def _chrome_args(headless: bool = False) -> list[str]:
    """Argumen Chrome untuk CI/Docker. Tambah --headless hanya jika diminta."""
    args = [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
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
        "--disable-blink-features=AutomationControlled",
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36",
    ]
    if headless:
        args.append("--headless=new")
    return args


async def _start_browser(headless: bool, proxy: str | None) -> uc.Browser:
    """Start browser dengan path eksplisit dan retry."""
    chrome_path = get_chrome_path()
    if not chrome_path:
        raise RuntimeError(
            "Chrome tidak ditemukan di path yang dikenal. "
            "Pastikan google-chrome-stable terinstal di /usr/bin/."
        )
    print(f"  🚀 Chrome: {chrome_path} | headless={headless}")

    last_exc = None
    for attempt in range(1, 4):
        try:
            browser = await uc.start(
                headless=headless,
                lang="en-US",
                proxy=proxy,
                sandbox=False,
                browser_executable_path=chrome_path,
                browser_args=_chrome_args(headless=headless),
            )
            return browser
        except Exception as e:
            last_exc = e
            print(f"  ⚠️ Gagal launch (percobaan {attempt}/3): {e}")
            if attempt < 3:
                await asyncio.sleep(3)
    raise RuntimeError(f"Gagal launch browser setelah 3 percobaan: {last_exc}")


# ------------------------------------------------------------------ CLI

def parse_args():
    parser = argparse.ArgumentParser(
        description="Automated Cloudflare account creation with Workers AI tokens"
    )
    parser.add_argument(
        "--accounts", "-n", type=int, default=1,
        help="Number of accounts to create (default: 1)"
    )
    parser.add_argument(
        "--config", "-c", type=str, default="config.json",
        help="Config file path (default: config.json)"
    )
    parser.add_argument(
        "--proxy", "-p", type=str, default=None,
        help="Proxy URL (http://user:pass@host:port)"
    )
    parser.add_argument(
        "--output", "-o", type=str, default=None,
        help="Output JSON file (default: from config)"
    )
    parser.add_argument(
        "--delay", "-d", type=int, default=None,
        help="Delay between accounts in seconds (default: from config)"
    )
    parser.add_argument(
        "--headless", action="store_true", default=False,
        help="Mode headless (default: False; pakai Xvfb untuk headed)"
    )
    parser.add_argument(
        "--no-headless", action="store_true",
        help="Paksa headed mode (default sudah headed kalau ada DISPLAY)"
    )
    parser.add_argument(
        "--validate-only", action="store_true",
        help="Only validate an existing token (requires --token and --account-id)"
    )
    parser.add_argument("--token", type=str, help="Token to validate")
    parser.add_argument("--account-id", type=str, help="Account ID for validation")
    parser.add_argument(
        "--retry", type=int, default=3,
        help="Number of retry attempts per account (default: 3)"
    )
    parser.add_argument(
        "--workers", "-w", type=int, default=1,
        help="Number of concurrent account workers (default: 1)"
    )
    parser.add_argument(
        "--no-dashboard", action="store_true",
        help="Disable Rich live dashboard"
    )
    parser.add_argument(
        "--export-txt", type=str, default=None,
        help="Export valid results to 9Router-friendly .txt"
    )
    return parser.parse_args()


# ------------------------------------------------------------------ Account creation

async def create_account(
    config: dict,
    proxy: str = None,
    headless: bool = False,
    browser: uc.Browser = None,
) -> dict:
    """Create a single Cloudflare account with API token."""
    # Generate credentials
    username = generate_username()
    password = generate_password()
    token_name = config.get("token_name", "workers-ai-auto")

    mail_api = config.get("mail_api") or "https://cair.eu.cc"
    domains = config.get("mail_domains") or []
    domain = random.choice(domains) if domains else None

    # Create temp email via Cair
    email_gen = EmailGenerator(mail_api, domains)
    try:
        mail = email_gen.create(username=username, domain=domain)
        email = mail["email"]
        if not config.get("mail_domains") and email_gen.domains:
            config["mail_domains"] = list(email_gen.domains)
    except Exception as e:
        return {
            "status": "error",
            "error": f"Email creation failed: {e}",
            "email": f"{username}@{domain or '?'}",
        }
    finally:
        email_gen.close()

    print(f"  📧 {email}")

    own_browser = False
    if browser is None:
        browser = await _start_browser(headless, proxy)
        own_browser = True

    try:
        # Phase 1: Signup
        print("  [1/4] Signing up...")
        page = await browser.get("https://dash.cloudflare.com/sign-up")
        signup_result = await signup(page, email, password)

        if not signup_result.success:
            return {
                "status": "error",
                "email": email,
                "password": password,
                "error": f"Signup failed: {signup_result.error}",
            }

        account_id = signup_result.account_id
        print(f"  🆔 Account ID: {account_id}")

        # Phase 2: Email verification
        print("  [2/4] Verifying Cloudflare email...")
        verify_result = await verify_cloudflare_email(
            page,
            mail_api=mail_api,
            jwt=mail.get("jwt", ""),
            timeout=config.get("email_verify_timeout", 120),
            poll_interval=config.get("email_verify_poll_interval", 5),
        )
        if verify_result.success:
            print("  ✅ Email verified")
        else:
            print(f"  ⚠️ Email verification failed: {verify_result.error}")

        # Phase 3: Token creation
        print("  [3/4] Creating API token...")
        if verify_result.success:
            token_result = await create_token_api(page, account_id, token_name)
            if not token_result.success:
                print(f"  ⚠️ API token failed: {token_result.error}; trying UI fallback")
                token_result = await create_token(page, account_id, token_name)
        else:
            token_result = await create_token(page, account_id, token_name)

        api_token = token_result.token if token_result.success else ""
        if api_token:
            print(f"  🔑 Token: {api_token[:30]}...")
        else:
            print(f"  ⚠️ Token creation failed: {token_result.error}")

        # Phase 4: Validation
        token_valid = False
        model_count = 0
        if api_token:
            print("  [4/4] Validating token...")
            validation = validate_token(api_token, account_id)
            token_valid = validation.valid
            model_count = validation.workers_ai_models
            if token_valid:
                print(f"  ✅ Valid! {model_count} Workers AI models available")
            else:
                print(f"  ❌ Validation failed: {validation.error}")
        else:
            print("  [4/4] Skipping validation (no token)")

        return {
            "email": email,
            "password": password,
            "jwt": mail.get("jwt", ""),
            "account_id": account_id,
            "api_token": api_token,
            "token_valid": token_valid,
            "workers_ai_models": model_count,
            "token_name": token_name,
            "email_verified": verify_result.success,
            "email_verify_error": "" if verify_result.success else verify_result.error,
            "status": "full" if token_valid else ("signup_only" if account_id else "error"),
            "created_at": timestamp(),
            "proxy_used": proxy or "direct",
        }

    except Exception as e:
        return {
            "status": "error",
            "email": email,
            "password": password,
            "error": str(e),
            "created_at": timestamp(),
        }
    finally:
        if own_browser:
            try:
                browser.stop()
            except Exception:
                pass
            await asyncio.sleep(3)


# ------------------------------------------------------------------ Export TXT

def export_txt(results: list[dict], output_path: str, proxy_pool: str = "None") -> int:
    lines = ["Cloudflare Workers AI keys for 9Router", ""]
    count = 0
    for result in results:
        token = result.get("api_token", "")
        account_id = result.get("account_id", "")
        if token.startswith("cfut_") and account_id and result.get("token_valid"):
            count += 1
            lines.extend([
                f"[{count}]",
                f"Name: {result.get('email', f'cloudflare-key-{count}')}",
                f"API Key: {token}",
                f"Account ID: {account_id}",
                "Priority: 1",
                f"Proxy Pool: {proxy_pool}",
                "",
            ])
    lines.extend(["--- Summary ---", f"Valid keys: {count}"])
    Path(output_path).write_text("\n".join(lines) + "\n")
    return count


# ------------------------------------------------------------------ Main

async def main():
    args = parse_args()

    config = load_config(args.config)
    proxy = args.proxy or config.get("proxy")
    output_file = args.output or config.get("output_file", "results.json")
    delay = args.delay if args.delay is not None else config.get("delay_between_accounts", 300)
    num_accounts = args.accounts
    max_retry = args.retry

    # Headless logic:
    #   - default: headed (lebih lolos Turnstile interstitial)
    #   - --headless: paksa headless
    #   - --no-headless: paksa headed
    #   - di CI + DISPLAY (xvfb-run): paksa headed
    headless = args.headless
    if args.no_headless:
        headless = False
    if os.getenv("CI") and os.getenv("DISPLAY") and not args.headless:
        headless = False
        print("  ℹ️  CI + DISPLAY terdeteksi → pakai headed mode via Xvfb")

    # Validate-only
    if args.validate_only:
        if not args.token or not args.account_id:
            print("❌ --validate-only requires --token and --account-id")
            sys.exit(1)
        print("🔍 Validating token...")
        result = validate_token(args.token, args.account_id)
        print(f"  Valid: {result.valid}")
        print(f"  Models: {result.workers_ai_models}")
        if result.error:
            print(f"  Error: {result.error}")
        sys.exit(0 if result.valid else 1)

    mail_backend = "Cair" if "cair" in (config.get("mail_api") or "").lower() else "custom"

    print("=" * 60)
    print("☁️  Cloudflare Auto Signup — Workers AI Token Creator")
    print("=" * 60)
    print(f"  Mail backend: {mail_backend} ({config.get('mail_api')})")
    print(f"  Accounts to create: {num_accounts}")
    print(f"  Proxy: {proxy or 'None (direct)'}")
    print(f"  Delay between: {delay}s")
    print(f"  Output: {output_file}")
    print(f"  Headless: {headless}")
    print("=" * 60)

    workers = max(1, args.workers)
    results = load_results(output_file)
    run_results: list[dict] = []
    save_lock = asyncio.Lock()
    queue: asyncio.Queue[int] = asyncio.Queue()
    for i in range(num_accounts):
        queue.put_nowait(i + 1)

    dashboard_state = DashboardState(total=num_accounts, workers=workers)

    async def run_one(worker_id: int, index: int) -> dict:
        dashboard_state.update(worker_id, "signup", "Starting signup", index=index)
        result: dict = {"status": "error", "error": "not_started"}
        success = False
        for attempt in range(max_retry):
            if attempt > 0:
                dashboard_state.update(worker_id, "signup", f"Retry {attempt}/{max_retry - 1}", index=index)
                await asyncio.sleep(30)

            result = await create_account(
                config=config,
                proxy=proxy,
                headless=headless,
            )
            if result.get("email"):
                dashboard_state.update(
                    worker_id, "validate",
                    result.get("status", "done"),
                    email=result["email"], index=index,
                )

            if result.get("status") in ("full", "signup_only"):
                success = True
                break
            if any(
                marker in str(result.get("error", "")).lower()
                for marker in ("rate", "unable", "connection closed", "no close frame")
            ):
                dashboard_state.update(
                    worker_id, "failed", "Transient issue; retry cooldown",
                    email=result.get("email", ""), index=index,
                )
                await asyncio.sleep(delay)
            else:
                break

        async with save_lock:
            save_result(result, output_file)
            results.append(result)
            run_results.append(result)

        dashboard_state.finish(
            worker_id, success,
            format_account(result) if success else result.get("error", "unknown"),
        )
        return result

    async def worker(worker_id: int):
        while not queue.empty():
            index = await queue.get()
            print(f"\n{'─' * 50}\n  Account {index}/{num_accounts} (Worker {worker_id})\n{'─' * 50}")
            try:
                await run_one(worker_id, index)
            finally:
                queue.task_done()
                if delay and not queue.empty():
                    dashboard_state.update(worker_id, "queued", f"Cooldown {delay}s")
                    await asyncio.sleep(delay)

    with LiveDashboard(dashboard_state, enabled=not args.no_dashboard) as live:
        worker_tasks = [asyncio.create_task(worker(wid)) for wid in range(1, workers + 1)]
        while any(not task.done() for task in worker_tasks):
            live.refresh()
            await asyncio.sleep(0.5)
        await asyncio.gather(*worker_tasks)
        live.refresh()

    created = sum(1 for r in run_results if r.get("status") in ("full", "signup_only"))
    failed = len(run_results) - created

    print(f"\n{'=' * 60}")
    print(f"📊 Results: {created} created, {failed} failed")
    print(f"💾 Saved to: {output_file}")
    if args.export_txt:
        exported = export_txt(
            results, args.export_txt,
            proxy_pool=("None" if not proxy else "configured-proxy"),
        )
        print(f"🧩 9Router TXT export: {args.export_txt} ({exported} valid keys)")
    if run_results:
        print(f"\nAccounts:")
        for r in run_results:
            print(format_account(r))
    print(f"{'=' * 60}")


if __name__ == "__main__":
    asyncio.run(main())
