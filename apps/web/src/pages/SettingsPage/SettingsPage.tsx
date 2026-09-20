import { useEffect, useState } from "react";
import { marketProvider } from "@/providers/HttpWsProvider";

export function SettingsPage() {
  const [provider, setProvider] = useState<string>("…");
  const [mode, setMode] = useState<string>("…");
  const [status, setStatus] = useState<string>("…");
  const [err, setErr] = useState<string>("");

  useEffect(() => {
    marketProvider
      .getHealth()
      .then((h) => {
        setProvider(h.provider);
        setMode(h.mode);
        setStatus(h.status);
      })
      .catch((e: Error) => setErr(e.message));
  }, []);

  return (
    <div className="shell-page">
      <h1>设置 / Settings</h1>
      <p className="muted">纸面默认；P0 仅展示当前 DATA_PROVIDER，不可切到实盘。</p>
      <dl className="settings-dl">
        <dt>DATA_PROVIDER</dt>
        <dd>
          <code>{provider}</code>
        </dd>
        <dt>MODE</dt>
        <dd>
          <code>{mode}</code>
        </dd>
        <dt>API health</dt>
        <dd>
          <code>{status}</code>
        </dd>
        <dt>VITE_API_BASE</dt>
        <dd>
          <code>{import.meta.env.VITE_API_BASE || "(same-origin / vite proxy)"}</code>
        </dd>
      </dl>
      {err ? <p className="error">健康检查失败：{err}</p> : null}
    </div>
  );
}
