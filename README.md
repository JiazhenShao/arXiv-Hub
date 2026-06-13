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

Codex, Homebrew, and a system Python installation are not required.

## Install

1. Download this repository as a ZIP and extract it.
2. Right-click `Install arXiv Hub.command`, choose **Open**, then confirm once.
3. Let the installer create its private Python 3.12 environment.
4. Open Spotlight and type `arXiv Hub`.
5. On first launch, complete the guided interest setup page. Saving opens the
   normal dashboard automatically in the same browser tab.

macOS may require the right-click **Open** step because this first release is
not code-signed or notarized. The installer is readable source code and only
writes to the locations documented below.

## Daily Use

Open `arXiv Hub` from Spotlight. The local dashboard lets you:

- Start a search after your configured local time.
- Reopen the guided interest editor from **Configure interests**.
- Browse dated reports with newest dates first.
- Rate papers High, Medium, Low, Skip, or Unrated.
- Download High papers from the specific dated report you are viewing.
- Close the local server from the browser.

The launcher opens one browser tab per session. First-run setup and later
**Configure interests** transitions reuse that tab instead of opening another.

Searches use the local SPECTER2 model and do not consume LLM API tokens.
Ratings update the Markdown record and influence later recommendations.

## Files

Application files and the private runtime:

```text
~/Library/Application Support/arXiv Hub/
```

Spotlight launchers:

```text
~/Applications/arXiv Hub.command
```

Default user data:

```text
~/Documents/arXiv Hub/Reports
~/Documents/arXiv Hub/Papers
~/Documents/arXiv Hub/Papers Archive
```

Use **Configure interests** on the dashboard to reopen the setup wizard.
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

This project is licensed under the [MIT License](LICENSE).
