"""Self-check for the CodeBuddy flow bridge. Run: python -m tests.test_codebuddy"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from github_register import codebuddy_flow as cf


def test_load_accounts_both_formats():
    text = "\n".join([
        "a@x.com----pw1----user1----JBSWY3DPEHPK3PXP----true",
        "b@x.com|pw2|user2|123456",
        "# comment",
        "",
    ])
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "acc.txt"
        p.write_text(text)
        accs = cf.load_accounts(p)
    assert len(accs) == 2
    assert accs[0] == {"email": "a@x.com", "password": "pw1",
                       "username": "user1", "totp": "JBSWY3DPEHPK3PXP"}
    assert accs[1] == {"email": "b@x.com", "password": "pw2",
                       "username": "user2", "totp": "123456"}


def test_slug_and_input_conversion():
    assert cf.slug("a@x.com") == "a_x.com"
    with tempfile.TemporaryDirectory() as td:
        dest = Path(td) / "codebuddy.txt"
        cf.write_codebuddy_input(
            [{"email": "a@x.com", "password": "pw", "username": "u", "totp": "S"}],
            dest)
        assert dest.read_text().strip() == "a@x.com|pw|u|S"


def test_config_generation_pins_proxies():
    with tempfile.TemporaryDirectory() as td:
        dest = Path(td) / "configgithublogin.json"
        cf.write_codebuddy_config(
            ["http://a:1@h:1", "http://b:2@h:2"],
            [{"email": "a@x.com", "password": "p"},
             {"email": "b@x.com", "password": "p"}],
            concurrency=3, delay_ms=8000, dest=dest)
        data = json.loads(dest.read_text())
    assert data["concurrency"] == 3
    assert data["delayMs"] == 8000
    assert data["accounts"]["a@x.com"].startswith("http://a:1")
    assert data["accounts"]["b@x.com"].startswith("http://b:2")


def test_sync_results_copies_tree():
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "results" / "a_x.com"
        src.mkdir(parents=True)
        (src / "cli_tokens.json").write_text('{"accessToken":"t"}')
        (src / "codebuddy_session.json").write_text("{}")
        import github_register.codebuddy_flow as mod
        old_cb, old_out = mod.CB_DIR, mod.OUT_DIR
        try:
            mod.CB_DIR = Path(td)
            mod.OUT_DIR = Path(td) / "out"
            n = mod.sync_results([{"email": "a@x.com"}], log=lambda m: None)
            assert n == 1
            assert (mod.OUT_DIR / "a_x.com" / "cli_tokens.json").is_file()
        finally:
            mod.CB_DIR, mod.OUT_DIR = old_cb, old_out


def test_list_and_export_tokens():
    import github_register.codebuddy_flow as mod
    with tempfile.TemporaryDirectory() as td:
        out = Path(td)
        (out / "a_x.com_cli_tokens.json").write_text(json.dumps(
            {"email": "a@x.com", "accessToken": "A1", "refreshToken": "R1",
             "saved_at": "2026-01-01T00:00:00"}))
        (out / "b_x.com_cli_tokens.json").write_text(json.dumps(
            {"email": "b@x.com", "accessToken": "A2", "refreshToken": "",
             "saved_at": "2026-01-02T00:00:00"}))
        old = mod.OUT_DIR
        try:
            mod.OUT_DIR = out
            rows = mod.list_tokens()
            assert [r["email"] for r in rows] == ["a@x.com", "b@x.com"]
            assert rows[1]["has_refresh"] is False
            assert "accessToken" not in json.dumps(rows)  # metadata only
            _, access = mod.export_tokens("access")
            assert access.splitlines() == ["a@x.com----A1", "b@x.com----A2"]
            _, refresh = mod.export_tokens("refresh")
            assert refresh.splitlines() == ["a@x.com----R1"]  # empty token skipped
            _, one = mod.export_tokens("refresh", ["A@x.com"])
            assert one.splitlines() == ["a@x.com----R1"]
            try:
                mod.export_tokens("nope")
            except ValueError:
                pass
            else:
                raise AssertionError("unknown kind must raise")
        finally:
            mod.OUT_DIR = old

def test_jwt_expiry():
    import base64
    def jwt(payload: dict) -> str:
        raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        return f"h.{raw}.s"
    assert cf.jwt_expiry(jwt({"exp": 1234})) == 1234
    assert cf.jwt_expiry("not-a-jwt") is None
    assert cf.jwt_expiry("") is None

def test_fetch_quota_normalizes_packages():
    import github_register.codebuddy_flow as mod
    calls = []
    def fake(path, token, body=None, method="POST", headers=None, timeout=25):
        calls.append(path)
        if path == "/console/accounts":
            return 200, {"data": {"accounts": [{"uid": "u1", "type": "personal",
                                                "areaInfoComplete": True}]}}
        if path.endswith("get-payment-type"):
            return 200, {"data": {"paymentType": "trial"}}
        return 200, {"code": 0, "data": {"Response": {"Data": {
            "TotalDosage": 300,
            "Accounts": [
                {"PackageName": "Bonus", "CapacityRemain": 250,
                 "CapacitySize": 250, "CapacityUsed": 0,
                 "CycleEndTime": "2026-12-02 08:38:13"},
                {"PackageName": "Free", "CapacityRemain": 40,
                 "CapacitySize": 100, "CapacityUsed": 0},
            ]}}}}
    old = mod._cb_call
    try:
        mod._cb_call = fake
        q = mod.fetch_quota("tok")
    finally:
        mod._cb_call = old
    assert q["ok"] is True and q["payment_type"] == "trial"
    assert (q["total_remain"], q["total_size"], q["total_used"]) == (290, 350, 60)
    assert [p["name"] for p in q["packages"]] == ["Bonus", "Free"]
    assert "tok" not in json.dumps(q)

def test_fetch_quota_names_unactivated_account():
    import github_register.codebuddy_flow as mod
    def fake(path, token, body=None, method="POST", headers=None, timeout=25):
        if path == "/console/accounts":
            return 200, {"data": {"accounts": [{"uid": "u1", "areaInfoComplete": False}]}}
        if path.endswith("get-payment-type"):
            return 200, {"data": {"paymentType": "free"}}
        return 500, {"code": 10000, "msg": "API request failed with status code: 500"}
    old = mod._cb_call
    try:
        mod._cb_call = fake
        q = mod.fetch_quota("tok")
    finally:
        mod._cb_call = old
    assert q["ok"] is False
    assert "belum diaktifkan" in q["detail"]


if __name__ == "__main__":
    for name, fn in sorted((n, f) for n, f in globals().items()
                           if n.startswith("test_")):
        fn()
        print(f"[OK] {name}")
    print("[*] all tests passed")
