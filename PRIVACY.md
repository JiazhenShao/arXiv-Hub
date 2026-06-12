# Privacy

arXiv Hub is local-first and has no telemetry, analytics, account system, or
cloud database.

It sends:

- Category searches and PDF requests to official `arxiv.org` endpoints.
- Model and Python package downloads to their documented upstream hosts during
  installation.
- The contact email entered during setup as part of the arXiv API user-agent,
  so operators can identify responsible traffic.

It stores reports, ratings, embeddings, deduplication state, and download
records on the Mac in the configured folders. The browser viewer binds only to
`127.0.0.1` and protects mutation endpoints with a random session token.

The project does not upload the reading library, reports, ratings, or interest
profile to the project author.
