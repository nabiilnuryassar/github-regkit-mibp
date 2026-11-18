"""Prove the CodeBuddy account can actually answer: one real ACP turn.

Tokens that "exist" are not a working account. This walks the same tail ConTools
(`src/app/bot/workbuddy_web.py`) uses after the token is minted:

    POST {ORIGIN}/console/as/conversations/         -> id + session{link, token, sessionId}
    POST {boot_url}  initialize + session/new       -> Acp-Connection-Id (SSE)
    POST {acp_url}   session/prompt                 -> the agent's reply, streamed

`ok` means the sandbox accepted the prompt and finished the turn — the credits this
exists for are keyed on the query, not on the reply having prose.

Usage:
    python3 /tmp/cb_prove.py
"""
from __future__ import annotations

import base64
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import sys
ROOT = Path(__file__).resolve().parent.parent
_slug = sys.argv[1] if len(sys.argv) > 1 else "vespera.quast_hushmail.com"
OUT = ROOT / "results" / _slug.replace("@", "_")
TOKENS = OUT / "cli_tokens.json"
ORIGIN = "https://www.codebuddy.ai"
MODEL = "default-model"
PROTOCOL_VERSION = "1"
INIT_ID = 1
SESSION_NEW_ID = 2
PROMPT_ID = 3
TIMEOUT = 180

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "X-Product": "SaaS",
    "X-IDE-Type": "IDE",
    "X-IDE-Name": "IDE",
    "X-CodeBuddy-Request": "1",
}


def _req(url, *, method="POST", body=None, headers=None, timeout=60, stream=False):
    data = json.dumps(body).encode() if body is not None else None
    h = dict(headers or {})
    if data is not None:
        h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    return urllib.request.urlopen(req, timeout=timeout)


def _json(url, **kw):
    try:
        with _req(url, **kw) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:  # noqa: BLE001
            return e.code, {}
    except Exception as e:  # noqa: BLE001
        return 0, {"error": str(e)[:160]}


def acp_urls(link: str) -> tuple[str, str]:
    """`(the ACP endpoint, its bootstrap twin)` — the boot leg is `/acp/bootstrap`.

    Verbatim from ConTools `workbuddy_web.acp_urls`: a `/acp` suffix is *replaced* by
    `/acp/bootstrap`, not appended to. Appending gave a 404 and the turn never started.
    """
    import re

    url = (link or "").strip()
    if url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    url = url.rstrip("/")
    return url, re.sub(r"/acp/?$", "/acp/bootstrap", url)


class Turn:
    """Collects JSON-RPC replies off the sandbox's SSE streams."""

    def __init__(self, headers: dict) -> None:
        self.headers = headers
        self.replies: dict[int, dict] = {}
        self.chunks: list[str] = []
        self.lock = threading.Lock()

    def pump(self, resp) -> None:
        try:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    obj = json.loads(payload)
                except Exception:  # noqa: BLE001
                    continue
                with self.lock:
                    rid = obj.get("id")
                    if isinstance(rid, int) and ("result" in obj or "error" in obj):
                        self.replies[rid] = obj
                    text = self._text(obj)
                    if text:
                        self.chunks.append(text)
        except Exception:  # noqa: BLE001 — a closed stream ends the pump
            pass

    @staticmethod
    def _text(obj: dict) -> str:
        method = str(obj.get("method") or "")
        if "session/update" not in method and "agent_message" not in method:
            return ""
        params = obj.get("params") or {}
        update = params.get("update") or params
        content = update.get("content") or {}
        if isinstance(content, dict):
            return str(content.get("text") or "")
        if isinstance(content, list):
            return "".join(str(c.get("text") or "") for c in content if isinstance(c, dict))
        return ""


