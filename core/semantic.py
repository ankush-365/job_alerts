"""Semantic similarity between the candidate profile and job text, using a small local
static-embedding model (model2vec; numpy only, no GPU, no API calls).

If the model can't be loaded (no network on first use, package missing), `similarities`
returns None and scoring falls back to the rule-based components alone.
"""

from __future__ import annotations

import logging

import numpy as np

logger = logging.getLogger(__name__)

MAX_CHARS = 6000  # long descriptions add boilerplate (benefits, legal), not signal


class SemanticMatcher:
    def __init__(self, model_name: str, profile_text: str):
        self.model_name = model_name
        self.profile_text = profile_text
        self._model = None
        self._profile_vec: np.ndarray | None = None
        self._failed = False

    def _load(self) -> bool:
        if self._model is not None:
            return True
        if self._failed or not self.profile_text.strip():
            return False
        try:
            from model2vec import StaticModel

            self._model = StaticModel.from_pretrained(self.model_name)
            self._profile_vec = self._normalize(self._model.encode([self.profile_text]))[0]
            return True
        except Exception as exc:  # noqa: BLE001 - any failure means "no semantic signal"
            logger.warning("Semantic model %s unavailable, using rule-based scoring only: %s", self.model_name, exc)
            self._failed = True
            return False

    @staticmethod
    def _normalize(vectors: np.ndarray) -> np.ndarray:
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        return vectors / np.where(norms == 0, 1, norms)

    def similarities(self, texts: list[str]) -> list[float] | None:
        """Cosine similarity of each text to the profile, or None if the model is unavailable."""
        if not texts:
            return []
        if not self._load():
            return None
        vectors = self._normalize(self._model.encode([t[:MAX_CHARS] for t in texts]))
        return [float(v) for v in vectors @ self._profile_vec]
