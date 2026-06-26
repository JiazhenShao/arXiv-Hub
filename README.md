# arXiv Hub

arXiv Hub is a private, local paper recommender and reading dashboard for
Apple Silicon Macs. It fetches real metadata from the official arXiv API,
ranks papers with a pinned SPECTER2 embedding model, writes auditable daily
Markdown/HTML reports, remembers ratings, and downloads selected High papers.

The included preset emphasizes nuclear astrophysics, neutron stars, nuclear
theory, particle phenomenology, field theory/RG, high-energy astrophysics,
experiments, and group theory. Every category, topic, phrase, and weight is
editable during setup or later in `profile.toml`.

## Requirements

- Apple Silicon Mac (`arm64`)
- macOS 14 or newer
- Internet access for installation and arXiv searches
- Approximately 5 GB free disk space for Python, PyTorch, and SPECTER2

Homebrew and a system Python installation are not required.

## Install

1. Download this repository as a ZIP and extract it.
2. Right-click `Install arXiv Hub.command`, choose **Open**, then confirm once.
3. Complete the setup page that opens in your default browser.
4. Let the installer download and verify its private Python 3.12 environment
   and the pinned SPECTER2 model.
5. Open Spotlight and type `arXiv Hub`.

macOS may require the right-click **Open** step because this first release is
not code-signed or notarized. The installer is readable source code and only
writes to the locations documented below.

## Daily Use

Open `arXiv Hub` from Spotlight. The local dashboard lets you:

- Start a search after your configured local time.
- Browse dated reports with newest dates first.
- Rate papers High, Medium, Low, Skip, or Unrated.
- Download High papers from the specific dated report you are viewing.
- Close the local server from the browser.

Searches use the local SPECTER2 model and do not consume LLM API tokens.
Ratings update the Markdown record and influence later recommendations.

## Files

Application files and the private runtime:

```text
~/Library/Application Support/arXiv Hub/
```

Spotlight launchers:

```text
~/Applications/arXiv Hub.app
~/Applications/arXiv Hub.command
```

Default user data:

```text
~/Documents/arXiv Hub/Reports
~/Documents/arXiv Hub/Papers
~/Documents/arXiv Hub/Papers Archive
```

Use the **Configure** button in the dashboard to reopen the setup wizard.
Existing reports and papers are not moved automatically when paths change.

## Update And Uninstall

To update, download a newer release and run `Install arXiv Hub.command` again.
The installer stages and verifies the replacement before switching versions.

To uninstall, run `Uninstall arXiv Hub.command` from the downloaded repository.
It removes the application and private runtime but preserves reports and
papers. A profile backup is placed in `~/Documents/arXiv Hub/`.

## Safety

- arXiv metadata is fetched from the official Atom API.
- Titles and abstracts are copied from verified metadata, not invented.
- API requests are shared-rate-limited and retried conservatively.
- Reports and state are written atomically.
- The viewer listens only on `127.0.0.1` and uses a random session token.
- PDF downloads validate canonical arXiv redirects, content type, signature,
  and size before atomic installation.
- No telemetry is collected.

See [PRIVACY.md](PRIVACY.md), [SECURITY.md](SECURITY.md), and
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Development

Run the tests with Python 3.12 in an environment containing
`requirements.lock`:

```sh
python -W error::ResourceWarning -m unittest discover -s tests -v
```

## Credits

arXiv Hub was built with the help of AI coding agents:

- [Claude Code](https://claude.com/claude-code) — Anthropic's agentic CLI.
- [Codex](https://openai.com/codex/) — OpenAI's coding agent.

This project is licensed under the [MIT License](LICENSE).
