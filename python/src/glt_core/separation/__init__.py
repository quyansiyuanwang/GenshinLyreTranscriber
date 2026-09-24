"""Optional source-separation component management and stem contracts."""

from glt_core.separation.component import (
    ComponentManifest,
    ComponentVerificationError,
    VerifiedComponent,
    load_component,
    verify_component,
)
from glt_core.separation.provider import (
    SeparationCancelled,
    SeparationError,
    SeparationRun,
    run_component,
)
from glt_core.separation.stem_set import StemArtifact, StemSet, build_instrumental

__all__ = [
    "ComponentManifest",
    "ComponentVerificationError",
    "StemArtifact",
    "StemSet",
    "SeparationCancelled",
    "SeparationError",
    "SeparationRun",
    "VerifiedComponent",
    "build_instrumental",
    "load_component",
    "run_component",
    "verify_component",
]
