"""Self-check for non-network logic. Run: python -m tests.test_core"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from github_register.profiles import (
    extract_github_code,
    generate_password,
    generate_username,
    is_valid_username,
    parse_public_profile,
)


def test_extract_code():
    assert extract_github_code("Here's your GitHub verification code: 1234 5678") == "12345678"
    assert extract_github_code("Your verification code is 12345678. It expires soon.") == "12345678"
    assert extract_github_code("verification code: 9876 5432") == "98765432"
    assert extract_github_code("no code here") is None
    assert extract_github_code("") is None


def test_password():
    for _ in range(50):
        pw = generate_password()
        assert len(pw) >= 12
        assert any(c.islower() for c in pw)
        assert any(c.isupper() for c in pw)
        assert any(c.isdigit() for c in pw)


def test_username():
    for _ in range(100):
        name = generate_username()
        assert is_valid_username(name), name


def test_litensi_zone_pick():
    from github_register.litensi import LitensiClient

    cli = LitensiClient("id", "key", "github", "")
    zones = [
        {"zone": "a", "stock": 0, "price": 1},
        {"zone": "b", "stock": 5, "price": 3},
        {"zone": "c", "stock": 2, "price": 1.5},
    ]
    stock = [z for z in zones if float(z.get("stock") or 0) > 0]
    assert min(stock, key=lambda z: float(z.get("price") or 0))["zone"] == "c"


def test_proxy_pool_pick():
    from github_register.config import Config
    from github_register.runner import (
        _pick_proxy_url, account_proxy_begin, load_proxy_pool,
    )

    from github_register import runner

    pool_name = "_test_pool_tmp.txt"
    pool_path = runner.ROOT / pool_name
    pool_path.write_text(
        "# comment\n"
        "http://u:p@1.1.1.1:8080\n"
        "\n"
        "not a proxy line\n"
        "socks5://u:p@2.2.2.2:1080\n",
        encoding="utf-8",
    )
    try:
        pool = load_proxy_pool(pool_name)
        assert pool == ["http://u:p@1.1.1.1:8080", "socks5://u:p@2.2.2.2:1080"], pool
        cfg = Config(proxy="http://fallback:1@3.3.3.3:80", proxy_file=pool_name)
        account_proxy_begin()  # the lock is process-wide; start this case clean
        assert _pick_proxy_url(cfg) in pool
        # the lock holds for the rest of the account's cycle
        assert _pick_proxy_url(cfg) == _pick_proxy_url(cfg)
        account_proxy_begin()
        cfg2 = Config(proxy="http://fallback:1@3.3.3.3:80", proxy_file="")
        assert _pick_proxy_url(cfg2) == "http://fallback:1@3.3.3.3:80"
        account_proxy_begin()
        cfg3 = Config(proxy="http://fallback:1@3.3.3.3:80", proxy_file="_missing_pool.txt")
        assert _pick_proxy_url(cfg3) == "http://fallback:1@3.3.3.3:80"
    finally:
        account_proxy_begin()
        pool_path.unlink(missing_ok=True)


def test_parse_public_profile():
    random_user = {
        "results": [{
            "name": {"title": "Mr", "first": "Caleb", "last": "Harvey"},
            "location": {"country": "Ireland"},
            # These must not be included in the resulting profile data.
            "email": "caleb.harvey@example.com",
            "login": {"password": "shop"},
        }]
    }
    quote = [{"q": "A public quote."}]
    assert parse_public_profile(random_user, quote) == {
        "name": "Mr Caleb Harvey", "location": "Ireland", "bio": "A public quote.",
    }
    try:
        parse_public_profile({}, [])
    except ValueError:
        pass
    else:
        raise AssertionError("invalid profile payload must fail")


def test_mailbox_timeout_is_not_fatal_provider_error():
    """A per-mailbox 'no code' timeout must NOT abort the whole job.

    Fatal provider/config errors (bad key, no balance, out of stock) are
    LitensiError / MailCxError and DO abort. A single mailbox that never got
    the GitHub code is a transient per-account failure and must be a separate,
    non-fatal type.
    """
    from github_register.litensi import LitensiError
    from github_register.mailcx import MailCxError
    from github_register.mail_errors import MailboxTimeoutError

    # A mailbox timeout must not be classified as a fatal provider error.
    assert not issubclass(MailboxTimeoutError, LitensiError)
    assert not issubclass(MailboxTimeoutError, MailCxError)


def test_litensi_wait_for_code_raises_mailbox_timeout(monkeypatch=None):
    """wait_for_code timeout raises MailboxTimeoutError, not LitensiError."""
    from github_register.litensi import LitensiClient, LitensiError
    from github_register.mail_errors import MailboxTimeoutError

    cli = LitensiClient("id", "key", "github.com", "zone")
    # Every poll reports "no message yet" (no code in the payload).
    cli.get_status = lambda order_id: {"status": "WAITING", "message": ""}

    try:
        cli.wait_for_code("123", timeout=0, poll_interval=5)
    except MailboxTimeoutError:
        pass
    except LitensiError as exc:  # the bug: a timeout looked like a fatal error
        raise AssertionError(f"timeout raised fatal LitensiError instead: {exc}")
    else:
        raise AssertionError("wait_for_code must raise on timeout")


def test_mailcx_wait_for_code_raises_mailbox_timeout():
    """mail.cx timeout raises MailboxTimeoutError, not MailCxError."""
    from github_register.mailcx import MailCxClient, MailCxError
    from github_register.mail_errors import MailboxTimeoutError

    cli = MailCxClient()
    cli.get_messages = lambda address: []

    try:
        cli.wait_for_code("a@b.com", timeout=0, poll_interval=5)
    except MailboxTimeoutError:
        pass
    except MailCxError as exc:  # the bug: a timeout looked like a fatal error
        raise AssertionError(f"timeout raised fatal MailCxError instead: {exc}")
    else:
        raise AssertionError("wait_for_code must raise on timeout")


def test_register_one_continues_after_mailbox_timeout():
    """register_one returns None (one failed account) on a mailbox timeout."""
    from github_register import runner
    from github_register.mail_errors import MailboxTimeoutError

    cfg = runner.Config(mail_provider="litensi", litensi_api_id="id",
                        litensi_api_key="key", litensi_site="github.com")

    def _boom(*args, **kwargs):
        raise MailboxTimeoutError("no GitHub code after 240s")

    orig = runner._run_signup
    runner._run_signup = _boom
    try:
        result = runner.register_one(cfg, log=lambda m: None)
    finally:
        runner._run_signup = orig
    assert result is None, f"mailbox timeout must fail one account, got {result!r}"


def test_run_job_continues_after_mailbox_timeout():
    """run_job keeps going after mailbox timeouts (real register_one path)."""
    from github_register import runner
    from github_register.mail_errors import MailboxTimeoutError

    cfg = runner.Config(mail_provider="mailcx", register_count=3, delay_sec=0)
    calls = {"n": 0}

    def _signup(*args, **kwargs):
        # Every account times out waiting for the GitHub code.
        calls["n"] += 1
        raise MailboxTimeoutError("no GitHub code after 240s")

    orig = runner._run_signup
    runner._run_signup = _signup
    try:
        ok, fail, out = runner.run_job(cfg, log=lambda m: None)
    finally:
        runner._run_signup = orig
        out.unlink(missing_ok=True)
    # The job must attempt ALL accounts instead of aborting on the first one.
    assert calls["n"] == 3, f"job stopped early after mailbox timeout (ran {calls['n']}/3)"
    assert fail == 3 and ok == 0, f"expected 3 fail / 0 ok, got {fail}/{ok}"


def test_run_job_aborts_on_fatal_provider_error():
    """A genuine provider error (bad key / no balance) still aborts the job."""
    from github_register import runner
    from github_register.litensi import LitensiError

    cfg = runner.Config(mail_provider="litensi", register_count=3, delay_sec=0)
    calls = {"n": 0}

    def _one(cfg, log, stop):
        calls["n"] += 1
        raise LitensiError("NOT ENOUGH BALANCE — Litensi balance is insufficient")

    orig = runner.register_one
    runner.register_one = _one
    try:
        ok, fail, out = runner.run_job(cfg, log=lambda m: None)
    finally:
        runner.register_one = orig
        out.unlink(missing_ok=True)
    assert calls["n"] == 1, f"fatal provider error must abort after 1 attempt (ran {calls['n']})"
def test_engine_registry_metadata():
    """Every default-order key has a spec; list_engines returns UI-shaped rows."""
    from github_register import engines
    from github_register.config import Config

    assert set(engines.DEFAULT_ENGINE_ORDER) <= set(engines.ENGINES.keys())
    assert "nextbrowser" in engines.ENGINES  # in the panel, but not in auto order
    for key in engines.DEFAULT_ENGINE_ORDER:
        assert engines.ENGINES[key].key == key
    for meta in engines.list_engines(Config()):
        for field in ("key", "label", "short", "family", "proxy_mode",
                      "description", "hint", "available", "status"):
            assert field in meta, meta


def test_engine_order_resolution():
    """Exclusive `engine` wins; unknown key raises; bad order falls back."""
    from github_register import engines
    from github_register.config import Config

    cfg = Config()
    assert engines.resolve_engine_order(cfg) == ["camoufox", "patchright", "cloakbrowser"]

    cfg.engine = "patchright"
    assert engines.resolve_engine_order(cfg) == ["patchright"]

    cfg.engine = "nonsense"
    try:
        engines.resolve_engine_order(cfg)
        raise AssertionError("unknown engine must raise EngineUnavailable")
    except engines.EngineUnavailable:
        pass

    cfg.engine = ""
    cfg.engine_order = ["patchright", "cloakbrowser", "damru"]
    assert engines.resolve_engine_order(cfg) == ["patchright", "cloakbrowser", "damru"]

    cfg.engine_order = ["bogus", "camoufox"]
    assert engines.resolve_engine_order(cfg) == ["camoufox"]

    cfg.engine_order = []
    assert engines.resolve_engine_order(cfg) == engines.DEFAULT_ENGINE_ORDER


def test_cloudflare_mailbox_rotation():
    """Cloudflare provider rotates domain/subdomain/localpart without network."""
    import re
    from github_register.cloudflare import CloudflareMailClient

    domains = ["beelze.biz.id", "djaitin.web.id"]
    subs = ["a", "b", "c", ""]
    client = CloudflareMailClient(domains=domains, subdomains=subs,
                                  imap_user="u@example.com", imap_pass="x")
    hosts = set()
    for _ in range(80):
        addr, order_id = client.create_mailbox()
        assert addr == order_id
        local, host = addr.split("@")
        assert re.fullmatch(r"[a-z0-9]{8,12}", local), addr
        ok = host in domains or any(
            host == f"{s}.{d}" for s in ["a", "b", "c"] for d in domains)
        assert ok, f"address outside configured hosts: {addr}"
        hosts.add(host)
    assert any(h in domains for h in hosts)          # apex rotation happens
    assert any("." in h for h in hosts)              # subdomain rotation happens


def test_cloudflare_fatal_without_domains():
    """Empty domain list is a fatal config error, not a silent no-op."""
    from github_register.cloudflare import CloudflareMailClient, CloudflareMailError

    try:
        CloudflareMailClient(domains=[], imap_user="u", imap_pass="p")
        raise AssertionError("empty cloudflare_domains must raise")
    except CloudflareMailError:
        pass


def test_tempik_offline_composition():
    """Tempik composes addresses inside the configured domain pool (no network)."""
    from github_register.tempik import TempikMailClient

    domains = ["djaitin.web.id", "a.djaitin.web.id", "urban8.site"]
    client = TempikMailClient(domains=domains, connect=False)
    for _ in range(20):
        addr, order_id = client.create_mailbox()
        assert addr == order_id
        local, host = addr.split("@")
        assert host in domains, addr
        assert local.startswith("inbox")
    assert client.last_order_id


def test_tempik_code_patterns():
    """GitHub code first, then tempik's looser fallback patterns."""
    from github_register.tempik import TempikMailClient

    assert TempikMailClient.extract_code(
        "", "Your GitHub verification code is 1234-5678") == "12345678"
    assert TempikMailClient.extract_code("", "code: 918273") == "918273"
    assert TempikMailClient.extract_code("K73-T6K", "") == ""   # Grok-style: ignored
    assert TempikMailClient.extract_code("", "no digits here") == ""


