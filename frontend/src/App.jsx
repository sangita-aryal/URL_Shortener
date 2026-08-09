import { useState, useRef, useEffect } from 'react';

// When served behind Nginx the frontend and API share the same origin,
// so a relative path is correct.  Override with VITE_API_BASE during
// local development (e.g. http://localhost:8000).
const API_BASE = import.meta.env.VITE_API_BASE ?? '';

// ── Small helper components ──────────────────────────────────────────────────

function Spinner() {
  return (
    <svg
      className="w-4 h-4 animate-spin"
      fill="none"
      viewBox="0 0 24 24"
      aria-hidden="true"
    >
      <circle
        className="opacity-25"
        cx="12" cy="12" r="10"
        stroke="currentColor" strokeWidth="4"
      />
      <path
        className="opacity-75"
        fill="currentColor"
        d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"
      />
    </svg>
  );
}

function LinkIcon() {
  return (
    <svg
      className="w-5 h-5 text-slate-500"
      fill="none" viewBox="0 0 24 24" stroke="currentColor"
      aria-hidden="true"
    >
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.75}
        d="M13.828 10.172a4 4 0 00-5.656 0l-4 4a4 4 0 105.656 5.656l1.102-1.101
           m-.758-4.899a4 4 0 005.656 0l4-4a4 4 0 00-5.656-5.656l-1.1 1.1" />
    </svg>
  );
}

function AlertIcon() {
  return (
    <svg
      className="w-4 h-4 shrink-0 text-rose-400"
      fill="none" viewBox="0 0 24 24" stroke="currentColor"
      aria-hidden="true"
    >
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
        d="M12 9v2m0 4h.01M10.29 3.86L1.82
           18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71
           3.86a2 2 0 00-3.42 0z" />
    </svg>
  );
}

function CheckIcon() {
  return (
    <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M5 13l4 4L19 7" />
    </svg>
  );
}

// ── Main application ─────────────────────────────────────────────────────────

