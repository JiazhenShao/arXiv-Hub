#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import re
import secrets
import sys
import tomllib
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import parse_qs, urlparse


APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT))

from arxiv_daily.setup import (  # noqa: E402
    SetupValues,
    build_profile_from_preset_text,
    write_profile_atomic,
)


def default_profile_path() -> Path:
    return (
        Path.home()
        / "Library"
        / "Application Support"
        / "arXiv Hub"
        / "profile.toml"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Configure arXiv Hub.")
    parser.add_argument("--profile", type=Path, default=default_profile_path())
    parser.add_argument(
        "--preset",
        type=Path,
        default=APP_ROOT / "config" / "nuclear-particle.toml",
    )
    parser.add_argument("--no-open", action="store_true")
    return parser.parse_args()


def _read_existing_profile(profile_path: Path) -> dict[str, str] | None:
    try:
        with profile_path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    paths = data.get("paths", {})
    viewer = data.get("viewer", {})
    source = data.get("source", {})
    user_agent = str(source.get("user_agent", ""))
    email_match = re.search(r"\(([^)]+)\)", user_agent)
    source_types = {str(s.get("type", "")) for s in data.get("extra_sources", [])}
    biorxiv_subjects = ", ".join(
        str(subj)
        for s in data.get("extra_sources", [])
        if s.get("type") == "biorxiv"
        for subj in s.get("subjects", [])
    )
    chemrxiv_subjects = ", ".join(
        str(subj)
        for s in data.get("extra_sources", [])
        if s.get("type") == "chemrxiv"
        for subj in s.get("subjects", [])
    )
    return {
        "record_dir": str(paths.get("record_dir", "")),
        "active_library_dir": str(paths.get("active_library_dir", "")),
        "archive_library_dir": str(paths.get("archive_library_dir", "")),
        "timezone": str(viewer.get("timezone", "America/Chicago")),
        "search_time": str(viewer.get("search_time", "20:00")),
        "contact_email": email_match.group(1) if email_match else "",
        "has_biorxiv": "true" if "biorxiv" in source_types else "",
        "has_chemrxiv": "true" if "chemrxiv" in source_types else "",
        "biorxiv_subjects": biorxiv_subjects,
        "chemrxiv_subjects": chemrxiv_subjects,
    }


def _extract_preset_text(profile_path: Path) -> str | None:
    try:
        text = profile_path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r"^\[(?:categories|ranking|model)\]", text, re.MULTILINE)
    if not match:
        return None
    preset = text[match.start():]
    # Strip [[extra_sources]] blocks — those are managed by the checkboxes
    preset = re.sub(r"\n*\[\[extra_sources\]\][\s\S]*$", "", preset)
    return preset.strip() + "\n"