def main() -> int:
    tokens = json.loads(TOKENS.read_text(encoding="utf-8"))
    bearer = tokens["accessToken"]
    headers = {**HEADERS, "Authorization": f"Bearer {bearer}"}
    print(f"bearer {len(bearer)} chars", flush=True)

    status, resp = _json(f"{ORIGIN}/console/as/conversations/", headers=headers,
                         body={"prompt": "hi", "model": MODEL},
                         timeout=75)
    data = resp.get("data") or {}
    cid = str(data.get("id") or "")
    print(f"1) create conversation -> {status} code={resp.get('code')} id={cid}", flush=True)
    if not cid:
        print(f"   {str(resp)[:300]}", flush=True)
        return 1

    session = data.get("session") if isinstance(data.get("session"), dict) else {}
    sstatus, sresp = _json(f"{ORIGIN}/console/as/conversations/{cid}/session",
                           method="GET", headers=headers, timeout=60)
    if not (session.get("link") and session.get("token")):
        sdata = sresp.get("data") or {}
        session = {k: sdata.get(k) for k in ("link", "token", "sessionId", "cwd")}
    link = session.get("link") or ""
    stoken = session.get("token") or ""
    sid = str(session.get("sessionId") or cid)
    print(f"2) session -> HTTP {sstatus} link={'yes' if link else 'no'} "
          f"token={'yes' if stoken else 'no'}", flush=True)
    if not link or not stoken:
        print(f"   {str(sresp)[:300]}", flush=True)
        return 1

    acp_url, boot_url = acp_urls(link)
    turn = Turn({"Authorization": f"Bearer {stoken}",
                 "Content-Type": "application/json"})

    boot = _req(boot_url, body={
        "initialize": {"jsonrpc": "2.0", "id": INIT_ID, "method": "initialize",
                       "params": {"protocolVersion": PROTOCOL_VERSION,
                                  "clientCapabilities": {}}},
        "sessionNew": {"jsonrpc": "2.0", "id": SESSION_NEW_ID, "method": "session/new",
                       "params": {"sessionId": sid, "cwd": "/workspace", "mcpServers": []}},
    }, headers={**turn.headers, "Accept": "text/event-stream"}, timeout=60)
    conn = str(boot.headers.get("Acp-Connection-Id") or "")
    print(f"3) boot -> Acp-Connection-Id={conn or '(none)'}", flush=True)
    if not conn:
        boot.close()
        return 1
    threading.Thread(target=turn.pump, args=(boot,), daemon=True).start()

    deadline = time.time() + 45
    new = None
    while time.time() < deadline and new is None:
        with turn.lock:
            new = turn.replies.get(SESSION_NEW_ID)
        if new is None:
            time.sleep(0.2)
    print(f"4) session/new -> {'answered' if new else 'never answered'}", flush=True)
    if new is None:
        return 1
    if new.get("error"):
        print(f"   refused: {str(new['error'])[:200]}", flush=True)
        return 1

    prompt = _req(acp_url, body={
        "jsonrpc": "2.0", "id": PROMPT_ID, "method": "session/prompt",
        "params": {"sessionId": sid,
                   "prompt": [{"type": "text", "text": "say hi in five words"}]},
    }, headers={**turn.headers, "Acp-Connection-Id": conn,
                "Accept": "application/json, text/event-stream"}, timeout=TIMEOUT)
    threading.Thread(target=turn.pump, args=(prompt,), daemon=True).start()

    deadline = time.time() + TIMEOUT
    done = None
    while time.time() < deadline:
        with turn.lock:
            done = turn.replies.get(PROMPT_ID)
            reply = "".join(turn.chunks)
        if done is not None:
            break
        time.sleep(0.5)

    with turn.lock:
        reply = "".join(turn.chunks).strip()
        stop = (done or {}).get("result", {}).get("stopReason", "")
    print(f"5) prompt -> {'finished' if done else 'timed out'} stopReason={stop or '(none)'}",
          flush=True)
    if reply:
        print(f"\nREPLY FROM THE MODEL:\n{reply[:600]}", flush=True)
        print("\n=> account is usable: a real turn was answered.", flush=True)
        return 0
    print("\n=> the turn ran but no prose came back (see stopReason)", flush=True)
    return 0 if done else 1


if __name__ == "__main__":
    sys.exit(main())