def test_provider_wait_for_code_contract():
    """Tempik & Cloudflare honor cancel_cb / exclude_codes kwargs (no network)."""
    from github_register.cloudflare import CloudflareMailClient
    from github_register.mail_errors import MailboxCancelled
    from github_register.tempik import TempikMailClient

    tmp = TempikMailClient(domains=["a.biz"], connect=False)
    try:
        tmp.wait_for_code("x@a.biz", timeout=5, cancel_cb=lambda: True)
        raise AssertionError("tempik cancel must raise MailboxCancelled")
    except MailboxCancelled:
        pass

    cf = CloudflareMailClient(domains=["a.biz"], imap_user="", imap_pass="")
    try:
        cf.wait_for_code("x@a.biz", timeout=5, cancel_cb=lambda: True)
        raise AssertionError("cloudflare cancel must raise MailboxCancelled")
    except MailboxCancelled:
        pass


def test_litensi_dashboard_status_summarizes_read_only_data():
    from github_register.litensi import LitensiClient

    cli = LitensiClient("id", "key", "github.com", "gmail.com")
    cli.profile = lambda: {"balance": "8709.00"}
    cli.prices = lambda: [
        {"zone": "gmail.com", "price": 1291, "stock": 2},
        {"zone": "other.com", "price": 100, "stock": 0},
    ]

    status = cli.dashboard_status()

    assert status["balance"] == "8709.00"
    assert status["site"] == "github.com"
    assert status["zones"] == [
        {"zone": "gmail.com", "price": 1291, "stock": 2},
        {"zone": "other.com", "price": 100, "stock": 0},
    ]
    assert status["available_zones"] == 1
    assert status["cheapest"] == "gmail.com"


if __name__ == "__main__":
    for name, fn in sorted((n, f) for n, f in globals().items() if n.startswith("test_")):
        fn()
        print(f"[OK] {name}")
    print("[*] all tests passed")
