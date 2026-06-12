from __future__ import annotations

import json
from pathlib import Path

from .models import Paper


class EmbeddingError(RuntimeError):
    """The pinned scientific embedding model could not run."""


class EmbeddingCache:
    def __init__(self, path: Path, model_label: str) -> None:
        self.path = path
        self.model_label = model_label
        self.values: dict[str, list[float]] = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("model_label") == model_label:
                    self.values = {
                        str(key): [float(value) for value in vector]
                        for key, vector in data.get("values", {}).items()
                    }
            except (OSError, ValueError, TypeError):
                self.values = {}

    @staticmethod
    def key(paper: Paper) -> str:
        return f"{paper.arxiv_id}|{paper.updated.isoformat()}"

    def get(self, paper: Paper) -> list[float] | None:
        return self.values.get(self.key(paper))

    def put(self, paper: Paper, vector: list[float]) -> None:
        self.values[self.key(paper)] = vector

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "model_label": self.model_label,
            "values": self.values,
        }
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        temporary.replace(self.path)


class Specter2Embedder:
    def __init__(
        self,
        *,
        base_model: str,
        base_revision: str,
        adapter_model: str,
        adapter_revision: str,
        batch_size: int,
    ) -> None:
        self.base_model = base_model
        self.base_revision = base_revision
        self.adapter_model = adapter_model
        self.adapter_revision = adapter_revision
        self.batch_size = batch_size
        self.label = (
            f"{base_model}@{base_revision} + "
            f"{adapter_model}@{adapter_revision}"
        )
        self._tokenizer = None
        self._model = None

    def _load(self) -> None:
        if self._model is not None:
            return
        try:
            import torch
            from adapters import AutoAdapterModel
            from transformers import AutoTokenizer

            tokenizer = AutoTokenizer.from_pretrained(
                self.base_model,
                revision=self.base_revision,
            )
            model = AutoAdapterModel.from_pretrained(
                self.base_model,
                revision=self.base_revision,
            )
            adapter_name = model.load_adapter(
                self.adapter_model,
                source="hf",
                revision=self.adapter_revision,
                load_as="proximity",
                set_active=False,
            )
            model.set_active_adapters(adapter_name)
            if adapter_name not in str(model.active_adapters):
                raise EmbeddingError(
                    f"SPECTER2 adapter {adapter_name!r} was not activated"
                )
            model.eval()
            if torch.backends.mps.is_available():
                model.to("mps")
            self._tokenizer = tokenizer
            self._model = model
        except Exception as exc:
            raise EmbeddingError(f"Unable to load pinned SPECTER2 model: {exc}") from exc

    def embed(self, papers: list[Paper]) -> dict[str, list[float]]:
        if not papers:
            return {}
        self._load()
        try:
            import torch

            assert self._tokenizer is not None
            assert self._model is not None
            device = next(self._model.parameters()).device
            vectors: dict[str, list[float]] = {}
            for offset in range(0, len(papers), self.batch_size):
                batch = papers[offset : offset + self.batch_size]
                texts = [
                    f"{paper.title}{self._tokenizer.sep_token}{paper.abstract}"
                    for paper in batch
                ]
                inputs = self._tokenizer(
                    texts,
                    padding=True,
                    truncation=True,
                    return_tensors="pt",
                    return_token_type_ids=False,
                    max_length=512,
                )
                inputs = {key: value.to(device) for key, value in inputs.items()}
                with torch.no_grad():
                    output = self._model(**inputs)
                embeddings = output.last_hidden_state[:, 0, :]
                embeddings = torch.nn.functional.normalize(embeddings, p=2, dim=1)
                for paper, vector in zip(batch, embeddings.cpu().tolist(), strict=True):
                    vectors[paper.arxiv_id] = [float(value) for value in vector]
            return vectors
        except EmbeddingError:
            raise
        except Exception as exc:
            raise EmbeddingError(f"SPECTER2 inference failed: {exc}") from exc
