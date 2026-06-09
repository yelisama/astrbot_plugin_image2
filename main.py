from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import mcp.types

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import File, Image
from astrbot.api.star import Context, Star, StarTools

from .core.debouncer import Debouncer
from .core.draw_service import ImageDrawService
from .core.edit_router import EditRouter
from .core.emoji_feedback import EmojiID, set_emoji
from .core.image_format import decode_base64_image_payload
from .core.image_manager import ImageManager
from .core.provider_chain import as_dict, as_list
from .core.provider_registry import ProviderRegistry
from .core.ref_store import ReferenceStore
from .core.utils import close_session, get_images_from_event


@dataclass(slots=True)
class SendImageResult:
    ok: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.ok


class Image2Plugin(Star):
    """AstrBot image2 plugin with text-to-image, image edit and selfie reference."""

    IMAGE_AS_FILE_THRESHOLD_BYTES = 20 * 1024 * 1024
    BUSY_MESSAGE = "当前你的生图任务较多，请稍后再试。"
    OUTPUT_RE = re.compile(r"^\d{2,5}x\d{2,5}$|^[124]K$", re.IGNORECASE)
    ANY_COMMAND_RE = re.compile(
        r"[/!！.。．](?P<cmd>aiimg|生图|aiedit|改图|图生图|修图|自拍)(?:\s+(?P<arg>.*))?",
        re.IGNORECASE | re.DOTALL,
    )

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config if isinstance(config, dict) else {}
        self.data_dir = Path(StarTools.get_data_dir("astrbot_plugin_image2"))
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.debouncer = Debouncer(self.config)
        self.imgr: ImageManager | None = None
        self.registry: ProviderRegistry | None = None
        self.draw: ImageDrawService | None = None
        self.edit: EditRouter | None = None
        self.ref_store: ReferenceStore | None = None

        self._job_lock = asyncio.Lock()
        self._user_inflight: dict[str, int] = {}
        self._global_inflight = 0
        self._last_image_by_user: dict[str, Path] = {}

    async def initialize(self) -> None:
        self.imgr = ImageManager(self.config, self.data_dir)
        self.registry = ProviderRegistry(self.config, imgr=self.imgr, data_dir=self.data_dir)
        for warning in self.registry.validate():
            logger.warning("[image2] config warning: %s", warning)
        self.draw = ImageDrawService(
            self.config, self.imgr, self.data_dir, registry=self.registry
        )
        self.edit = EditRouter(
            self.config, self.imgr, self.data_dir, registry=self.registry
        )
        self.ref_store = ReferenceStore(self.data_dir)
        logger.info("[image2] initialized with providers=%s", self.registry.provider_ids())

    async def terminate(self) -> None:
        self.debouncer.clear_all()
        if self.draw is not None:
            await self.draw.close()
        if self.edit is not None and self.edit.registry is not self.registry:
            await self.edit.close()
        if self.imgr is not None:
            await self.imgr.close()
        await close_session()

    @staticmethod
    def _tool_text(message: str) -> mcp.types.CallToolResult:
        return mcp.types.CallToolResult(
            content=[mcp.types.TextContent(type="text", text=message)]
        )

    @staticmethod
    def _as_bool(value: Any, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        return str(value).strip().lower() in {"1", "true", "yes", "on", "是", "启用"}

    @staticmethod
    def _as_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
        try:
            n = int(value)
        except Exception:
            n = default
        return max(minimum, min(maximum, n))

    def _features(self, name: str) -> dict:
        return as_dict(as_dict(self.config.get("features")).get(name))

    def _busy_message(self) -> str:
        return str(self.config.get("busy_message") or self.BUSY_MESSAGE)

    def _max_user_concurrency(self) -> int:
        return self._as_int(
            self.config.get("max_user_concurrency"), default=3, minimum=1, maximum=20
        )

    def _max_global_concurrency(self) -> int:
        return self._as_int(
            self.config.get("max_global_concurrency"), default=10, minimum=1, maximum=100
        )

    def _sender_id(self, event: AstrMessageEvent) -> str:
        try:
            return str(event.get_sender_id() or "").strip()
        except Exception:
            return ""

    def _group_id(self, event: AstrMessageEvent) -> str:
        try:
            group_id = str(event.get_group_id() or "").strip()
            if group_id:
                return group_id
        except Exception:
            pass
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if isinstance(raw, dict):
            return str(raw.get("group_id") or "").strip()
        return ""

    def _origin(self, event: AstrMessageEvent) -> str:
        return str(getattr(event, "unified_msg_origin", "") or "").strip()

    def _message_id(self, event: AstrMessageEvent) -> str:
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if isinstance(raw, dict):
            return str(raw.get("message_id") or "").strip()
        return ""

    def _message_may_have_image(self, event: AstrMessageEvent) -> bool:
        try:
            chain = event.get_messages()
        except Exception:
            chain = getattr(getattr(event, "message_obj", None), "message", None)
        if isinstance(chain, list):
            for seg in chain:
                if isinstance(seg, Image) or type(seg).__name__.lower() == "image":
                    return True
                if type(seg).__name__.lower() == "reply":
                    return True
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        return "image" in str(raw or "").lower()

    def _qq_only_allowed(self, event: AstrMessageEvent) -> bool:
        origin = self._origin(event).lower()
        if "aiocqhttp" in origin or "qq" in origin:
            return True
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if not isinstance(raw, dict):
            return False
        markers = (
            raw.get("platform"),
            raw.get("adapter"),
            raw.get("self_id"),
            raw.get("post_type"),
            raw.get("message_type"),
        )
        marker_text = " ".join(str(x or "").lower() for x in markers)
        return "aiocqhttp" in marker_text or "qq" in marker_text

    @staticmethod
    def _list_str(value: Any) -> set[str]:
        if not isinstance(value, list):
            return set()
        return {str(x).strip() for x in value if str(x).strip()}

    def _access_allowed(self, event: AstrMessageEvent) -> bool:
        if not self._qq_only_allowed(event):
            return False
        conf = as_dict(self.config.get("access_control"))
        if not self._as_bool(conf.get("enabled"), default=False):
            return True

        user_id = self._sender_id(event)
        group_id = self._group_id(event)
        origin = self._origin(event)
        group_tokens = {x for x in (group_id, origin) if x}

        user_black = self._list_str(conf.get("user_blacklist"))
        group_black = self._list_str(conf.get("group_blacklist"))
        if user_id and user_id in user_black:
            return False
        if group_tokens & group_black:
            return False

        if not self._as_bool(conf.get("whitelist_mode"), default=False):
            return True

        user_white = self._list_str(conf.get("user_whitelist"))
        group_white = self._list_str(conf.get("group_whitelist"))
        return (user_id and user_id in user_white) or bool(group_tokens & group_white)

    async def _begin_job(self, user_id: str) -> bool:
        async with self._job_lock:
            if self._global_inflight >= self._max_global_concurrency():
                return False
            current = self._user_inflight.get(user_id, 0)
            if current >= self._max_user_concurrency():
                return False
            self._user_inflight[user_id] = current + 1
            self._global_inflight += 1
            return True

    async def _end_job(self, user_id: str) -> None:
        async with self._job_lock:
            current = self._user_inflight.get(user_id, 0)
            if current <= 1:
                self._user_inflight.pop(user_id, None)
            else:
                self._user_inflight[user_id] = current - 1
            self._global_inflight = max(0, self._global_inflight - 1)

    def _debounce_key(
        self, event: AstrMessageEvent, prefix: str, user_id: str, payload: str = ""
    ) -> str:
        origin = self._origin(event) or "unknown"
        message_id = self._message_id(event)
        if message_id:
            return f"{prefix}:{origin}:{message_id}"
        text = str(payload or getattr(event, "message_str", "") or "")
        digest = hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest()[:16]
        return f"{prefix}:{origin}:{user_id}:{digest}"

    async def _emoji(self, event: AstrMessageEvent, key: str) -> None:
        conf = as_dict(self.config.get("emoji_feedback"))
        if not self._as_bool(conf.get("enabled"), default=True):
            return
        defaults = {
            "processing": EmojiID.PROCESSING,
            "success": EmojiID.SUCCESS,
            "failed": EmojiID.FAILED,
        }
        emoji_id = self._as_int(
            conf.get(key), default=defaults[key], minimum=1, maximum=100000
        )
        try:
            await set_emoji(event, emoji_id)
        except Exception as exc:
            logger.debug("[image2] emoji feedback skipped: %s", exc)

    async def _mark_processing(self, event: AstrMessageEvent) -> None:
        await self._emoji(event, "processing")

    async def _mark_success(self, event: AstrMessageEvent) -> None:
        await self._emoji(event, "success")

    async def _mark_failed(self, event: AstrMessageEvent) -> None:
        await self._emoji(event, "failed")

    async def _guard_start(
        self, event: AstrMessageEvent, prefix: str, payload: str
    ) -> tuple[bool, str]:
        if not self._access_allowed(event):
            return False, "denied"
        user_id = self._sender_id(event) or "unknown"
        if not await self._begin_job(user_id):
            await self._mark_failed(event)
            await event.send(event.plain_result(self._busy_message()))
            return False, "busy"
        if self.debouncer.hit(self._debounce_key(event, prefix, user_id, payload)):
            await self._end_job(user_id)
            await self._mark_failed(event)
            return False, "duplicate"
        await self._mark_processing(event)
        return True, user_id

    @staticmethod
    def _split_command_arg(event: AstrMessageEvent, fallback: str = "") -> str:
        text = str(fallback or "").strip()
        if text:
            return text
        raw = str(getattr(event, "message_str", "") or "").strip()
        parts = raw.split(maxsplit=1)
        return parts[1].strip() if len(parts) > 1 else ""

    def _parse_provider_prefix(self, text: str) -> tuple[str | None, str]:
        s = str(text or "").strip()
        if not s.startswith("@") or self.registry is None:
            return None, s
        head, _, rest = s.partition(" ")
        pid = head[1:].strip()
        if pid in self.registry.provider_ids():
            return pid, rest.strip()
        return None, s

    def _parse_output_suffix(self, text: str) -> tuple[str, str | None, str | None]:
        parts = str(text or "").strip().split()
        if not parts:
            return "", None, None
        last = parts[-1]
        if self.OUTPUT_RE.fullmatch(last):
            prompt = " ".join(parts[:-1]).strip()
            if "x" in last.lower():
                return prompt, last.lower(), None
            return prompt, None, last.upper()
        return " ".join(parts).strip(), None, None

    async def _send_image_with_fallback(
        self, event: AstrMessageEvent, image_path: Path
    ) -> SendImageResult:
        p = Path(image_path)
        if not p.exists():
            return SendImageResult(False, "image file not found")
        try:
            size_bytes = p.stat().st_size
        except Exception:
            size_bytes = 0

        if size_bytes > self.IMAGE_AS_FILE_THRESHOLD_BYTES:
            try:
                await event.send(event.chain_result([File(name=p.name, file=str(p))]))
                return SendImageResult(True)
            except Exception as exc:
                logger.warning("[image2] file send failed: %s", exc)

        try:
            await event.send(event.chain_result([Image.fromFileSystem(str(p))]))
            return SendImageResult(True)
        except Exception as fs_exc:
            logger.warning("[image2] filesystem image send failed: %s", fs_exc)

        try:
            data = await asyncio.to_thread(p.read_bytes)
            await event.send(event.chain_result([Image.fromBytes(data)]))
            return SendImageResult(True)
        except Exception as bytes_exc:
            logger.warning("[image2] bytes image send failed: %s", bytes_exc)
            return SendImageResult(False, str(bytes_exc))

    def _remember_last_image(self, event: AstrMessageEvent, image_path: Path) -> None:
        user_id = self._sender_id(event)
        if user_id:
            self._last_image_by_user[user_id] = Path(image_path)

    async def _event_image_bytes(
        self,
        event: AstrMessageEvent,
        *,
        include_avatar: bool = True,
        include_sender_avatar_fallback: bool = False,
    ) -> list[bytes]:
        image_segs = await get_images_from_event(
            event,
            include_avatar=include_avatar,
            include_sender_avatar_fallback=include_sender_avatar_fallback,
        )
        out: list[bytes] = []
        for seg in image_segs:
            try:
                b64 = await seg.convert_to_base64()
                out.append(decode_base64_image_payload(b64))
            except Exception as exc:
                logger.warning("[image2] input image conversion failed: %s", exc)
        return out

    def _selfie_ref_key(self, event: AstrMessageEvent) -> str:
        user_id = self._sender_id(event) or "unknown"
        return f"user_{user_id}"

    def _configured_selfie_refs(self) -> list[Path]:
        conf = self._features("selfie")
        out: list[Path] = []
        for raw in as_list(conf.get("reference_images")):
            p = Path(str(raw or "").strip())
            if not p.is_absolute():
                p = self.data_dir / p
            if p.is_file():
                out.append(p)
        return out

    async def _selfie_ref_paths(self, event: AstrMessageEvent) -> list[Path]:
        configured = self._configured_selfie_refs()
        if configured:
            return configured
        if self.ref_store is None:
            return []
        return await self.ref_store.get_paths(self._selfie_ref_key(event))

    def _selfie_prompt(self, user_prompt: str) -> str:
        prefix = str(
            self._features("selfie").get("prompt_prefix")
            or "请根据参考图生成一张新的自拍照：保持第1张参考图的人脸身份特征，"
            "其余参考图只作为服装、姿势、构图或场景参考。输出高质量照片风格自拍，不要拼图，不要水印。"
        ).strip()
        prompt = str(user_prompt or "日常自拍照").strip()
        return f"{prefix}\n用户需求：{prompt}"

    def _selfie_chain(self) -> list | None:
        conf = self._features("selfie")
        chain = as_list(conf.get("chain"))
        return chain if chain else None

    async def _generate_text(
        self,
        event: AstrMessageEvent,
        prompt: str,
        *,
        provider: str | None = None,
        size: str | None = None,
        resolution: str | None = None,
    ) -> Path:
        if self.draw is None:
            raise RuntimeError("draw service is not initialized")
        image_path = await self.draw.generate(
            prompt, provider_id=provider, size=size, resolution=resolution
        )
        self._remember_last_image(event, image_path)
        return image_path

    async def _generate_edit(
        self,
        event: AstrMessageEvent,
        prompt: str,
        *,
        provider: str | None = None,
        size: str | None = None,
        resolution: str | None = None,
    ) -> Path:
        if self.edit is None:
            raise RuntimeError("edit service is not initialized")
        images = await self._event_image_bytes(event)
        if not images:
            raise RuntimeError("no input image found")
        image_path = await self.edit.edit(
            prompt, images, backend=provider, size=size, resolution=resolution
        )
        self._remember_last_image(event, image_path)
        return image_path

    async def _generate_selfie(
        self,
        event: AstrMessageEvent,
        prompt: str,
        *,
        provider: str | None = None,
        size: str | None = None,
        resolution: str | None = None,
    ) -> Path:
        if not self._as_bool(self._features("selfie").get("enabled"), default=True):
            raise RuntimeError("selfie feature is disabled")
        if self.edit is None:
            raise RuntimeError("edit service is not initialized")
        paths = await self._selfie_ref_paths(event)
        if not paths:
            raise RuntimeError("no selfie reference image configured")
        images = [await asyncio.to_thread(p.read_bytes) for p in paths]
        images.extend(
            await self._event_image_bytes(
                event, include_avatar=False, include_sender_avatar_fallback=False
            )
        )
        conf = self._features("selfie")
        image_path = await self.edit.edit(
            self._selfie_prompt(prompt),
            images,
            backend=provider,
            size=size,
            resolution=resolution,
            default_output=str(conf.get("default_output") or "").strip() or None,
            chain_override=self._selfie_chain(),
        )
        self._remember_last_image(event, image_path)
        return image_path

    async def _run_and_send(
        self,
        event: AstrMessageEvent,
        prefix: str,
        payload: str,
        runner,
    ) -> None:
        ok, state = await self._guard_start(event, prefix, payload)
        if not ok:
            return
        user_id = state
        try:
            image_path = await runner()
            sent = await self._send_image_with_fallback(event, image_path)
            if sent:
                await self._mark_success(event)
            else:
                await self._mark_failed(event)
                logger.warning("[image2] image generated but send failed: %s", sent.reason)
        except Exception as exc:
            await self._mark_failed(event)
            logger.error("[image2] image task failed: %s", exc)
        finally:
            await self._end_job(user_id)

    @filter.command("aiimg", alias={"生图"})
    async def generate_image_command(self, event: AstrMessageEvent, prompt: str = ""):
        if not self._access_allowed(event):
            return
        text = self._split_command_arg(event, prompt)
        provider, text = self._parse_provider_prefix(text)
        text, size, resolution = self._parse_output_suffix(text)
        if not text:
            await self._mark_failed(event)
            return
        await self._run_and_send(
            event,
            "draw",
            text,
            lambda: self._generate_text(
                event, text, provider=provider, size=size, resolution=resolution
            ),
        )

    @filter.command("aiedit", alias={"改图", "图生图", "修图"})
    async def edit_image_command(self, event: AstrMessageEvent, prompt: str = ""):
        if not self._access_allowed(event):
            return
        text = self._split_command_arg(event, prompt)
        provider, text = self._parse_provider_prefix(text)
        text, size, resolution = self._parse_output_suffix(text)
        await self._run_and_send(
            event,
            "edit",
            text,
            lambda: self._generate_edit(
                event, text, provider=provider, size=size, resolution=resolution
            ),
        )

    @filter.command("自拍")
    async def selfie_command(self, event: AstrMessageEvent, prompt: str = ""):
        if not self._access_allowed(event):
            return
        text = self._split_command_arg(event, prompt) or "日常自拍照"
        provider, text = self._parse_provider_prefix(text)
        text, size, resolution = self._parse_output_suffix(text)
        await self._run_and_send(
            event,
            "selfie",
            text,
            lambda: self._generate_selfie(
                event, text, provider=provider, size=size, resolution=resolution
            ),
        )

    @filter.regex(r".*[/!！.。．](aiimg|生图|aiedit|改图|图生图|修图|自拍).*", priority=-10)
    async def image_command_fallback(self, event: AstrMessageEvent):
        """Handle messages where an image component appears before the command text."""
        if not self._access_allowed(event):
            return
        raw = str(getattr(event, "message_str", "") or "")
        stripped = raw.lstrip()
        if self.ANY_COMMAND_RE.match(stripped):
            return
        match = self.ANY_COMMAND_RE.search(raw)
        if not match:
            return

        cmd = (match.group("cmd") or "").lower()
        text = (match.group("arg") or "").strip()
        provider, text = self._parse_provider_prefix(text)
        text, size, resolution = self._parse_output_suffix(text)

        if cmd in {"aiimg", "生图"}:
            if not text:
                await self._mark_failed(event)
                return
            await self._run_and_send(
                event,
                "draw",
                text,
                lambda: self._generate_text(
                    event, text, provider=provider, size=size, resolution=resolution
                ),
            )
        elif cmd in {"自拍"}:
            text = text or "日常自拍照"
            await self._run_and_send(
                event,
                "selfie",
                text,
                lambda: self._generate_selfie(
                    event, text, provider=provider, size=size, resolution=resolution
                ),
            )
        else:
            await self._run_and_send(
                event,
                "edit",
                text,
                lambda: self._generate_edit(
                    event, text, provider=provider, size=size, resolution=resolution
                ),
            )

    @filter.command("自拍参考")
    async def selfie_reference_command(self, event: AstrMessageEvent, action: str = ""):
        if not self._access_allowed(event):
            return
        if self.ref_store is None:
            await self._mark_failed(event)
            return

        text = self._split_command_arg(event, action)
        normalized = text.strip().lower()
        key = self._selfie_ref_key(event)

        if normalized in {"删除", "delete", "del", "clear"}:
            removed = await self.ref_store.delete(key)
            await self._mark_success(event) if removed else await self._mark_failed(event)
            return

        if normalized in {"查看", "show", "list"}:
            paths = await self._selfie_ref_paths(event)
            if not paths:
                await self._mark_failed(event)
                return
            await event.send(event.chain_result([Image.fromFileSystem(str(p)) for p in paths[:5]]))
            await self._mark_success(event)
            return

        images = await self._event_image_bytes(
            event, include_avatar=False, include_sender_avatar_fallback=False
        )
        if not images:
            await self._mark_failed(event)
            return
        count = await self.ref_store.set(key, images[:5])
        logger.info("[image2] selfie reference updated: key=%s count=%s", key, count)
        await self._mark_success(event)

    @filter.command("重发图片")
    async def resend_last_image(self, event: AstrMessageEvent):
        if not self._access_allowed(event):
            return
        user_id = self._sender_id(event)
        p = self._last_image_by_user.get(user_id)
        if not p or not Path(p).exists():
            await self._mark_failed(event)
            return
        sent = await self._send_image_with_fallback(event, p)
        await self._mark_success(event) if sent else await self._mark_failed(event)

    @filter.llm_tool(name="gitee_draw_image")
    async def gitee_draw_image(self, event: AstrMessageEvent, prompt: str):
        """Compatibility tool: generate one image from a text prompt."""
        return await self.aiimg_generate(event, prompt=prompt, mode="draw")

    @filter.llm_tool(name="gitee_edit_image")
    async def gitee_edit_image(self, event: AstrMessageEvent, prompt: str):
        """Compatibility tool: edit the image attached to the current message."""
        return await self.aiimg_generate(event, prompt=prompt, mode="edit")

    @filter.llm_tool(name="aiimg_generate")
    async def aiimg_generate(
        self,
        event: AstrMessageEvent,
        prompt: str,
        mode: str = "auto",
        backend: str = "",
        output: str = "",
    ) -> mcp.types.CallToolResult:
        if not self._access_allowed(event):
            return self._tool_text("The image request is not allowed and has ended.")

        provider = str(backend or "").strip()
        if provider.lower() == "auto":
            provider = ""
        size, resolution = None, None
        if output:
            _empty, size, resolution = self._parse_output_suffix(f"x {output}")

        resolved_mode = str(mode or "auto").strip().lower()
        if resolved_mode == "auto":
            resolved_mode = "edit" if self._message_may_have_image(event) else "draw"

        feature_name = "selfie" if resolved_mode in {"selfie", "selfie_ref"} else (
            "edit" if resolved_mode in {"edit", "image2image", "img2img"} else "draw"
        )
        feature_conf = self._features(feature_name)
        if not self._as_bool(feature_conf.get("llm_tool_enabled"), default=True):
            return self._tool_text("The requested image tool is disabled.")

        payload = f"{resolved_mode}:{provider}:{output}:{prompt}"
        ok, state = await self._guard_start(event, f"llm_{resolved_mode}", payload)
        if not ok:
            return self._tool_text("The image request was not started.")
        user_id = state

        try:
            if resolved_mode in {"edit", "image2image", "img2img"}:
                image_path = await self._generate_edit(
                    event, prompt, provider=provider or None, size=size, resolution=resolution
                )
            elif resolved_mode in {"selfie", "selfie_ref"}:
                image_path = await self._generate_selfie(
                    event, prompt, provider=provider or None, size=size, resolution=resolution
                )
            else:
                image_path = await self._generate_text(
                    event, prompt, provider=provider or None, size=size, resolution=resolution
                )
            sent = await self._send_image_with_fallback(event, image_path)
            if not sent:
                await self._mark_failed(event)
                return self._tool_text("The image was generated but could not be sent.")
            await self._mark_success(event)
            return self._tool_text(
                "The image has been generated and sent. Do not send another confirmation."
            )
        except Exception as exc:
            await self._mark_failed(event)
            logger.error("[image2] llm tool failed: %s", exc)
            return self._tool_text("The image request failed and has ended.")
        finally:
            await self._end_job(user_id)