export default function App() {
  const [url, setUrl]         = useState('');
  const [result, setResult]   = useState(null);   // { short_code, short_url }
  const [loading, setLoading] = useState(false);
  const [error, setError]     = useState('');
  const [copied, setCopied]   = useState(false);

  const inputRef  = useRef(null);
  const resultRef = useRef(null);

  // Scroll the result panel into view when it appears.
  useEffect(() => {
    if (result && resultRef.current) {
      resultRef.current.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
  }, [result]);

  async function handleSubmit(e) {
    e.preventDefault();
    const trimmed = url.trim();
    if (!trimmed) return;

    setLoading(true);
    setError('');
    setResult(null);
    setCopied(false);

    try {
      const res = await fetch(`${API_BASE}/shorten`, {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ url: trimmed }),
      });

      const data = await res.json();

      if (!res.ok) {
        // FastAPI returns { "detail": "..." } for 4xx/5xx responses.
        // SSRF violations arrive as 400 with a descriptive detail string.
        setError(
          typeof data.detail === 'string'
            ? data.detail
            : 'An unexpected error occurred. Please try again.'
        );
        return;
      }

      setResult({ short_code: data.short_code, short_url: data.short_url });
    } catch {
      setError('Could not reach the server. Check your connection and try again.');
    } finally {
      setLoading(false);
    }
  }

  async function handleCopy() {
    if (!result) return;
    try {
      await navigator.clipboard.writeText(result.short_url);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard API unavailable (non-secure context); silently skip.
    }
  }

  function handleReset() {
    setUrl('');
    setResult(null);
    setError('');
    setCopied(false);
    inputRef.current?.focus();
  }

  const isIdle = !loading && !result && !error;

  return (
    <div className="min-h-screen bg-[#07070e] flex flex-col items-center justify-center px-4 py-16">

      {/* ── Wordmark ──────────────────────────────────────────────────────── */}
      <header className="mb-10 text-center select-none">
        <div className="inline-flex items-center gap-2 mb-4">
          <span className="w-7 h-7 rounded-lg bg-indigo-600 flex items-center justify-center">
            <svg className="w-4 h-4 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.25}
                d="M13.828 10.172a4 4 0 00-5.656 0l-4 4a4 4 0 105.656
                   5.656l1.102-1.101m-.758-4.899a4 4 0 005.656 0l4-4a4 4 0
                   00-5.656-5.656l-1.1 1.1" />
            </svg>
          </span>
          <span className="text-slate-400 text-xs font-medium tracking-widest uppercase">
            snip.io
          </span>
        </div>

        <h1 className="text-4xl sm:text-5xl font-bold tracking-tight text-white leading-tight">
          Make it{' '}
          <span
            className="text-transparent bg-clip-text"
            style={{ backgroundImage: 'linear-gradient(135deg, #818cf8 0%, #6366f1 50%, #4338ca 100%)' }}
          >
            shorter.
          </span>
        </h1>
        <p className="mt-3 text-slate-500 text-sm max-w-xs mx-auto leading-relaxed">
          Paste any URL. Get a compact 7-character link, instantly.
        </p>
      </header>

      {/* ── Card ──────────────────────────────────────────────────────────── */}
      <main className="w-full max-w-xl">
        <div
          className="rounded-2xl p-6 sm:p-8"
          style={{
            background: '#0f0f1a',
            border: '1px solid #1e1e3a',
            boxShadow: '0 0 0 1px rgba(99,102,241,0.04), 0 25px 60px rgba(0,0,0,0.6), 0 -1px 0 rgba(99,102,241,0.08) inset',
          }}
        >
          {/* ── Input form ─────────────────────────────────────────────── */}
          <form onSubmit={handleSubmit} noValidate>
            <label htmlFor="url-input" className="sr-only">
              Enter a long URL to shorten
            </label>

            {/* Unified input bar */}
            <div
              className="flex items-center rounded-xl overflow-hidden transition-all duration-150"
              style={{
                background: '#0a0a15',
                border: '1px solid #1e1e3a',
              }}
              onFocus={() => {}}
            >
              {/* Left icon */}
              <span className="pl-4 pr-1 flex-shrink-0">
                <LinkIcon />
              </span>

              <input
                id="url-input"
                ref={inputRef}
                type="url"
                value={url}
                onChange={e => { setUrl(e.target.value); setError(''); }}
                placeholder="https://example.com/long/path?query=value"
                required
                autoFocus
                disabled={loading}
                className="flex-1 bg-transparent text-slate-100 placeholder-slate-600
                           text-sm px-3 py-3.5 outline-none
                           disabled:opacity-50 disabled:cursor-not-allowed"
                style={{ fontFamily: 'inherit' }}
              />

              <button
                type="submit"
                disabled={loading || !url.trim()}
                className="m-1.5 flex-shrink-0 inline-flex items-center gap-2
                           text-white text-sm font-semibold px-5 py-2.5 rounded-lg
                           transition-all duration-150 focus-visible:outline
                           focus-visible:outline-2 focus-visible:outline-indigo-500
                           disabled:cursor-not-allowed"
                style={{
                  background: loading || !url.trim()
                    ? 'rgba(79,70,229,0.35)'
                    : 'linear-gradient(135deg, #6366f1, #4f46e5)',
                  boxShadow: loading || !url.trim() ? 'none' : '0 0 20px rgba(99,102,241,0.35)',
                }}
              >
                {loading ? <><Spinner /> Shortening…</> : 'Shorten →'}
              </button>
            </div>
          </form>

          {/* ── Live region for screen readers ────────────────────────── */}
          <div aria-live="polite" aria-atomic="true" className="sr-only">
            {error  && `Error: ${error}`}
            {result && `Shortened URL ready: ${result.short_url}`}
          </div>

          {/* ── Error panel ────────────────────────────────────────────── */}
          {error && (
            <div
              role="alert"
              className="mt-4 flex items-start gap-3 rounded-xl px-4 py-3.5 animate-in fade-in"
              style={{
                background: 'rgba(244,63,94,0.06)',
                border: '1px solid rgba(244,63,94,0.2)',
              }}
            >
              <AlertIcon />
              <div className="min-w-0">
                <p className="text-rose-300 text-xs font-semibold uppercase tracking-wider mb-1">
                  Request blocked
                </p>
                <p className="text-rose-400/80 text-sm break-words leading-relaxed">
                  {error}
                </p>
              </div>
            </div>
          )}

          {/* ── Success panel ──────────────────────────────────────────── */}
          {result && (
            <div
              ref={resultRef}
              role="status"
              className="mt-4 rounded-xl px-4 py-4"
              style={{
                background: 'rgba(16,185,129,0.05)',
                border: '1px solid rgba(16,185,129,0.18)',
                animation: 'slideIn 0.25s ease-out',
              }}
            >
              <p className="text-emerald-500 text-xs font-semibold uppercase tracking-wider mb-3">
                ✦ Link ready
              </p>

              {/* Short URL row */}
              <div className="flex items-center gap-2">
                <a
                  href={result.short_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="flex-1 min-w-0 font-mono text-sm text-indigo-300
                             hover:text-indigo-200 truncate underline underline-offset-2
                             decoration-indigo-500/40 hover:decoration-indigo-400
                             transition-colors"
                  title={`Open ${result.short_url} in a new tab`}
                >
                  {result.short_url}
                </a>

                {/* Copy button */}
                <button
                  onClick={handleCopy}
                  className="flex-shrink-0 inline-flex items-center gap-1.5
                             text-xs font-medium px-3 py-1.5 rounded-lg
                             transition-colors duration-150 focus-visible:outline
                             focus-visible:outline-2 focus-visible:outline-emerald-500"
                  style={{
                    background: copied ? 'rgba(16,185,129,0.2)' : 'rgba(16,185,129,0.1)',
                    color: copied ? '#34d399' : '#6ee7b7',
                    border: '1px solid rgba(16,185,129,0.2)',
                  }}
                  aria-label={copied ? 'Copied to clipboard' : 'Copy short URL to clipboard'}
                >
                  {copied ? <><CheckIcon /> Copied</> : 'Copy'}
                </button>

                {/* New / Reset button */}
                <button
                  onClick={handleReset}
                  className="flex-shrink-0 text-xs font-medium px-3 py-1.5
                             rounded-lg text-slate-400 hover:text-slate-200
                             transition-colors duration-150 focus-visible:outline
                             focus-visible:outline-2 focus-visible:outline-slate-400"
                  style={{
                    background: 'rgba(148,163,184,0.07)',
                    border: '1px solid rgba(148,163,184,0.12)',
                  }}
                  aria-label="Shorten another URL"
                >
                  New ↩
                </button>
              </div>

              {/* Short code chip */}
              <p className="mt-2.5 text-slate-600 text-xs">
                Code:{' '}
                <code
                  className="font-mono text-slate-500 text-xs px-1.5 py-0.5 rounded"
                  style={{ background: 'rgba(148,163,184,0.07)' }}
                >
                  {result.short_code}
                </code>
              </p>
            </div>
          )}

          {/* ── Idle hint ──────────────────────────────────────────────── */}
          {isIdle && (
            <p className="mt-4 text-center text-slate-700 text-xs">
              Supports <code className="text-slate-600">https://</code> and{' '}
              <code className="text-slate-600">http://</code> URLs only.
              Private IPs are blocked.
            </p>
          )}
        </div>

        {/* Footer tag line */}
        <p className="mt-5 text-center text-slate-700 text-xs tracking-wide">
          SSRF-shielded · Redis-cached · 3.52 trillion unique keys
        </p>
      </main>

      {/* Slide-in keyframe injected globally */}
      <style>{`
        @keyframes slideIn {
          from { opacity: 0; transform: translateY(6px); }
          to   { opacity: 1; transform: translateY(0);   }
        }
      `}</style>
    </div>
  );
}
