"""profiles — 납품 형식 레지스트리 (GRAPH-17).

`profile_for_spec(spec)`로 고르고, `get_profile(name)`으로 직접 꺼낸다.
내장 profile은 `web`·`print` 두 가지이며, 새 형식은 파일 하나 + `register()`다.
"""

from __future__ import annotations

from graphs.profiles.base import (
    DEFAULT_PROFILE,
    Gate,
    Profile,
    ProfileContext,
    ProfileError,
    artifact_entry,
    collect_artifacts,
    context_from_state,
    convert_png,
    default_run,
    get_profile,
    known_profiles,
    profile_for_spec,
    register,
    resolve_profile_name,
    run_manifest,
    svg_size,
    svg_width,
    to_jpg,
)
from graphs.profiles.print import PrintGateError, PrintProfile
from graphs.profiles.web import WebProfile

register(WebProfile())
register(PrintProfile())

__all__ = [
    "DEFAULT_PROFILE",
    "Gate",
    "PrintGateError",
    "PrintProfile",
    "Profile",
    "ProfileContext",
    "ProfileError",
    "WebProfile",
    "artifact_entry",
    "collect_artifacts",
    "context_from_state",
    "convert_png",
    "default_run",
    "get_profile",
    "known_profiles",
    "profile_for_spec",
    "register",
    "resolve_profile_name",
    "run_manifest",
    "svg_size",
    "svg_width",
    "to_jpg",
]
