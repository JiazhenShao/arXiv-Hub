#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import secrets
import sys
import tomllib
import webbrowser
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock, Thread
from urllib.parse import parse_qs, urlparse


APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT))

from arxiv_daily.arxiv_api import ArxivClient  # noqa: E402
from arxiv_daily.config import ProfileConfig  # noqa: E402
from arxiv_daily.embedding import Specter2Embedder  # noqa: E402
from arxiv_daily.seed_library import (  # noqa: E402
    scan_seed_library,
    suggest_profile,
    verify_seed_candidates,
)
from arxiv_daily.setup import (  # noqa: E402
    CategoryPreference,
    ProfileEditorState,
    TopicPreference,
    apply_profile_editor_payload,
    load_profile_editor,
    render_profile_editor,
    save_profile_with_backup,
    write_profile_atomic,
)
from scripts.browser_bridge import atomic_write_destination  # noqa: E402


CATEGORY_CHOICES = (
    "astro-ph.CO", "astro-ph.EP", "astro-ph.GA", "astro-ph.HE",
    "astro-ph.IM", "astro-ph.SR", "cond-mat.dis-nn", "cond-mat.mes-hall",
    "cond-mat.mtrl-sci", "cond-mat.quant-gas", "cond-mat.soft",
    "cond-mat.stat-mech", "cond-mat.str-el", "cond-mat.supr-con",
    "cs.AI", "cs.CL", "cs.CR", "cs.CV", "cs.DC", "cs.HC", "cs.IR",
    "cs.LG", "cs.LO", "cs.NE", "cs.PL", "cs.RO", "cs.SE", "econ.EM",
    "econ.GN", "econ.TH", "eess.AS", "eess.IV", "eess.SP", "eess.SY",
    "gr-qc", "hep-ex", "hep-lat", "hep-ph", "hep-th", "math-ph",
    "math.AC", "math.AG", "math.AP", "math.AT", "math.CA", "math.CO",
    "math.CT", "math.CV", "math.DG", "math.DS", "math.FA", "math.GN",
    "math.GR", "math.GT", "math.LO", "math.MP", "math.NA", "math.NT",
    "math.OA", "math.OC", "math.PR", "math.QA", "math.RA", "math.RT",
    "math.SG", "math.SP", "math.ST", "nlin.AO", "nlin.CD", "nlin.CG",
    "nlin.PS", "nlin.SI", "nucl-ex", "nucl-th", "physics.acc-ph",
    "physics.ao-ph", "physics.app-ph", "physics.atm-clus",
    "physics.atom-ph", "physics.bio-ph", "physics.chem-ph",
    "physics.class-ph", "physics.comp-ph", "physics.data-an",
    "physics.flu-dyn", "physics.gen-ph", "physics.geo-ph",
    "physics.hist-ph", "physics.ins-det", "physics.med-ph",
    "physics.optics", "physics.plasm-ph", "physics.pop-ph",
    "physics.soc-ph", "physics.space-ph", "q-bio.BM", "q-bio.CB",
    "q-bio.GN", "q-bio.MN", "q-bio.NC", "q-bio.OT", "q-bio.PE",
    "q-bio.QM", "q-bio.SC", "q-bio.TO", "q-fin.CP", "q-fin.EC",
    "q-fin.GN", "q-fin.MF", "q-fin.PM", "q-fin.PR", "q-fin.RM",
    "q-fin.ST", "q-fin.TR", "quant-ph", "stat.AP", "stat.CO", "stat.ME",
    "stat.ML", "stat.OT", "stat.TH",
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
    parser = argparse.ArgumentParser(description="Configure arXiv Hub interests.")
    parser.add_argument("--profile", type=Path, default=default_profile_path())
    parser.add_argument(
        "--preset",
        type=Path,
        default=APP_ROOT / "config" / "nuclear-particle.toml",
    )
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--bridge-state", type=Path)
    parser.add_argument("--bridge-generation", type=int)
    parser.add_argument("--supervisor-handoff-url")
    return parser.parse_args()


def _state_payload(state: ProfileEditorState) -> dict[str, object]:
    return {
        "record_dir": str(state.record_dir),
        "active_library_dir": str(state.active_library_dir),
        "archive_library_dir": str(state.archive_library_dir),
        "seed_library_dir": (
            str(state.seed_library_dir) if state.seed_library_dir else ""
        ),
        "seed_library_limit": state.seed_library_limit,
        "timezone": state.timezone,
        "search_time": state.search_time,
        "categories": [
            {"name": item.name, "weight": item.weight}
            for item in state.categories
        ],
        "topics": [
            {
                "name": item.name,
                "weight": item.weight,
                "phrases": list(item.phrases),
            }
            for item in state.topics
        ],
    }


def editor_page(
    token: str,
    state: ProfileEditorState,
    *,
    supervisor_handoff_url: str | None = None,
    return_to_viewer_on_close: bool = True,
) -> str:
    config = json.dumps(
        {
            "token": token,
            "saveHandoffUrl": supervisor_handoff_url,
            "closeHandoffUrl": (
                supervisor_handoff_url
                if return_to_viewer_on_close
                else None
            ),
            "state": _state_payload(state),
            "categoryChoices": CATEGORY_CHOICES,
        },
        ensure_ascii=True,
    ).replace("<", "\\u003c")
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Configure arXiv Hub</title>
  <style>
    :root {{
      --ink:#17211d; --muted:#64726a; --paper:#fbf8ef; --panel:#fffdf7;
      --accent:#ae402c; --green:#246b50; --line:#d8d2c3; --gold:#b7892e;
    }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; color:var(--ink); background:
      radial-gradient(circle at 8% 0,#efd6a2 0,transparent 31rem),
      linear-gradient(135deg,#faf5e7,#e7efea);
      font-family:Georgia,"Times New Roman",serif; }}
    main {{ max-width:1080px; margin:auto; padding:48px 22px 100px; }}
    .eyebrow {{ color:var(--accent); font:700 .72rem/1.2 ui-monospace,monospace;
      letter-spacing:.17em; text-transform:uppercase; }}
    h1 {{ font-size:clamp(3.2rem,9vw,7rem); line-height:.82; margin:18px 0 25px; }}
    h2 {{ font-size:clamp(1.7rem,4vw,2.6rem); margin:0 0 8px; }}
    h3 {{ margin:0; }}
    .lede {{ max-width:720px; color:var(--muted); font-size:1.1rem; }}
    .section {{ margin-top:28px; padding:25px; border:1px solid var(--line);
      border-radius:22px; background:rgba(255,255,255,.78);
      box-shadow:0 18px 55px rgba(40,55,47,.07); }}
    .section-head {{ display:flex; justify-content:space-between; gap:18px;
      align-items:flex-start; margin-bottom:20px; }}
    .grid {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; }}
    .wide {{ grid-column:1/-1; }}
    label {{ display:block; font-weight:700; }}
    label > span {{ display:block; margin-bottom:7px; }}
    input,button {{ font:inherit; }}
    input[type=text],input[type=email],input[type=time],input[type=number] {{
      width:100%; padding:11px 12px; border:1px solid var(--line);
      border-radius:10px; background:white; color:var(--ink);
    }}
    button {{ border:1px solid var(--green); background:#eff9f3;
      color:var(--green); padding:10px 15px; border-radius:999px;
      cursor:pointer; font-weight:700; }}
    button.secondary {{ border-color:var(--line); background:white; color:var(--ink); }}
    button.danger {{ border-color:#a53c2d; color:#923122; background:#fff5f2; }}
    button:disabled {{ opacity:.5; cursor:not-allowed; }}
    .topic-card,.category-row {{ border:1px solid var(--line); border-radius:15px;
      background:var(--panel); padding:16px; margin:11px 0; }}
    .topic-top,.category-row {{ display:grid; grid-template-columns:1fr 180px auto;
      gap:12px; align-items:end; }}
    .range-wrap {{ display:grid; grid-template-columns:1fr 48px; gap:8px; align-items:center; }}
    input[type=range] {{ width:100%; accent-color:var(--green); }}
    .chips {{ display:flex; flex-wrap:wrap; gap:7px; margin:13px 0 9px; }}
    .chip {{ display:flex; align-items:center; gap:5px; padding:5px 8px;
      border:1px solid var(--line); border-radius:999px; background:white; }}
    .chip input {{ border:0; outline:0; width:150px; background:transparent; }}
    .chip button {{ border:0; padding:0 3px; background:transparent; color:var(--accent); }}
    .scan-row {{ display:grid; grid-template-columns:1fr auto; gap:10px; }}
    .scan-summary {{ margin-top:15px; padding:14px; border-radius:12px;
      background:#f2f6f2; color:var(--muted); }}
    .count-grid {{ display:flex; flex-wrap:wrap; gap:8px; margin-top:10px; }}
    .count {{ padding:6px 9px; border:1px solid var(--line); border-radius:8px;
      background:white; font:700 .76rem/1 ui-monospace,monospace; }}
    .suggestion {{ display:none; margin-top:15px; border-top:1px solid var(--line);
      padding-top:15px; }}
    .actions {{ position:sticky; bottom:12px; display:flex; gap:12px;
      align-items:center; margin-top:28px; padding:14px; border:1px solid var(--line);
      border-radius:999px; background:rgba(251,248,239,.94);
      backdrop-filter:blur(10px); }}
    #save-profile {{ flex:1; padding:13px; }}
    #status {{ color:var(--muted); min-width:180px; }}
    details {{ margin-top:22px; }}
    summary {{ cursor:pointer; color:var(--accent); font-weight:700; }}
    .help {{ color:var(--muted); margin:0; }}
    @media(max-width:720px) {{
      .grid,.topic-top,.category-row,.scan-row {{ grid-template-columns:1fr; }}
      .wide {{ grid-column:auto; }} .section {{ padding:18px; }}
      .section-head {{ flex-direction:column; }}
      .actions {{ border-radius:18px; flex-wrap:wrap; }}
    }}
  </style>
</head>
<body>
<main>
  <div class="eyebrow">Private local configuration</div>
  <h1>Shape your<br>arXiv</h1>
  <p class="lede">Your current profile is loaded. Edit topics directly, or scan
  a read-only folder of papers you already value. Nothing changes until you
  click <strong>Save Changes</strong>.</p>

  <section class="section">
    <div class="section-head"><div><h2>Edit my interests</h2>
      <p class="help">Importance controls explicit ranking priority. Keywords
      provide transparent lexical evidence alongside SPECTER2 similarity.</p></div>
      <button id="add-topic" class="secondary">+ Add topic</button></div>
    <div id="topics"></div>
  </section>

  <section class="section">
    <div class="section-head"><div><h2>Choose arXiv categories</h2>
      <p class="help">Search the bundled taxonomy or type any valid category ID.</p></div>
      <button id="add-category" class="secondary">+ Add category</button></div>
    <datalist id="category-options"></datalist>
    <div id="categories"></div>
  </section>

  <section class="section">
    <h2>Learn from a PDF folder</h2>
    <p class="help">The folder is scanned recursively and read-only. Hub never
    moves, renames, copies, or deletes these PDFs.</p>
    <div class="scan-row" style="margin-top:16px">
      <input id="seed-folder" type="text" placeholder="/Users/you/Papers">
      <button id="scan-library">Scan PDF Library</button>
    </div>
    <div id="scan-summary" class="scan-summary">No folder scanned in this session.</div>
    <div id="suggestion" class="suggestion">
      <h3>Suggested profile additions</h3>
      <p class="help">Review these after adding them; suggestions never save automatically.</p>
      <button id="apply-suggestion" style="margin-top:12px">Add suggestions to editor</button>
    </div>
  </section>

  <details class="section">
    <summary>Folders, schedule, and expert settings</summary>
    <div class="grid" style="margin-top:18px">
      <label class="wide"><span>Reports folder</span><input id="record-dir" type="text"></label>
      <label class="wide"><span>Managed High-paper download folder</span><input id="active-dir" type="text"></label>
      <label class="wide"><span>Older paper archive</span><input id="archive-dir" type="text"></label>
      <label><span>Timezone</span><input id="timezone" type="text"></label>
      <label><span>Search available after</span><input id="search-time" type="time"></label>
      <label><span>Maximum seed papers</span><input id="seed-limit" type="number" min="25" max="500"></label>
    </div>
  </details>

  <div class="actions">
    <button id="save-profile">Save Changes</button>
    <span id="status" aria-live="polite"></span>
    <button id="close-configure" class="danger">Close</button>
  </div>
</main>
<script>
  const config = {config};
  const state = structuredClone(config.state);
  let latestSuggestion = null;
  const topics = document.getElementById("topics");
  const categories = document.getElementById("categories");

  function slider(value) {{
    return `<div class="range-wrap"><input class="weight" type="range" min="0" max="100" value="${{Math.round(value * 100)}}"><output>${{Math.round(value * 100)}}</output></div>`;
  }}
  function addKeyword(container, value="") {{
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.innerHTML = `<input value="${{escapeAttribute(value)}}" placeholder="keyword"><button type="button" aria-label="Remove keyword">×</button>`;
    chip.querySelector("button").onclick = () => chip.remove();
    container.appendChild(chip);
  }}
  function addTopic(item={{name:"New topic",weight:.5,phrases:["keyword"]}}) {{
    const card = document.createElement("div");
    card.className = "topic-card";
    card.innerHTML = `<div class="topic-top"><label><span>Topic</span><input class="topic-name" type="text" value="${{escapeAttribute(item.name)}}"></label><label><span>Importance</span>${{slider(item.weight)}}</label><button type="button" class="danger remove">Remove</button></div><div class="chips"></div><button type="button" class="secondary add-keyword">+ Keyword</button>`;
    card.querySelector(".remove").onclick = () => card.remove();
    const range = card.querySelector(".weight");
    range.oninput = () => range.nextElementSibling.value = range.value;
    const chipBox = card.querySelector(".chips");
    item.phrases.forEach(value => addKeyword(chipBox, value));
    card.querySelector(".add-keyword").onclick = () => addKeyword(chipBox);
    topics.appendChild(card);
  }}
  function addCategory(item={{name:"",weight:.5}}) {{
    const row = document.createElement("div");
    row.className = "category-row";
    row.innerHTML = `<label><span>Category ID</span><input class="category-name" type="text" list="category-options" value="${{escapeAttribute(item.name)}}"></label><label><span>Importance</span>${{slider(item.weight)}}</label><button type="button" class="danger remove">Remove</button>`;
    row.querySelector(".remove").onclick = () => row.remove();
    const range = row.querySelector(".weight");
    range.oninput = () => range.nextElementSibling.value = range.value;
    categories.appendChild(row);
  }}
  function escapeAttribute(value) {{
    return String(value).replaceAll("&","&amp;").replaceAll('"',"&quot;").replaceAll("<","&lt;");
  }}
  function collect() {{
    return {{
      record_dir: document.getElementById("record-dir").value,
      active_library_dir: document.getElementById("active-dir").value,
      archive_library_dir: document.getElementById("archive-dir").value,
      seed_library_dir: document.getElementById("seed-folder").value,
      seed_library_limit: Number(document.getElementById("seed-limit").value),
      timezone: document.getElementById("timezone").value,
      search_time: document.getElementById("search-time").value,
      categories: [...categories.querySelectorAll(".category-row")].map(row => ({{
        name: row.querySelector(".category-name").value,
        weight: Number(row.querySelector(".weight").value) / 100
      }})),
      topics: [...topics.querySelectorAll(".topic-card")].map(card => ({{
        name: card.querySelector(".topic-name").value,
        weight: Number(card.querySelector(".weight").value) / 100,
        phrases: [...card.querySelectorAll(".chip input")].map(input => input.value).filter(Boolean)
      }}))
    }};
  }}
  async function request(path, payload=null) {{
    const response = await fetch(`${{path}}?token=${{encodeURIComponent(config.token)}}`, {{
      method: payload === null ? "GET" : "POST",
      headers: payload === null ? {{}} : {{"Content-Type":"application/json"}},
      body: payload === null ? null : JSON.stringify(payload)
    }});
    const result = await response.json();
    if (!response.ok) throw new Error(result.message || "Request failed");
    return result;
  }}
  function renderCounts(counts) {{
    return Object.entries(counts).filter(([,value]) => value).map(
      ([name,value]) => `<span class="count">${{name}}: ${{value}}</span>`
    ).join("");
  }}
  async function pollScan() {{
    try {{
      const result = await request("/api/seed/status");
      if (result.state === "running") {{
        document.getElementById("scan-summary").textContent = result.message;
        setTimeout(pollScan, 700);
        return;
      }}
      document.getElementById("scan-library").disabled = false;
      if (result.state === "ready") {{
        latestSuggestion = result.suggestion;
        document.getElementById("scan-summary").innerHTML =
          `<strong>${{result.message}}</strong><div class="count-grid">${{renderCounts(result.counts)}}</div>`;
        document.getElementById("suggestion").style.display = "block";
      }} else {{
        document.getElementById("scan-summary").textContent = result.message;
      }}
    }} catch (error) {{
      document.getElementById("scan-library").disabled = false;
      document.getElementById("scan-summary").textContent = error.message;
    }}
  }}
  document.getElementById("scan-library").onclick = async () => {{
    const button = document.getElementById("scan-library");
    button.disabled = true;
    document.getElementById("scan-summary").textContent = "Starting read-only scan...";
    try {{
      await request("/api/seed/scan", {{
        folder: document.getElementById("seed-folder").value,
        limit: Number(document.getElementById("seed-limit").value)
      }});
      pollScan();
    }} catch (error) {{
      button.disabled = false;
      document.getElementById("scan-summary").textContent = error.message;
    }}
  }};
  document.getElementById("apply-suggestion").onclick = () => {{
    if (!latestSuggestion) return;
    const existingCategories = new Map(
      [...categories.querySelectorAll(".category-row")].map(row => [
        row.querySelector(".category-name").value, row
      ])
    );
    latestSuggestion.categories.forEach(item => {{
      const row = existingCategories.get(item.name);
      if (row) {{
        const range = row.querySelector(".weight");
        range.value = Math.max(Number(range.value), Math.round(item.weight * 100));
        range.nextElementSibling.value = range.value;
      }} else addCategory(item);
    }});
    const existingTopics = new Set(
      [...topics.querySelectorAll(".topic-name")].map(input => input.value.toLowerCase())
    );
    latestSuggestion.topics.forEach(item => {{
      if (!existingTopics.has(item.name.toLowerCase())) addTopic(item);
    }});
    document.getElementById("status").textContent = "Suggestions added for review. Not saved yet.";
  }};
  document.getElementById("save-profile").onclick = async () => {{
    const button = document.getElementById("save-profile");
    button.disabled = true;
    document.getElementById("status").textContent = "Saving...";
    try {{
      const result = await request("/api/setup", collect());
      document.getElementById("status").textContent = result.message;
      if (config.saveHandoffUrl) {{
        window.location.replace(config.saveHandoffUrl);
      }}
    }} catch (error) {{
      document.getElementById("status").textContent = error.message;
    }} finally {{ button.disabled = false; }}
  }};
  document.getElementById("close-configure").onclick = async () => {{
    try {{
      await request("/api/shutdown", {{}});
      document.getElementById("status").textContent = "Configuration server closed.";
      if (config.closeHandoffUrl) {{
        window.location.replace(config.closeHandoffUrl);
      }}
    }} catch (error) {{ document.getElementById("status").textContent = error.message; }}
  }};
  document.getElementById("add-topic").onclick = () => addTopic();
  document.getElementById("add-category").onclick = () => addCategory();
  config.categoryChoices.forEach(value => {{
    const option = document.createElement("option"); option.value = value;
    document.getElementById("category-options").appendChild(option);
  }});
  state.topics.forEach(addTopic); state.categories.forEach(addCategory);
  document.getElementById("record-dir").value = state.record_dir;
  document.getElementById("active-dir").value = state.active_library_dir;
  document.getElementById("archive-dir").value = state.archive_library_dir;
  document.getElementById("seed-folder").value = state.seed_library_dir;
  document.getElementById("seed-limit").value = state.seed_library_limit;
  document.getElementById("timezone").value = state.timezone;
  document.getElementById("search-time").value = state.search_time;
</script>
</body>
</html>"""


def make_seed_scan_runner(
    profile_path: Path,
    initial_state: ProfileEditorState | None = None,
) -> Callable[[Path, int], dict[str, object]]:
    def run(folder: Path, limit: int) -> dict[str, object]:
        if profile_path.is_file():
            config = ProfileConfig.load(profile_path)
            categories = config.categories
            source = {
                "user_agent": config.user_agent,
                "min_interval_seconds": config.api_min_interval_seconds,
                "timeout_seconds": config.api_timeout_seconds,
                "retry_backoffs": config.api_retry_backoffs,
                "retry_deadline_seconds": config.api_retry_deadline_seconds,
            }
            model = {
                "base_model": config.base_model,
                "base_revision": config.base_revision,
                "adapter_model": config.adapter_model,
                "adapter_revision": config.adapter_revision,
                "batch_size": config.batch_size,
            }
            record_dir = config.record_dir
        elif initial_state is not None:
            categories = {
                item.name: item.weight for item in initial_state.categories
            }
            source = initial_state.source
            model = initial_state.model
            record_dir = initial_state.record_dir
        else:
            raise ValueError("No profile is available for PDF verification")
        scan = scan_seed_library(
            folder,
            cache_path=record_dir / ".state" / "seed-library-index.json",
            limit=limit,
        )
        client = ArxivClient(
            categories,
            user_agent=str(source["user_agent"]),
            min_interval_seconds=float(source["min_interval_seconds"]),
            timeout_seconds=float(source["timeout_seconds"]),
            state_dir=record_dir / ".state",
            retry_backoffs=tuple(source["retry_backoffs"]),
            retry_deadline_seconds=float(source["retry_deadline_seconds"]),
        )
        verified = verify_seed_candidates(scan, fetcher=client.fetch_by_ids)
        embedder = Specter2Embedder(
            base_model=str(model["base_model"]),
            base_revision=str(model["base_revision"]),
            adapter_model=str(model["adapter_model"]),
            adapter_revision=str(model["adapter_revision"]),
            batch_size=int(model["batch_size"]),
        )
        embeddings = embedder.embed(list(verified.papers))
        suggestion = suggest_profile(verified.papers, embeddings)
        return {
            "counts": verified.counts,
            "papers": [
                {"arxiv_id": paper.arxiv_id, "title": paper.title}
                for paper in verified.papers
            ],
            "suggestion": {
                "categories": [
                    {"name": item.name, "weight": item.weight}
                    for item in suggestion.categories
                ],
                "topics": [
                    {
                        "name": item.name,
                        "weight": item.weight,
                        "phrases": list(item.phrases),
                    }
                    for item in suggestion.topics
                ],
            },
        }

    return run


def create_setup_server(
    *,
    profile_path: Path,
    token: str,
    scan_runner: Callable[[Path, int], dict[str, object]] | None = None,
    initial_state: ProfileEditorState | None = None,
    supervisor_handoff_url: str | None = None,
    return_to_viewer_on_close: bool = True,
) -> ThreadingHTTPServer:
    state_lock = Lock()
    scan_state: dict[str, object] = {
        "state": "idle",
        "message": "No folder scanned in this session.",
        "counts": {},
        "papers": [],
        "suggestion": {"categories": [], "topics": []},
    }
    scan_runner = scan_runner or make_seed_scan_runner(
        profile_path,
        initial_state,
    )

    def current_state() -> ProfileEditorState:
        if profile_path.is_file():
            return load_profile_editor(profile_path)
        if initial_state is not None:
            return initial_state
        raise ValueError("No initial profile state is available")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            print(f"[configure] {format % args}")

        def _token_valid(self) -> bool:
            query = parse_qs(urlparse(self.path).query)
            return secrets.compare_digest(query.get("token", [""])[0], token)

        def _mutation_allowed(self) -> bool:
            if not self._token_valid():
                return False
            origin = self.headers.get("Origin", "")
            expected = f"http://{self.headers.get('Host', '')}"
            return secrets.compare_digest(origin, expected)

        def _json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_payload(self) -> dict[str, object]:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 1_000_000:
                raise ValueError("Invalid request size")
            value = json.loads(self.rfile.read(length))
            if not isinstance(value, dict):
                raise ValueError("Request body must be an object")
            return value

        def do_GET(self) -> None:
            route = urlparse(self.path).path
            if not self._token_valid():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if route == "/":
                try:
                    state = current_state()
                    body = editor_page(
                        token,
                        state,
                        supervisor_handoff_url=supervisor_handoff_url,
                        return_to_viewer_on_close=return_to_viewer_on_close,
                    ).encode("utf-8")
                except (OSError, TypeError, ValueError) as exc:
                    self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
                    return
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if route == "/api/seed/status":
                with state_lock:
                    self._json(HTTPStatus.OK, dict(scan_state))
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            route = urlparse(self.path).path
            if not self._token_valid():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if not self._mutation_allowed():
                self._json(
                    HTTPStatus.FORBIDDEN,
                    {"ok": False, "message": "Same-origin request required."},
                )
                return
            try:
                payload = self._read_payload()
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                self._json(
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "message": str(exc)},
                )
                return
            if route == "/api/seed/scan":
                with state_lock:
                    if scan_state["state"] == "running":
                        self._json(
                            HTTPStatus.CONFLICT,
                            {"ok": False, "message": "A scan is already running."},
                        )
                        return
                    try:
                        folder = Path(str(payload["folder"])).expanduser()
                        limit = int(payload.get("limit", 100))
                        if folder.is_symlink() or not folder.is_dir():
                            raise ValueError(
                                "Choose an existing, non-symlinked PDF folder."
                            )
                        if not 25 <= limit <= 500:
                            raise ValueError(
                                "Seed library limit must be between 25 and 500."
                            )
                    except (KeyError, TypeError, ValueError) as exc:
                        self._json(
                            HTTPStatus.BAD_REQUEST,
                            {"ok": False, "message": str(exc)},
                        )
                        return
                    scan_state.update(
                        state="running",
                        message=f"Scanning {folder} read-only...",
                        counts={},
                        papers=[],
                        suggestion={"categories": [], "topics": []},
                    )

                def worker() -> None:
                    try:
                        result = scan_runner(folder, limit)
                        result.update(
                            state="ready",
                            message=(
                                f"Verified {result['counts'].get('verified', 0)} "
                                "arXiv papers. Review suggestions before saving."
                            ),
                        )
                    except Exception as exc:
                        print(f"[configure] seed scan failed: {exc}")
                        result = {
                            "state": "failed",
                            "message": f"PDF scan failed: {str(exc)[:200]}",
                            "counts": {},
                            "papers": [],
                            "suggestion": {"categories": [], "topics": []},
                        }
                    with state_lock:
                        scan_state.clear()
                        scan_state.update(result)

                Thread(target=worker, daemon=True).start()
                self._json(
                    HTTPStatus.ACCEPTED,
                    {"ok": True, "message": "Read-only scan started."},
                )
                return
            if route == "/api/setup":
                with state_lock:
                    if scan_state["state"] == "running":
                        self._json(
                            HTTPStatus.CONFLICT,
                            {
                                "ok": False,
                                "message": "Wait for the PDF scan to finish.",
                            },
                        )
                        return
                try:
                    current = current_state()
                    updated = apply_profile_editor_payload(current, payload)
                    if updated.seed_library_dir is not None and (
                        updated.seed_library_dir.is_symlink()
                        or not updated.seed_library_dir.is_dir()
                    ):
                        raise ValueError(
                            "The seed PDF folder must be an existing regular directory."
                        )
                    text = render_profile_editor(updated)
                    for directory in (
                        updated.record_dir,
                        updated.active_library_dir,
                        updated.archive_library_dir,
                    ):
                        directory.mkdir(parents=True, exist_ok=True)
                    if profile_path.is_file():
                        backup = save_profile_with_backup(profile_path, text)
                        message = f"Saved. Backup: {backup.name}"
                    else:
                        write_profile_atomic(profile_path, text)
                        message = "Saved. Opening arXiv Hub..."
                except (OSError, TypeError, ValueError) as exc:
                    self._json(
                        HTTPStatus.BAD_REQUEST,
                        {"ok": False, "message": str(exc)},
                    )
                    return
                self._json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "message": message,
                    },
                )
                Thread(target=self.server.shutdown, daemon=True).start()
                return
            if route == "/api/shutdown":
                with state_lock:
                    if scan_state["state"] == "running":
                        self._json(
                            HTTPStatus.CONFLICT,
                            {"ok": False, "message": "Wait for the scan to finish."},
                        )
                        return
                self._json(
                    HTTPStatus.OK,
                    {"ok": True, "message": "Configuration server closed."},
                )
                Thread(target=self.server.shutdown, daemon=True).start()
                return
            self.send_error(HTTPStatus.NOT_FOUND)

    return ThreadingHTTPServer(("127.0.0.1", 0), Handler)


def default_editor_state(
    preset_path: Path,
    *,
    home: Path | None = None,
) -> ProfileEditorState:
    preset = tomllib.loads(preset_path.read_text(encoding="utf-8"))
    hub = (home or Path.home()) / "Documents" / "arXiv Hub"
    return ProfileEditorState(
        record_dir=hub / "Reports",
        active_library_dir=hub / "Papers",
        archive_library_dir=hub / "Papers Archive",
        seed_library_dir=None,
        seed_library_limit=100,
        timezone="America/Chicago",
        search_time="20:00",
        categories=tuple(
            CategoryPreference(str(name), float(weight))
            for name, weight in preset["categories"].items()
        ),
        topics=tuple(
            TopicPreference(
                str(item["name"]),
                float(item["weight"]),
                tuple(str(value) for value in item["phrases"]),
            )
            for item in preset["topics"]
        ),
        ranking=dict(preset["ranking"]),
        model=dict(preset["model"]),
        source={
            "user_agent": "arxiv-hub/1.0 (local research digest)",
            "min_interval_seconds": 3.0,
            "retry_backoffs": [10.0, 20.0, 40.0],
            "retry_deadline_seconds": 90.0,
            "timeout_seconds": 60.0,
        },
    )
def main() -> int:
    args = parse_args()
    if (args.bridge_state is None) != (args.bridge_generation is None):
        print(
            "CONFIGURE FAILED: bridge state and generation must be supplied together",
            file=sys.stderr,
        )
        return 2
    profile_path = args.profile.expanduser()
    profile_existed = profile_path.is_file()
    try:
        initial_state = (
            load_profile_editor(profile_path)
            if profile_path.is_file()
            else default_editor_state(args.preset)
        )
    except (OSError, TypeError, ValueError) as exc:
        print(f"CONFIGURE FAILED: {exc}", file=sys.stderr)
        return 2
    token = secrets.token_urlsafe(32)
    server = create_setup_server(
        profile_path=profile_path,
        token=token,
        initial_state=initial_state,
        supervisor_handoff_url=args.supervisor_handoff_url,
        return_to_viewer_on_close=profile_existed,
    )
    url = f"http://127.0.0.1:{server.server_port}/?token={token}"
    if args.bridge_state is not None:
        try:
            atomic_write_destination(
                args.bridge_state,
                generation=args.bridge_generation,
                url=url,
            )
        except (OSError, ValueError) as exc:
            server.server_close()
            print(
                f"CONFIGURE FAILED: could not publish browser destination: {exc}",
                file=sys.stderr,
            )
            return 2
    print(f"arXiv Hub configuration: {url}")
    if not args.no_open and not webbrowser.open(url):
        print(f"Open this URL in a browser: {url}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nConfiguration cancelled.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
