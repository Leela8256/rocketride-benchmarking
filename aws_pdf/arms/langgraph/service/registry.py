"""Pipeline discovery. Knows names and objects — never topology or modality.

Registration validates the full Pipeline protocol so a malformed adapter
fails at STARTUP, not on the first request.
"""

from typing import Any, Dict, List

from service.pipeline import PIPELINE_METHODS, Pipeline


class RegistryError(Exception):
    pass


class PipelineRegistry:
    def __init__(self) -> None:
        self._pipelines: Dict[str, Pipeline] = {}

    def register(self, name: str, pipeline: Any) -> None:
        if name in self._pipelines:
            raise RegistryError(f"pipeline {name!r} already registered")
        missing = [m for m in PIPELINE_METHODS if not callable(getattr(pipeline, m, None))]
        if missing:
            raise RegistryError(
                f"pipeline {name!r} does not satisfy the Pipeline protocol; "
                f"missing: {', '.join(missing)}"
            )
        self._pipelines[name] = pipeline

    def clear(self) -> None:
        """Drop all registrations so startup is repeatable.

        Lifespan can run more than once against the same app object (test
        clients, in-process restarts); without this the second startup dies
        on 'already registered'.
        """
        self._pipelines.clear()

    def get(self, name: str) -> Pipeline:
        try:
            return self._pipelines[name]
        except KeyError:
            raise RegistryError(f"unknown pipeline {name!r}") from None

    def __contains__(self, name: str) -> bool:
        return name in self._pipelines

    def names(self) -> List[str]:
        return sorted(self._pipelines)

    async def warmup_all(self) -> None:
        for name in self.names():
            await self._pipelines[name].warmup()
