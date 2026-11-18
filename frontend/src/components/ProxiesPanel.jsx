import React, { useEffect, useRef, useState } from "react";
import {
  Activity,
  CheckCircle2,
  RefreshCw,
  ShieldAlert,
  Upload,
  XCircle,
} from "lucide-react";
import { api } from "../api.js";
import { Badge, Button, Card, Input, Spinner, EmptyState } from "./ui.jsx";

const VERDICT = {
  usable: { tone: "success", label: "usable", icon: CheckCircle2 },
  blocked: { tone: "accent", label: "blocked", icon: ShieldAlert },
  dead: { tone: "danger", label: "dead", icon: XCircle },
};

const mono = { fontFamily: "ui-monospace, SFMono-Regular, monospace", fontSize: 12.5 };

export default function ProxiesPanel() {
  const [pool, setPool] = useState(null);
  const [paste, setPaste] = useState("");
  const [busy, setBusy] = useState("");
  const [msg, setMsg] = useState("");
  const [bad, setBad] = useState(false);
  const [limit, setLimit] = useState("20");
  const [test, setTest] = useState(null);
  const fileInput = useRef(null);

  function notify(text, isBad = false) {
    setMsg(text);
    setBad(isBad);
    setTimeout(() => setMsg(""), 5000);
  }

  function load() {
    api
      .get("/api/proxy/pool")
      .then(setPool)
      .catch((e) => notify(e.message, true));
  }

  useEffect(load, []);

  async function onUpload(e) {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    setBusy("upload");
    try {
      const d = await api.upload("/api/proxy/upload", file);
      notify(`${d.count} proxies saved to ${d.proxy_file}`);
      load();
    } catch (err) {
      notify(err.message, true);
    } finally {
      setBusy("");
    }
  }

  async function onPaste() {
    if (!paste.trim()) return notify("paste at least one proxy line", true);
    setBusy("paste");
    try {
      const d = await api.upload(
        "/api/proxy/upload",
        new Blob([paste], { type: "text/plain" })
      );
      notify(`${d.count} proxies saved to ${d.proxy_file}`);
      setPaste("");
      load();
    } catch (err) {
      notify(err.message, true);
    } finally {
      setBusy("");
    }
  }

  async function onTest(usePaste) {
    if (usePaste && !paste.trim()) return notify("paste a list to test", true);
    setBusy("test");
    setTest(null);
    try {
      const d = await api.post("/api/proxy/test", {
        raw: usePaste ? paste : null,
        limit: Number(limit) || 20,
      });
      setTest(d);
      notify(
        `${d.summary.usable}/${d.summary.total} usable — ${
          d.summary.healthy ? "pool healthy" : "POOL NOT USABLE"
        }`,
        !d.summary.healthy
      );
    } catch (err) {
      notify(err.message, true);
    } finally {
      setBusy("");
    }
  }

  const s = test?.summary;
  const testing = busy === "test";

  return (
    <div className="cb-wrap">
      <Card className="cb-head" style={{ padding: 20 }}>
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            flexWrap: "wrap",
            gap: 12,
          }}
        >
          <div>
            <div
              style={{
                fontSize: 19,
                fontWeight: 800,
                display: "flex",
                alignItems: "center",
                gap: 10,
              }}
            >
              <Activity size={18} /> Proxy Pool
            </div>
            <div style={{ fontSize: 12.5, color: "var(--muted)", marginTop: 3 }}>
              Upload or paste a list, then test how GitHub treats each exit IP —
              no account is created by a test.
            </div>
          </div>
          <Badge tone={pool?.count ? "accent" : "muted"}>
            {pool ? `${pool.count} proxies` : "loading…"}
          </Badge>
        </div>
      </Card>

      <Card style={{ padding: 20 }}>
        <div className="cb-grid">
          <label className="cb-field">
            <span>Upload .txt (one proxy per line)</span>
            <input
              ref={fileInput}
              type="file"
              accept=".txt,text/plain"
              onChange={onUpload}
              style={{ display: "none" }}
            />
            <Button
              type="button"
              onClick={() => fileInput.current?.click()}
              disabled={busy === "upload"}
            >
              <Upload size={14} style={{ marginRight: 6 }} />
              {busy === "upload" ? "Saving…" : "Choose file"}
            </Button>
          </label>

          <label className="cb-field cb-small">
            <span>Proxies to test</span>
            <Input
              type="number"
              min={1}
              max={200}
              value={limit}
              onChange={(e) => setLimit(e.target.value)}
            />
          </label>
        </div>

        <label className="cb-field" style={{ marginTop: 12 }}>
          <span>…or paste the list here</span>
          <textarea
            className="ui-input"
            rows={7}
            spellCheck={false}
            value={paste}
            onChange={(e) => setPaste(e.target.value)}
            placeholder={"socks5://user:pass@host:port\nhttp://user:pass@host:port"}
            style={{ ...mono, width: "100%", resize: "vertical" }}
          />
        </label>

        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginTop: 10 }}>
          <Button onClick={onPaste} disabled={!paste.trim() || busy === "paste"}>
            <Upload size={14} style={{ marginRight: 6 }} />
            {busy === "paste" ? "Saving…" : "Save as pool"}
          </Button>
          <Button variant="ghost" onClick={() => onTest(true)} disabled={testing}>
            <Activity size={14} style={{ marginRight: 6 }} />
            Test pasted list
          </Button>
          <Button variant="primary" onClick={() => onTest(false)} disabled={testing}>
            {testing ? (
              <Spinner style={{ marginRight: 6 }} />
            ) : (
              <RefreshCw size={14} style={{ marginRight: 6 }} />
            )}
            {testing ? "Testing…" : "Test pool"}
          </Button>
        </div>

        {msg && (
          <div
            style={{
              fontSize: 13,
              color: bad ? "var(--danger)" : "var(--ok)",
              marginTop: 10,
            }}
          >
            {msg}
          </div>
        )}
      </Card>

      <Card style={{ padding: 20 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}>
          <ShieldAlert size={16} />
          <strong style={{ fontSize: 15 }}>Reputation test</strong>
        </div>
        <p style={{ fontSize: 12.5, color: "var(--muted)", marginTop: 0 }}>
          Each proxy is probed for reachability, then whether{" "}
          <code>github.com/signup</code> serves the form or the DataDome stub, and
          whether <code>api.github.com</code> still answers. A proxy that
          connects but gets challenged is <b>blocked</b>: in production it burns
          a full signup cycle before failing.
        </p>

        {s && (
          <>
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap", margin: "12px 0" }}>
              <Badge tone={s.healthy ? "success" : "danger"}>
                {s.healthy ? "pool healthy" : "pool not usable"}
              </Badge>
              <Badge tone="success">{s.usable} usable</Badge>
              <Badge tone="accent">{s.blocked} blocked</Badge>
              <Badge tone="danger">{s.dead} dead</Badge>
              <Badge tone="muted">{Math.round(s.usable_ratio * 100)}% usable</Badge>
            </div>

            {Object.keys(s.countries || {}).length > 0 && (
              <div style={{ fontSize: 12.5, color: "var(--muted)", marginBottom: 10 }}>
                Countries:{" "}
                {Object.entries(s.countries)
                  .map(([c, n]) => `${c} ×${n}`)
                  .join(" · ")}
              </div>
            )}

            <div style={{ overflowX: "auto" }}>
              <table style={{ width: "100%", fontSize: 12.5, borderCollapse: "collapse" }}>
                <thead>
                  <tr style={{ textAlign: "left", color: "var(--muted)" }}>
                    <th style={{ padding: "6px 8px" }}>Proxy</th>
                    <th style={{ padding: "6px 8px" }}>Verdict</th>
                    <th style={{ padding: "6px 8px" }}>Exit IP</th>
                    <th style={{ padding: "6px 8px" }}>Signup</th>
                    <th style={{ padding: "6px 8px" }}>ms</th>
                    <th style={{ padding: "6px 8px" }}>Notes</th>
                  </tr>
                </thead>
                <tbody>
                  {test.results.map((r) => {
                    const v = VERDICT[r.verdict] || VERDICT.dead;
                    const Icon = v.icon;
                    return (
                      <tr key={r.id} style={{ borderTop: "1px solid var(--border)" }}>
                        <td style={{ padding: "6px 8px", ...mono }}>{r.id}</td>
                        <td style={{ padding: "6px 8px" }}>
                          <Badge tone={v.tone}>
                            <Icon size={11} style={{ marginRight: 4, verticalAlign: "-1px" }} />
                            {v.label}
                          </Badge>
                        </td>
                        <td style={{ padding: "6px 8px" }}>
                          {r.exit_ip || "—"}
                          {r.country ? ` (${r.country})` : ""}
                        </td>
                        <td style={{ padding: "6px 8px" }}>
                          {r.signup_challenged ? "challenged" : r.signup_ok ? "form" : "—"}
                        </td>
                        <td style={{ padding: "6px 8px" }}>{r.latency_ms || "—"}</td>
                        <td style={{ padding: "6px 8px", color: "var(--muted)" }}>
                          {(r.reasons || []).join(", ") || r.error || "—"}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </>
        )}

        {!s && !testing && (
          <EmptyState
            icon={ShieldAlert}
            title="No test yet"
            description="Run a test to see which exit IPs GitHub accepts."
          />
        )}
        {testing && (
          <div style={{ padding: "18px 0", display: "flex", justifyContent: "center" }}>
            <Spinner />
          </div>
        )}
      </Card>
    </div>
  );
}
