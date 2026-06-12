# Contributing

Issues and pull requests are welcome. Please keep changes fail-closed around
metadata, model, state, and download errors.

Before submitting:

```sh
python -W error::ResourceWarning -m unittest discover -s tests -v
```

Do not commit personal profiles, reports, PDFs, embeddings, caches, email
addresses, or absolute home-directory paths.
