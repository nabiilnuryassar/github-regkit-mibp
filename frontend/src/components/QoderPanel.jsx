import React, { useEffect, useRef, useState } from "react";
import { RefreshCw, Square, Play, Zap, FileText } from "lucide-react";
import { api } from "../api.js";
import { Badge, Button, Card, Input, Spinner, EmptyState } from "./ui.jsx";

export default function QoderPanel({ onGotoStatus }) {
  const [pats, setPats] = useState([])
  const [status, setStatus] = useState(null)
  const [results, setResults] = useState(null)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState("")
  const [count, setCount] = useState("1")
  const timer = useRef(null)

  function notify(text) {
    setMsg(text)
    setTimeout(() => setMsg(""), 3000)
  }

  function refresh(silent = true) {
    return Promise.all([
      api.get("/api/qoder/pats").then((d) => setPats(d.pats || [])),
      api.get("/api/qoder/status").then((d) => setStatus(d)),
      api.get("/api/qoder/results").then((d) => setResults(d)),
    ]).catch((e) => {
      if (!silent) notify(e.message || "gagal memuat")
    })
  }

  useEffect(() => {
    refresh(false)
    timer.current = setInterval(() => refresh(true), 5000)
    return () => clearInterval(timer.current)
  }, [])

  const running = !!(status && status.running)

  async function startReg() {
    if (running) return
    setBusy(true)
    try {
      await api.post("/api/qoder/start", { count: Number(count) || 1 })
      notify("Qoder registration dimulai — pantau Live Log")
      onGotoStatus && onGotoStatus()
    } catch (e) {
      notify(e.message || "gagal memulai")
    } finally {
      setBusy(false)
    }
  }

  async function startInject() {
    setBusy(true)
    try {
      const d = await api.post("/api/qoder/inject", {})
      notify(d.detail || `Inject selesai: ${d.ok_count || 0} OK, ${d.fail_count || 0} fail`)
      refresh(false)
    } catch (e) {
      notify(e.message || "gagal inject")
    } finally {
      setBusy(false)
    }
  }

  async function stopAll() {
    try {
      await api.post("/api/qoder/stop", {})
      notify("Stop diminta")
    } catch (e) {
      notify(e.message || "gagal stop")
    }
  }

  return (
    <div className="qoder-wrap">
      <Card style={{ padding: 20 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: 12, marginBottom: 14 }}>
          <div>
            <div style={{ fontSize: 19, fontWeight: 800, display: "flex", alignItems: "center", gap: 10 }}>
              <Zap size={18} /> Qoder Registration & PAT Injection
            </div>
            <div style={{ fontSize: 12.5, color: "var(--muted)", marginTop: 3 }}>
              Modul terpisah — register akun Qoder, capture PAT, inject Pro trial
            </div>
          </div>
          <Badge tone={running ? "accent" : "muted"}>{running ? "Running" : "Idle"}</Badge>
        </div>

        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
          <label style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 13, color: "var(--muted)" }}>
            Count:
            <input
              type="number"
              value={count}
              onChange={(e) => setCount(e.target.value)}
              style={{ width: 60, padding: "8px 10px", fontSize: 13, background: "var(--bg-input)", color: "var(--text)", border: "1px solid var(--border)", borderRadius: 8, outline: "none" }}
            />
          </label>
          <Button variant="primary" onClick={startReg} disabled={busy || running}>
            <Play size={15} /> Register
          </Button>
          <Button variant="primary" onClick={startInject} disabled={busy || running}>
            <Zap size={15} /> Inject All PATs
          </Button>
          <Button variant="destructive" onClick={stopAll} disabled={!running}>
            <Square size={15} /> Stop
          </Button>
          <Button onClick={() => refresh(false)}>
            <RefreshCw size={15} /> Refresh
          </Button>
        </div>

        {status && (
          <div style={{ marginTop: 10, fontSize: 12.5, color: "var(--muted)" }}>
            PATs: {pats.length} | injected: {status.injected || 0} | running: {status.running ? "yes" : "no"}
          </div>
        )}
        {msg && <div style={{ marginTop: 8, fontSize: 13, color: "var(--accent-text)" }}>{msg}</div>}
      </Card>

      <Card style={{ flex: 1, padding: 0, overflow: "hidden", minHeight: 200 }}>
        {!pats.length ? (
          <EmptyState
            icon={FileText}
            title="Belum ada PAT"
            description="Register akun Qoder dulu — PAT akan muncul di sini setelah di-capture."
          />
        ) : (
          <div className="accounts-table-wrap">
            <table className="accounts-table" style={{ width: "100%", borderCollapse: "collapse", minWidth: 600 }}>
              <thead>
                <tr>
                  <th style={th}>#</th>
                  <th style={th}>PAT</th>
                  <th style={th}>Email</th>
                  <th style={th}>Status</th>
                  <th style={th}>Claimed</th>
                </tr>
              </thead>
              <tbody>
                {pats.map((p, i) => (
                  <tr key={i}>
                    <td style={td}>{i + 1}</td>
                    <td style={{ ...td, fontFamily: "var(--font-mono)", fontSize: 12 }}>{p.pat_preview}</td>
                    <td style={td}>{p.email || "-"}</td>
                    <td style={td}>
                      {p.pro_claimed ? <Badge tone="success">PRO TRIAL</Badge> : <Badge tone="muted">pending</Badge>}
                    </td>
                    <td style={td}>{p.claimed_count || 0}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <style>{`
        .qoder-wrap { display: flex; flex-direction: column; gap: 14px; flex: 1; max-width: 900px; width: 100%; margin: 0 auto; min-height: 0; }
      `}</style>
    </div>
  )
}

const th = {
  textAlign: "left", padding: "10px 16px", fontSize: 10.5, fontWeight: 700,
  letterSpacing: 0.8, textTransform: "uppercase", color: "var(--text-secondary)",
  borderBottom: "1px solid var(--border)", background: "var(--bg-card)", whiteSpace: "nowrap",
}
const td = { padding: "12px 16px", fontSize: 13, verticalAlign: "middle" }
