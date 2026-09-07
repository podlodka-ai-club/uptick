"""Offline FastEmbed adapter for the neutral embedding port."""

from __future__ import annotations

import math
from collections.abc import Sequence
from pathlib import Path

from uptick_agent.memory.contracts import MemoryValidationError


class FastEmbedPort:
    """Adapt FastEmbed's query/passage generators without permitting downloads."""

    def __init__(
        self,
        *,
        model_name: str,
        specific_model_path: str | Path,
        local_files_only: bool = True,
    ) -> None:
        if not isinstance(model_name, str) or not model_name.strip():
            raise MemoryValidationError("FastEmbed model_name must be non-empty")
        if not isinstance(specific_model_path, (str, Path)) or not str(specific_model_path).strip():
            raise MemoryValidationError("FastEmbed specific_model_path must be non-empty")
        if not isinstance(local_files_only, bool):
            raise MemoryValidationError("FastEmbed local_files_only must be boolean")
        if not local_files_only:
            raise MemoryValidationError("FastEmbed retrieval requires local_files_only=True")
        model_path = Path(specific_model_path)
        if not model_path.exists():
            raise MemoryValidationError("FastEmbed specific_model_path does not exist")
        try:
            from fastembed import TextEmbedding
        except ImportError as error:
            raise MemoryValidationError(
                "FastEmbed integration requires the optional fastembed dependency"
            ) from error
        try:
            self._model = TextEmbedding(
                model_name=model_name,
                specific_model_path=str(model_path),
                local_files_only=True,
            )
        except Exception as error:
            raise MemoryValidationError("FastEmbed model could not be loaded locally") from error
        self.model_name = model_name
        self.specific_model_path = str(model_path)

    @property
    def dimension(self) -> int | None:
        return None

    @staticmethod
    def _builtin_vector(value: object) -> list[float]:
        if isinstance(value, (str, bytes, bytearray)):
            raise MemoryValidationError("FastEmbed returned a non-numeric embedding")
        try:
            values = list(value)  # type: ignore[arg-type]
        except TypeError as error:
            raise MemoryValidationError("FastEmbed returned a non-numeric embedding") from error
        result: list[float] = []
        for component in values:
            if isinstance(component, (bool, str, bytes, bytearray)):
                raise MemoryValidationError("FastEmbed returned a non-numeric embedding")
            try:
                converted = float(component)
            except (TypeError, ValueError, OverflowError) as error:
                raise MemoryValidationError("FastEmbed returned a non-numeric embedding") from error
            if not math.isfinite(converted):
                raise MemoryValidationError("FastEmbed returned a non-finite embedding")
            result.append(converted)
        if not result:
            raise MemoryValidationError("FastEmbed returned an empty embedding")
        return result

    def embed_query(self, text: str) -> Sequence[float]:
        values = list(self._model.query_embed([text]))
        if len(values) != 1:
            raise MemoryValidationError("FastEmbed returned an invalid query embedding count")
        return self._builtin_vector(values[0])

    def embed_passages(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        values = list(self._model.passage_embed(list(texts)))
        if len(values) != len(texts):
            raise MemoryValidationError("FastEmbed returned an invalid passage embedding count")
        return [self._builtin_vector(value) for value in values]


FastEmbedEmbeddingPort = FastEmbedPort


__all__ = ["FastEmbedEmbeddingPort", "FastEmbedPort"]
