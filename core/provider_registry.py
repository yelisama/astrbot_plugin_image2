from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .openai_compat_backend import OpenAICompatBackend
from .openai_full_url_backend import OpenAIFullURLBackend


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _provider_id(raw: dict) -> str:
    return str(raw.get("id") or raw.get("provider_id") or "").strip()


def _template_key(raw: dict) -> str:
    return str(
        raw.get("template_key")
        or raw.get("template")
        or raw.get("type")
        or raw.get("backend")
        or ""
    ).strip().lower()


def _api_keys(raw: dict) -> list[str]:
    keys = raw.get("api_keys")
    if isinstance(keys, list):
        out = [str(k).strip() for k in keys if str(k).strip()]
    else:
        key = str(raw.get("api_key") or "").strip()
        out = [key] if key else []
    return out


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on", "是", "启用"}


@dataclass(frozen=True)
class ProviderRef:
    provider_id: str
    template_key: str
    raw: dict


class ProviderRegistry:
    """Build and cache image providers for the simplified image2 plugin."""

    OPENAI_IMAGES_ALIASES = {
        "image2",
        "openai",
        "openai_images",
        "openai_compat",
        "openai-compatible",
    }
    FULL_URL_ALIASES = {"openai_full_url", "full_url", "custom_full_url"}

    def __init__(self, config: dict, *, imgr, data_dir: Path):
        self.config = config if isinstance(config, dict) else {}
        self.imgr = imgr
        self.data_dir = Path(data_dir)
        self._providers = self._load_provider_refs()
        self._backend_cache: dict[str, object] = {}

    def _load_provider_refs(self) -> dict[str, ProviderRef]:
        refs: dict[str, ProviderRef] = {}
        for raw in _as_list(self.config.get("providers")):
            if not isinstance(raw, dict) or raw.get("enabled", True) is False:
                continue
            pid = _provider_id(raw)
            key = _template_key(raw)
            if not pid or not key:
                continue
            refs[pid] = ProviderRef(pid, key, raw)
        return refs

    def provider_ids(self) -> list[str]:
        return list(self._providers.keys())

    def get_provider_ref(self, provider_id: str) -> ProviderRef:
        pid = str(provider_id or "").strip()
        ref = self._providers.get(pid)
        if ref is None:
            raise RuntimeError(f"Unknown provider: {pid}")
        return ref

    def validate(self) -> list[str]:
        warnings: list[str] = []
        for pid, ref in self._providers.items():
            raw = ref.raw
            if ref.template_key in self.OPENAI_IMAGES_ALIASES:
                if not str(raw.get("base_url") or "").strip():
                    warnings.append(f"provider {pid}: base_url is empty")
                if not str(raw.get("model") or raw.get("default_model") or "").strip():
                    warnings.append(f"provider {pid}: model is empty")
                if not _api_keys(raw):
                    warnings.append(f"provider {pid}: api_key/api_keys is empty")
            elif ref.template_key in self.FULL_URL_ALIASES:
                if not str(raw.get("full_generate_url") or "").strip():
                    warnings.append(f"provider {pid}: full_generate_url is empty")
                if not str(raw.get("model") or raw.get("default_model") or "").strip():
                    warnings.append(f"provider {pid}: model is empty")
                if not _api_keys(raw):
                    warnings.append(f"provider {pid}: api_key/api_keys is empty")
            else:
                warnings.append(f"provider {pid}: unsupported template_key={ref.template_key}")
        return warnings

    def get_backend(self, provider_id: str):
        pid = str(provider_id or "").strip()
        cached = self._backend_cache.get(pid)
        if cached is not None:
            return cached

        ref = self.get_provider_ref(pid)
        raw = ref.raw
        key = ref.template_key
        common = {
            "imgr": self.imgr,
            "api_keys": _api_keys(raw),
            "timeout": int(raw.get("timeout") or self.config.get("timeout") or 120),
            "max_retries": int(raw.get("max_retries") or 2),
            "default_model": str(raw.get("model") or raw.get("default_model") or "").strip(),
            "default_size": str(raw.get("default_size") or "1024x1024").strip(),
            "supports_edit": _as_bool(raw.get("supports_edit"), default=True),
            "extra_body": _as_dict(raw.get("extra_body")),
        }

        if key in self.OPENAI_IMAGES_ALIASES:
            backend = OpenAICompatBackend(
                **common,
                base_url=str(raw.get("base_url") or "").strip(),
                proxy_url=str(raw.get("proxy_url") or "").strip() or None,
                allowed_sizes=[
                    str(x).strip() for x in _as_list(raw.get("allowed_sizes")) if str(x).strip()
                ],
                ratio_default_sizes=_as_dict(raw.get("ratio_default_sizes")),
            )
        elif key in self.FULL_URL_ALIASES:
            backend = OpenAIFullURLBackend(
                **common,
                full_generate_url=str(raw.get("full_generate_url") or "").strip(),
                full_edit_url=str(raw.get("full_edit_url") or "").strip(),
            )
        else:
            raise RuntimeError(f"Unsupported provider template_key: {key}")

        self._backend_cache[pid] = backend
        return backend

    async def close(self) -> None:
        for backend in self._backend_cache.values():
            close = getattr(backend, "close", None)
            if callable(close):
                await close()
        self._backend_cache.clear()
