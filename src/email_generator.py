"""Temp email generator — creates disposable addresses via Cair (cair.eu.cc)."""

from __future__ import annotations

import random
import string
from typing import Optional

import httpx


CAIR_BASE = "https://cair.eu.cc"
CAIR_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) "
    "Gecko/20100101 Firefox/133.0"
)


class EmailGenerator:
    """Generate temporary email addresses via cair.eu.cc API."""

    def __init__(
        self,
        api_url: str = CAIR_BASE,
        domains: Optional[list[str]] = None,
        timeout: int = 30,
    ):
        # Normalisasi base URL: terima "https://cair.eu.cc",
        # "https://cair.eu.cc/api", atau "https://.../api/new_address" (legacy).
        base = api_url.rstrip("/")
        for suffix in ("/api/new_address", "/new_address", "/api"):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
                break
        self.base = base or CAIR_BASE
        self.domains: list[str] = list(domains or [])
        self.timeout = timeout
        self._client: Optional[httpx.Client] = None

    @property
    def client(self) -> httpx.Client:
        if self._client is None or self._client.is_closed:
            self._client = httpx.Client(
                timeout=self.timeout,
                headers={
                    "User-Agent": CAIR_UA,
                    "Accept": "application/json",
                },
            )
        return self._client

    # ------------------------------------------------------------------ domain

    def fetch_domains(self) -> list[str]:
        """Ambil daftar domain aktif dari Cair."""
        r = self.client.get(f"{self.base}/domains")
        r.raise_for_status()
        data = r.json()
        members = data.get("hydra:member") or data.get("member") or []
        return [
            m["domain"] for m in members
            if isinstance(m, dict) and m.get("domain") and m.get("isActive", True)
        ]

    # ------------------------------------------------------------------ create

    def create(
        self,
        username: Optional[str] = None,
        domain: Optional[str] = None,
    ) -> dict:
        """
        Buat alamat email baru via Cair.

        1) POST /accounts   -> {"address": "..."}
        2) POST /token      -> {"token": "<JWT>"}

        Returns dict dengan kunci: email, jwt, address, domain, id
        (kompatibel dengan interface lama supaya main.py tidak perlu diubah).
        """
        if not domain:
            if not self.domains:
                self.domains = self.fetch_domains()
            if not self.domains:
                raise RuntimeError("Cair: tidak ada domain aktif")
            domain = random.choice(self.domains)

        if not username:
            username = "cf" + "".join(
                random.choices(string.ascii_lowercase + string.digits, k=10)
            )

        address = f"{username}@{domain}"

        # 1) Daftarkan alamat
        r = self.client.post(f"{self.base}/accounts", json={"address": address})
        r.raise_for_status()
        try:
            acc = r.json()
        except Exception:
            acc = {}

        # 2) Ambil token akses
        r = self.client.post(f"{self.base}/token", json={"address": address})
        r.raise_for_status()
        tok = r.json()
        token = tok.get("token") or tok.get("jwt")
        if not token:
            raise RuntimeError(f"Cair: token tidak dikembalikan ({tok})")

        return {
            "email": address,
            "jwt": token,
            "address": address,
            "domain": domain,
            "id": acc.get("id") or tok.get("id") or "",
        }

    # ------------------------------------------------------------------ inbox

    def check_inbox(self, jwt: str, limit: int = 20, offset: int = 0) -> list:
        """List email untuk alamat Cair. Body belum ada di endpoint ini."""
        r = self.client.get(
            f"{self.base}/messages",
            headers={"Authorization": f"Bearer {jwt}"},
        )
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict):
            return data.get("hydra:member") or data.get("member") or []
        return data or []

    def get_mail(self, jwt: str, mail_id) -> dict:
        """Ambil detail email (termasuk rawHtml/rawText)."""
        r = self.client.get(
            f"{self.base}/messages/{mail_id}",
            headers={"Authorization": f"Bearer {jwt}"},
        )
        r.raise_for_status()
        return r.json()

    # ------------------------------------------------------------------ lifecycle

    def close(self):
        if self._client and not self._client.is_closed:
            self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
