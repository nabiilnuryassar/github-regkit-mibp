import React, { useEffect, useRef, useState } from "react";
import { Coins, Download, KeyRound, Play, RefreshCw, Square, Upload } from "lucide-react";
import { api } from "../api.js";
import { Badge, Button, Card, Input, Spinner, EmptyState } from "./ui.jsx";

const fmtSize = (n) => (n > 1024 ? `${(n / 1024).toFixed(1)} KB` : `${n} B`)
const fmtExp = (epoch) => (epoch ? new Date(epoch * 1000).toLocaleDateString() : "-")
const isExpired = (epoch) => !!epoch && epoch * 1000 < Date.now()

export default function CodeBuddyPanel({ onGotoStatus }) {
  const [files, setFiles] = useState([])
  const [results, setResults] = useState(null)
  const [status, setStatus] = useState(null)
  const [tokens, setTokens] = useState([])
  const [quotas, setQuotas] = useState({})
  const [exportKind, setExportKind] = useState("access")
  const [accounts, setAccounts] = useState("")
  const [limit, setLimit] = useState("0")
  const [concurrency, setConcurrency] = useState("1")
  const [delayMs, setDelayMs] = useState("15000")
  const [activate, setActivate] = useState(false)
  const [prove, setProve] = useState(false)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState("")
  const timer = useRef(null)

  function notify(text, bad = false) {
    setMsg(text)
    setTimeout(() => setMsg(""), 2600)
  }

  function refresh(silent = true) {
    return Promise.all([
      api.get("/api/codebuddy/files").then((d) => setFiles(d.files || [])),
      api.get("/api/codebuddy/results").then((d) => setResults(d)),
      api.get("/api/codebuddy/status").then((d) => setStatus(d)),
      api.get("/api/codebuddy/tokens").then((d) => setTokens(d.rows || [])),
    ]).catch((e) => {
      if (!silent) notify(e.message || "gagal memuat", true)
    })
  }

  useEffect(() => {
    refresh(false)
    timer.current = setInterval(() => refresh(true), 4000)
    return () => clearInterval(timer.current)
  }, [])

  const running = !!(status && status.running)

  async function start() {
    if (running) return
    setBusy(true)
    try {
      await api.post("/api/codebuddy/start", {
        accounts: accounts || "",
        limit: Number(limit) || 0,
        concurrency: Number(concurrency) || 1,
        delay_ms: Number(delayMs) || 0,
        activate,
        prove,
      })
      notify("Job CodeBuddy dimulai — pantau di tab Status/Live Log")
      onGotoStatus && onGotoStatus()
    } catch (e) {
      notify(e.message || "gagal memulai", true)
    } finally {
      setBusy(false)
    }
  }

  async function stop() {
    try {
      await api.post("/api/codebuddy/stop", {})
      notify("Stop diminta — Node akan dihentikan di baris berikutnya")
    } catch (e) {
      notify(e.message || "gagal stop", true)
    }
  }

  function download(content, filename) {
    const url = URL.createObjectURL(new Blob([content], { type: "text/plain" }))
    const a = document.createElement("a")
    a.href = url
    a.download = filename
    a.click()
    URL.revokeObjectURL(url)
  }

  async function exportTokens(kind, emails = []) {
    try {
      const q = `kind=${kind}${emails.length ? `&emails=${encodeURIComponent(emails.join(","))}` : ""}`
      const d = await api.get(`/api/codebuddy/export?${q}`)
      if (!d.count) return notify(`Tidak ada ${kind} token untuk diexport`, true)
      download(d.content, d.filename)
      notify(`${d.count} ${kind} token → ${d.filename}`)
    } catch (e) {
      notify(e.message || "export gagal", true)
    }
  }

  async function checkQuota(email) {
    setQuotas((q) => ({ ...q, [email]: { pending: true } }))
    try {
      const d = await api.get(`/api/codebuddy/quota?email=${encodeURIComponent(email)}`)
      setQuotas((q) => ({ ...q, [email]: d }))
    } catch (e) {
      setQuotas((q) => ({ ...q, [email]: { ok: false, detail: e.message || "gagal" } }))
    }
  }

  async function checkAllQuota() {
    for (const t of tokens) await checkQuota(t.email)
  }

  const quotaCell = (email) => {
    const q = quotas[email]
    if (!q) return <span style={{ color: "var(--muted)" }}>-</span>
    if (q.pending) return <Spinner />
    if (!q.ok) return <span style={{ color: "var(--danger-text, var(--accent-text))", fontSize: 12 }}>{q.detail}</span>
    const pct = q.total_size ? Math.round((q.total_remain / q.total_size) * 100) : 0
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
        <span style={{ fontWeight: 700 }}>
          {q.total_remain} / {q.total_size} credit
          {q.payment_type ? <span style={{ color: "var(--muted)", fontWeight: 500 }}> · {q.payment_type}</span> : null}
        </span>
        <span style={{ fontSize: 11.5, color: "var(--muted)" }}>
          terpakai {q.total_used} · sisa {pct}%
          {q.packages?.[0]?.cycle_end ? ` · s/d ${q.packages[0].cycle_end.slice(0, 10)}` : ""}
        </span>
      </div>
    )
  }

  return (
    <div className="cb-wrap">
      <Card className="cb-head" style={{ padding: 20 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: 12 }}>
          <div>
            <div style={{ fontSize: 19, fontWeight: 800, display: "flex", alignItems: "center", gap: 10 }}>
              <KeyRound size={18} /> CodeBuddy Login
            </div>
            <div style={{ fontSize: 12.5, color: "var(--muted)", marginTop: 3 }}>
              Modul terpisah — panen accessToken/refreshToken CodeBuddy lewat mesin Node di <code>codebuddy_login/</code>
            </div>
          </div>
          <Badge tone={running ? "accent" : "muted"}>
            {running ? "Running" : "Idle"}
          </Badge>
        </div>
      </Card>

      <Card style={{ padding: 20 }}>
        <div className="cb-grid">
          <label className="cb-field">
            <span>Accounts file</span>
            <select
              className="ui-input"
              value={accounts}
              onChange={(e) => setAccounts(e.target.value)}
              disabled={running}
            >
              <option value="">Terbaru (github_accounts_*.txt)</option>
              {files.map((f) => (
                <option key={f.name} value={`accounts/${f.name}`}>
                  {f.name} ({fmtSize(f.size)})
                </option>
              ))}
            </select>
          </label>
          <label className="cb-field cb-small">
            <span>Limit akun (0 = semua)</span>
            <Input type="number" value={limit} onChange={(e) => setLimit(e.target.value)} disabled={running} />
          </label>
          <label className="cb-field cb-small">
            <span>Concurrency (1-10)</span>
            <Input type="number" value={concurrency} onChange={(e) => setConcurrency(e.target.value)} disabled={running} />
          </label>
          <label className="cb-field cb-small">
            <span>Delay (ms)</span>
            <Input type="number" value={delayMs} onChange={(e) => setDelayMs(e.target.value)} disabled={running} />
          </label>
          <label className="cb-check">
            <Input type="checkbox" checked={activate} onChange={(e) => setActivate(e.target.checked)} disabled={running} />
            <span>--activate (region + trial)</span>
          </label>
          <label className="cb-check">
            <Input type="checkbox" checked={prove} onChange={(e) => setProve(e.target.checked)} disabled={running} />
            <span>--prove (satu turn ACP)</span>
          </label>
        </div>
        <div style={{ display: "flex", gap: 8, marginTop: 14, flexWrap: "wrap" }}>
          <Button variant="primary" onClick={start} disabled={busy || running}>
            <Play size={15} /> Start
          </Button>
          <Button variant="destructive" onClick={stop} disabled={!running}>
            <Square size={15} /> Stop
          </Button>
          <Button onClick={() => refresh(false)} disabled={running}>
            <RefreshCw size={15} /> Refresh
          </Button>
        </div>
        {status && (
          <div style={{ marginTop: 10, fontSize: 12.5, color: "var(--muted)" }}>
            total: {status.total} | synced: {status.synced} | last exit: {status.last_exit ?? "-"} | source: {status.source || "(terbaru)"}
          </div>
        )}
        {msg && <div style={{ marginTop: 8, fontSize: 13, color: "var(--accent-text)" }}>{msg}</div>}
      </Card>

      <Card style={{ flex: 1, padding: 0, overflow: "hidden", minHeight: 180 }}>
        {results === null ? (
          <div className="panel-state"><Spinner /> <span>Memuat hasil</span></div>
        ) : !results.rows.length ? (
          <EmptyState
            icon={Upload}
            title="Belum ada hasil"
            description="Hasil panen muncul di sini sebagai accounts/codebuddy/<email>/ setelah job selesai."
          />
        ) : (
          <div className="accounts-table-wrap">
            <table className="accounts-table" style={{ width: "100%", borderCollapse: "collapse", minWidth: 640 }}>
              <thead>
                <tr>
                  <th style={th}>#</th>
                  <th style={th}>Email</th>
                  <th style={th}>Token CLI</th>
                  <th style={th}>Files</th>
                </tr>
              </thead>
              <tbody>
                {results.rows.map((r, i) => (
                  <tr key={r.email}>
                    <td style={td}>{i + 1}</td>
                    <td style={{ ...td, fontWeight: 600 }}>{r.email}</td>
                    <td style={td}>
                      {r.has_tokens ? (
                        <Badge tone="success">ada</Badge>
                      ) : (
                        <Badge tone="danger">belum</Badge>
                      )}
                    </td>
                    <td style={{ ...td, color: "var(--muted)" }}>{r.files.join(", ")}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Card style={{ padding: 0, overflow: "hidden" }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between",
                      gap: 12, padding: "14px 16px", borderBottom: "1px solid var(--border)", flexWrap: "wrap" }}>
          <div style={{ fontSize: 14, fontWeight: 700, display: "flex", alignItems: "center", gap: 8 }}>
            <Coins size={16} /> Token &amp; Kuota ({tokens.length})
          </div>
          <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            <select className="ui-input" style={{ width: "auto" }} value={exportKind}
                    onChange={(e) => setExportKind(e.target.value)} aria-label="Jenis token">
              <option value="access">Access token</option>
              <option value="refresh">Refresh token</option>
            </select>
            <Button onClick={() => exportTokens(exportKind)} disabled={!tokens.length}>
              <Download size={15} /> Export .txt
            </Button>
            <Button onClick={checkAllQuota} disabled={!tokens.length}>
              <RefreshCw size={15} /> Cek semua kuota
            </Button>
          </div>
        </div>
        {!tokens.length ? (
          <EmptyState
            icon={KeyRound}
            title="Belum ada token tersimpan"
            description="Jalankan job CodeBuddy dulu — token tersimpan di accounts/codebuddy/<email>_cli_tokens.json."
          />
        ) : (
          <div className="accounts-table-wrap">
            <table className="accounts-table" style={{ width: "100%", borderCollapse: "collapse", minWidth: 860 }}>
              <thead>
                <tr>
                  <th style={th}>#</th>
                  <th style={th}>Email</th>
                  <th style={th}>Access</th>
                  <th style={th}>Refresh</th>
                  <th style={th}>Disimpan</th>
                  <th style={th}>Expired</th>
                  <th style={th}>Kuota</th>
                  <th style={th}>Aksi</th>
                </tr>
              </thead>
              <tbody>
                {tokens.map((t, i) => (
                  <tr key={t.email}>
                    <td style={td}>{i + 1}</td>
                    <td style={{ ...td, fontWeight: 600 }}>{t.email}</td>
                    <td style={td}>
                      {t.has_access
                        ? <Badge tone={isExpired(t.access_exp) ? "danger" : "success"}>ada</Badge>
                        : <Badge tone="danger">tidak</Badge>}
                    </td>
                    <td style={td}>
                      {t.has_refresh ? <Badge tone="success">ada</Badge> : <Badge tone="danger">tidak</Badge>}
                    </td>
                    <td style={{ ...td, color: "var(--muted)", whiteSpace: "nowrap" }}>{t.saved_at || "-"}</td>
                    <td style={{ ...td, color: "var(--muted)", whiteSpace: "nowrap" }}>{fmtExp(t.access_exp)}</td>
                    <td style={{ ...td, minWidth: 200 }}>{quotaCell(t.email)}</td>
                    <td style={{ ...td, whiteSpace: "nowrap" }}>
                      <div style={{ display: "flex", gap: 6 }}>
                        <Button size="sm" onClick={() => exportTokens("access", [t.email])}
                                disabled={!t.has_access}>Access</Button>
                        <Button size="sm" onClick={() => exportTokens("refresh", [t.email])}
                                disabled={!t.has_refresh}>Refresh</Button>
                        <Button size="sm" onClick={() => checkQuota(t.email)}
                                disabled={!t.has_access}>Kuota</Button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <style>{`
        .cb-wrap { display: flex; flex-direction: column; gap: 14px; flex: 1; max-width: 900px; width: 100%; margin: 0 auto; min-height: 0; }
        .cb-grid { display: grid; gap: 12px; grid-template-columns: 1fr; }
        .cb-field { display: flex; flex-direction: column; gap: 6px; font-size: 12.5px; color: var(--muted); }
        .cb-field span { font-weight: 600; }
        .cb-check { display: flex; align-items: center; gap: 8px; font-size: 13px; color: var(--text); min-height: 32px; }
        .cb-check input[type=checkbox] { width: 18px; height: 18px; accent-color: var(--accent); }
        @media (min-width: 640px) {
          .cb-grid { grid-template-columns: 2fr 1fr 1fr; }
          .cb-small { grid-column: span 1; }
        }
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
