"""A read-only, picklable mapping for result types (review finding 11)."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any, TypeVar

__all__ = ["ReadOnlyMap", "freeze"]

K = TypeVar("K")
V = TypeVar("V")


class ReadOnlyMap(Mapping[K, V]):
    """Immutable view over a private copy of a mapping.

    Unlike ``types.MappingProxyType`` it can be pickled (results travel to and from
    worker processes). Equality is mapping equality; instances are unhashable.
    """

    __slots__ = ("_data",)

    def __init__(self, data: Mapping[K, V] | None = None) -> None:
        self._data: dict[K, V] = dict(data) if data is not None else {}

    def __getitem__(self, key: K) -> V:
        return self._data[key]

    def __iter__(self) -> Iterator[K]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __contains__(self, key: object) -> bool:
        return key in self._data

    def __repr__(self) -> str:
        return f"ReadOnlyMap({self._data!r})"

    def __reduce__(self) -> tuple[Any, ...]:
        return (ReadOnlyMap, (self._data,))

    __hash__ = None  # type: ignore[assignment]


def freeze(mapping: Mapping[K, V]) -> Mapping[K, V]:
    """``mapping`` as a :class:`ReadOnlyMap` (unchanged if it already is one)."""
    return mapping if isinstance(mapping, ReadOnlyMap) else ReadOnlyMap(mapping)