def setup_page(token: str, preset_text: str, existing: dict[str, str] | None = None) -> str:
    hub = Path.home() / "Documents" / "arXiv Hub"
    defaults = {
        "reports": str(hub / "Reports"),
        "papers": str(hub / "Papers"),
        "archive": str(hub / "Papers Archive"),
        "timezone": "America/Chicago",
        "search_time": "20:00",
        "contact_email": "",
    }
    if existing:
        for key in ("record_dir", "active_library_dir", "archive_library_dir",
                    "timezone", "search_time", "contact_email"):
            form_key = {
                "record_dir": "reports",
                "active_library_dir": "papers",
                "archive_library_dir": "archive",
            }.get(key, key)
            if existing.get(key):
                defaults[form_key] = existing[key]
    values = {**defaults, "preset": preset_text}
    escaped = {key: html.escape(value, quote=True) for key, value in values.items()}
    biorxiv_checked = "checked" if existing and existing.get("has_biorxiv") else ""
    chemrxiv_checked = "checked" if existing and existing.get("has_chemrxiv") else ""
    biorxiv_subjects_val = html.escape(existing.get("biorxiv_subjects", "") if existing else "", quote=True)
    chemrxiv_subjects_val = html.escape(existing.get("chemrxiv_subjects", "") if existing else "", quote=True)
    config = json.dumps({"token": token}, ensure_ascii=True).replace("<", "\\u003c")
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Set up arXiv Hub</title>
  <style>
    :root {{ --ink:#17211d; --muted:#617069; --paper:#fbf8ef;
      --accent:#a83d29; --green:#246b50; --line:#d7d1c2; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; color:var(--ink); background:
      radial-gradient(circle at 10% 0,#efd8a8 0,transparent 28rem),
      linear-gradient(135deg,#faf5e8,#e8efea);
      font-family:Georgia,"Times New Roman",serif; }}
    main {{ max-width:860px; margin:auto; padding:52px 22px 90px; }}
    .eyebrow {{ color:var(--accent); font:700 .72rem/1.2 ui-monospace,monospace;
      letter-spacing:.17em; text-transform:uppercase; }}
    h1 {{ font-size:clamp(3rem,9vw,6.8rem); line-height:.82; margin:18px 0 24px; }}
    .lede {{ max-width:650px; color:var(--muted); font-size:1.12rem; }}
    form {{ margin-top:34px; padding:26px; border:1px solid var(--line);
      border-radius:22px; background:rgba(255,255,255,.78);
      box-shadow:0 20px 60px rgba(40,55,47,.08); }}
    .grid {{ display:grid; grid-template-columns:1fr 1fr; gap:18px; }}
    label {{ display:block; font-weight:700; }}
    label span {{ display:block; margin-bottom:7px; }}
    input,textarea {{ width:100%; border:1px solid var(--line); border-radius:10px;
      background:#fff; color:var(--ink); padding:11px 12px; font:inherit; }}
    textarea {{ min-height:350px; font:12px/1.45 ui-monospace,SFMono-Regular,monospace; }}
    .wide {{ grid-column:1/-1; }}
    details {{ margin-top:22px; }}
    summary {{ cursor:pointer; color:var(--accent); font-weight:700; }}
    .sources-section {{ margin-top:22px; border-top:1px solid var(--line); padding-top:18px; }}
    .sources-section h3 {{ margin:0 0 14px; font-size:1rem; }}
    .source-row {{ display:grid; grid-template-columns:auto 1fr; gap:10px 14px;
      align-items:center; margin-bottom:12px; }}
    .source-row input[type=checkbox] {{ width:auto; margin:0; accent-color:var(--green); }}
    .source-row label {{ font-weight:700; margin:0; }}
    .source-row .source-subjects {{ width:100%; border:1px solid var(--line);
      border-radius:10px; background:#fff; color:var(--ink); padding:9px 12px;
      font:inherit; font-size:.88rem; }}
    button {{ margin-top:24px; border:1px solid var(--green); border-radius:999px;
      padding:13px 24px; background:#eff9f3; color:var(--green);
      font:700 1rem Georgia,serif; cursor:pointer; }}
    #status {{ display:inline-block; margin-left:14px; color:var(--muted); }}
    @media(max-width:650px) {{ .grid {{ grid-template-columns:1fr; }}
      .wide {{ grid-column:auto; }} form {{ padding:18px; }} }}
  </style>
</head>
<body>
<main>
  <div class="eyebrow">Private local setup</div>
  <h1>arXiv<br>Hub</h1>
  <p class="lede">Choose where reports and papers live. The included physics
  profile is only a starting point; its categories, topics, and weights remain
  editable.</p>
  <form id="setup">
    <div class="grid">
      <label class="wide"><span>Reports folder</span>
        <input name="record_dir" value="{escaped["reports"]}" required></label>
      <label class="wide"><span>Downloaded papers folder</span>
        <input name="active_library_dir" value="{escaped["papers"]}" required></label>
      <label class="wide"><span>Older paper archive</span>
        <input name="archive_library_dir" value="{escaped["archive"]}" required></label>
      <label><span>Timezone</span>
        <input id="timezone" name="timezone" value="{escaped["timezone"]}" required></label>
      <label><span>Search available after</span>
        <input name="search_time" type="time" value="{escaped["search_time"]}" required></label>
      <label class="wide"><span>Contact email for arXiv requests</span>
        <input name="contact_email" type="email" placeholder="you@example.org" value="{escaped["contact_email"]}" required></label>
    </div>
    <div class="sources-section">
      <h3>Additional preprint sources</h3>
      <p style="color:var(--muted);font-size:.9rem;margin:0 0 14px">
        bioRxiv and chemRxiv papers will be ranked alongside arXiv using the
        same embedding model. Leave subjects blank to fetch all subjects.
      </p>
      <div class="source-row">
        <input type="checkbox" id="enable_biorxiv" name="enable_biorxiv" {biorxiv_checked}>
        <label for="enable_biorxiv">bioRxiv</label>
        <span></span>
        <input class="source-subjects" name="biorxiv_subjects"
               placeholder="e.g. biophysics, biochemistry, molecular-biology"
               aria-label="bioRxiv subjects (comma-separated)"
               value="{biorxiv_subjects_val}">
      </div>
      <div class="source-row">
        <input type="checkbox" id="enable_chemrxiv" name="enable_chemrxiv" {chemrxiv_checked}>
        <label for="enable_chemrxiv">chemRxiv</label>
        <span></span>
        <input class="source-subjects" name="chemrxiv_subjects"
               placeholder="e.g. physical-chemistry, biochemistry"
               aria-label="chemRxiv subjects (comma-separated)"
               value="{chemrxiv_subjects_val}">
      </div>
    </div>
    <details>
      <summary>Advanced: categories, topics, weights, and model</summary>
      <p>Edit TOML carefully. You can change these values later in
      <code>profile.toml</code>.</p>
      <textarea name="preset_text">{escaped["preset"]}</textarea>
    </details>
    <button type="submit">Save configuration</button>
    <span id="status" aria-live="polite"></span>
  </form>
</main>
<script>
  const config = {config};
  const tzField = document.getElementById("timezone");
  if (!tzField.value) {{
    const detected = Intl.DateTimeFormat().resolvedOptions().timeZone;
    if (detected) tzField.value = detected;
  }}
  document.getElementById("setup").addEventListener("submit", async (event) => {{
    event.preventDefault();
    const status = document.getElementById("status");
    const button = event.target.querySelector("button");
    button.disabled = true;
    status.textContent = "Saving...";
    const payload = Object.fromEntries(new FormData(event.target).entries());
    try {{
      const response = await fetch(`/api/setup?token=${{encodeURIComponent(config.token)}}`, {{
        method:"POST", headers:{{"Content-Type":"application/json"}},
        body:JSON.stringify(payload)
      }});
      const result = await response.json();
      if (!response.ok) throw new Error(result.message || "Setup failed");
      status.textContent = result.message;
      event.target.querySelectorAll("input,textarea,button").forEach(
        element => element.disabled = true);
    }} catch (error) {{
      status.textContent = error.message;
      button.disabled = false;
    }}
  }});
</script>
</body>
</html>"""


def create_setup_server(
    *,
    profile_path: Path,
    preset_text: str,
    token: str,
    existing: dict[str, str] | None = None,
) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            print(f"[setup] {format % args}")

        def _authorized(self) -> bool:
            query = parse_qs(urlparse(self.path).query)
            return secrets.compare_digest(query.get("token", [""])[0], token)

        def _json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if urlparse(self.path).path != "/" or not self._authorized():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            body = setup_page(token, preset_text, existing).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            if urlparse(self.path).path != "/api/setup" or not self._authorized():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 1_000_000:
                    raise ValueError("Invalid setup request size")
                payload = json.loads(self.rfile.read(length))
                values = SetupValues(
                    record_dir=Path(str(payload["record_dir"])),
                    active_library_dir=Path(str(payload["active_library_dir"])),
                    archive_library_dir=Path(str(payload["archive_library_dir"])),
                    timezone=str(payload["timezone"]),
                    search_time=str(payload["search_time"]),
                    contact_email=str(payload["contact_email"]),
                )
                preset_text = str(payload["preset_text"])
                for source_type in ("biorxiv", "chemrxiv"):
                    if payload.get(f"enable_{source_type}"):
                        subjects_raw = str(payload.get(f"{source_type}_subjects", ""))
                        subjects = [s.strip() for s in subjects_raw.split(",") if s.strip()]
                        subjects_toml = (
                            "[" + ", ".join(f'"{s}"' for s in subjects) + "]"
                            if subjects else "[]"
                        )
                        preset_text = (
                            preset_text.rstrip()
                            + f'\n\n[[extra_sources]]\ntype = "{source_type}"\n'
                            f"subjects = {subjects_toml}\n"
                        )
                profile_text = build_profile_from_preset_text(values, preset_text)
                for directory in (
                    values.record_dir,
                    values.active_library_dir,
                    values.archive_library_dir,
                ):
                    directory.expanduser().mkdir(parents=True, exist_ok=True)
                write_profile_atomic(profile_path, profile_text)
            except (KeyError, OSError, TypeError, ValueError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "message": str(exc)})
                return
            self._json(
                HTTPStatus.OK,
                {"ok": True, "message": "Saved. You may close this tab."},
            )
            Thread(target=self.server.shutdown, daemon=True).start()

    return ThreadingHTTPServer(("127.0.0.1", 0), Handler)


def main() -> int:
    args = parse_args()
    profile_path = args.profile.expanduser()
    existing = _read_existing_profile(profile_path)
    preset_text = _extract_preset_text(profile_path)
    if preset_text is None:
        try:
            preset_text = args.preset.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"SETUP FAILED: {exc}", file=sys.stderr)
            return 2
    token = secrets.token_urlsafe(32)
    server = create_setup_server(
        profile_path=profile_path,
        preset_text=preset_text,
        existing=existing,
        token=token,
    )
    url = f"http://127.0.0.1:{server.server_port}/?token={token}"
    print(f"arXiv Hub setup: {url}", flush=True)
    if not args.no_open and not webbrowser.open(url):
        print(f"Open this URL in a browser: {url}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nSetup cancelled.")
        return 130
    finally:
        server.server_close()
    return 0 if args.profile.is_file() else 2


if __name__ == "__main__":
    raise SystemExit(main())
