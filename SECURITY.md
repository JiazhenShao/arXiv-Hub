# Security

## Reporting

Please report security issues privately through GitHub's security advisory
feature rather than a public issue.

## Local Server

The viewer binds to loopback only, uses a random token, checks same-origin
headers for mutations, and prevents search, download, and shutdown jobs from
overlapping.

## Downloads

PDF downloads accept only canonical arXiv HTTPS destinations, enforce a size
limit, validate content type and the PDF signature, and use atomic replacement.

## Installer

The installer supports Apple Silicon macOS only. It downloads a pinned `uv`
installer over TLS, installs a private managed Python 3.12 runtime, installs
pinned Python dependencies, and verifies the pinned SPECTER2 revisions before
activating an update. This unsigned first release should be reviewed before
running.
