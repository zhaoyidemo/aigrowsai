"""Thread-safe lazy compatibility resources for historical pipeline versions."""
from __future__ import annotations

from collections.abc import Callable
from threading import Lock
from typing import Generic, TypeVar


RegistryT = TypeVar('RegistryT')


class LazyRegistryProxy(Generic[RegistryT]):
    """Load an immutable registry only when a historical path actually uses it."""

    def __init__(self, factory: Callable[[], RegistryT]):
        self._factory = factory
        self._value: RegistryT | None = None
        self._lock = Lock()

    def _resolve(self) -> RegistryT:
        value = self._value
        if value is not None:
            return value
        with self._lock:
            if self._value is None:
                self._value = self._factory()
            return self._value

    def __getattr__(self, name: str):
        return getattr(self._resolve(), name)

    def __repr__(self) -> str:
        state = 'loaded' if self._value is not None else 'deferred'
        return f'<LazyRegistryProxy {state}>'
