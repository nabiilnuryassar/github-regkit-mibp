"""Configuration loading for the GitHub register toolkit."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

@dataclass
class Config:
    # Mail provider: "mailcx" (free, default) or "litensi" (paid, reliable)
    mail_provider: str = "mailcx"  # "mailcx" | "litensi"
    # Mail.cx temp email settings
    mailcx_domain: str = ""  # empty = auto-pick from uqu.me, ddker.com, 9k3r.com
    # Litensi Mail settings
    litensi_api_id: str = ""
    litensi_api_key: str = ""
    litensi_site: str = ""   # e.g. "github.com"
    litensi_zone: str = ""   # blank = auto-pick cheapest in-stock zone
    # Cloudflare Email Routing settings (mail_provider="cloudflare")
    cloudflare_domains: list[str] = field(default_factory=list)
    # rotating subdomains per domain; include "" for apex, e.g. ["a","b","c"]
    cloudflare_subdomains: list[str] = field(default_factory=lambda: ["a", "b", "c"])
    # Tempik (mail_provider="tempik") — the project's own Cloudflare Worker
    # temp-mail API; empty domains = auto-fetch from the worker /api/config.
    tempik_api: str = "https://tempik.iotsentinel.web.id/api"
    tempik_domains: list[str] = field(default_factory=list)
    imap_host: str = "imap.gmail.com"
    imap_user: str = ""
    imap_pass: str = ""
    register_count: int = 1
    proxy: str = ""
    proxy_file: str = ""     # proxy pool file in project root (one URL per line); overrides proxy
    # Browser engine: "" = automatic fallback order (engine_order); any engine
    # key from github_register.engines makes the run exclusive to that engine.
    engine: str = ""
    engine_order: list[str] = field(default_factory=lambda: [
        "camoufox", "patchright", "cloakbrowser",
    ])
    # Per-engine extras: {"fingerprint_chromium": "/path/to/binary",
    #                     "clawbrowser": "ws://...", "aliasmode": "ws://..."}
    engine_paths: dict[str, str] = field(default_factory=dict)

    headless: bool = False
    delay_sec: float = 5.0
    max_username_tries: int = 6
    otp_timeout_sec: int = 240
    browser_profile_dir: str = ".browser-profile"
    # fresh browser per account (incognito-like, zero cached state); the
    # DataDome trust cookie is carried over via .datadome-trust.json so the
    # signup page keeps loading without hard 403s
    fresh_profile: bool = True
    proxy_hard_block_retries: int = 2
    proxy_rate_limit_retries: int = 2
    # post-signup stages (from user recording)
    create_repo: bool = True          # stage 4: create first repository
    repo_name: str = "hello"          # repo name prefix (username-suffix appended on conflict)
    enable_2fa: bool = True           # stage 5: enable TOTP 2FA and store the secret
    set_profile_status: bool = True
    profile_status: str = "On vacation"  # blank disables custom status text
    complete_profile: bool = True
    profile_name: str = ""            # blank = Random User
    profile_bio: str = ""             # blank = ZenQuotes
    profile_location: str = ""        # blank = Random User country
    codebuddy_login: bool = True      # stage 6: sign in to CodeBuddy with the live GitHub session

    @classmethod
    def from_dict(cls, data: dict) -> "Config":
        known = set(cls.__dataclass_fields__)
        mapped = {k: v for k, v in data.items() if k in known}
        return cls(**mapped)


def load_config(path: str | Path) -> Config:
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(
            f"config not found: {p} (copy config.example.json to config.json and fill it in)"
        )
    data = json.loads(p.read_text(encoding="utf-8"))
    return Config.from_dict(data)
